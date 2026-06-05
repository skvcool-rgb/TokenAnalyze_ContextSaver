# Pulse

> **Live vitals, efficiency, and memory for long Claude Code sessions.** **See** what's going on (live),
> **measure** exactly where your tokens go (from the *real* API usage, not guesses), **prevent** the two biggest
> sinks before they happen, **never lose** your thread when context auto-compacts, and keep a per-project
> **memory**. Pulse bundles a live **watch panel + statusline**, an exact **token-utilization analyzer**,
> **auto-checkpoint** across compaction, **waste-prevention hooks**, and **KASM** per-project memory — pure
> stdlib, MIT, nothing phones home.

`/context` is a one-shot snapshot. Cost trackers show $/day. Neither tells you the cumulative, **by-source**
story of a session — and the tools that *act* for you (checkpoint, write-guard, memory recall) run as **hooks**,
which are **invisible**: you never see them fire. This suite closes both gaps — a live panel + an always-on
statusline so you can *watch* what's happening, plus the analyzer, the prevention hooks, and a bundled memory
layer (KASM) they all surface.

---

## What's in the box

| component | what it does | how you run it |
|-----------|--------------|----------------|
| **`watch_panel.py`** | live TUI: context fill, output/cache/cost (exact), KASM state, hook-activity feed | side terminal |
| **`statusline.py`** | always-on one-liner in the Claude Code UI | `statusLine` config |
| **`token_report.py`** | one-shot, full token-utilization report + ranked levers | on demand |
| **`scope_rules.py`** | cut the per-turn rules tax (`paths:` scoping) | on demand / `--apply` |
| **`write_guard.py`** | nudge `Edit` over a wasteful full-file `Write` | `PreToolUse` hook |
| **`checkpoint.py` / `resume.py`** | save + re-inject resume state across compaction | `PreCompact` / `SessionStart` hooks |
| **`kasm/`** | per-project memory (KOS-Memory, vendored) | its own installer |

The hooks all log to a tiny shared activity feed (`.signals.jsonl`) so the panel and statusline can show them.

---

## What it saves

Anchored to **one measured** Claude Code session (5,736 turns). These are *mechanism-based* estimates, not a
guarantee — your numbers depend on your workflow. The real value is that you can finally **see which lever is
yours**; `token_report.py` ranks them for *your* session.

| lever | what it cuts | in the measured session |
|-------|--------------|--------------------------|
| **KASM** | the whole context window re-read **every turn** (cache-read) | **2.88B** cache-read tokens — the largest lever: externalize history, re-inject a slice |
| `write_guard` | file content re-sent on a full `Write` (output is the priciest, un-cacheable token) | 44.6M output tokens; turning wasteful rewrites into `Edit` diffs saves the highest-$ token |
| `scope_rules` | unscoped rules re-read on every turn | ~9.5K never-used tokens/turn × 5.7K turns ≈ 54M cache-reads — fixed in one `--apply` |
| `checkpoint`/`resume` | re-deriving "where were we" after a compaction | avoids re-reading files and re-exploring state |

## Benefits

- **See the invisible** — hooks act silently; the panel + statusline surface every checkpoint, nudge, and recall.
- **Exact, not guessed** — token totals come from the API `usage` Claude records, not a tokenizer proxy.
- **Cheap to run** — incremental parsing; a refresh is ~0.01s even on a 30 MB+ transcript.
- **Zero lock-in, zero deps** — pure stdlib, local files, MIT. Nothing phones home.
- **Never lose your place** — conversation state survives auto-compaction.
- **Per-project memory that won't cross-contaminate** — KASM is isolated per repo (verified end-to-end).

## Use cases

- **Solo devs on long sessions** — watch context fill, `/compact` at the right moment, keep cost in view.
- **Teams burning real money on agents** — `token_report` + the panel make per-session spend legible — the first
  step toward a fleet cost policy.
- **Reviewers / leads** — the activity feed shows what the agent actually did (writes, checkpoints, recalls).
- **Resuming cold** — checkpoint/resume + KASM rebuild context after a compaction or days away.

---

## 1. See what's going on

### `watch_panel.py` — live panel
Run it in a **side terminal**; it refreshes in place (~free per tick — it reads the transcript *incrementally*,
not from scratch):

```text
$ python ~/.claude/tools/watch_panel.py
╭─ Claude Code — live watch ───────────────────────────────── 14:22:07 ─╮
│ CONTEXT  ███████████████████░░░░░░░░░░░   64%   128K / 200K            │
│ OUTPUT   1.2M  tokens · un-cacheable, the $ driver                    │
│ CACHE    read 88.4M · write 2.1M · hit 93%                            │
│ COST     ~$312  session est (adjust RATE)                             │
│ TURNS    412  ·  avg out 3.0K · last ctx 128K                         │
├─ KASM memory (per-project) ────────────────────────────────────────────┤
│ project  my-app · 142 chunks · primary · ingest 4m ago                │
│ recall   3/50 calls · 1.2K/50K tok · $0.01 today                      │
├─ activity ─────────────────────────────────────────────────────────────┤
│ saved    checkpoint 2m ago                                            │
│ 14:20  checkpoint  saved · auto                                       │
│ 14:19  write_guard README.md ~88% unchanged                          │
│ 14:15  kasm        +6 chunks ingested                                 │
╰────────────────────────────────────────────────────────────────────────╯
  refresh 2s · Ctrl-C to quit · ctx window 200K (CLAUDE_CTX_WINDOW)
```
`python watch_panel.py --once` prints a single snapshot (good for piping). Set `CLAUDE_CTX_WINDOW=1000000` for a
1M-context model. `NO_COLOR=1` disables color.

