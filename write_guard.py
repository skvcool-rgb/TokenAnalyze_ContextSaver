#!/usr/bin/env python3
"""PreToolUse(Write) hook — nudge `Edit` over a wasteful full-file `Write`.

When a `Write` rewrites an existing LARGE file that's mostly UNCHANGED, the full content is re-sent as tokens
(tool-call input — the #1 session cost). An `Edit` sends only the diff. This detects that case and, by default,
emits a non-blocking nudge; set WRITE_GUARD_STRICT=1 to BLOCK it (forcing an Edit). New files and genuine
rewrites pass through untouched.

Enable: add to the `PreToolUse` hooks in settings.json with matcher "Write" (see settings.example.json).
"""
import os, sys, json, difflib

OLD_MIN_LINES = 40     # don't nag on small files (cheap to rewrite)
SIMILAR = 0.60         # >= this similarity = "mostly unchanged" = should have been an Edit

def est(s):
    try:
        import tiktoken; return len(tiktoken.get_encoding("cl100k_base").encode(s, disallowed_special=()))
    except Exception:
        return len(s) // 4

def main():
    try: data = json.loads(sys.stdin.read() or "{}")
    except Exception: sys.exit(0)
    if data.get("tool_name") != "Write": sys.exit(0)
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("path") or ""
    new = ti.get("content") or ""
    if not path or not os.path.exists(path): sys.exit(0)        # new file -> Write is correct
    try: old = open(path, encoding="utf-8", errors="ignore").read()
    except Exception: sys.exit(0)
    if old.count("\n") + 1 < OLD_MIN_LINES: sys.exit(0)         # small file -> cheap, don't nag
    ratio = difflib.SequenceMatcher(None, old, new).ratio()
    if ratio < SIMILAR: sys.exit(0)                             # genuine rewrite -> allow silently
    msg = (f"efficiency: Write rewrites {os.path.basename(path)} "
           f"({old.count(chr(10))+1} lines, ~{int(ratio*100)}% unchanged) — a full Write re-sends ~{est(new)} "
           f"tokens; Edit sends only the changed lines. Prefer Edit for small changes.")
    if os.environ.get("WRITE_GUARD_STRICT") == "1":
        print(msg, file=sys.stderr); sys.exit(2)               # block -> forces an Edit
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
          "permissionDecision": "allow", "permissionDecisionReason": msg}, "systemMessage": "💡 " + msg}))
    sys.exit(0)

if __name__ == "__main__":
    main()
