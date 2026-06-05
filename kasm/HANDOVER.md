# kos-memory v4 — HANDOVER

> Last updated: 2026-05-02 (post-ship)
> Project: kos-memory v4 — pure-stdlib backup memory layer for Claude Code

---

## RESUME-HERE — read this FIRST

**STATUS: SHIP-READY. All 8 sprints complete. 164/164 tests passing.**

Path: `C:\Users\suraj\Downloads\kos-memory-v4\`

### Ship checklist (operator)
1. `python scripts/install.py` — registers plugin in `~/.claude/settings.json` + smoke-tests.
2. Restart Claude Code.
3. Try `/memory-status` — should print empty-store state. Have a normal session, end it, then `/memory-status` again — chunks > 0.
4. `python -m unittest discover tests` — must pass 164/164.

### What's done
- 7 lib modules (paths, store, chunker, search, budget, catalog, recall, stdio_utf8)
- 4 hooks (SessionStart, Stop, PreCompact-auto-only, UserPromptSubmit)
- 5 slash commands (recall, remember, memory-status, memory-export, memory-import)
- MCP stdio server with 2 tools (recall_project_memory, remember_fact) + throttling
- CLI helper (mcp/cli.py) for slash commands to shell out to
- Skill (skills/memory-recovery/SKILL.md) telling Claude when to recall
- Install script with smoke test, README, DEPLOYMENT.md
- 164 tests across 9 files: store, chunker, search, budget, catalog, recall, paths, hooks, cli, mcp_server, integration, real_docx
- End-to-end verified on real ~/Downloads/self_improving_ai_fields.docx (69 chunks, 16ms recall)

### Bugs found+fixed by tests during build
- Windows cp1252 stdout crash on `→` char → wrote `lib/stdio_utf8.py`, wired into all entry points
- Chunker: long sentences mid-stream not hard-wrapped → emitted oversize chunks → fixed `_split_prose`
- UserPromptSubmit regex: only matched "left off", not "leave off" / "leaving off" → broadened
- Store API mismatch (count without filters, list_sessions without limit) → extended Store
- Budget API mismatch (callers wanted `tokens=` and `estimated_tokens=` kwargs) → extended Budget
- BOM-encoded settings.json on Windows → installer now tries utf-8-sig fallback

### What's NOT in v4 (deferred)
- Rust port (v5) — gates on adoption signal
- Marketplace listing — manual install only for now
- /memory-prune subcommand — exporters can do this manually
- Cross-machine sync — exporters/importers are the manual path

---

## 1. Project location and current commit state

### Local path

`C:\Users\suraj\Downloads\kos-memory-v4\`

### GitHub repo

`https://github.com/skvcool-rgb/KOS-MemoryV4` — dedicated v4 repo (v3 lives separately at `KOS-Memory`).

### Files already created (full inventory with line counts)

