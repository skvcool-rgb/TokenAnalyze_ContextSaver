#!/usr/bin/env python3
"""Install TokenAnalyze + ContextSaver into ~/.claude/tools/ and print the hook config to add to settings.json.
Cross-platform; copies scripts only — does NOT modify settings.json (you merge the printed hooks yourself)."""
import os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(os.path.expanduser("~"), ".claude", "tools")

def main():
    os.makedirs(DEST, exist_ok=True)
    files = ["token_report.py", "checkpoint.py", "resume.py", os.path.join("docs", "TOKEN_EFFICIENCY.md")]
    for rel in files:
        src = os.path.join(HERE, rel)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DEST, os.path.basename(rel)))
            print(f"  installed {os.path.basename(rel)}")
    snippet = os.path.join(HERE, "settings.example.json")
    print(f"\nInstalled to: {DEST}\n")
    print("Add these to the \"hooks\" object in ~/.claude/settings.json (merge, don't overwrite):\n")
    if os.path.exists(snippet):
        print(open(snippet, encoding="utf-8").read())
    print("On Windows, replace `python` with the full python.exe path if it's not on the hook's PATH.\n")
    print(f"Run the analyzer anytime:  python {os.path.join(DEST, 'token_report.py')}")

if __name__ == "__main__":
    main()