### `statusline.py` — always-on, in the UI
The lowest-friction "what's going on" — Claude Code renders it at the bottom every turn:

```text
Opus · ctx 64% · out 1.2M · $312 · hit 93% · kasm 142·3⤓ · ✓2m
```
Wire it via the `statusLine` block in [`settings.example.json`](settings.example.json). `ctx` turns yellow then
red as the window fills; `kasm N·R` = chunks · recalls today; `✓2m` = last checkpoint age.

### `token_report.py` — the full report
- **Exact totals** from the `usage` Claude Code records per response (input / output / cache-read / cache-write).
  No tokenizer guessing — Claude's tokenizer isn't public for v3+, so the recorded usage **is** ground truth.
- **Cost** (exact tokens × adjustable rates) + **cache-hit %**, a **by-source content map** (what to trim), and
  **ranked levers** computed for *your* session.

---

## 2. Prevent — stop the two biggest sinks before they happen

### `scope_rules.py` — cut the per-turn rules tax
Unscoped rules in `~/.claude/rules/` load on **every** turn (and re-inject after every compaction). Language rules
(web/python/go/…) only matter when you touch those files. This audits them, shows each one's token cost, and
(`--apply`) adds `paths:` frontmatter so they load only when relevant.
```bash
python scope_rules.py            # dry-run report
python scope_rules.py --apply    # scope the language-dir rules
```

### `write_guard.py` — nudge `Edit` over wasteful full-`Write`
A `PreToolUse(Write)` hook. When a `Write` rewrites a large existing file that's **mostly unchanged**, the whole
content is re-sent as tokens (a top session cost) — an `Edit` sends only the diff. Non-blocking nudge by default;
**blocks** (forcing an Edit) with `WRITE_GUARD_STRICT=1`. New files and genuine rewrites pass through.

---

## 3. Never lose your thread — ContextSaver

- **`checkpoint.py`** (`PreCompact` hook) writes `.claude/CHECKPOINT.md` — your resume state — the instant
  **before** Claude Code compacts context.
- **`resume.py`** (`SessionStart` hook) re-injects it **after** compaction, so the resumed turn knows what you
  were doing.

Files on disk are never lost on compaction (`Write`/`Edit` persist immediately) — what's lost is the *conversation
state*. This preserves it. Auto-commit is off by default (`CLAUDE_CHECKPOINT_COMMIT=1` to enable).

---

## 4. Remember — KASM per-project memory ([`kasm/`](kasm/))

KASM (**KOS-Memory**, vendored — see [`kasm/VENDORED.md`](kasm/VENDORED.md)) is a local memory layer: it captures
session/project state into a per-project SQLite store and injects a relevant slice at session start / on demand —
**BM25 + grep, pure stdlib, no embeddings, no network.**

- **Per-project isolation** is the headline property: memory lives in `<project>/.kos-memory/chunks.db`; a recall
  resolves to exactly one project's DB — never a cross-project merge. (Verified end-to-end: a project cannot
  surface another project's chunks; a user-level pin doesn't bleed into a project recall.)
- **Bounded cost:** recall is throttled (50 calls / 50K tokens / $0.50 daily) and retrieval is local (zero
  inference). The watch panel surfaces today's recall budget so you can see it.

```bash
cd kasm && python scripts/install.py        # registers KASM's plugin + hooks (its own settings block)
cd kasm && python -m unittest discover -s tests   # ~399 tests
```

---

## Install

```bash
python install.py        # copies the scripts to ~/.claude/tools/ and prints the hooks + statusLine config
pip install tiktoken     # optional — sharper content-map estimate (falls back to ~4 chars/token)
```
Then merge the printed `statusLine` + `hooks` blocks into `~/.claude/settings.json` (see
[`settings.example.json`](settings.example.json)). KASM installs separately (step 3 of the installer output). On
Windows, use the full `python.exe` path if `python` isn't on the hook's PATH.

## How tokens are spent (the matrix)

| category | when it loads | reduce by |
|----------|---------------|-----------|
| system prompt + tool schemas | every turn | defer MCP tools; disable unused MCP servers |
| CLAUDE.md (global + project) | every turn; re-injected after compaction | trim; archive stale history |
| `~/.claude/rules/*.md` | every turn (**unscoped → always**) | `scope_rules.py --apply` |
| conversation history | grows → compaction | `/compact` at task boundaries; subagents; **KASM** to re-inject a slice |
| **tool-call inputs** (Write/Edit content) | per call | **`write_guard` → Edit diffs > full `Write`** |
| tool outputs (Bash/Read) | per call | grep/tail/head; Read `offset`/`limit` |
| **output tokens** | per turn (un-cacheable) | **generate less — the single biggest $ lever** |

Full guide: [`docs/TOKEN_EFFICIENCY.md`](docs/TOKEN_EFFICIENCY.md).

## Notes & accuracy

- **Token totals are EXACT** (from the API `usage` in the transcript). The panel/statusline read it
  **incrementally** (byte-offset cache) so a refresh is cheap even on a multi-hundred-MB transcript.
- Only the **by-source content map** in `token_report.py` is estimated (tiktoken `cl100k` proxy, ~10–20% off).
- The **$ figure** is exact token counts × *adjustable* Opus-class rates — set `RATE` at the top of
  `token_report.py`. The context-window % assumes 200K unless `CLAUDE_CTX_WINDOW` is set.
- Everything parses `~/.claude/projects/**/*.jsonl` and walks up from `cwd`. No hardcoded paths.

## License

MIT — see [`LICENSE`](LICENSE). KASM (`kasm/`) is MIT, vendored from
[KOS-MemoryV4](https://github.com/skvcool-rgb/KOS-MemoryV4).
