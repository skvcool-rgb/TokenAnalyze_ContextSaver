#!/usr/bin/env python
"""UserPromptSubmit hook — mode-aware natural-language recall trigger.

Pure regex match. <100 ms in backup mode, <500 ms in primary mode (which
runs Stage 0+1+2 of the recall pipeline inline so passages reach Claude
without waiting for it to invoke the recall_project_memory MCP tool).

Backup mode  → emit a 1-line hint, let Claude decide (legacy v4.0).
Primary mode → auto-run Stage 0+1+2, emit catalog+top-5-passages inline.

Trigger discipline (both modes): max 1 trigger per turn. Slash commands
go through commands/, not via this hook.
"""
from __future__ import annotations

import json
import os
import re
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


# Recall triggers — user is asking about the past
TRIGGER_PATTERNS = [
    r"\b(check|look at|recall|find|remember)\b.*\b(past|last|previous|earlier)\b",
    r"\bwhat did (we|i) (discuss|decide|do|build|ship|fix)\b",
    r"\b(left off|leave off|leaving off|where we (were|stopped|left))\b",
    r"\bas (we|i) (discussed|mentioned|said)\b",
    r"\bthe (project|spec|design|thing|feature) we (built|made|worked on)\b",
    r"\b(remember|recall) (when|that|the time)\b",
    r"\bsince (yesterday|last (week|session|time))\b",
    r"\b/recall|\b/recover|\b/check[- ]history\b",
]

# Build-status triggers — user wants the CURRENT state of something. v5.0
# auto-runs the reality_sync verdict so Claude doesn't have to invoke a tool.
# Patterns deliberately require a status-vocab anchor word so they don't
# overlap with past-tense recall triggers like "where did we leave off".
BUILD_STATUS_PATTERNS = [
    r"\bis\s+(\w[\w\s\-]{1,40}?)\s+(built|done|shipped|ready|working|live|deployed|complete|in\s+production|in\s+main|merged|pushed|tagged)\b",
    r"\b(did|have)\s+(we|i|you)\s+(build|built|ship|shipped|finish|finished|complete|completed|implement|implemented|deploy|deployed|release|released)\s+(\w[\w\s\-]{1,40})",
    r"\bwhat'?s?\s+(the\s+)?(status|state)\s+of\s+(\w[\w\s\-]{1,40})",
    r"\bcurrent\s+(state|status)\s+(of|on)\s+(\w[\w\s\-]{1,40})",
    r"\bwhere\s+(does|stands?)\s+(\w[\w\s\-]{1,40})",
]

COMPILED = [re.compile(p, re.IGNORECASE) for p in TRIGGER_PATTERNS]
COMPILED_BUILD = [re.compile(p, re.IGNORECASE) for p in BUILD_STATUS_PATTERNS]


# Bounds for primary-mode auto-recall output
MAX_PASSAGES_INLINE = 5
MAX_OUTPUT_CHARS = 6000


def _read_payload() -> tuple[str, str]:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return "", ""
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if not isinstance(prompt, str):
        return "", ""
    project = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return prompt, project


def _detect_trigger(prompt: str) -> str | None:
    if not prompt or prompt.lstrip().startswith("/"):
        return None
    for pat in COMPILED:
        m = pat.search(prompt)
        if m:
            return m.group(0)
    return None


def _detect_build_status_trigger(prompt: str) -> tuple[str, str] | None:
    """If the prompt asks about CURRENT build state, return (matched, topic).
    The topic is the noun-phrase the user is asking about."""
    if not prompt or prompt.lstrip().startswith("/"):
        return None
    for pat in COMPILED_BUILD:
        m = pat.search(prompt)
        if m:
            # Pull the most likely topic group — last non-stopword group
            groups = [g for g in m.groups() if g and len(g) > 1]
            stop = {"the", "we", "i", "you", "did", "have", "has", "of", "on",
                    "in", "is", "are", "production", "main", "merged",
                    "pushed", "tagged", "complete", "completed", "done",
                    "built", "shipped", "ready", "working", "live", "deployed",
                    "implement", "implemented", "create", "created", "finish",
                    "finished", "build", "ship", "deploy", "state", "status"}
            for g in reversed(groups):
                tokens = [t for t in g.split() if t.lower() not in stop]
                if tokens:
                    return m.group(0), " ".join(tokens).strip()
            return m.group(0), m.group(0)
    return None


