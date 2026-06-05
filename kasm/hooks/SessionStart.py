#!/usr/bin/env python
"""SessionStart hook — mode-aware memory reconstruction.

v4.1.0 (default mode = PRIMARY):

  Primary mode  → emit a memory-reconstruction block:
                    1) [kos-memory PRIMARY] header + chunk/session counts
                    2) Rendered Stage-1 catalog (top recent sessions, tags)
                    3) MEMORY.md heading skeleton (anchor list)
                    4) Drift warnings (stale anchor, missing MEMORY.md, etc.)

  Backup mode   → emit only the 1-line marker (legacy v4.0 behavior).

Pure stdlib. SQLite read-only. NO LLM calls. Bounded output (<2 KB) so
context doesn't get blown out at session boot. <100 ms typical.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from lib.stdio_utf8 import force_utf8_io
    force_utf8_io()
except Exception:
    pass

try:
    from lib.paths import (
        FILE_CHUNKS_DB,
        FILE_LAST_INGEST,
        MODE_BACKUP,
        MODE_PRIMARY,
        ensure_kos_dir,
        get_mode,
    )
    from lib.memory_md import (
        detect_drift,
        find_memory_files,
        parse_memory_file,
        render_memory_block,
    )
    from lib.catalog import (
        build_catalog,
        render_catalog_for_claude,
        save_catalog,
    )
    from lib.paths import FILE_CATALOG
    from lib.store import Store
    from lib.codebase_survey import render_live_state, survey_project
    from lib.reality_sync import reconcile, render_reconciliation
except Exception:
    sys.exit(0)


# Output token-cost guards (raised in v5.0 for Live state + Reconciliation)
MAX_OUTPUT_CHARS = 16000


def _format_age(latest_ts: int | None) -> str:
    if not latest_ts:
        return "never"
    delta = int(time.time()) - latest_ts
    if delta < 86400:
        return "today"
    if delta < 2 * 86400:
        return "yesterday"
    return f"{delta // 86400}d ago"


def _emit_backup_line(n_chunks: int, n_sessions: int, last_ingest: str) -> str:
    return (
        f"[kos-memory BACKUP] {n_chunks} chunks, {n_sessions} sessions for "
        f"this project (last ingest: {last_ingest}). "
        f"Use /recall when current context is missing past detail."
    )


def _emit_primary_block(
    n_chunks: int,
    n_sessions: int,
    last_ingest: str,
    catalog_text: str,
    memory_block: str,
    drift_warnings: list[str],
    live_state: str,
    reconciliation: str,
) -> str:
    parts: list[str] = [
        f"[kos-memory PRIMARY] Memory reconstruction "
        f"({n_chunks} chunks, {n_sessions} sessions, last ingest: "
        f"{last_ingest})",
        "",
    ]

    if memory_block:
        parts.append("## MEMORY.md anchors (operator-curated truth)")
        parts.append(memory_block)
        parts.append("")

    if live_state:
        parts.append("## Live project state (filesystem + git, surveyed now)")
        parts.append(live_state)
        parts.append("")

    if catalog_text:
        parts.append("## Recent session catalog (auto-extracted)")
        parts.append(catalog_text)
        parts.append("")

    if reconciliation:
        parts.append("## Build-status reconciliation (chunks vs filesystem)")
        parts.append(reconciliation)
        parts.append("")

    if drift_warnings:
        parts.append("## Drift")
        for w in drift_warnings:
            parts.append(f"- ⚠ {w}")
        parts.append("")

    parts.append(
        "Authority order for any claim about project state:\n"
        "  1. Live project state (filesystem + git) — ground truth\n"
        "  2. MEMORY.md anchors — operator-curated truth\n"
        "  3. User-asserted chunks (/remember) — explicit pins\n"
        "  4. Auto-extracted chunks — high-recall, may be stale\n"
        "BEFORE claiming anything is 'not built', check the Live state "
        "and Reconciliation sections above. If reconciliation flags drift, "
        "surface it instead of asserting. Use /recall for deep retrieval."
    )
    out = "\n".join(parts)
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n... [truncated for context budget]"
    return out


def main() -> int:
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    try:
        kos_dir = ensure_kos_dir(project, user_level=False)
    except Exception:
        sys.exit(0)

    db = kos_dir / FILE_CHUNKS_DB

    # ── Read store stats (cheap, read-only) ───────────
    if not db.exists():
        # No store yet — silent in backup mode, brief primary-mode hint
        # only if MEMORY.md exists (so reconstruction is still useful).
        memory_files = find_memory_files(project)
        if get_mode(project) == MODE_PRIMARY and memory_files:
            parsed = [parse_memory_file(mf) for mf in memory_files]
            block = render_memory_block(parsed, heading_only=True)
            print(
                f"[kos-memory PRIMARY] No chunks yet for this project, but "
                f"{len(memory_files)} MEMORY.md anchor(s) found:\n\n{block}"
            )
        # v6.0: empty-store nudge — if no chunks AND no MEMORY.md, look
        # for bootstrap sources (README/CHANGELOG/CC transcripts) so the
        # operator can /memory-bootstrap to seed in one shot.
        if get_mode(project) == MODE_PRIMARY and not memory_files:
            try:
                from lib.bootstrap import find_bootstrap_sources
                srcs = find_bootstrap_sources(project)
            except Exception:
                srcs = []
            if srcs:
                n_doc = sum(1 for s in srcs if s.kind == "doc")
                n_ts = sum(1 for s in srcs if s.kind == "transcript")
                print(
                    f"[kos-memory PRIMARY] Empty store. Found {n_doc} docs + "
                    f"{n_ts} prior transcripts on disk — run "
                    f"/memory-bootstrap to seed memory in one shot."
                )
        sys.exit(0)

    try:
        c = sqlite3.connect(str(db), timeout=1.0)
        n_chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        latest_ts = c.execute("SELECT MAX(ts) FROM chunks").fetchone()[0]
        n_sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        c.close()
    except Exception:
        sys.exit(0)

    if n_chunks == 0:
        sys.exit(0)

    last_ingest = _format_age(latest_ts)
    mode = get_mode(project)

    # ── Backup mode: legacy 1-line marker ───────────
    if mode == MODE_BACKUP:
        print(_emit_backup_line(n_chunks, n_sessions, last_ingest))
        return 0

    # ── Primary mode: full reconstruction block ───────────
    # Stage 1: build & render catalog + read all chunks for reconciliation
    catalog_text = ""
    chunks_for_reconcile: list = []
    try:
        store = Store(db)
        try:
            catalog = build_catalog(store, project=project)
            save_catalog(kos_dir / FILE_CATALOG, catalog)
            catalog_text = render_catalog_for_claude(catalog)
            # Pull last 30 days of chunks for reality-sync (bounded)
            cutoff = int(time.time()) - 30 * 86400
            chunks_for_reconcile = [
                {
                    "text": r["text"], "ts": r["ts"],
                    "session_id": r["session_id"],
                    "asserted_by_user": bool(r["asserted_by_user"]),
                }
                for r in store.iter_chunks(since_ts=cutoff)
            ][:500]  # cap for performance
        finally:
            store.close()
    except Exception:
        catalog_text = "(catalog build failed)"

    # MEMORY.md: locate, parse, render heading skeleton
    memory_files = find_memory_files(project)
    parsed_memory = [parse_memory_file(mf) for mf in memory_files]
    memory_block = render_memory_block(parsed_memory, heading_only=True)

    # Live filesystem + git state (NEW v5.0)
    live_state = ""
    survey = None
    try:
        survey = survey_project(project)
        live_state = render_live_state(survey)
    except Exception:
        live_state = ""

    # Reconciliation (NEW v5.0)
    reconciliation = ""
    if survey and chunks_for_reconcile:
        try:
            rep = reconcile(chunks_for_reconcile, survey, parsed_memory)
            reconciliation = render_reconciliation(rep)
        except Exception:
            reconciliation = ""

    # Drift detection: compare MEMORY.md mtime vs latest chunk ts.
    # v6.0.1: also count bootstrap chunks separately so detect_drift can
    # suppress false drift right after /memory-bootstrap.
    chunks_since_memory_update = 0
    bootstrap_chunks_since_memory_update = 0
    if parsed_memory:
        newest_mem_ts = max(pm.file.mtime for pm in parsed_memory)
        try:
            c = sqlite3.connect(str(db), timeout=1.0)
            chunks_since_memory_update = c.execute(
                "SELECT COUNT(*) FROM chunks WHERE ts > ?", (newest_mem_ts,)
            ).fetchone()[0]
            bootstrap_chunks_since_memory_update = c.execute(
                "SELECT COUNT(*) FROM chunks WHERE ts > ? "
                "AND kind LIKE 'bootstrap%'", (newest_mem_ts,),
            ).fetchone()[0]
            c.close()
        except Exception:
            pass
    drift_warnings = detect_drift(
        parsed_memory, latest_ts, chunks_since_memory_update,
        bootstrap_chunks_since_memory_update,
    )

    print(_emit_primary_block(
        n_chunks=n_chunks,
        n_sessions=n_sessions,
        last_ingest=last_ingest,
        catalog_text=catalog_text,
        memory_block=memory_block,
        drift_warnings=drift_warnings,
        live_state=live_state,
        reconciliation=reconciliation,
    ))
    return 0


if __name__ == "__main__":
    try:
        from lib.safety import run_safely
        sys.exit(run_safely(main, hook_name="SessionStart", timeout_s=8.0))
    except Exception:
        # Defensive: even safety setup failed. Last resort: silent exit.
        sys.exit(0)
