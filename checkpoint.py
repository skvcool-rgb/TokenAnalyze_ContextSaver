#!/usr/bin/env python3
"""PreCompact checkpoint — writes a resume snapshot the instant before Claude Code compacts context.

Files on disk are NOT lost on compaction (Write/Edit persist immediately) — what's lost is the CONVERSATION
STATE (what we were doing / next steps). This captures that state so the post-compaction turn can resume.
Reads the hook JSON on stdin ({session_id, trigger, cwd, transcript_path}). Side-effect only; never blocks
(always exits 0). Auto-commit is OFF by default — set CLAUDE_CHECKPOINT_COMMIT=1 to also git-commit.
"""
import os, sys, json, subprocess, datetime

def sh(args, timeout=20):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""

def main():
    payload = {}
    try: payload = json.loads(sys.stdin.read() or "{}")
    except Exception: pass
    proj = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    if os.path.isdir(proj):
        try: os.chdir(proj)
        except Exception: pass
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(".claude", exist_ok=True)

    is_git = bool(sh(["git", "rev-parse", "--git-dir"]))
    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]) if is_git else ""
    status = sh(["git", "status", "--short"]) if is_git else ""
    commit = ""
    if is_git and os.environ.get("CLAUDE_CHECKPOINT_COMMIT") == "1":
        sh(["git", "add", "-A"])
        subprocess.run(["git", "commit", "-q", "-m", f"checkpoint: pre-compaction {ts}"], capture_output=True)
        commit = sh(["git", "log", "-1", "--oneline"])

    out = os.path.join(".claude", "CHECKPOINT.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"## Auto-checkpoint (pre-compaction) {ts}\n")
        f.write(f"trigger: {payload.get('trigger', '?')}  ·  branch: {branch or 'n/a'}"
                + (f"  ·  committed: {commit}" if commit else "  ·  (not committed — files are on disk)") + "\n\n")
        f.write("### uncommitted / in-flight at checkpoint\n```\n" + (status or "(working tree clean)") + "\n```\n")
        f.write("\n_Resume tip: read this + the project CLAUDE.md resume pointer to continue. "
                "Run `python ~/.claude/tools/token_report.py` to see where tokens went._\n")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import _signals; _signals.emit("checkpoint", f"saved · {payload.get('trigger', '?')}")
    except Exception:
        pass
    print(f"[checkpoint] {ts} -> {out}" + (f" + commit {commit}" if commit else ""))
    sys.exit(0)

if __name__ == "__main__":
    main()