| Path | Lines | Purpose |
|---|---|---|
| `.claude-plugin/plugin.json` | 28 | Plugin manifest. Declares 4 hooks (SessionStart, Stop, PreCompact `auto`-only, UserPromptSubmit) and 1 MCP server (`kos-memory`). |
| `lib/__init__.py` | 3 | Package marker. Exposes `__version__ = "4.0.0"` and `SCHEMA_VERSION = 1`. |
| `lib/paths.py` | 51 | Single source of truth for filesystem paths. Project memory at `<project>/.kos-memory/`. User memory at `%APPDATA%/kos-memory/user/` (Windows) or `$XDG_CONFIG_HOME/kos-memory/user/` (POSIX). Path is **distinct** from any v3 location to avoid collision. |
| `lib/store.py` | 252 | SQLite-backed chunk + session store. WAL mode, schema v1, INSERT OR IGNORE for idempotent re-ingest, `PRAGMA user_version` migration gate. `chunks` table has the two booleans `asserted_by_user` and `contradicted_by_later_session` (NOT a float confidence). `sessions` table holds `summary` and `tags` JSON for catalog generation. |
| `lib/chunker.py` | 150 | Pure-stdlib chunker. Prose: sentence-aware with overlap. Code: fence-preserving (every chunk gets opening + closing ` ``` `, language hint preserved across continuations). `extract_file_refs` regex pulls plausible file paths. **Carry-over v3 bugs intentionally fixed** — see docstring. |
| `lib/search.py` | 196 | Hybrid search layer. Inline BM25 Okapi (epsilon-floored for tiny corpora), `SynonymCache` (LRU-bounded at 500 entries, persistable to `synonyms.json`, conservative seed dict for the 10 most common dev terms), `grep_passages` (returns matched line + ±N context lines, dedupes overlapping windows). |
| `lib/budget.py` | 86 | Daily and per-session caps. `Budget.can_recall()` returns `(allowed, reason)`. Defaults: 50K tokens/day (~$0.50), 50 recalls/day, 5 recalls/session, 1 recall/turn. Atomic JSON write via `os.replace(tmp, path)`. UTC date rollover. |

**Total built so far: 766 lines across 7 files.**

### Empty-but-scaffolded directories

These exist but contain no files yet:

- `commands/` — slash command definitions live here as `*.md`
- `hooks/` — Python hook scripts go here
- `mcp/` — MCP server source goes here
- `scripts/` — installer + utility scripts
- `skills/memory-recovery/` — skill that teaches Claude when to invoke the MCP recall tool
- `tests/` — unit + integration + production-grade tests

---

## 2. The v4 design — copy in full

### Five principles (locked, do not change)

1. **Backup-not-primary.** Claude's native context is always the primary source of truth. kos-memory is a recovery layer for after compaction or across sessions. Never injected silently into every turn.
2. **Trigger discipline.** Memory is loaded only when explicitly triggered. The 4 trigger types (next subsection) are exhaustive. There is no always-on injection.
3. **Catalog-first.** Recall starts by reading a tiny catalog (~1K tokens regardless of corpus size) so Claude can decide *which* deeper artifacts are worth fetching. Avoids the "dump 30 chunks into context" anti-pattern.
4. **Synthesis-not-transcripts.** What gets injected is a Claude-synthesized answer over the retrieved chunks, not the raw chunks themselves. Cost discipline + relevance.
5. **Local-intelligence-unnecessary.** Claude is the ranker, the synonym-expander, the tagger, and the synthesizer. We do not ship a local cross-encoder, a local embedding model, or any ML weights. Pure stdlib + Claude API.

### Four triggers

- **Trigger A — Explicit.** User-initiated. Slash commands (`/recall <q>`, `/remember <fact>`) or NL phrases the UserPromptSubmit hook detects ("what did we decide about X", "remember that Y", "remind me about Z").
- **Trigger B — Heuristic.** Three sub-heuristics:
  - B1: Compaction just occurred (detected by the post-compact session-shape change). Load a tiny "you were just compacted" hint into context so Claude can self-invoke recall.
  - B2: User mentions a file/symbol/topic Claude has no in-context evidence of having seen. UserPromptSubmit hook flags it.
  - B3: User asks a question that pattern-matches "follow-up to a prior decision" (e.g. "did we ever finish...", "what happened with...").
- **Trigger C — PreCompact auto.** Fires only on `matcher: "auto"` in plugin.json (NEVER on `manual`). User-initiated `/compact` is intentional and should not trigger memory backup; auto-compaction is the only case where we *steal* a few hundred ms to capture critical state before it's lost.
- **Trigger D — Claude self-invocation via MCP.** When Claude reads the `memory-recovery` skill and decides on its own that recall is warranted, it calls the MCP tool `recall_project_memory`. Throttled (1/turn, 5/session, 50/day, $0.50/day cap) to stop runaway loops.

### 4-stage recall pipeline

This is the core flow. All four stages run for every recall; cost is dominated by Stages 0 and 3.

| Stage | What | Cost | Latency |
|---|---|---|---|
| **Stage 0 — Query expansion** | Query goes to Haiku via `claude` API. Returns synonyms, related terms, file-name hints. Cached in `synonyms.json` per query hash so repeat queries are free. | ~$0.001 cached, ~$0.005 first time | 200-400 ms |
| **Stage 1 — Catalog scan** | Read `catalog.json` (~1K tokens regardless of corpus size) into Claude. Claude picks the top ~30 candidate chunk IDs to inspect deeper. | ~1K tokens to Claude | <50 ms file read |
| **Stage 2 — Targeted grep** | Pure-Python `grep_passages` over the candidate chunks' text. Zero LLM cost. Returns matched line + context. | $0 | ~50 ms |
| **Stage 3 — Synthesis** | The 30-ish passages + the original query go to Claude (Haiku or Sonnet depending on caller). Returns a 1-3 paragraph synthesis with citations to chunk IDs. | ~1.5K tokens to Claude | 500-1500 ms |

**Total typical recall cost: ~$0.002-0.008 + 1-2 seconds.** The catalog cap keeps Stage 1 constant regardless of corpus growth.

### Storage format

**Per-project:** `<project>/.kos-memory/`

| File | Purpose |
|---|---|
| `chunks.db` | SQLite. Schema v1 — see `lib/store.py`. WAL mode. |
| `catalog.json` | Hierarchical metadata index — time-windowed (recent / mid / archive) and tag-clustered. Capped at ~1K tokens by aggressive summarization at the tag-cluster level. |
| `synonyms.json` | LRU cache (500 entries) of query -> expanded-terms. Populated by Stage 0. |
| `last_ingest_marker` | Plain text file. Unix timestamp of last successful Stop-hook ingest. Lets the next ingest pick up only new transcript content. |
| `budget.json` | Daily token/recall accounting. Owned by `lib/budget.py`. |
| `ingest_log.jsonl` | Append-only log of every ingest run. Useful for debugging. |

**User-level (cross-project):** `%APPDATA%/kos-memory/user/` on Windows or `$XDG_CONFIG_HOME/kos-memory/user/` on POSIX.

This path is **deliberately distinct** from any v3 location to avoid the collision that burned the user previously. Same six file types; cross-project memory only stores facts the user has explicitly elevated via `/remember --user`.

### Two booleans on chunks (not a float)

- `asserted_by_user` (0/1) — set when a chunk originated from explicit `/remember` or a `<user>:remember-this` annotation. Higher trust at ranking time.
- `contradicted_by_later_session` (0/1) — set lazily by the recall pipeline when Stage 3 detects "this chunk says X, a newer chunk says not-X". Never set at ingest. Never a fuzzy float — it's a binary marker that down-weights but does not delete.

Rationale: floats invite over-tuning and pretend-precision. Two booleans capture the only information that actually matters at recall time.

### Lazy contradiction detection

Contradictions are detected at recall time, not ingest time. Why:

- Ingest is fast and cheap; we do not want to spend LLM tokens looking for contradictions that may never be queried.
- The recall pipeline already has Claude in the loop at Stage 3 — adding "and flag any contradictions you noticed" to that prompt is essentially free.
- Cold contradictions remain dormant until they matter.

When Stage 3 sees a contradiction, it returns the chunk IDs to mark, and `Store.mark_contradicted(ids)` flips the boolean. Future recalls down-rank those chunks but still surface them when context warrants.

---

## 3. What's already built (full file inventory)

Already covered in section 1's table. Summary:

```
kos-memory-v4/
├── .claude-plugin/
│   └── plugin.json              28 lines  — manifest, hooks, MCP server reg
├── lib/
│   ├── __init__.py               3 lines  — version + SCHEMA_VERSION
│   ├── paths.py                 51 lines  — project + user path resolution
│   ├── store.py                252 lines  — SQLite store, schema v1, sessions
│   ├── chunker.py              150 lines  — prose+code chunker
│   ├── search.py               196 lines  — BM25 + synonyms + grep
│   └── budget.py                86 lines  — daily/session caps
├── commands/                    (empty)
├── hooks/                       (empty)
├── mcp/                         (empty)
├── scripts/                     (empty)
├── skills/
│   └── memory-recovery/         (empty)
└── tests/                       (empty)

7 files, 766 lines committed locally, 0 pushed to GitHub.
```

---

## 4. What remains to build (in priority order)

> Build in this order. Each item below has spec-level detail. Do NOT redesign.

### 4.1 `lib/catalog.py` (PRIORITY 1)

Hierarchical metadata index. Time-windowed and tag-clustered. **Capped at ~1K tokens** regardless of corpus size by summarization at the cluster level.

**Time windows:**
- `recent` — last 7 days. Per-session entries, full session summary visible.
- `mid` — 8-30 days. Per-session entries, summary truncated to ~120 chars.
- `archive` — older than 30 days. Aggregated by week + tag cluster only; no per-session detail.

**Tag clustering:**
Tags come from the per-session `summary` + `tags` fields written by the Stop hook (LLM-generated via Haiku). Cluster sessions by Jaccard similarity over tag sets; merge clusters until total catalog tokens fits in budget.

**Public API:**
```python
class Catalog:
    def __init__(self, store: Store, catalog_path: Path): ...
    def rebuild(self) -> dict: ...           # full rebuild from store
    def update_for_session(self, sid: str): ...  # incremental, called by Stop hook
    def render(self, max_tokens: int = 1000) -> str: ...  # text for Stage 1
    def candidate_chunk_ids(self, top_n: int = 30) -> list[str]: ...
```

**Storage:** `catalog.json` written atomically (`os.replace`).

### 4.2 `lib/recall.py` (PRIORITY 2)

Orchestrator for the 4-stage pipeline. Calls `search.py` for Stage 2 and the `claude` CLI (subprocess) or `anthropic` SDK for Stages 0 and 3. **Decision:** subprocess to `claude` CLI is preferred — keeps the "no external pip installs" rule. If the CLI isn't on PATH, fall back to `anthropic` SDK only if it's already installed; otherwise return a degraded BM25-only result with a clear warning.

**Public API:**
```python
def recall(
    query: str,
    *,
    project_root: str | Path,
    session_id: str | None = None,
    max_tokens: int = 1500,
    budget: Budget | None = None,
) -> RecallResult:
    """4-stage pipeline. Honors budget. Returns synthesis + citations."""
```

`RecallResult` dataclass: `synthesis: str`, `citations: list[ChunkCitation]`, `cost_tokens: int`, `degraded: bool`, `degraded_reason: str | None`.

Throttling check happens **before** Stage 0. If denied, return a `RecallResult` with `degraded=True` and `degraded_reason="budget cap reached"`.

### 4.3 `hooks/SessionStart.py` (PRIORITY 3)

Print backup-availability marker (≤50 tokens). SQLite read only. **No LLM calls.**

Output format (single line, will be appended to Claude's first-turn context):
```
[kos-memory] backup ready · N chunks · last ingest M minutes ago · /recall to query
```

Reads `chunks.db` count + `last_ingest_marker` mtime. If neither file exists, prints nothing (silent on first run).

**Timeout: 3 seconds.** Must not block session start.

### 4.4 `hooks/Stop.py` (PRIORITY 4)

Ingest the just-ended session transcript. Called when Claude Code session ends.

Steps:
1. Read transcript from `$CLAUDE_TRANSCRIPT_PATH` env var (Claude Code provides).
2. Slice everything after the timestamp in `last_ingest_marker` (idempotent re-ingest).
3. `chunker.chunk_text` -> `Store.add_chunks_bulk`.
4. Generate session summary + tags via Haiku (1 API call, ~200 tokens out). Cache via subprocess to `claude` CLI.
5. `Catalog.update_for_session(sid)` — incremental catalog update.
6. Update `last_ingest_marker` to now.
7. Append a line to `ingest_log.jsonl`.

**Timeout: 15 seconds.** If LLM call times out, still write the chunks (degraded ingest — summary will be backfilled on next Stop).

### 4.5 `hooks/PreCompact.py` (PRIORITY 5)

Fires **before auto-compaction only** (matcher already locked to `"auto"` in plugin.json). Captures critical state that's about to be summarized away.

Steps:
1. Read recent transcript tail (last ~5K tokens worth).
2. Quick chunker pass — no LLM call (PreCompact is latency-sensitive).
3. `Store.add_chunks_bulk` with `asserted_by_user=False`.
4. Print a single-line marker into Claude's surviving context: `[kos-memory] N chunks captured pre-compact; /recall to query`.

**Timeout: 8 seconds.** No API calls. Pure local work.

### 4.6 `hooks/UserPromptSubmit.py` (PRIORITY 6)

Detect natural-language recall triggers. Pure regex; no LLM call.

Patterns to detect (case-insensitive):
- `^(what did|when did|where did|how did) we (decide|do|build|fix|implement)`
- `\bremind me\b`
- `\bremember (that|when)\b`
- `\bwe (talked about|discussed|agreed)\b`
- `\bdid we (ever|finish|complete)\b`

If matched, prepend a single line to the prompt:
```
[kos-memory hint] This looks like a recall query. Consider invoking the recall_project_memory MCP tool.
```

**Timeout: 2 seconds.** Must not delay user input.

### 4.7 `commands/*.md`

Slash command definitions. Each is a small Markdown file with a YAML frontmatter `name:` and a body that the Claude Code harness uses as a prompt.

Required commands:
- `recall.md` — `/recall <query>` — runs full 4-stage recall, injects synthesis.
- `remember.md` — `/remember <fact>` — stores a chunk with `asserted_by_user=1`. Optional `--user` flag elevates to user-level memory.
- `memory-status.md` — `/memory-status` — prints chunk count, ingest log tail, today's budget usage.
- `memory-export.md` — `/memory-export <path>` — dumps everything to JSON. Includes `schema_version` field.
- `memory-import.md` — `/memory-import <path>` — restores from a `/memory-export` file. Validates `schema_version`.

### 4.8 `mcp/server.py`

MCP server exposing one tool: `recall_project_memory`.

**Tool description must be deliberate** — this is one of the six locked tightenings (section 5). The description tells Claude exactly:
- **When to call:** "When the user references a fact, decision, file, or symbol you have no in-context evidence of, AND the user appears to expect you to know it."
- **What it returns:** "A 1-3 paragraph synthesis from the project's memory store, with chunk citations. Returns 'no relevant memory' if nothing matches."
- **Cost:** "Approximately $0.002-0.008 and 1-2 seconds. Throttled to 1 call per turn, 5 per session, 50 per day."

Throttling enforced before any Stage 0 work begins. Use `lib/budget.py`.

Implementation: stdio MCP server using the JSON-RPC 2.0 envelope. Pure stdlib (no `mcp` package needed) — read stdin line-by-line, write JSON to stdout.

### 4.9 `skills/memory-recovery/SKILL.md`

A Claude skill that teaches when to use the MCP tool. Claude reads the skill on session start; the skill tells it the heuristics for self-invocation (Trigger D).

Content outline:
- When to invoke `recall_project_memory`
- When NOT to invoke it (e.g. user is just asking a generic factual question, not project-specific)
- How to interpret the synthesis + citations
- How to handle a "no relevant memory" return

### 4.10 `tests/*`

Required test files (each must be runnable with `python -m unittest`):

- `test_paths.py` — path resolution on Windows + POSIX (use `unittest.mock.patch` for `os.environ`)
- `test_store.py` — schema migration, WAL mode, INSERT OR IGNORE, mark_contradicted, session upsert idempotency
- `test_chunker.py` — code fence preservation across splits, file ref extraction, empty input
- `test_search.py` — BM25 epsilon floor on tiny corpus, synonym LRU eviction, grep dedup
- `test_budget.py` — UTC date rollover, all three caps, per-session counter
- `test_catalog.py` — token cap respected, time window assignment, incremental update
- `test_recall.py` — 4-stage pipeline (mock the LLM calls), degraded-mode behavior, throttling-rejection path
- `test_integration_docx.py` — ingest `C:\Users\suraj\Downloads\self_improving_ai_fields.docx` (85-page real doc), run 10 representative recalls, assert R@10 ≥ 0.80 (matching v3's BM25-only baseline as floor)
- `test_production_crash_recovery.py` — kill mid-write to `chunks.db`, verify WAL recovery
- `test_production_schema_migration.py` — open a v0 DB, verify it migrates to v1 cleanly; open a v2 DB, verify it raises clearly
- `test_production_throttling.py` — assert 1/turn, 5/session, 50/day, $0.50/day are all enforced

### 4.11 `scripts/install.py`

Registers the plugin in `~/.claude/settings.json`. Steps:
1. Resolve install dir (default `~/.claude/plugins/kos-memory/`).
2. Copy this directory there (or symlink for dev mode with `--dev`).
3. Read `~/.claude/settings.json`, merge in plugin reference, atomic write back.
4. Verify `python` is on PATH (else print clear error).
5. Print "installed; restart Claude Code to activate".

### 4.12 `README.md`

User-facing docs. Sections: what it is, why backup-not-primary, install, slash commands, troubleshooting, FAQ ("does this replace native memory?" — no), uninstall.

### 4.13 `DEPLOYMENT.md`

Deploy + rollback guide. Sections: pre-flight checklist, install steps, smoke test, rollback to v3 OSS, common failure modes, where logs live.

---

## 5. Six tightenings already locked into the design

These were debated and decided in prior sessions. Do NOT relitigate.

1. **Hook only `auto` PreCompact (not manual).** Already encoded in `.claude-plugin/plugin.json`:
   ```json
   "PreCompact": [{"matcher": "auto", ...}]
   ```
   User-invoked `/compact` is intentional; auto-compact is where state gets surprise-lost.

2. **Lazy contradiction detection at recall, not ingest.** The `contradicted_by_later_session` boolean is set by Stage 3 of the recall pipeline. Ingest never spends LLM tokens looking for contradictions.

3. **MCP tool description deliberately written.** The `recall_project_memory` tool description must spell out: when to call, what it returns, cost. See section 4.8 for the mandated phrasing.

4. **Throttling on Trigger D.** Max 1/turn, 5/session, 50/day, $0.50/day budget cap. Already encoded as defaults in `lib/budget.py`. The MCP server must check `Budget.can_recall(...)` before any Stage 0 work.

5. **`schema_version` field in `/memory-export` JSON.** The export command must emit `{"schema_version": 1, "chunks": [...], "sessions": [...]}` so future migrations have a versioning anchor.

6. **User-level memory at `~/.config/kos-memory/user/` (NOT `~/.kos-memory/user/`).** Already encoded in `lib/paths.py`. Distinct from any v3 location to avoid collision. Windows uses `%APPDATA%/kos-memory/user/`.

---

## 6. Key context from prior sessions

### Why v4 exists (the v3 burn)

User got burned by v3's daemon architecture. The daemon process died silently across all four projects the user tried it on. Hooks broke when the daemon was unavailable. **No data was ever saved.** v4 is therefore designed with **zero daemon, zero ML, pure stdlib only** as a hard constraint.

### Open decision point — Rust port (v5, not v4)

User asked "can we build it in Rust." Quick analysis for the next session:

**Rust pros:**
- Even more dependency-free (single statically linked binary; no Python install required on the user's machine).
- Faster cold-start for hooks (no Python interpreter spinup).
- Memory-safe by construction.
- Aligns with the user's other Rust work (BoundaryAI daemon-rs, sdk-load-monitor — see user's MEMORY.md).

**Rust cons:**
- Loses the Claude Code plugin-marketplace ecosystem (most plugins are Python/JS today).
- Compile-per-platform burden (Windows / macOS / Linux x3 each at minimum).
- Slower iteration during the build phase (Cargo compile vs Python edit-and-run).
- MCP server in Rust is doable but the JSON-RPC stdio path is simpler in Python.

**Recommendation:** Ship v4 in Python. Park Rust as v5 — re-evaluate after v4 has 3-4 weeks of dogfood data. Frame this clearly to the user when asked. Do NOT pivot v4 to Rust mid-build.

### Time pressure

User is time-pressed and asked for max-efficiency parallel build. Use parallel sub-agents where independent (e.g. write `recall.py` + `catalog.py` + `mcp/server.py` in parallel, since they have no compile-time coupling). Tests can run in parallel too.

### Benchmark target — the 85-page docx

The 85-page docx test on v3's BM25-only mode showed:
- **R@10 = 0.80** on real (non-adversarial) queries
- **R@1 = 0.20** on adversarial paraphrase queries

v4's bet: routing through Claude as the semantic ranker (Stages 0 + 3) does better than a 22M-parameter cross-encoder would. The integration test (`tests/test_integration_docx.py`) must reproduce this benchmark and report R@10 + R@1 numbers. **Floor: must not regress R@10 below 0.80.** Stretch: R@1 should improve to 0.50+.

Test corpus path: `C:\Users\suraj\Downloads\self_improving_ai_fields.docx`

---

## 7. Sprint plan to resume from

Currently in **Sprint 0/1** — skeleton + first 4 lib files done.

| Sprint | Scope | Est. time |
|---|---|---|
| **0** ✓ | Repo scaffold, `plugin.json`, `paths.py`, `__init__.py` | done |
| **1** ✓ | `store.py`, `chunker.py`, `search.py`, `budget.py` | done |
| **2** | `catalog.py` + unit tests | 60-90 min |
| **3** | `recall.py` + unit tests (with mocked LLM) | 90-120 min |
| **4** | All 4 hooks + `commands/*.md` | 60 min |
| **5** | `mcp/server.py` + `skills/memory-recovery/SKILL.md` | 60-90 min |
| **6** | Integration test on the 85-page docx + production-grade tests | 60-90 min |
| **7** | `scripts/install.py`, `README.md`, `DEPLOYMENT.md` | 30-45 min |

**Estimated remaining: 5-7 hours of focused work to ship v4 production-ready.**

When all sprints are green, push to GitHub on a `v4-rewrite` branch (NOT main), tag `v4.0.0-rc1`, then dogfood for 1-2 weeks before tagging `v4.0.0`.

---

## 8. Quick-reference

**Project path:** `C:\Users\suraj\Downloads\kos-memory-v4\`
**GitHub:** `https://github.com/skvcool-rgb/KOS-MemoryV4`
**Test corpus:** `C:\Users\suraj\Downloads\self_improving_ai_fields.docx`
**User memory dir (Windows):** `%APPDATA%/kos-memory/user/`
**Plugin install dir (eventual):** `~/.claude/plugins/kos-memory/`
**Schema version:** 1
**Plugin version:** 4.0.0
**Python version target:** 3.10+ (uses `from __future__ import annotations` + PEP 604 unions; tested against the system Python the user has on Windows)

**Total lines so far:** 766 across 7 files
**Lines projected for full v4:** ~3500-4500 (rough estimate — depends on test depth)

**Hard rules (do NOT break):**
- Pure Python stdlib. No `pip install` of anything.
- Zero daemon. All work is hook-driven or MCP-call-driven.
- No ML weights shipped. Claude is the model.
- All file writes are atomic (`os.replace(tmp, final)`).
- All SQLite work is WAL + idempotent.
- All LLM calls go through `lib/budget.py` first.

End of handover.
