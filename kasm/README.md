# kos-memory v6

Local-first memory + reality-sync for any LLM coding tool. Auto-injects MEMORY.md, git state, and session history at session start. Bootstraps from existing project docs and Claude Code transcripts. Multi-machine sync, marker-fenced MEMORY.md auto-curation, opt-in test-runner integration. HTTP + MCP + CLI surfaces for Claude Code, Claude Desktop, Cursor, Cline, Zed, Aider, Continue.dev, and shell scripts. Pure-stdlib, zero dependencies.

[![Tests](https://img.shields.io/badge/tests-399%2F399-brightgreen)](#testing)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](#install-60-seconds)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](#license)
[![Dependencies](https://img.shields.io/badge/deps-zero-brightgreen)](#install-60-seconds)
[![Mode](https://img.shields.io/badge/default-primary-blueviolet)](#modes)

## What v6 closes

v5 was Claude-Code-only with bounded retrieval and no project bootstrap. v6 fills six gaps:

| Gap | v6 closes it via |
|---|---|
| Cold start (empty store on fresh install) | `/memory-bootstrap` seeds from README/CHANGELOG/etc. + replays existing `~/.claude/projects/<encoded>/*.jsonl` transcripts |
| MEMORY.md is operator-only / never auto-updates | `/memory-curate` writes high-value chunks (user-asserted, decision-pattern matches, version bumps) into a marker-fenced section operator reviews |
| Tree summary too shallow | `tree_depth` + `tree_max_entries` config keys in `.kos-memory/config.json` |
| No live test signal | `lib.test_runner` auto-runs collect-only on every survey (safe); full run opt-in via `KOS_MEMORY_RUN_TESTS=1` |
| Single-machine, no sync | `/memory-sync push|pull` via sidecar git repo + JSON snapshot, conflict resolution at chunk-id level |
| Claude-Code-only | Standalone MCP stdio server + HTTP API on 127.0.0.1 + universal CLI; integration recipes for 7 tools |

## What you see at session start (Claude Code)

```
[kos-memory PRIMARY] Memory reconstruction (1,247 chunks, 52 sessions, last ingest: today)

## MEMORY.md anchors (operator-curated truth)
### project_memory_md (MEMORY.md, 14m ago, 256892 bytes)
  # 🚨 RESUME-HERE POINTER
  # 🌐 LANGUAGE MIGRATION ROADMAP
  ...

## Live project state (filesystem + git, surveyed now)
  branch:        main (clean)
  head:          abc1234 "v6.0.0 — close all gaps"
  vs upstream:   origin/main (0 ahead, 0 behind)
  tags:          v6.0.0, v5.0.0, v4.1.0, ...
  last commits:
    abc1234  v6.0.0 — close all gaps
    bbcaa92  feat(v6 baseline): configurable tree depth/breadth
    2e3c6a2  docs(README): polish pass
  versions:
    .claude-plugin/plugin.json: 6.0.0
    lib/__init__.py: 6.0.0
  tree:          .claude-plugin/, lib/ (16 .py), hooks/ (4 .py),
                 mcp/ (5 .py), commands/ (10 .md), tests/ (18 .py)
  test runner:   unittest, collect=374

## Recent session catalog (auto-extracted)
- 2026-05-04 · abc1234 [v6, sync, bootstrap, http] → ...
- (more)

## Build-status reconciliation (chunks vs filesystem)
  ✓ confirmed (chunks + filesystem agree): lib/store.py, hooks/Stop.py, ...

## Drift
- (no drift; MEMORY.md aligns with chunks)

Authority order for any claim about project state:
  1. Live project state (filesystem + git) — ground truth
  2. MEMORY.md anchors — operator-curated truth
  3. User-asserted chunks (/remember) — explicit pins
  4. Auto-extracted chunks — high-recall, may be stale
```

When you ask "is X built", the UserPromptSubmit hook auto-runs:

```
[reality check] 'X' is BUILT — confirmed by 12 chunks, filesystem evidence,
and git (commit abc123 "X release").
  evidence: chunks=claimed_built, filesystem=confirms, git=committed (confidence: high)
```

## Install (60 seconds)

**Requirements:** Python 3.9+ and Claude Code (or any MCP-compatible / HTTP-capable tool). Nothing else.

```bash
git clone https://github.com/skvcool-rgb/KOS-MemoryV4.git
cd KOS-MemoryV4
python scripts/install.py            # use python3 on Mac/Linux if python isn't on PATH
```

Restart Claude Code. In any project, type `/memory-status` to verify, or `/memory-bootstrap` if the project already has a README/CHANGELOG/prior CC transcripts you want to seed memory with in one shot.

The installer:
1. Detects which Python interpreter you used and bakes its absolute path into the plugin manifest.
2. Registers the plugin and MCP server in `~/.claude/settings.json` (atomic write, with backup).
3. Runs a smoke test (creates a temp `.kos-memory/`, ingests a chunk, recalls it).
4. Prints integration nudges for non-Claude-Code tools (see `docs/integrations/`).

Idempotent. Re-run any time.

## Cross-tool integrations

Same per-project `.kos-memory/chunks.db` — every tool that knows where to look reads from and writes to it. Integration configs in `docs/integrations/`:

| Tool | Surface | Config |
|---|---|---|
| **Claude Code** | hooks + MCP (auto via plugin) | `~/.claude/settings.json` (installer handles) |
| **Claude Desktop** | MCP stdio | `docs/integrations/claude-desktop.md` |
| **Cursor** | MCP stdio | `docs/integrations/cursor.md` |
| **Cline (VS Code)** | MCP stdio | `docs/integrations/cline.md` |
| **Zed AI** | MCP stdio | `docs/integrations/zed.md` |
| **Aider** | HTTP at 127.0.0.1:7621 | `docs/integrations/aider.md` |
| **Continue.dev** | HTTP at 127.0.0.1:7621 | `docs/integrations/continue-dev.md` |
| **Shell scripts** | `python -m mcp.standalone_cli ...` | run from any cwd |
| **Generic MCP client** | MCP stdio | `docs/integrations/generic-mcp.md` |

To start the HTTP server (for non-MCP tools like Aider):

```bash
TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python -m mcp.http_server --port 7621 --token "$TOKEN"
```

Bind is loopback-only (127.0.0.1), token-protected, host-header guarded against DNS-rebinding.

## What it does (full flow)

**Capture** (silent, automatic):
- `Stop` hook ingests transcripts at session end.
- `PreCompact` hook (auto-only) saves state before auto-compaction.

**Inject at session start** (primary mode default):
- `SessionStart` hook emits the reconstruction block above. Bounded ~16 KB.
- If the store is empty AND `find_bootstrap_sources()` returns sources, prints a one-line nudge to run `/memory-bootstrap`.

**Inject on user prompt** (primary mode):
- Past-tense triggers ("where did we leave off") → auto-runs Stage 0+1+2 of recall, emits passages inline.
- Present-tense build-status triggers ("is X built", "what's the status of Y", "did we ship Z") → auto-runs reality-sync verdict.

**Manual surfaces** (on demand):
- `/memory-bootstrap` — one-shot seed from project docs + CC transcripts.
- `/memory-curate [--write]` — refresh marker-fenced auto-suggestions in MEMORY.md.
- `/memory-sync push|pull|init|status` — sidecar-git multi-machine sync.
- `/memory-mode primary|backup` — toggle modes.
- `/memory-serve [--port 7621] [--token X]` — start HTTP API for cross-tool access.
- `/recall <query>` — full 4-stage pipeline.
- `/remember <fact>` — pin a user-asserted chunk.
- `/memory-status`, `/memory-export`, `/memory-import` — admin operations.
- `recall_project_memory`, `remember_fact` MCP tools — Claude self-invokes when needed (5/session, 50/day, $0.50/day caps).

**Storage** (local SQLite, zero processes between sessions):
- `.kos-memory/chunks.db` per project.
- `~/.config/kos-memory/user/` for cross-project pins (opt-in).
- `.kos-memory/sync/` sidecar git repo when sync is configured.

## Configuration

Config lives at `<project>/.kos-memory/config.json`. All keys optional:

```json
{
  "mode": "primary",            // "primary" | "backup"
  "tree_depth": 1,              // tree walk depth (default 1; 2-3 useful for monorepos)
  "tree_max_entries": 60,       // total tree summary entries
  "max_commits": 20,            // last-commit log cap
  "max_tags": 10,               // tag list cap
  "git_timeout_s": 2.0,         // git subprocess timeout
  "cache_ttl_s": 60,            // survey cache TTL
  "run_tests": false,           // opt-in full test execution (collect-only is always on)
  "auto_curate_every_n_sessions": 0  // 0 = disabled (recommended); N>0 auto-curates after N sessions
}
```

Env var override (highest priority): `KOS_MEMORY_MODE=backup`, `KOS_MEMORY_RUN_TESTS=1`.

## MEMORY.md as truth-anchor

Search order — first match wins per kind:

1. `<project>/MEMORY.md`
2. `<project>/.claude/MEMORY.md`
3. `<project>/CLAUDE.md`
4. `~/.claude/projects/<encoded>/memory/MEMORY.md` (Claude Code auto-memory)
5. `~/.claude/CLAUDE.md` (user-global)

Drift detection: if MEMORY.md is 12+ hours older than the most recent chunk AND ≥5 chunks have been ingested since, you'll see a warning at session start.

Conflict resolution: live state > MEMORY.md > user-asserted chunks > auto-extracted chunks.

`/memory-curate` writes `<!-- KOS-AUTO-START v1 -->` ... `<!-- KOS-AUTO-END -->` markers. Content **outside** the markers is never touched (byte-for-byte preservation, tested explicitly).

## Modes

| Mode | SessionStart | UserPromptSubmit (trigger) | Default |
|---|---|---|---|
| **primary** | Catalog + MEMORY.md + Live state + Reconciliation + bootstrap nudge | Auto-runs Stage 0+1+2 OR build-status verdict inline | ✓ v5.0+ |
| **backup** | 1-line marker only | 1-line hint only | v4.0 legacy |

Switch:

```bash
/memory-mode backup        # for this project
/memory-mode primary       # back to default
KOS_MEMORY_MODE=backup     # env override
```

## Repository layout

```
kos-memory/
├── .claude-plugin/plugin.json   plugin manifest (hooks + MCP server)
├── lib/
│   ├── paths.py                 cross-platform paths + mode + config
│   ├── store.py                 SQLite schema + CRUD
│   ├── chunker.py               prose+code splitter
│   ├── search.py                BM25 + synonyms + grep
│   ├── budget.py                daily/session throttle
│   ├── catalog.py               recent/mid/archive view
│   ├── recall.py                4-stage pipeline orchestrator
│   ├── memory_md.py             MEMORY.md / CLAUDE.md detection + parsing
│   ├── codebase_survey.py       git + tree + version + test-cache survey
│   ├── reality_sync.py          chunks ↔ filesystem cross-reference
│   ├── stdio_utf8.py            Windows cp1252 fix
│   ├── bootstrap.py             v6: README/CHANGELOG/transcript seeding
│   ├── auto_suggestions.py      v6: marker-fenced MEMORY.md curation
│   ├── sync.py                  v6: sidecar-git JSON sync
│   └── test_runner.py           v6: collect-only + opt-in full-suite
├── hooks/
│   ├── SessionStart.py          memory reconstruction + bootstrap nudge
│   ├── Stop.py                  WAL-first ingest at end of session
│   ├── PreCompact.py            WAL-first ingest before auto-compact
│   └── UserPromptSubmit.py      regex triggers + auto-recall + auto-build-status
├── commands/
│   ├── recall.md                /recall
│   ├── remember.md              /remember
│   ├── memory-status.md         /memory-status
│   ├── memory-mode.md           /memory-mode
│   ├── memory-export.md         /memory-export
│   ├── memory-import.md         /memory-import
│   ├── memory-bootstrap.md      v6: /memory-bootstrap
│   ├── memory-curate.md         v6: /memory-curate
│   ├── memory-sync.md           v6: /memory-sync
│   └── memory-serve.md          v6: /memory-serve (HTTP API)
├── mcp/
│   ├── cli.py                   slash-command CLI (14 subcommands)
│   ├── server.py                JSON-RPC stdio MCP server (Claude Code)
│   ├── standalone_server.py     v6: MCP stdio for Claude Desktop/Cursor/etc.
│   ├── http_server.py           v6: 127.0.0.1 HTTP API for non-MCP tools
│   └── standalone_cli.py        v6: shell-friendly CLI proxy
├── skills/memory-recovery/SKILL.md     contract for Claude Code
├── docs/integrations/
│   ├── claude-desktop.md        v6
│   ├── cursor.md                v6
│   ├── cline.md                 v6
│   ├── zed.md                   v6
│   ├── aider.md                 v6
│   ├── continue-dev.md          v6
│   └── generic-mcp.md           v6
├── scripts/install.py           registers plugin + cross-tool nudge
├── tests/                       18 test files, 374 tests
├── README.md                    this file
├── DEPLOYMENT.md                operator install + ops guide
└── HANDOVER.md                  build-time notes
```

## Throttling

Auto-injection (SessionStart, UserPromptSubmit triggers) is NOT throttled. Explicit recall surfaces are:

| Surface | Per-session cap | Daily cap |
|---|---|---|
| `recall_project_memory` (MCP) | 5 | 50 calls / 50K tokens / $0.50 |
| `/recall` (slash) | none | 50 calls / 50K tokens / $0.50 |
| `remember_fact` (MCP) | none | none |
| `/remember`, `/memory-bootstrap`, `/memory-curate`, `/memory-sync` | none | none |
| HTTP `/v1/recall` | none | shares the daily token budget |

## Privacy

- All data stays local. No network calls from any hook or library code (except the operator-invoked `/memory-sync push/pull` which runs `git push/pull` to whatever remote the operator configured).
- HTTP server binds 127.0.0.1 ONLY, with Bearer token + Host-header check.
- The MCP server runs on stdio; no listening sockets.
- Secrets guard: `/remember`, `remember_fact`, AND `/memory-bootstrap` skip content matching API-key/password/token patterns under 200 chars.
- The codebase survey runs `git` subcommands as subprocesses (read-only) and `python -m unittest --collect-only` (no test execution).
- Full test run is OFF by default; opt-in via env var or config.
- To wipe a project's memory: delete `.kos-memory/`. To wipe user-level: delete `~/.config/kos-memory/user/`.

## Compared to other approaches

This is a deliberately narrow tool — local files + git + chunks, no embeddings, no daemon. Different tools serve different needs:

| Tool | Approach | How kos-memory differs |
|---|---|---|
| **Cursor / Windsurf built-in memory** | Vendor-managed, embedding-based | Local SQLite; per-project; portable JSON export; works across Claude Code, Claude Desktop, Cursor, Cline, Zed, Aider, Continue.dev simultaneously |
| **claude-mem** | Auto-summarization, chunked recall | Adds reality-sync (git + filesystem), bootstrap from existing transcripts, multi-machine sync, cross-tool surface |
| **mem0 / supermemory** | Embedding store + LLM extraction | Pure-stdlib BM25 — no embedding model, no inference cost, no external service |
| **OpenAI ChatGPT memory** | Server-side, opaque, single-app | Local-only, multi-project, multi-tool, you can read/edit the SQLite directly |
| **Manual MEMORY.md / CLAUDE.md** | You write it, Claude Code loads it | kos-memory still reads these files (treats them as authority); auto-extracts session history + git survey + suggested updates |

Pick kos-memory if you want: local-first, auditable, per-project isolation, no ML inference cost, explicit operator control via MEMORY.md, cross-tool memory standardization. Pick something else if you want: cross-app cloud memory, automatic semantic clustering, or vendor-managed convenience.

## Limitations

Honest list of things this tool does NOT do (v6 closed several v5 gaps):

- **No inference.** No embeddings, no LLM calls inside the plugin. Recall ranking is BM25 + grep.
- **Doesn't auto-update MEMORY.md curated content.** `/memory-curate` writes ONLY between markers; the rest is yours.
- **Doesn't run tests by default.** Collect-only is automatic and safe; full run is opt-in via env var or config (subprocess-isolated, timeout-bounded).
- **Sync is operator-invoked.** `/memory-sync push/pull` is a manual step. No auto-push on Stop hook (that would block session end on network).
- **HTTP server is loopback-only.** No remote-tool memory access without operator opening a tunnel themselves.
- **First-session bootstrap surfaces what's on disk.** If you have neither MEMORY.md nor CC transcripts nor a README, the preamble is empty until you start using kos-memory.
- **Token cost.** Primary mode adds ~500-1500 tokens to every session start. Switch to `backup` mode if it bothers you.

## Testing

```bash
python -m unittest discover tests    # 374 tests, ~4 minutes
```

Coverage spans: store schema migration, chunker boundary cases, BM25 epsilon-floor for tiny corpora, BOM-encoded settings.json, MEMORY.md detection across 5 search locations, drift thresholds, mode resolution, fresh-clone install end-to-end, real-document ingest+recall on a 19 KB .docx, codebase survey on real git repos, reality_sync reconciliation, build-status verdict generation, trigger ordering, SessionStart v5 output integration, **bootstrap from README/CHANGELOG/transcripts (idempotent + secrets-aware)**, **MEMORY.md auto-suggestions with byte-for-byte safety guarantee outside markers**, **sidecar-git sync round-trip across two simulated machines**, **test_runner detection across pytest/unittest/jest/vitest/mocha/cargo/go**, **HTTP server loopback-binding + token auth + host-header guard**, **standalone MCP server JSON-RPC handshake**.

## Migration

**From v5.x:**
- No schema changes. `git pull && python scripts/install.py`. Idempotent.
- New surfaces are additive — existing slash commands and MCP tools unchanged.

**From v4.x:**
- Same as above.
- v4.0 default behavior (1-line marker only) is preserved as `/memory-mode backup`.

**From v3 (the daemon-based release):**
1. Stop any v3 daemon.
2. v3 stored data in `~/.kos-memory/`. v6 uses `<project>/.kos-memory/` and `~/.config/kos-memory/user/` — separate paths.
3. If v3 has extractable data, use v3's CLI to export to JSON, then `/memory-import <path>` per-project.
4. v6 will not touch `~/.kos-memory/` — safe to delete.

## License

MIT.

## Repository

- Code: https://github.com/skvcool-rgb/KOS-MemoryV4
- Issues / discussions: same repo, Issues tab

**Suggested GitHub topic tags** (set on the repo's About panel):

```
claude-code  claude-plugin  mcp  mcp-server  memory  context-management
local-first  python  sqlite  bm25  developer-tools  ai-tools
cursor  cline  zed  aider  continue-dev  cross-tool
```
