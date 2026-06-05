#!/usr/bin/env python3
"""Claude Code statusLine — an always-on, one-line 'what's going on'.

Wire into ~/.claude/settings.json:
  "statusLine": { "type": "command", "command": "python ~/.claude/tools/statusline.py" }

Claude Code pipes the session JSON ({transcript_path, cwd, model, …}) on stdin and renders the first stdout
line at the bottom of the UI. Shows: context-fill %, cumulative output, $ est, cache-hit, KASM chunk count +
recalls today, and checkpoint freshness. Reuses the EXACT incremental metrics core (cheap per render).
"""
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _metrics as M

_NOCOLOR = bool(os.environ.get("NO_COLOR"))
def c(s, code):                       # statusline IS color-capable even though stdout isn't a tty
    return s if _NOCOLOR else f"\033[{code}m{s}\033[0m"
DIM, GREEN, YELLOW, RED, CYAN = "2", "32", "33", "31", "36"

def main():
    payload = {}
    try:
        if not sys.stdin.isatty():
            payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    tx = payload.get("transcript_path")
    cwd = payload.get("cwd") or (payload.get("workspace") or {}).get("current_dir")
    model = ((payload.get("model") or {}).get("display_name") or "").replace("Claude ", "").strip()

    try:
        m = M.gather(transcript=tx, cwd=cwd)
    except Exception:
        print("kasm-watch: (metrics unavailable)"); return

    pct = m["ctx_pct"]
    ctxcol = GREEN if pct < 60 else (YELLOW if pct < 85 else RED)
    parts = []
    if model:
        parts.append(c(model, DIM))
    parts.append(c(f"ctx {pct:.0f}%", ctxcol))
    parts.append(f"out {M.H(m['out'])}")
    parts.append(c(f"${m['cost']:,.0f}", GREEN))
    parts.append(c(f"hit {m['cache_hit']:.0f}%", DIM))
    k = m["kasm"]
    if k["present"]:
        parts.append(c(f"kasm {k['chunks']}·{k['recalls_today']}⤓", CYAN))
    cp = m["checkpoint"]
    if cp["exists"]:
        parts.append(c("✓" + M.fmt_age(cp["mtime"]).replace(" ago", ""), GREEN))
    print(c(" · ", DIM).join(parts))

if __name__ == "__main__":
    main()
