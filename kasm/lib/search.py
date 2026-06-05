"""Search layer: BM25 + synonym expansion + grep with surrounding context.

Pure stdlib. Inline BM25 Okapi (epsilon-floored to handle tiny corpora).
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return [m.lower() for m in _TOKEN_RE.findall(text)]


# ── BM25 Okapi (inline) ──────────────────────────────────
class BM25:
    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = len(corpus)
        self.doc_lens = [len(d) for d in corpus]
        self.avgdl = sum(self.doc_lens) / self.N if self.N else 0.0
        self.doc_freqs: list[Counter] = [Counter(d) for d in corpus]

        df = Counter()
        for f in self.doc_freqs:
            for t in f:
                df[t] += 1

        self.idf: dict[str, float] = {}
        idf_sum, neg = 0.0, []
        for t, freq in df.items():
            v = math.log((self.N - freq + 0.5) / (freq + 0.5))
            self.idf[t] = v
            idf_sum += v
            if v < 0:
                neg.append(t)
        # Floor negatives + tiny-corpus near-zeros
        if self.idf:
            avg = idf_sum / len(self.idf)
            eps = max(0.25 * avg, 0.1)
            for t in neg:
                self.idf[t] = eps
            for t, v in list(self.idf.items()):
                if 0 <= v < eps:
                    self.idf[t] = eps

    def scores(self, query_tokens: list[str]) -> list[float]:
        s = [0.0] * self.N
        for t in query_tokens:
            idf = self.idf.get(t, 0.0)
            if idf == 0:
                continue
            for i, f in enumerate(self.doc_freqs):
                tf = f.get(t, 0)
                if not tf:
                    continue
                norm = 1 - self.b + self.b * (
                    self.doc_lens[i] / self.avgdl if self.avgdl else 1
                )
                s[i] += idf * (tf * (self.k1 + 1)) / (tf + self.k1 * norm)
        return s

    def top_n(self, query_tokens: list[str], n: int = 50) -> list[tuple[int, float]]:
        sc = self.scores(query_tokens)
        out = [(i, v) for i, v in enumerate(sc) if v > 0]
        out.sort(key=lambda x: -x[1])
        return out[:n]


# ── Synonym expansion ────────────────────────────────────
class SynonymCache:
    """Persistable query → expanded-terms cache.

    Sources:
      - Bundled seed (small, conservative)
      - Per-project accretion (Stage 0 LLM expansions cached by query hash)

    Cap: 500 entries per file. LRU eviction.
    """
    MAX_ENTRIES = 500
    SEED: dict[str, list[str]] = {
        "auth": ["authentication", "login", "credential", "session", "token"],
        "deploy": ["deployment", "release", "ship", "rollout", "publish"],
        "build": ["compile", "package", "bundle"],
        "fix": ["repair", "patch", "resolve", "correct"],
        "test": ["spec", "assertion", "validate", "check"],
        "docs": ["documentation", "readme", "guide", "manual"],
        "perf": ["performance", "speed", "latency", "optimize"],
        "bug": ["defect", "issue", "error", "regression"],
        "config": ["configuration", "setting", "option"],
        "db": ["database", "sqlite", "postgres", "mysql"],
    }

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._cache: dict[str, list[str]] = {}
        self._access_order: list[str] = []
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._cache = data.get("cache", {})
                self._access_order = data.get("access_order", list(self._cache.keys()))
            except Exception:
                self._cache, self._access_order = {}, []

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "cache": self._cache,
                "access_order": self._access_order[-self.MAX_ENTRIES:],
            }, indent=2), encoding="utf-8")
            import os
            os.replace(tmp, self.path)
        except Exception:
            pass

    def expand(self, query: str) -> list[str]:
        """Return expanded terms (original tokens + synonyms)."""
        toks = tokenize(query)
        expanded = list(toks)
        # Per-query cache
        key = " ".join(sorted(toks))
        if key in self._cache:
            self._touch(key)
            for t in self._cache[key]:
                if t not in expanded:
                    expanded.append(t)
            return expanded
        # Per-token seed
        for t in toks:
            for syn in self.SEED.get(t, []):
                if syn not in expanded:
                    expanded.append(syn)
        return expanded

    def remember(self, query: str, expanded_terms: list[str]) -> None:
        """Store an LLM-generated expansion for future reuse."""
        toks = tokenize(query)
        key = " ".join(sorted(toks))
        self._cache[key] = list(dict.fromkeys(expanded_terms))[:30]
        self._touch(key)
        # LRU eviction
        if len(self._cache) > self.MAX_ENTRIES:
            evict = self._access_order[0]
            self._cache.pop(evict, None)
            self._access_order = self._access_order[1:]
        self._save()

    def _touch(self, key: str) -> None:
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)


# ── Grep with surrounding context ────────────────────────
def grep_passages(
    text: str, terms: list[str], context_lines: int = 1
) -> list[str]:
    """Return matched line + ±context_lines lines around each hit.
    De-duplicates overlapping windows.
    """
    if not text or not terms:
        return []
    lines = text.split("\n")
    pat = re.compile(
        "|".join(re.escape(t) for t in terms),
        re.IGNORECASE,
    )
    hits: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if pat.search(line):
            lo = max(0, i - context_lines)
            hi = min(len(lines), i + context_lines + 1)
            hits.append((lo, hi))
    if not hits:
        return []
    # Merge overlapping
    hits.sort()
    merged: list[tuple[int, int]] = [hits[0]]
    for lo, hi in hits[1:]:
        if lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return ["\n".join(lines[lo:hi]) for lo, hi in merged]
