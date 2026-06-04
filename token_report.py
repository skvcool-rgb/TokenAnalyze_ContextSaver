#!/usr/bin/env python3
"""Token utilization report for a Claude Code session.

ACCURATE layer: reads the EXACT per-turn `usage` (input/output/cache) that Claude Code records from each API
response — ground truth, no tokenizer needed (Claude's tokenizer isn't public for v3+; the usage IS the real count).
ESTIMATE layer: a tiktoken by-source "content map" (what's in the window / what to trim) — labeled as estimate.

Usage:  python token_report.py [transcript.jsonl]
"""
import os, sys, json, glob

# ---- approximate per-Mtoken USD (Opus-class). ADJUST to your model/tier — estimates, not authoritative ----
RATE = {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75}

HOME = os.path.expanduser("~")
try:
    import tiktoken; _ENC = tiktoken.get_encoding("cl100k_base")
    def est(s): return len(_ENC.encode(s, disallowed_special=())) if s else 0
    ESTNOTE = "tiktoken cl100k (proxy, ~10-20% off Claude)"
except Exception:
    def est(s): return (len(s) // 4) if s else 0
    ESTNOTE = "~4 chars/token"

def ftok(p):
    try:
        with open(p, encoding="utf-8", errors="ignore") as f: return est(f.read())
    except Exception: return 0
def dtok(d, pat="*.md"): return sum(ftok(p) for p in glob.glob(os.path.join(d, "**", pat), recursive=True))
def H(n): return f"{n/1e9:.2f}B" if n >= 1e9 else (f"{n/1e6:.1f}M" if n >= 1e6 else (f"{n/1e3:.1f}K" if n >= 1e3 else str(int(n))))

def find_transcript():
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]): return sys.argv[1]
    js = glob.glob(os.path.join(HOME, ".claude", "projects", "**", "*.jsonl"), recursive=True)
    return max(js, key=os.path.getmtime) if js else None

def turn_usage(u):
    """Exact per-turn counts; usage lives top-level OR in iterations[] — take max (one is 0) to avoid double-count."""
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    out = {k: int(u.get(k, 0) or 0) for k in keys}
    its = u.get("iterations") or []
    if isinstance(its, list):
        for k in keys:
            out[k] = max(out[k], sum(int(i.get(k, 0) or 0) for i in its if isinstance(i, dict)))
    return out

