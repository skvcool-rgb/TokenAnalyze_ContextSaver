"""4-stage recall pipeline.

Stage 0  Query expansion (LLM-cached)            ~$0.001 / 500ms
Stage 1  Catalog scan (zero LLM, just hand catalog to caller)
Stage 2  Targeted grep over selected sessions    zero cost / ~50ms
Stage 3  Synthesis (delta-vs-current-context)    ~$0.007 / 1s

Stage 0 and 3 are LLM calls — but the caller (CLI / MCP / hook) decides
whether to actually invoke an LLM or to do them locally with primitive
heuristics. This file is LLM-agnostic; it returns structured handoffs.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .budget import Budget
from .catalog import (
    build_catalog,
    load_catalog,
    render_catalog_for_claude,
    save_catalog,
    session_ids_matching_tags,
)
from .paths import (
    FILE_BUDGET,
    FILE_CATALOG,
    FILE_CHUNKS_DB,
    FILE_SYNONYMS,
    ensure_kos_dir,
)
from .search import SynonymCache, grep_passages, tokenize
from .store import Store


@dataclass
class RecallContext:
    """Carry-context across stages. Caller fills in LLM calls externally."""
    query: str
    window_days: int = 30
    project_root: str | None = None
    user_level: bool = False
    session_id: str | None = None

    # Filled by stages
    expanded_terms: list[str] = field(default_factory=list)
    catalog: dict[str, Any] = field(default_factory=dict)
    catalog_text: str = ""
    selected_session_ids: list[str] = field(default_factory=list)
    passages: list[dict[str, Any]] = field(default_factory=list)
    synthesis: str = ""

    # Bookkeeping
    tokens_used: int = 0
    cost_usd: float = 0.0
    timings_ms: dict[str, float] = field(default_factory=dict)
    contradicted_chunk_ids: list[str] = field(default_factory=list)


def stage_0_local_expansion(rc: RecallContext, kos_dir: Path) -> RecallContext:
    """Local-only expansion via SynonymCache. No LLM call."""
    t0 = time.perf_counter()
    cache = SynonymCache(kos_dir / FILE_SYNONYMS)
    rc.expanded_terms = cache.expand(rc.query)
    rc.timings_ms["stage_0"] = (time.perf_counter() - t0) * 1000
    return rc


def stage_0_remember_llm_expansion(
    rc: RecallContext, kos_dir: Path, expanded_terms: list[str]
) -> None:
    """Caller-invoked: store the LLM expansion result for future reuse."""
    cache = SynonymCache(kos_dir / FILE_SYNONYMS)
    cache.remember(rc.query, expanded_terms)
    # Update rc with the LLM expansion
    rc.expanded_terms = list(dict.fromkeys(rc.expanded_terms + expanded_terms))


def stage_1_catalog(rc: RecallContext, kos_dir: Path) -> RecallContext:
    """Build (or load + refresh) the catalog. Returns text for Claude."""
    t0 = time.perf_counter()
    catalog_path = kos_dir / FILE_CATALOG
    db_path = kos_dir / FILE_CHUNKS_DB

    store = Store(db_path)
    try:
        catalog = build_catalog(store, project=rc.project_root)
        save_catalog(catalog_path, catalog)
    finally:
        store.close()

    rc.catalog = catalog
    rc.catalog_text = render_catalog_for_claude(catalog)
    rc.timings_ms["stage_1"] = (time.perf_counter() - t0) * 1000
    return rc


def stage_2_grep(rc: RecallContext, kos_dir: Path) -> RecallContext:
    """Run grep with expanded terms across selected (or all) chunks.
    Returns matched passages with surrounding context.
    """
    t0 = time.perf_counter()
    db_path = kos_dir / FILE_CHUNKS_DB
    store = Store(db_path)
    try:
        # Default to chunks from selected sessions; if none picked, search all
        # within the time window.
        cutoff = int(time.time()) - rc.window_days * 24 * 3600
        rows = list(store.iter_chunks(since_ts=cutoff))
        if rc.selected_session_ids:
            picks = set(rc.selected_session_ids)
            rows = [r for r in rows if r["session_id"] in picks]

        # Combine all rows into one big text per chunk so grep_passages
        # can do per-chunk windowing.
        terms = rc.expanded_terms or tokenize(rc.query)
        passages: list[dict[str, Any]] = []
        for r in rows:
            matches = grep_passages(r["text"], terms, context_lines=1)
            for m in matches:
                passages.append({
                    "chunk_id": r["id"],
                    "session_id": r["session_id"],
                    "ts": r["ts"],
                    "text": m,
                    "asserted_by_user": bool(r["asserted_by_user"]),
                    "contradicted_by_later_session": bool(
                        r["contradicted_by_later_session"]
                    ),
                })

        # Cap to top-N passages (most recent first; user-asserted bumped up)
        passages.sort(
            key=lambda p: (-int(p["asserted_by_user"]), -p["ts"])
        )
        rc.passages = passages[:20]
    finally:
        store.close()

    rc.timings_ms["stage_2"] = (time.perf_counter() - t0) * 1000
    return rc


def build_synthesis_prompt(rc: RecallContext, current_context: str) -> str:
    """Construct the Stage 3 prompt for the caller's LLM. Returns prompt
    string. Caller invokes the LLM with this and stuffs the response into
    rc.synthesis.
    """
    passages_text = "\n\n---\n\n".join(
        f"[{p['session_id'][:8]}, {time.strftime('%Y-%m-%d', time.gmtime(p['ts']))}"
        f"{', user-asserted' if p['asserted_by_user'] else ''}"
        f"]\n{p['text']}"
        for p in rc.passages
    )

    return f"""You are doing context recovery for a Claude Code session.