def _emit_backup_hint(matched: str) -> None:
    print(
        f"[kos-memory hint] User prompt matched recall pattern "
        f"(\"{matched[:60]}\"). Consider calling the "
        f"recall_project_memory MCP tool if you sense missing context."
    )


def _run_primary_auto_recall(prompt: str, project: str, matched: str) -> None:
    """In primary mode, run Stage 0+1+2 inline and emit passages directly."""
    try:
        from lib.budget import Budget
        from lib.memory_md import (
            find_memory_files,
            parse_memory_file,
            render_memory_block,
        )
        from lib.paths import FILE_BUDGET, FILE_CHUNKS_DB, ensure_kos_dir
        from lib.recall import (
            RecallContext,
            stage_0_local_expansion,
            stage_1_catalog,
            stage_2_grep,
        )
    except Exception:
        # Imports failed — degrade to hint behavior
        _emit_backup_hint(matched)
        return

    try:
        kos_dir = ensure_kos_dir(project, user_level=False)
    except Exception:
        _emit_backup_hint(matched)
        return

    db = kos_dir / FILE_CHUNKS_DB
    memory_files = find_memory_files(project)
    if not db.exists() and not memory_files:
        # Nothing to recall — stay silent (no false positives)
        return

    # Budget gate (lightweight — auto-recall is cheap so we set a small
    # estimate; daily caps still protect against runaway).
    try:
        budget = Budget(kos_dir / FILE_BUDGET)
        allowed, reason = budget.can_recall(estimated_tokens=600)
        if not allowed:
            print(
                f"[kos-memory PRIMARY] auto-recall throttled: {reason}. "
                f"Use /recall manually if needed."
            )
            return
    except Exception:
        pass

    # Use the trigger phrase as the query if user gave nothing more specific
    query = prompt.strip()
    if len(query) > 200:
        query = matched

    rc = RecallContext(
        query=query,
        window_days=30,
        project_root=project,
        user_level=False,
    )

    parts: list[str] = [
        f"[kos-memory PRIMARY] Auto-recall fired on trigger "
        f"(\"{matched[:60]}\")",
        "",
    ]

    # Stage 0+1+2 if there's a chunks DB
    n_passages = 0
    if db.exists():
        try:
            stage_0_local_expansion(rc, kos_dir)
            stage_1_catalog(rc, kos_dir)
            stage_2_grep(rc, kos_dir)
            n_passages = len(rc.passages)
        except Exception:
            n_passages = 0

        if rc.catalog_text:
            parts.append("## Catalog (top recent sessions)")
            # Compress catalog to stay within budget
            cat = rc.catalog_text
            if len(cat) > 1500:
                cat = cat[:1500] + "\n... [catalog truncated]"
            parts.append(cat)
            parts.append("")

        if rc.passages:
            parts.append(f"## Top passages (showing {min(n_passages, MAX_PASSAGES_INLINE)} of {n_passages})")
            for p in rc.passages[:MAX_PASSAGES_INLINE]:
                date = time.strftime("%Y-%m-%d", time.gmtime(p["ts"]))
                tags: list[str] = []
                if p.get("asserted_by_user"):
                    tags.append("user-asserted")
                if p.get("contradicted_by_later_session"):
                    tags.append("SUPERSEDED")
                tag_str = (" [" + ", ".join(tags) + "]") if tags else ""
                sid = (p.get("session_id") or "")[:8]
                text = p["text"]
                if len(text) > 600:
                    text = text[:600] + "..."
                parts.append(f"\n[{sid}, {date}{tag_str}]\n{text}")
            parts.append("")

    # MEMORY.md anchors (always include if present — the truth-anchor)
    if memory_files:
        parsed = [parse_memory_file(mf) for mf in memory_files]
        block = render_memory_block(parsed, heading_only=True)
        if block:
            parts.append("## MEMORY.md anchors (truth)")
            parts.append(block)
            parts.append("")

    parts.append(
        "Synthesize against current context. Prefer MEMORY.md anchors as "
        "authoritative; auto-extracted chunks are higher-recall but may "
        "be stale. Cite source dates."
    )

    out = "\n".join(parts)
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n... [truncated for context budget]"

    print(out)

    # Record budget spend (rough estimate: ~tokens = chars/4)
    try:
        budget.record_recall(tokens=len(out) // 4 + 100, cost_usd=0.0)
    except Exception:
        pass


def _run_build_status_check(prompt: str, project: str,
                             matched: str, topic: str) -> None:
    """v5.0: when user asks about current build state, run reality_sync
    inline so Claude has a verdict + evidence before answering."""
    try:
        from lib.codebase_survey import survey_project
        from lib.paths import FILE_CHUNKS_DB, ensure_kos_dir
        from lib.reality_sync import quick_status_for_topic, render_status_verdict
        from lib.store import Store
    except Exception:
        return

    try:
        kos_dir = ensure_kos_dir(project, user_level=False)
    except Exception:
        return

    db = kos_dir / FILE_CHUNKS_DB
    chunks: list = []
    if db.exists():
        try:
            store = Store(db)
            try:
                # Pull last 60 days for status checks (broader than recall)
                cutoff = int(time.time()) - 60 * 86400
                chunks = [
                    {
                        "text": r["text"], "ts": r["ts"],
                        "session_id": r["session_id"],
                        "asserted_by_user": bool(r["asserted_by_user"]),
                    }
                    for r in store.iter_chunks(since_ts=cutoff)
                ][:1000]
            finally:
                store.close()
        except Exception:
            pass

    try:
        survey = survey_project(project)
    except Exception:
        return

    verdict = quick_status_for_topic(topic, chunks, survey)
    print(
        f"[kos-memory PRIMARY] Build-status check fired on \"{matched[:60]}\"\n"
        f"\n{render_status_verdict(verdict)}\n"
        f"\nUse this verdict and the SessionStart Live state + "
        f"Reconciliation sections to answer the user. If chunks claim a "
        f"thing was built but filesystem/git disagree (above), surface "
        f"the contradiction — DO NOT assert 'not built' without checking."
    )


def main() -> int:
    prompt, project = _read_payload()
    if not prompt:
        return 0

    # Resolve mode (defaults to primary in v4.1+)
    try:
        from lib.paths import MODE_BACKUP, MODE_PRIMARY, get_mode
        mode = get_mode(project)
    except Exception:
        mode = "primary"

    # Past-tense recall triggers FIRST — they take priority when both match
    # (e.g. "where did we leave off" should not be treated as build-status)
    matched = _detect_trigger(prompt)

    # v5.0: build-status triggers run reality-sync inline (primary mode only)
    if matched is None and mode != "backup":
        bs = _detect_build_status_trigger(prompt)
        if bs is not None:
            matched_bs, topic = bs
            _run_build_status_check(prompt, project, matched_bs, topic)
            return 0

    if matched is None:
        return 0

    if mode == "backup":
        _emit_backup_hint(matched)
    else:
        _run_primary_auto_recall(prompt, project, matched)

    return 0


if __name__ == "__main__":
    try:
        from lib.safety import run_safely
        sys.exit(run_safely(main, hook_name="UserPromptSubmit", timeout_s=1.8))
    except Exception:
        sys.exit(0)
