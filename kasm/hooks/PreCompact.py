#!/usr/bin/env python
"""PreCompact hook — fires BEFORE Claude Code compacts conversation.

Critical path: this is our chance to capture in-flight session state
before it's compressed away. Plugin manifest binds matcher: "auto" only,
so /compact (manual) does not trigger this — that's user-intended
compression and they don't need recovery prep.

Behavior: same as Stop hook (chunk + ingest), but tagged with
kind=pre_compact in the WAL log so we can distinguish later.

NEVER blocks compaction. Always exits 0.
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
except Exception:
    sys.exit(0)


def main() -> int:
    # Hook input arrives on stdin as JSON
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    trigger = payload.get("trigger", "auto")
    if trigger != "auto":
        # Defensive: manifest already filters but double-check
        sys.exit(0)

    transcript_path = payload.get("transcript_path") or os.environ.get(
        "CLAUDE_TRANSCRIPT_PATH", ""
    )
    project = (
        payload.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    session_id = payload.get("session_id") or os.environ.get(
        "CLAUDE_SESSION_ID"
    ) or str(int(time.time()))

    try:
        kos_dir = ensure_kos_dir(project, user_level=False)
    except Exception:
        sys.exit(0)

    # Try to grab the transcript
    text = ""
    if transcript_path and Path(transcript_path).exists():
        try:
            with open(transcript_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # Larger window than Stop hook — compaction means lots of
                # content is about to be lost
                f.seek(max(0, size - 200_000))
                raw = f.read().decode("utf-8", errors="replace")
            # Extract role+content from JSONL
            lines = []
            for line in raw.split("\n"):
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
                if isinstance(content, str) and content.strip() and role in (
                    "user", "assistant"
                ):
                    lines.append(f"[{role}] {content[:3000]}")
            text = "\n\n".join(lines[-50:])  # last 50 turns
        except Exception:
            pass

    if not text or len(text) < 50:
        sys.exit(0)

    chunks = chunk_text(text, max_chars=400, overlap=50)
    if not chunks:
        sys.exit(0)

    now = int(time.time())
    file_refs = extract_file_refs(text)

    # Append WAL marker first
    log_path = kos_dir / FILE_INGEST_LOG
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now,
                "session_id": session_id,
                "project": project,
                "kind": "pre_compact",
                "trigger": trigger,
                "len": len(text),
            }) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass

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
        store.add_chunks_bulk(records)
        store.upsert_session(
            session_id,
            ended_at=now,
            project=project,
            chunk_count=len(records),
        )
        (kos_dir / FILE_LAST_INGEST).write_text(str(now), encoding="utf-8")
    except Exception as e:
        print(f"[PreCompact] ingest failed: {e}", file=sys.stderr)
    finally:
        store.close()

    # NEVER block compaction. Always exit 0.
    return 0


if __name__ == "__main__":
    try:
        from lib.safety import run_safely
        sys.exit(run_safely(main, hook_name="PreCompact", timeout_s=7.0))
    except Exception:
        sys.exit(0)