Below are two sources:
1. CURRENT CONTEXT — what the active session already knows.
2. RETRIEVED PASSAGES — relevant past conversation, scoped to the user's window.

Compare them. Produce a structured synthesis with these sections:

**(a) NEW ITEMS (not in current context)**
   Bullet points. Include source date if available.

**(b) POTENTIALLY STALE IN CURRENT CONTEXT**
   Items the session believes that past content updated or contradicted.

**(c) SUGGESTED UPDATED STATE**
   Concise paragraph with integrated understanding.

**(d) UNCERTAINTY**
   Anything ambiguous or needing user confirmation.

**(e) CONTRADICTIONS DETECTED**
   If retrieved passages conflict with each other across timestamps,
   list the chunk_ids that are now superseded by newer ones.
   Format: `superseded_chunk_ids: [<chunk_id>, ...]`
   Empty list if no contradictions.

Conflict-resolution rule: when passages conflict, prefer the most recent
unless explicitly contradicted by an even-newer passage. Surface the
conflict in section (d).

User-asserted passages should be weighted higher than auto-extracted ones,
all else equal.

DO NOT reproduce raw passage text. Synthesize. The user wants the delta,
not a transcript.

QUERY: {rc.query}
WINDOW: past {rc.window_days} days

CURRENT CONTEXT:
{current_context or '(not provided)'}

RETRIEVED PASSAGES (n={len(rc.passages)}):
{passages_text or '(none)'}

Produce the structured synthesis now.
"""


def render_recall_output(rc: RecallContext) -> str:
    """Format the final user-facing output."""
    n_sessions = len({p["session_id"] for p in rc.passages})
    n_passages = len(rc.passages)
    cost_str = f"~${rc.cost_usd:.4f}" if rc.cost_usd else "free (no LLM call)"
    out = []
    out.append("=" * 60)
    out.append(f"kos-memory recovery — past {rc.window_days}d")
    out.append(f"Sources: {n_passages} passages from {n_sessions} sessions")
    out.append("=" * 60)
    out.append("")
    if rc.synthesis:
        out.append(rc.synthesis)
    else:
        out.append("(no synthesis — LLM call skipped or failed)")
        if rc.passages:
            out.append("")
            out.append("Top passages (raw fallback):")
            for p in rc.passages[:5]:
                date = time.strftime("%Y-%m-%d", time.gmtime(p["ts"]))
                out.append(f"\n[{date}] {p['text'][:200]}")
    out.append("")
    out.append(
        f"Tokens: {rc.tokens_used} | Cost: {cost_str} | "
        f"Latency: stage1={rc.timings_ms.get('stage_1', 0):.0f}ms "
        f"stage2={rc.timings_ms.get('stage_2', 0):.0f}ms"
    )
    out.append("")
    out.append("Confirm or correct?")
    return "\n".join(out)


def parse_synthesis_for_contradictions(synthesis: str) -> list[str]:
    """Extract `superseded_chunk_ids: [...]` from synthesis output."""
    import re
    m = re.search(
        r"superseded_chunk_ids\s*:\s*\[([^\]]*)\]",
        synthesis,
        flags=re.IGNORECASE,
    )
    if not m:
        return []
    inside = m.group(1)
    return [x.strip().strip("\"'") for x in inside.split(",") if x.strip()]


def execute_recall_local_only(
    query: str,
    window_days: int = 30,
    project_root: str | None = None,
    user_level: bool = False,
) -> RecallContext:
    """Convenience: run a fully local recall (no LLM stages 0/3).

    Returns RecallContext with passages populated, synthesis empty.
    Caller can either render directly (raw fallback) or invoke an LLM
    for stages 0 and 3 separately.
    """
    rc = RecallContext(
        query=query,
        window_days=window_days,
        project_root=project_root,
        user_level=user_level,
    )
    kos_dir = ensure_kos_dir(project_root, user_level=user_level)

    stage_0_local_expansion(rc, kos_dir)
    stage_1_catalog(rc, kos_dir)
    # No catalog selection in local-only mode — search all chunks
    stage_2_grep(rc, kos_dir)
    return rc
