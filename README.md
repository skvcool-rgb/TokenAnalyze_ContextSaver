# TokenAnalyze · ContextSaver

> Two tiny, dependency-light utilities for **long Claude Code sessions**: see exactly where your tokens go — from
> the *real* API usage, not guesses — and never lose your thread when the context window auto-compacts.

`/context` shows a live snapshot. The popular cost trackers show $/day. **Neither tells you the cumulative,
by-source story of a session** — that (say) *47% went to file-`Write` content*, or your *unscoped rules cost 39K
tokens on every single turn* — and neither reads the **exact** usage Claude Code already records. This does both,
plus a safety net so a surprise compaction never loses your conversation state.

---

## 1. `token_report.py` — token utilization analyzer

- **Exact totals** from the `usage` Claude Code records per API response (input / output / cache-read / cache-write).
  No tokenizer guessing — Claude's tokenizer isn't public for v3+, so the recorded usage **is** the ground truth.
- **Cost estimate** (exact tokens × adjustable rates) + **cache-hit %**.
- **By-source content map** (tiktoken estimate) — what's in the window and what to trim.
- **Ranked efficiency levers** computed for *your* session, anchored to the real cost drivers.

```text
$ python token_report.py
==========================================================================================
  TOKEN UTILIZATION REPORT
  ── REAL throughput (EXACT, from the API usage in the transcript) — N turns ──
    output tokens          44.0M    ← un-cacheable, ~5x input price = the $ driver
    cache READ (context)    2.78B    ← the window re-read every turn (context_size × turns)
    total input processed   2.99B    cache-hit 93.2%  (caching working)
    ~cost (Opus-class est) $X,XXX    (output $… + cache_read $… + cache_write $…) — ADJUST RATES
  ── CONTENT MAP (ESTIMATE via tiktoken; 'what is in the window / what to trim') ──
    STATIC config (every turn): 48K   rules/ 39K (UNSCOPED → add paths: frontmatter)
    CONVERSATION unique:        1.8M  tool inputs 47% · outputs 28% · assistant 21% · user 4%
  ── EFFICIENCY (ranked by the REAL cost drivers) ──
    1. OUTPUT is the priciest/un-cacheable — generate less (Edit > full Write, shorter replies, no big dumps)
    2. CONTEXT SIZE drives cache_read — trim STATIC config, /compact at task boundaries
    3. Filter tool outputs (grep/tail/head), Read offset/limit, subagents for big searches
==========================================================================================
```

## 2. ContextSaver — auto-checkpoint before compaction

- **`checkpoint.py`** (PreCompact hook) writes `.claude/CHECKPOINT.md` — your resume state — the instant **before**
  Claude Code compacts context.
- **`resume.py`** (SessionStart hook) re-injects that checkpoint **after** compaction, so the resumed turn knows what
  you were doing.

Files on disk are never lost on compaction (`Write`/`Edit` persist immediately) — what's lost is the *conversation
state*. This preserves it.

---

## Install

```bash
python install.py        # copies the scripts to ~/.claude/tools/ and prints the hook config
pip install tiktoken     # optional — sharper content-map estimate (falls back to ~4 chars/token)
```
Then merge the printed `PreCompact` / `SessionStart` blocks into the `hooks` object of `~/.claude/settings.json`
(see [`settings.example.json`](settings.example.json)). On Windows, use the full path or `python.exe` if `python`
isn't on the hook's PATH.

## Usage

```bash
python ~/.claude/tools/token_report.py            # analyze the most-recent session
python ~/.claude/tools/token_report.py <file.jsonl>   # a specific transcript
/context                                            # built-in live-window snapshot
```
The hooks fire automatically on compaction — nothing to run.

## How tokens are spent (the matrix)

| category | when it loads | reduce by |
|----------|---------------|-----------|
| system prompt + tool schemas | every turn | defer MCP tools; disable unused MCP servers |
| CLAUDE.md (global + project) | every turn; re-injected after compaction | trim; archive stale history to a non-loaded file |
| `~/.claude/rules/*.md` | every turn (**unscoped → always**) | add `paths:` frontmatter to scope each rule |
| `MEMORY.md` | every turn | consolidate |
| conversation history | grows → compaction | `/compact` or `/clear` at task boundaries; subagents |
| **tool-call inputs** (Write/Edit content) | per call | **`Edit` small diffs > full-file `Write`**; don't re-send content |
| tool outputs (Bash/Read) | per call | grep/tail/head; background + filter; Read `offset`/`limit` |
| thinking | per turn | `MAX_THINKING_TOKENS` cap |
| **output tokens** | per turn (un-cacheable) | **generate less — the single biggest $ lever** |

Full guide: [`docs/TOKEN_EFFICIENCY.md`](docs/TOKEN_EFFICIENCY.md).

## Notes & accuracy

- **Totals are EXACT** (from the API `usage` in the transcript). Only the **by-source content map** is estimated
  (tiktoken `cl100k` proxy, ~10–20% off) — for per-segment exact counts use Anthropic's `count_tokens` API.
- The **$ figure** is exact token counts × *adjustable* Opus-class rates — set `RATE` at the top of `token_report.py`.
- Parses `~/.claude/projects/**/*.jsonl`; walks up from `cwd` for the project `CLAUDE.md`. No hardcoded paths.

## License

MIT — see [`LICENSE`](LICENSE). Use freely.
