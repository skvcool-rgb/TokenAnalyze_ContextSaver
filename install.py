#!/usr/bin/env python3
"""Install the TokenAnalyze · ContextSaver · KASM suite into ~/.claude/tools/ and print the config to add.
Cross-platform; copies scripts only — does NOT modify settings.json (you merge the printed blocks yourself).
KASM (kasm/) has its OWN installer; this points you to it rather than touching its settings block."""
import os, shutil, sys
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(os.path.expanduser("~"), ".claude", "tools")

FILES = ["token_report.py", "checkpoint.py", "resume.py", "scope_rules.py", "write_guard.py",
         "_signals.py", "_metrics.py", "watch_panel.py", "statusline.py",
         os.path.join("docs", "TOKEN_EFFICIENCY.md")]

def main():
    os.makedirs(DEST, exist_ok=True)
    for rel in FILES:
        src = os.path.join(HERE, rel)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DEST, os.path.basename(rel)))
            print(f"  installed {os.path.basename(rel)}")
    print(f"\nInstalled to: {DEST}\n")

    snippet = os.path.join(HERE, "settings.example.json")
    print("1) Merge into ~/.claude/settings.json (merge into `hooks`, add `statusLine` — don't overwrite):\n")
    if os.path.exists(snippet):
        print(open(snippet, encoding="utf-8").read())
    print("   On Windows, replace `python` with the full python.exe path if it's not on the hook's PATH.\n")

    print("2) See what's going on:")
    print(f"     live panel : python {os.path.join(DEST, 'watch_panel.py')}        (run in a side terminal)")
    print(f"     snapshot   : python {os.path.join(DEST, 'watch_panel.py')} --once")
    print(f"     full report: python {os.path.join(DEST, 'token_report.py')}")
    print( "     statusline : the statusLine block above puts it always-on in the Claude Code UI\n")

    print("3) (optional) KASM per-project memory — install separately (it writes its own hooks block):")
    print(f"     cd {os.path.join(HERE, 'kasm')}  &&  python scripts/install.py\n")

    print("4) (optional) sharper estimates / rules scoping:")
    print("     pip install tiktoken                            # sharper content-map estimate")
    print(f"     python {os.path.join(DEST, 'scope_rules.py')}   # audit per-turn rules tax (--apply to fix)")

if __name__ == "__main__":
    main()
