"""Hierarchical catalog — the metadata index Claude scans first.

Three windows (auto-bounded to ~1K tokens regardless of corpus size):
  RECENT  (last 30 days)  — full session entries: date, summary, tags
  MID     (30-180 days)   — collapsed by tag: "auth (8 sessions)"
  ARCHIVE (180+ days)     — collapsed by tag: "auth (24 sessions, archived)"

Tag clustering: tags come from session summaries (Stop hook generates
1-3 tag words via Haiku at ingest time; cached in sessions.tags).

Purpose: Stage 1 of the recall pipeline returns this catalog to Claude.
Claude scans hierarchy, picks candidate sessions, then Stage 2 grep
fetches passages from those sessions only.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .store import Store

# Window thresholds (seconds)
RECENT_WINDOW = 30 * 24 * 3600
MID_WINDOW = 180 * 24 * 3600

# Bounds — keeps catalog token cost predictable
MAX_RECENT_ENTRIES = 30
MAX_MID_TAGS = 15
MAX_ARCHIVE_TAGS = 10


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def build_catalog(
    store: Store, project: str | None = None, now_ts: int | None = None
) -> dict[str, Any]:
    """Compute a fresh catalog from the store. Read-only on store."""
    now_ts = now_ts or int(time.time())
    recent_cutoff = now_ts - RECENT_WINDOW
    mid_cutoff = now_ts - MID_WINDOW

    sessions = store.list_sessions()
    if project:
        sessions = [s for s in sessions if s["project"] == project]

    recent_entries: list[dict] = []
    mid_tag_counts: Counter = Counter()
    archive_tag_counts: Counter = Counter()
    mid_examples: dict[str, str] = {}
    archive_examples: dict[str, str] = {}

    for s in sessions:
        try:
            tags = json.loads(s["tags"] or "[]")
        except Exception:
            tags = []

        ts = s["started_at"]
        summary = s["summary"] or "(no summary)"

        if ts >= recent_cutoff:
            if len(recent_entries) < MAX_RECENT_ENTRIES:
                recent_entries.append({
                    "session_id": s["session_id"],
                    "date": time.strftime(
                        "%Y-%m-%d", time.gmtime(ts)
                    ),
                    "summary": summary[:200],
                    "tags": tags,
                    "chunks": s["chunk_count"],
                })
        elif ts >= mid_cutoff:
            for t in tags or ["uncategorized"]:
                mid_tag_counts[t] += 1
                if t not in mid_examples:
                    mid_examples[t] = summary[:80]
        else:
            for t in tags or ["uncategorized"]:
                archive_tag_counts[t] += 1
                if t not in archive_examples:
                    archive_examples[t] = summary[:60]

    mid = [
        {"tag": t, "session_count": c, "example": mid_examples.get(t, "")}
        for t, c in mid_tag_counts.most_common(MAX_MID_TAGS)
    ]
    archive = [
        {"tag": t, "session_count": c, "example": archive_examples.get(t, "")}
        for t, c in archive_tag_counts.most_common(MAX_ARCHIVE_TAGS)
    ]

    return {
        "schema_version": 1,
        "generated_at": now_ts,
        "project": project,
        "total_sessions": len(sessions),
        "recent": recent_entries,
        "mid": mid,
        "archive": archive,
    }


def save_catalog(path: str | Path, catalog: dict[str, Any]) -> None:
    _atomic_write_json(Path(path), catalog)


def load_catalog(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def render_catalog_for_claude(catalog: dict[str, Any]) -> str:
    """Human/Claude-readable catalog. Bounded token cost."""
    lines: list[str] = []
    lines.append(f"# Memory catalog ({catalog.get('total_sessions', 0)} sessions)")
    lines.append("")

    if catalog.get("recent"):
        lines.append("## Recent (last 30 days)")
        for e in catalog["recent"]:
            tag_str = (" [" + ", ".join(e["tags"]) + "]") if e.get("tags") else ""
            lines.append(
                f"- {e['date']} · {e['session_id'][:8]}{tag_str}\n"
                f"  → {e['summary']}"
            )
        lines.append("")

    if catalog.get("mid"):
        lines.append("## Mid (30-180 days, grouped by tag)")
        for m in catalog["mid"]:
            lines.append(
                f"- **{m['tag']}** ({m['session_count']} sessions) "
                f"e.g. {m['example']}"
            )
        lines.append("")

    if catalog.get("archive"):
        lines.append("## Archive (>180 days, grouped by tag)")
        for a in catalog["archive"]:
            lines.append(f"- **{a['tag']}** ({a['session_count']} sessions)")

    return "\n".join(lines)


def session_ids_matching_tags(
    store: Store, tags: list[str], project: str | None = None
) -> list[str]:
    """Find session IDs whose tag list intersects given tags."""
    out: list[str] = []
    target = set(t.lower() for t in tags)
    for s in store.list_sessions():
        if project and s["project"] != project:
            continue
        try:
            stags = json.loads(s["tags"] or "[]")
        except Exception:
            stags = []
        if any(t.lower() in target for t in stags):
            out.append(s["session_id"])
    return out