def main():
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    tx = find_transcript()
    print("=" * 90); print("  TOKEN UTILIZATION REPORT"); print("=" * 90)

    real = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    turns = nlines = 0
    buckets = {"user": 0, "assistant text": 0, "thinking": 0, "tool calls (input)": 0, "tool outputs": 0}
    tool_out = {}; id2tool = {}
    if tx:
        for line in open(tx, encoding="utf-8", errors="ignore"):
            nlines += 1
            try: obj = json.loads(line)
            except Exception: continue
            msg = obj.get("message") or obj
            u = msg.get("usage")
            if isinstance(u, dict):
                tu = turn_usage(u)
                if any(tu.values()): turns += 1
                for k in real: real[k] += tu[k]
            role = msg.get("role", obj.get("type", "")); content = msg.get("content", "")
            if isinstance(content, str):
                buckets["user" if role == "user" else "assistant text"] += est(content); continue
            if not isinstance(content, list): continue
            for b in content:
                t = b.get("type") if isinstance(b, dict) else None
                if t == "text": buckets["assistant text" if role == "assistant" else "user"] += est(b.get("text", ""))
                elif t == "thinking": buckets["thinking"] += est(b.get("thinking", "") or b.get("text", ""))
                elif t == "tool_use":
                    buckets["tool calls (input)"] += est(json.dumps(b.get("input", {})))
                    if b.get("id"): id2tool[b["id"]] = b.get("name", "?")
                elif t == "tool_result":
                    nm = id2tool.get(b.get("tool_use_id"), "tool"); c = b.get("content", "")
                    tk = est(c if isinstance(c, str) else json.dumps(c))
                    buckets["tool outputs"] += tk; tool_out[nm] = tool_out.get(nm, 0) + tk

    # ---- REAL (exact, from API usage) ----
    inp, outp = real["input_tokens"], real["output_tokens"]
    cr, cw = real["cache_read_input_tokens"], real["cache_creation_input_tokens"]
    total_in = inp + cr + cw
    cost = inp/1e6*RATE["input"] + outp/1e6*RATE["output"] + cr/1e6*RATE["cache_read"] + cw/1e6*RATE["cache_write"]
    print(f"\n  ── REAL throughput (EXACT, from the API usage in the transcript) — {turns} turns ──")
    print(f"    output tokens          {H(outp):>9}   (un-cacheable; ~5x input price — the $ driver)")
    print(f"    cache READ  (context)  {H(cr):>9}   (cheap ~10% input, but volume = context_size × turns)")
    print(f"    cache WRITE            {H(cw):>9}")
    print(f"    input (fresh)          {H(inp):>9}")
    print(f"    total input processed  {H(total_in):>9}   cache-hit { (100*cr/max(total_in,1)):.1f}%  (caching working)")
    print(f"    ~cost (Opus-class est) ${cost:,.0f}   (output ${outp/1e6*RATE['output']:,.0f} + "
          f"cache_read ${cr/1e6*RATE['cache_read']:,.0f} + cache_write ${cw/1e6*RATE['cache_write']:,.0f}) — ADJUST RATES")
    if turns: print(f"    per turn avg           out {H(outp/turns)}  ·  context {H(cr/max(turns,1))}")

    # ---- STATIC config (loaded every turn; tiktoken estimate) ----
    g = ftok(os.path.join(HOME, ".claude", "CLAUDE.md")); rules = dtok(os.path.join(HOME, ".claude", "rules"))
    mem = sum(ftok(p) for p in glob.glob(os.path.join(HOME, ".claude", "projects", "**", "MEMORY.md"), recursive=True))
    proj = 0; d = os.getcwd()
    for _ in range(8):
        c = os.path.join(d, "CLAUDE.md")
        if os.path.exists(c): proj = max(proj, ftok(c))
        nd = os.path.dirname(d); d = nd if nd != d else d
        if nd == d: break
    static = g + rules + proj + mem
    print(f"\n  ── CONTENT MAP (ESTIMATE via {ESTNOTE}; for 'what is in the window / what to trim') ──")
    print(f"    STATIC config (every turn → re-read into cache each turn): {H(static)}")
    for nm, v, note in [("rules/", rules, "UNSCOPED → add paths: frontmatter"), ("project CLAUDE.md", proj, ""),
                        ("memory MEMORY.md", mem, "consolidate"), ("global CLAUDE.md", g, "")]:
        if v: print(f"      {nm:<22} {H(v):>8}   {note}")
    conv = sum(buckets.values())
    print(f"    CONVERSATION unique content: {H(conv)}")
    for nm in ("tool calls (input)", "tool outputs", "assistant text", "thinking", "user"):
        if buckets[nm]: print(f"      {nm:<22} {H(buckets[nm]):>8}   {100*buckets[nm]/max(conv,1):3.0f}%")
    if tool_out:
        top = sorted(tool_out.items(), key=lambda x: -x[1])[:5]
        print(f"      └ outputs by tool: " + " · ".join(f"{k} {H(v)}" for k, v in top))

    # ---- levers (anchored to the REAL numbers) ----
    print(f"\n  ── EFFICIENCY (ranked by the REAL cost drivers) ──")
    print(f"    1. OUTPUT ({H(outp)}, the priciest/un-cacheable) — generate less: shorter replies, Edit>Write, no big file dumps, fewer re-prints.")
    print(f"    2. CONTEXT SIZE drives cache_read ({H(cr)}) — every turn re-reads the whole window. Trim STATIC ({H(static)}: rules {H(rules)} unscoped!), /compact at task boundaries.")
    print(f"    3. Filter tool outputs (grep/tail/head), Read offset/limit, subagents for big searches (separate context).")
    print(f"\n    For per-SEGMENT exact counts use Anthropic's count_tokens API; /context = live window snapshot.")
    print(f"  transcript: {tx}")
    print("=" * 90)

if __name__ == "__main__":
    main()
