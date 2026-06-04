#!/usr/bin/env python3
"""Scope Claude Code global rules — cut the per-turn token tax.

Unscoped rules in ~/.claude/rules/ load on EVERY turn (and re-inject after every compaction). Language-specific
rules (web/python/go/…) only matter when you touch those files, so scoping them with `paths:` frontmatter means
they load only when relevant. This reports the unscoped rules + their token cost + a suggested scope; `--apply`
adds the frontmatter to the language-dir rules (leaving cross-cutting dirs like common/ for you to decide).

Usage:  python scope_rules.py            # dry-run report
        python scope_rules.py --apply    # add `paths:` to language-dir rules
"""
import os, sys, glob, re

HOME = os.path.expanduser("~")
RULES = os.path.join(HOME, ".claude", "rules")
# directory -> suggested glob scope. Cross-cutting dirs (common, zh, …) are reported but NOT auto-scoped.
SCOPE = {
    "web": '["**/*.{ts,tsx,js,jsx,vue,svelte,css,scss,html,astro}"]',
    "typescript": '["**/*.{ts,tsx}"]', "javascript": '["**/*.{js,jsx,mjs,cjs}"]',
    "python": '["**/*.py"]', "golang": '["**/*.go"]', "go": '["**/*.go"]',
    "swift": '["**/*.swift"]', "php": '["**/*.php"]', "rust": '["**/*.rs"]',
    "java": '["**/*.java"]', "csharp": '["**/*.cs"]', "ruby": '["**/*.rb"]',
}
CROSS_CUT = {"common", "zh", "rules"}  # intentionally global / translations — left to the user

def est(s):
    try:
        import tiktoken; return len(tiktoken.get_encoding("cl100k_base").encode(s, disallowed_special=()))
    except Exception:
        return len(s) // 4

def has_paths(txt):
    m = re.match(r"^\s*---\s*\n(.*?)\n---", txt, re.S)
    return bool(m and re.search(r"^\s*paths\s*:", m.group(1), re.M))

def main():
    apply = "--apply" in sys.argv
    if not os.path.isdir(RULES):
        print(f"No rules dir at {RULES}"); return
    files = glob.glob(os.path.join(RULES, "**", "*.md"), recursive=True)
    rows = []; total = 0; scoped_total = 0
    for f in files:
        txt = open(f, encoding="utf-8", errors="ignore").read()
        tk = est(txt); total += tk
        if has_paths(txt): scoped_total += tk; continue
        d = os.path.basename(os.path.dirname(f))
        rows.append((f, d, tk, SCOPE.get(d)))
    unscoped = sum(r[2] for r in rows)
    print("=" * 80)
    print(f"  RULES SCOPE AUDIT — {RULES}")
    print("=" * 80)
    print(f"  {len(files)} rule files · ~{total:,} tok total · already scoped ~{scoped_total:,} · "
          f"UNSCOPED ~{unscoped:,} tok loaded EVERY turn\n")
    applied = 0
    for f, d, tk, scope in sorted(rows, key=lambda x: -x[2]):
        rel = os.path.relpath(f, RULES)
        if scope:
            tag = f"-> scope {scope}"
            if apply:
                txt = open(f, encoding="utf-8", errors="ignore").read()
                m = re.match(r"^\s*---\s*\n.*?\n---\s*\n", txt, re.S)
                if m:  # has frontmatter without paths -> inject
                    new = m.group(0).rstrip()[:-3] + f"paths: {scope}\n---\n" + txt[m.end():]
                else:  # no frontmatter -> prepend
                    new = f"---\npaths: {scope}\n---\n\n" + txt
                open(f, "w", encoding="utf-8").write(new); applied += 1; tag = f"** APPLIED scope {scope}"
        else:
            tag = "(cross-cutting — review: scope manually, keep global, or delete if unused e.g. translations)"
        print(f"  {rel:<38} ~{tk:>6,} tok   {tag}")
    print()
    if apply:
        print(f"  Applied `paths:` to {applied} language-dir rule(s). They now load only for matching files.")
        print(f"  Re-run token_report.py to confirm the per-turn static cost dropped.")
    else:
        scopable = sum(r[2] for r in rows if r[3])
        crosscut = unscoped - scopable
        print(f"  DRY-RUN. ~{scopable:,} tok is in language-dir rules safely scopable now (run --apply).")
        print(f"  ~{crosscut:,} tok is cross-cutting (common/zh/…) — review manually (e.g. delete unused translations).")
    print("=" * 80)

if __name__ == "__main__":
    main()
