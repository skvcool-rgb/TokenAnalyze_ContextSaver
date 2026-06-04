#!/usr/bin/env python3
"""SessionStart (matcher: compact) hook — re-injects the pre-compaction checkpoint after compaction,
so the resumed turn sees what we were doing. Reads .claude/CHECKPOINT.md and emits it as additionalContext.
Harmless no-op if the file or the inject format isn't honored."""
import os, sys, json
def main():
    payload = {}
    try: payload = json.loads(sys.stdin.read() or "{}")
    except Exception: pass
    proj = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    p = os.path.join(proj, ".claude", "CHECKPOINT.md")
    if os.path.exists(p):
        try: txt = open(p, encoding="utf-8", errors="ignore").read()[:3000]
        except Exception: txt = ""
        if txt:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                  "additionalContext": "[resume — last pre-compaction checkpoint]\n" + txt}}))
    sys.exit(0)
if __name__ == "__main__":
    main()
