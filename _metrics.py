#!/usr/bin/env python3
"""Shared metrics core for the watch panel + statusline.

Reads the EXACT token usage Claude Code records (reusing token_report's reader) — INCREMENTALLY: a byte-offset
cache means each refresh only parses the new transcript lines, so the watcher itself stays cheap even on a
multi-hundred-MB transcript. Also reads the per-project KASM (kos-memory) store + the hook signal log.
"""
import os, sys, json, time, sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import token_report as tr                       # reuse turn_usage / find_transcript / RATE / H
try:
    import _signals
except Exception:
    _signals = None

HOME = os.path.expanduser("~")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".metrics_cache.json")
CTX_WINDOW = int(os.environ.get("CLAUDE_CTX_WINDOW", "200000"))   # 200K default; set 1000000 for 1M-context
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

# ── incremental transcript totals ────────────────────────────────────────────
def _load_cache():
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return {}

def _save_cache(c):
    try:
        tmp = CACHE + ".tmp"; json.dump(c, open(tmp, "w", encoding="utf-8")); os.replace(tmp, CACHE)
    except Exception:
        pass

def _zero():
    return {"offset": 0, "size": 0, "turns": 0, "last_ctx": 0,
            "totals": {k: 0 for k in _USAGE_KEYS}}

def transcript_totals(tx):
    """Cumulative exact usage for transcript `tx`, parsing only bytes appended since last call."""
    cache = _load_cache()
    st = cache.get(tx)
    try:
        size = os.path.getsize(tx)
    except Exception:
        return _zero()
    if not st or size < st.get("size", 0):     # new file or truncated/rotated -> full reparse
        st = _zero()
    try:
        with open(tx, "rb") as f:
            f.seek(st["offset"])
            chunk = f.read()
    except Exception:
        return st
    cut = chunk.rfind(b"\n")
    if cut != -1:
        complete = chunk[:cut + 1]
        for raw in complete.split(b"\n"):
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "ignore"))
            except Exception:
                continue
            u = (obj.get("message") or obj).get("usage")
            if not isinstance(u, dict):
                continue
            tu = tr.turn_usage(u)
            if any(tu.values()):
                st["turns"] += 1
                for k in _USAGE_KEYS:
                    st["totals"][k] += tu[k]
                ctx = tu["input_tokens"] + tu["cache_read_input_tokens"] + tu["cache_creation_input_tokens"]
                if ctx:
                    st["last_ctx"] = ctx
        st["offset"] += len(complete)
    st["size"] = size
    cache[tx] = st
    _save_cache(cache)
    return st

# ── KASM (kos-memory) per-project state ──────────────────────────────────────
def find_kos(start=None):
    d = start or os.getcwd()
    for _ in range(12):
        cand = os.path.join(d, ".kos-memory")
        if os.path.isdir(cand):
            return cand
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None

def kasm_state(cwd=None):
    kdir = find_kos(cwd)
    m = {"present": bool(kdir), "dir": kdir, "chunks": 0, "last_ts": None, "mode": "primary",
         "recalls_today": 0, "tokens_today": 0, "cost_today": 0.0, "token_cap": 50000, "recall_cap": 50}
    if not kdir:
        return m
    db = os.path.join(kdir, "chunks.db")
    if os.path.exists(db):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
            r = con.execute("SELECT COUNT(*), MAX(ts) FROM chunks").fetchone()
            m["chunks"], m["last_ts"] = (r[0] or 0), r[1]
            con.close()
        except Exception:
            pass
    try:
        b = json.load(open(os.path.join(kdir, "budget.json"), encoding="utf-8"))
        m["recalls_today"] = b.get("recalls_today", 0)
        m["tokens_today"] = b.get("tokens_used", 0)
        m["cost_today"] = b.get("cost_usd_today", 0.0)
    except Exception:
        pass
    try:
        m["mode"] = json.load(open(os.path.join(kdir, "config.json"), encoding="utf-8")).get("mode", "primary")
    except Exception:
        pass
    return m

# ── checkpoint freshness ─────────────────────────────────────────────────────
def checkpoint_info(cwd=None):
    p = os.path.join(cwd or os.getcwd(), ".claude", "CHECKPOINT.md")
    return {"exists": os.path.exists(p), "mtime": (os.path.getmtime(p) if os.path.exists(p) else 0)}

# ── public entrypoint ────────────────────────────────────────────────────────
def gather(transcript=None, cwd=None):
    tx = transcript or tr.find_transcript()
    st = transcript_totals(tx) if (tx and os.path.exists(tx)) else _zero()
    t = st["totals"]
    inp, outp = t["input_tokens"], t["output_tokens"]
    cr, cw = t["cache_read_input_tokens"], t["cache_creation_input_tokens"]
    total_in = inp + cr + cw
    cost = (inp * tr.RATE["input"] + outp * tr.RATE["output"]
            + cr * tr.RATE["cache_read"] + cw * tr.RATE["cache_write"]) / 1e6
    return {
        "transcript": tx, "turns": st["turns"],
        "out": outp, "cache_read": cr, "cache_write": cw, "input": inp, "total_in": total_in,
        "cache_hit": 100 * cr / max(total_in, 1), "cost": cost,
        "ctx_tokens": st["last_ctx"], "ctx_window": CTX_WINDOW,
        "ctx_pct": 100 * st["last_ctx"] / max(CTX_WINDOW, 1),
        "kasm": kasm_state(cwd),
        "checkpoint": checkpoint_info(cwd),
        "signals": (_signals.tail(12) if _signals else []),
    }

def fmt_age(epoch):
    if not epoch:
        return "—"
    s = max(0, int(time.time() - epoch))
    if s < 60:   return f"{s}s ago"
    if s < 3600: return f"{s // 60}m ago"
    if s < 86400:return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"

H = tr.H

if __name__ == "__main__":
    import pprint; pprint.pprint(gather())
