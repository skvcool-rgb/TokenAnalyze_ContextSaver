#!/usr/bin/env python3
"""Tiny append-only activity log so hook actions become VISIBLE.

Hooks fire silently — the user never sees that a checkpoint saved or write_guard nudged. Each tool appends a
one-line JSON event here; the watch panel / statusline read the tail. Best-effort: never raises, self-trims.
Format per line: {"ts": <epoch>, "event": "<name>", "detail": "<text>"}
"""
import os, sys, json, time

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".signals.jsonl")
_MAX_BYTES = 256 * 1024     # self-trim above this
_KEEP_LINES = 400

def emit(event, detail=""):
    """Append one event. Swallows all errors — a logging failure must never break a hook."""
    try:
        line = json.dumps({"ts": int(time.time()), "event": str(event), "detail": str(detail)[:300]})
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG) > _MAX_BYTES:
            _trim()
    except Exception:
        pass

def _trim():
    try:
        with open(LOG, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-_KEEP_LINES:]
        tmp = LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, LOG)
    except Exception:
        pass

def tail(n=15):
    """Return up to the last n events (oldest first), each a dict. Missing file -> []."""
    try:
        with open(LOG, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out

if __name__ == "__main__":
    # CLI: `_signals.py emit <event> <detail>`  |  `_signals.py tail [n]`
    a = sys.argv[1:]
    if a and a[0] == "emit":
        emit(a[1] if len(a) > 1 else "manual", " ".join(a[2:]))
    else:
        for e in tail(int(a[1]) if len(a) > 1 else 15):
            print(f"{e.get('ts')}  {e.get('event'):<14} {e.get('detail','')}")
