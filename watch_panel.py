#!/usr/bin/env python3
"""Live watch panel — see what's going on in a Claude Code session, in real time.

Run in a SIDE terminal:  python ~/.claude/tools/watch_panel.py  [--interval 2] [transcript.jsonl]

Refreshing in place: context-window fill, cumulative output / cache / cost (EXACT from API usage), KASM
(kos-memory) per-project state, and a live feed of hook activity (checkpoint saved, write_guard nudged, …).
Pure stdlib. Ctrl-C to quit.
"""
import os, sys, time, shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _metrics as M

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_NOCOLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()
def c(s, code):
    return s if _NOCOLOR else f"\033[{code}m{s}\033[0m"
DIM, BOLD, GREEN, YELLOW, RED, CYAN = "2", "1", "32", "33", "31", "36"

def gauge(pct, width):
    pct = max(0.0, min(100.0, pct)); fill = int(round(pct / 100 * width))
    col = GREEN if pct < 60 else (YELLOW if pct < 85 else RED)
    return c("█" * fill, col) + c("░" * (width - fill), DIM)

def clip(s, n):
    return s if len(s) <= n else (s[:max(0, n - 1)] + "…")

class Box:
    """Box builder that keeps every line exactly W+4 visible chars wide."""
    def __init__(self, W):
        self.W = W; self.rows = []
    def top(self, title, right):
        head = f"╭─ {title} "; tail = f" {right} ─╮"
        self.rows.append(c(head + "─" * max(0, (self.W + 4) - len(head) - len(tail)) + tail, DIM))
    def sep(self, title=""):
        if title:
            seg = f"─ {title} "
            self.rows.append(c("├" + seg + "─" * max(0, (self.W + 2) - len(seg)) + "┤", DIM))
        else:
            self.rows.append(c("├" + "─" * (self.W + 2) + "┤", DIM))
    def bottom(self):
        self.rows.append(c("╰" + "─" * (self.W + 2) + "╯", DIM))
    def row(self, colored, plain):
        pad = " " * max(0, self.W - len(plain))
        self.rows.append(c("│", DIM) + " " + colored + pad + " " + c("│", DIM))

def render(m, width):
    W = max(50, min(width, 100)) - 4
    b = Box(W)
    k = m["kasm"]
    proj = os.path.basename(os.path.dirname(k["dir"])) if k.get("dir") else os.path.basename(os.getcwd())

    b.top(c("Claude Code — live watch", BOLD), time.strftime("%H:%M:%S"))

    ctxv = f"{M.H(m['ctx_tokens'])} / {M.H(m['ctx_window'])}"
    pctstr = f"  {m['ctx_pct']:4.0f}%  "
    gw = max(8, W - len("CONTEXT  ") - len(pctstr) - len(ctxv))
    b.row(c("CONTEXT  ", BOLD) + gauge(m["ctx_pct"], gw) + pctstr + c(ctxv, DIM),
          "CONTEXT  " + "x" * gw + pctstr + ctxv)
    b.row(c("OUTPUT   ", BOLD) + c(M.H(m["out"]), YELLOW) + c("  tokens · un-cacheable, the $ driver", DIM),
          "OUTPUT   " + M.H(m["out"]) + "  tokens · un-cacheable, the $ driver")
    cl = f"read {M.H(m['cache_read'])} · write {M.H(m['cache_write'])} · hit {m['cache_hit']:.0f}%"
    b.row(c("CACHE    ", BOLD) + cl, "CACHE    " + cl)
    b.row(c("COST     ", BOLD) + c(f"~${m['cost']:,.0f}", GREEN) + c("  session est (adjust RATE)", DIM),
          "COST     " + f"~${m['cost']:,.0f}" + "  session est (adjust RATE)")
    tl = f"{m['turns']}  ·  avg out {M.H(m['out'] / max(m['turns'], 1))} · last ctx {M.H(m['ctx_tokens'])}"
    b.row(c("TURNS    ", BOLD) + tl, "TURNS    " + tl)

    b.sep("KASM memory (per-project)")
    if not k["present"]:
        b.row(c("not initialized here", DIM) + " — bootstrap with kasm/ (see README)",
              "not initialized here — bootstrap with kasm/ (see README)")
    else:
        l1 = f"{proj} · {k['chunks']} chunks · {k['mode']} · ingest {M.fmt_age(k['last_ts'])}"
        b.row(c("project  ", BOLD) + clip(l1, W - 9), "project  " + clip(l1, W - 9))
        rc = (f"{k['recalls_today']}/{k['recall_cap']} calls · "
              f"{M.H(k['tokens_today'])}/{M.H(k['token_cap'])} tok · ${k['cost_today']:.2f} today")
        b.row(c("recall   ", BOLD) + rc, "recall   " + rc)

    b.sep("activity")
    cp = m["checkpoint"]
    cpv = ("checkpoint " + M.fmt_age(cp["mtime"])) if cp["exists"] else "no checkpoint yet"
    b.row(c("saved    ", BOLD) + c(cpv, GREEN if cp["exists"] else DIM), "saved    " + cpv)
    sig = m["signals"]
    if not sig:
        b.row(c("(no hook activity yet — checkpoint/write_guard log here as they fire)", DIM),
              "(no hook activity yet — checkpoint/write_guard log here as they fire)")
    for e in sig[-6:]:
        hm = time.strftime("%H:%M", time.localtime(e.get("ts", 0)))
        ev = e.get("event", "?"); det = clip(e.get("detail", ""), W - 16)
        b.row(c(hm + "  ", DIM) + c(f"{ev:<11} ", CYAN) + det, f"{hm}  {ev:<11} {det}")

    b.bottom()
    return b.rows

def main():
    interval, tx, once = 2.0, None, False
    args = sys.argv[1:]
    if "--once" in args:
        once = True; args.remove("--once")
    if "--interval" in args:
        i = args.index("--interval"); interval = float(args[i + 1]); del args[i:i + 2]
    if args:
        tx = args[0]
    if once:
        width = shutil.get_terminal_size((80, 24)).columns
        sys.stdout.write("\n".join(render(M.gather(transcript=tx), width)) + "\n")
        return
    try:
        while True:
            width = shutil.get_terminal_size((80, 24)).columns
            m = M.gather(transcript=tx)
            foot = f"  refresh {interval:g}s · Ctrl-C to quit · ctx window {M.H(m['ctx_window'])} (CLAUDE_CTX_WINDOW)"
            sys.stdout.write("\033[H\033[2J" + "\n".join(render(m, width)) + "\n" + c(foot, DIM) + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")

if __name__ == "__main__":
    main()
