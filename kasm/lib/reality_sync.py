"""Reality sync — cross-reference chunks.db claims vs filesystem ground truth.

The point: when a chunk says "we shipped X to AWS", but the filesystem has
no Cargo.toml entry for X and no commit references it, that's drift —
Claude needs to know BEFORE it tells the user "X is built".

Two main entry points:
    reconcile(chunks_iter, survey, memory_md_parsed) -> ReconciliationReport
        Full project audit — goes into SessionStart preamble.

    quick_status_for_topic(topic, chunks_iter, survey) -> StatusVerdict
        Single-topic check — used by UserPromptSubmit when user asks
        "is X built / what's the status of Y".

Pure stdlib. No LLM calls. Works on any project regardless of language.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# What counts as "evidence" of something being built
EVIDENCE_FILE_RE = re.compile(
    r"\b([A-Za-z0-9_\-]+\.(?:py|js|ts|tsx|jsx|rs|go|java|rb|php|c|cpp|h|hpp|sql|"
    r"toml|yaml|yml|json|md|sh))\b"
)
# Version-tag regex. v6.0.1: tightened to avoid IP-address false positives.
# Negative lookbehind blocks matches preceded by `digit.` (which would mean
# we're in the middle of a longer dotted run — i.e. an IP address). Negative
# lookahead blocks matches followed by `.digit` (same reason).
# Three valid shapes:
#   1. "v" prefix + N.N.N(-suffix)?       — e.g. v1.2.3, v0.7.26-RC1
#   2. Bare N.N.N + REQUIRED -suffix      — e.g. 1.2.3-RC1 (suffix disambiguates)
#   3. Bare N.N.N standalone              — e.g. shipped 5.0.0 yesterday
#                                            (rejected when adjacent digit-dots
#                                             present, like in 127.0.0.1)
EVIDENCE_TAG_RE = re.compile(
    r"(?<![.\d])"                              # not preceded by digit-or-dot
    r"(?:"
    r"v\d+\.\d+\.\d+(?:-[A-Za-z][\w.-]*)?"     # vX.Y.Z(-RC1)?
    r"|"
    r"\d+\.\d+\.\d+(?:-[A-Za-z][\w.-]*)?"      # X.Y.Z(-RC1)?
    r")"
    r"(?!\.?\d)"                               # not followed by another dotted digit
)
EVIDENCE_COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


@dataclass
class StatusVerdict:
    """Result of cross-referencing a single claim/topic."""
    topic: str = ""
    chunks_say: str = ""        # "claimed_built" | "claimed_in_progress" | "silent"
    filesystem_says: str = ""   # "confirms" | "contradicts" | "silent"
    git_says: str = ""          # "tagged" | "committed" | "silent"
    confidence: str = "low"     # "high" | "medium" | "low"
    summary: str = ""           # one-line human-readable verdict
    evidence: list[str] = field(default_factory=list)


@dataclass
class ReconciliationReport:
    """Whole-project reality check."""
    confirmed: list[str] = field(default_factory=list)
    claimed_but_missing: list[str] = field(default_factory=list)
    built_but_undocumented: list[str] = field(default_factory=list)
    version_skew: list[str] = field(default_factory=list)
    drift_flags: list[str] = field(default_factory=list)


def _file_refs_from_chunks(chunks_iter: Iterable) -> set[str]:
    """Collect every file path mentioned across all chunks."""
    out: set[str] = set()
    for c in chunks_iter:
        # chunks come as sqlite Row or dict
        text = c["text"] if isinstance(c, dict) or hasattr(c, "keys") else getattr(c, "text", "")
        for m in EVIDENCE_FILE_RE.finditer(text or ""):
            out.add(m.group(1))
    return out


def _versions_from_chunks(chunks_iter: Iterable) -> set[str]:
    """Pick up version-looking tokens claimed in chunks."""
    out: set[str] = set()
    for c in chunks_iter:
        text = c["text"] if isinstance(c, dict) or hasattr(c, "keys") else getattr(c, "text", "")
        for m in EVIDENCE_TAG_RE.finditer(text or ""):
            out.add(m.group(0))
    return out


def reconcile(
    chunks: list,
    survey,
    memory_md_parsed: list | None = None,
) -> ReconciliationReport:
    """Whole-project audit. Always returns a report (never raises)."""
    rep = ReconciliationReport()
    if not chunks:
        return rep

    # Gather what chunks claim
    claimed_files = _file_refs_from_chunks(chunks)
    claimed_versions = _versions_from_chunks(chunks)

    # Real filesystem files (extract from tree_summary — which is bounded)
    real_files: set[str] = set()
    for line in survey.tree_summary:
        # tree_summary entries look like "lib/ (10 .py)" or "README.md"
        # We just check filenames as substrings — not perfect but cheap
        token = line.split(" ", 1)[0].rstrip("/")
        real_files.add(token)

    # Real package versions
    real_versions = set(survey.versions.values())

    # Real git tags
    real_tags = set(survey.tags)

    # Confirmed: files in chunks AND in real_files
    for f in sorted(claimed_files):
        if any(f == r or f.endswith("/" + r) or r.endswith("/" + f)
               for r in real_files):
            rep.confirmed.append(f)
        else:
            # File mentioned in chunks but not visible in tree summary —
            # MAY just be a deeper-than-2-level file. Mark only if it
            # looks like a top-level claim ("we created src/foo.py").
            if "/" not in f or f.split("/")[0] in real_files:
                continue  # plausibly buried deeper — don't flag
            rep.claimed_but_missing.append(f)

    # Version skew: chunks mention a version that's not in any package
    # file or git tag → either not yet released or stale claim.
    for v in sorted(claimed_versions):
        v_no_v = v.lstrip("v")
        in_pkg = any(v_no_v == rv or v == rv or v_no_v in rv
                     for rv in real_versions)
        in_tag = any(v == t or v_no_v == t.lstrip("v")
                     for t in real_tags)
        if not in_pkg and not in_tag:
            # Common false positives: dates like 2026-04-29 don't match
            # this regex, but bare version-looking tokens in prose still slip
            # in. Only flag if it appears in 2+ chunks (signal of a real claim).
            count = sum(
                1 for c in chunks
                if v in (c["text"] if isinstance(c, dict) or hasattr(c, "keys")
                         else getattr(c, "text", ""))
            )
            if count >= 2:
                rep.version_skew.append(
                    f"chunks mention {v} ({count}x), no matching tag or package version"
                )

    # Built-but-undocumented: file in real_files (top-level) not mentioned
    # in any chunk — may be brand new, may be legacy, just signal it.
    chunk_basenames = {f.split("/")[-1] for f in claimed_files}
    chunk_basenames |= claimed_files
    for r in sorted(real_files):
        if r in {".gitignore", ".kos-memory", ".git", ".claude-plugin"}:
            continue
        if not r:
            continue
        rname = r.rstrip("/")
        if rname not in chunk_basenames and "." in rname:
            rep.built_but_undocumented.append(rname)

    # Cap to keep output bounded
    rep.confirmed = rep.confirmed[:20]
    rep.claimed_but_missing = rep.claimed_but_missing[:10]
    rep.built_but_undocumented = rep.built_but_undocumented[:10]
    rep.version_skew = rep.version_skew[:5]

    return rep


def render_reconciliation(rep: ReconciliationReport, max_chars: int = 2500) -> str:
    """Format the reconciliation report for SessionStart output."""
    lines: list[str] = []

    if rep.confirmed:
        sample = ", ".join(rep.confirmed[:8])
        more = (f", ... +{len(rep.confirmed) - 8}"
                if len(rep.confirmed) > 8 else "")
        lines.append(f"  ✓ confirmed (chunks + filesystem agree): {sample}{more}")

    if rep.claimed_but_missing:
        for c in rep.claimed_but_missing[:5]:
            lines.append(f"  ⚠ claimed but missing: {c}")
        if len(rep.claimed_but_missing) > 5:
            lines.append(f"  ⚠ ... +{len(rep.claimed_but_missing) - 5} more discrepancies")

    if rep.version_skew:
        for vs in rep.version_skew[:3]:
            lines.append(f"  ⚠ version skew: {vs}")

    if rep.built_but_undocumented:
        sample = ", ".join(rep.built_but_undocumented[:5])
        lines.append(f"  ? built but not in chunks: {sample}")

    if not lines:
        lines.append("  (no reconciliation signal — chunks aligned with filesystem)")

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... [truncated]"
    return out


def quick_status_for_topic(
    topic: str,
    chunks: list,
    survey,
) -> StatusVerdict:
    """Single-topic check used by UserPromptSubmit when user asks
    'is X built / what's the status of Y'."""
    v = StatusVerdict(topic=topic)
    topic_low = topic.lower().strip()
    if not topic_low:
        return v

    # Chunks evidence: how many chunks mention the topic, and what kind
    chunk_hits = 0
    asserted_hits = 0
    last_ts = 0
    for c in chunks:
        text = (c["text"] if isinstance(c, dict) or hasattr(c, "keys")
                else getattr(c, "text", ""))
        if topic_low in (text or "").lower():
            chunk_hits += 1
            if isinstance(c, dict) or hasattr(c, "keys"):
                if c.get("asserted_by_user") if isinstance(c, dict) else c["asserted_by_user"]:
                    asserted_hits += 1
                ts = c.get("ts") if isinstance(c, dict) else c["ts"]
            else:
                ts = getattr(c, "ts", 0)
            if ts and ts > last_ts:
                last_ts = ts

    if chunk_hits == 0:
        v.chunks_say = "silent"
    elif asserted_hits > 0 or chunk_hits >= 5:
        v.chunks_say = "claimed_built"
    else:
        v.chunks_say = "claimed_in_progress"

    # Filesystem evidence
    fs_hits = sum(1 for line in survey.tree_summary
                  if topic_low in line.lower())
    if fs_hits > 0:
        v.filesystem_says = "confirms"
    else:
        v.filesystem_says = "silent"

    # Git evidence: does any tag, branch, or recent commit subject mention it
    git_hits: list[str] = []
    if any(topic_low in t.lower() for t in survey.tags):
        git_hits.append("tag")
    for c in survey.last_commits:
        if topic_low in c.get("subject", "").lower():
            git_hits.append(f"commit {c.get('sha', '?')}")
            break
    v.git_says = "tagged" if "tag" in git_hits else (
        "committed" if git_hits else "silent")
    v.evidence = git_hits

    # Verdict
    has_chunk = v.chunks_say != "silent"
    has_fs = v.filesystem_says == "confirms"
    has_git = v.git_says != "silent"

    score = sum([has_chunk, has_fs, has_git])
    if score == 3:
        v.confidence = "high"
        v.summary = (
            f"'{topic}' is BUILT — confirmed by {chunk_hits} chunks, "
            f"filesystem evidence, and git ({', '.join(git_hits)})."
        )
    elif score == 2:
        v.confidence = "medium"
        if has_chunk and has_fs:
            v.summary = (
                f"'{topic}' is likely built — chunks ({chunk_hits} mentions) "
                f"and filesystem agree, but no git tag or commit reference."
            )
        elif has_chunk and has_git:
            v.summary = (
                f"'{topic}' is likely built — chunks ({chunk_hits} mentions) "
                f"and git ({', '.join(git_hits)}) agree, but no obvious "
                f"top-level filesystem trace."
            )
        else:
            v.summary = (
                f"'{topic}' has filesystem + git evidence "
                f"but isn't documented in past chunks — possibly very recent."
            )
    elif score == 1:
        v.confidence = "low"
        if has_chunk and v.chunks_say == "claimed_built":
            v.summary = (
                f"'{topic}' was claimed built in {chunk_hits} chunks, but "
                f"NO filesystem or git evidence found — VERIFY before "
                f"asserting status."
            )
        elif has_fs:
            v.summary = (
                f"'{topic}' has filesystem trace but no chunk history or "
                f"git mention — likely scaffolded but not committed."
            )
        else:
            v.summary = (
                f"'{topic}' has only git evidence ({', '.join(git_hits)}) — "
                f"no chunks or filesystem confirmation."
            )
    else:
        v.confidence = "low"
        v.summary = (
            f"NO evidence found for '{topic}' — not in chunks, not in "
            f"filesystem, not in git history. Genuinely not built, OR "
            f"the topic name doesn't match the actual feature name."
        )

    return v


def render_status_verdict(v: StatusVerdict) -> str:
    parts = [f"[reality check] {v.summary}"]
    parts.append(
        f"  evidence: chunks={v.chunks_say}, filesystem={v.filesystem_says}, "
        f"git={v.git_says} (confidence: {v.confidence})"
    )
    return "\n".join(parts)
