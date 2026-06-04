# Token Utilization & Efficiency — Claude Code

Two kinds of token cost:
- **STATIC** — re-paid **every turn** and re-injected from disk after **every compaction**. The permanent tax.
- **CONVERSATION** — the cumulative history that grows until it triggers compaction.

## The utilization matrix — what consumes tokens, when, and how to cut it
| category | what it is | when it loads | reduce by |
|----------|------------|---------------|-----------|
| system prompt + tool schemas | harness + tool defs | every turn (static) | deferred MCP tools; **disable unused MCP servers** |
| CLAUDE.md (global + project) | your instructions | every turn; re-injected after compaction | **trim**; archive superseded history to a NON-loaded file; keep only the active resume pointer |
| `~/.claude/rules/*.md` | global rules | every turn (**UNSCOPED → always**) | add **`paths:` frontmatter** to scope each rule to matching files |
| `MEMORY.md` | auto-memory | every turn | **consolidate-memory** skill |
| skill bodies | invoked skills | on invoke (capped 5K/25K) | fine as-is |
| conversation history | the dialogue | grows → compaction | compact/`clear` at task boundaries; subagents |
| **tool-call inputs** | Write/Edit content, tool args | per call | **Edit (small diff) > full-file Write**; iterate via `scriptPath`; don't paste content back |
| tool outputs | Bash/Read results | per call | **grep/tail/head**; background + filter; Read with `offset`/`limit` |
| thinking | extended reasoning | per turn (≤32K) | `MAX_THINKING_TOKENS` cap |

## This session — measured (2026-06-03, `token_report.py`, ~1.85M est. tokens)
```
STATIC 59.1K (3%):   rules/ 39.3K (unscoped) · project CLAUDE.md 11.6K · memory 8.2K · global CLAUDE.md 0.06K
CONVO 1791K (97%):   tool-call inputs 846K (46%) · tool outputs 509K (28%: Bash 230K + Read 181K)
                     · assistant text 369K (20%) · user 66K (4%)
```

## Your top 3 levers (ranked for THIS setup)
1. **Tool-call inputs — 46%, the per-session lever.** This session wrote dozens of scripts + docs; each full-file
   `Write` sends the whole content as tokens. Use **`Edit` (small diffs)** for changes, **iterate a script via its
   `scriptPath`** instead of re-sending it, and **don't paste large file content back into chat**.
2. **Unscoped rules — 39.3K loaded EVERY turn, the permanent lever.** Add `paths:` frontmatter so e.g. web rules load
   only for web files, python rules only for `*.py`. This is a fixed tax you pay on every single turn + after every compaction.
3. **Memory + CLAUDE.md hygiene.** Consolidate `MEMORY.md` (8.2K); archive superseded CLAUDE.md history (the giant
   BoundaryAI resume log) to a non-auto-loaded doc, keeping only the live pointer + locks.

## Efficiency checklist
- **`/context`** — authoritative live snapshot + the harness's own suggestions
- **`python ~/.claude/tools/token_report.py`** — cumulative by-source view (this tool; what `/context` doesn't show)
- Filter **every** command output (grep/tail/head); background long runs
- Read with `offset`/`limit`; never re-read a file you just edited (the harness already tracks its state)
- **Edit > Write** for changes; `scriptPath` to iterate scripts
- **Subagents** for big fan-out searches — their context is separate; only the summary returns
- **Compact / `clear`** at task boundaries
- Scope rules with `paths:`; consolidate memory; archive stale CLAUDE.md
- Deferred MCP tools (already on) + disable unused MCP servers

## Companion: auto-save before compaction
`checkpoint.py` runs on the **PreCompact** hook → writes `.claude/CHECKPOINT.md` (resume state) the instant before
compaction. Files are already on disk (Write/Edit persist immediately) — this preserves the *conversation state*
(what we were doing) so the post-compaction turn resumes cleanly. Auto-commit is OFF by default
(`CLAUDE_CHECKPOINT_COMMIT=1` to enable). Wire via the PreCompact hook in `settings.json`.
