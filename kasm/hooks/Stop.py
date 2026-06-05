#!/usr/bin/env python
"""Stop hook — ingest session transcript at end of conversation.

Append-only via WAL log; chunk-and-insert into chunks.db.
Summary + tags generation deferred (caller can pass via env or done lazily
on next /memory-status). NO LLM call here — keeps hook <2s.

Crash-safe: appends to ingest_log.jsonl FIRST (durable), then attempts
SQLite insert. On crash, next session start replays uncommitted log lines.
"""
from __future__ import annotations

import json
import os
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
    from lib.chunker import chunk_text, extract_file_refs
    from lib.paths import FILE_CHUNKS_DB, FILE_INGEST_LOG, FILE_LAST_INGEST, ensure_kos_dir
    from lib.store import Store
except Exception as e:
    print(f"[Stop] kos-memory import failed: {e}", file=sys.stderr)
    sys.exit(0)


def _read_transcript() -> str | None:
    transcript = os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
    if not transcript or not Path(transcript).exists():
        # Fallback: most-recent jsonl in claude projects dir
        home = Path.home() / ".claude" / "projects"
        if home.exists():
            jsonls = sorted(
                home.rglob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if jsonls:
                transcript = str(jsonls[0])
            else:
                return None
        else:
            return None

    try:
        # Read last 60 KB only — covers most recent multi-turn content
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 60_000))
            data = f.read().decode("utf-8", errors="replace")
        return data
    except Exception:
        return None


def _extract_messages(transcript: str) -> str:
    """Extract conversational text from transcript JSONL."""
    out = []
    for line in transcript.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message", rec)
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or rec.get("type", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                (c.get("text", "") if isinstance(c, dict) else str(c))
                for c in content
            )
        if not isinstance(content, str) or not content.strip():
            continue
        if role in ("user", "assistant"):
            out.append(f"[{role}] {content[:2000]}")
    return "\n\n".join(out[-30:])  # last 30 turns


def main() -> int:
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    session_id = os.environ.get("CLAUDE_SESSION_ID") or str(int(time.time()))

    try:
        kos_dir = ensure_kos_dir(project, user_level=False)
    except Exception:
        sys.exit(0)

    transcript = _read_transcript()
    if not transcript:
        sys.exit(0)

    text = _extract_messages(transcript)
    if not text or len(text) < 50:
        sys.exit(0)

    # WAL: append durable record FIRST
    log_path = kos_dir / FILE_INGEST_LOG
    log_entry = {
        "ts": int(time.time()),
        "session_id": session_id,
        "project": project,
        "kind": "session_end",
        "len": len(text),
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass

    # Chunk + insert
    chunks = chunk_text(text, max_chars=400, overlap=50)
    if not chunks:
        sys.exit(0)

    file_refs = extract_file_refs(text)
    now = int(time.time())

    store = Store(kos_dir / FILE_CHUNKS_DB)
    try:
        records = [
            {
                "session_id": session_id,
                "project": project,
                "ts": now,
                "text": c.text,
                "kind": c.kind,
                "language": c.language,
                "file_refs": file_refs,
                "asserted_by_user": False,
            }
            for c in chunks
        ]
        n = store.add_chunks_bulk(records)
        store.upsert_session(
            session_id,
            started_at=now - 60,  # rough; real start unknown from this hook
            ended_at=now,
            project=project,
            chunk_count=n,
        )
    except Exception as e:
        print(f"[Stop] ingest failed: {e}", file=sys.stderr)
    finally:
        store.close()

    # Update last_ingest_marker
    try:
        (kos_dir / FILE_LAST_INGEST).write_text(str(now), encoding="utf-8")
    except Exception:
        pass

    # Silent on success — Stop hook should never speak
    return 0


if __name__ == "__main__":
    try:
        from lib.safety import run_safely
        sys.exit(run_safely(main, hook_name="Stop", timeout_s=14.0))
    except Exception:
        sys.exit(0)
