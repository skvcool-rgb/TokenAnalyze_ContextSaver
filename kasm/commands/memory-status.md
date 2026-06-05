---
description: Show kos-memory store stats — chunks, sessions, last ingest, budget, contradictions
argument-hint: "[--user] [--verbose]"
allowed-tools: ["Bash"]
---

# /memory-status — Inspect the store

Shows a one-screen summary of the kos-memory state for this project (or user-level scope).

## What this command does

Pure read-only. Reports:
- Total chunks, sessions, projects covered
- Last ingest time
- Last 5 sessions (id prefix, date, chunk count)
- Budget state today (recall calls used, tokens, USD)
- Contradicted-chunk count
- Catalog freshness (last build time)
- DB file size on disk

## Steps

### 1. Parse arguments

- `--user` → scope is `~/.config/kos-memory/user/`
- `--verbose` → also list per-tag chunk counts and top file refs

### 2. Run the status query

```bash
python -m mcp.cli status [--user] [--verbose]
```

Returns JSON. Render it as a compact table:

```
=========================================================
kos-memory status — <project_path>
=========================================================
Chunks:      1,247  (user-asserted: 38, contradicted: 4)
Sessions:    52     (last 5 below)
Projects:    3 covered
Last ingest: today (2 hours ago)
DB size:     8.3 MB
Catalog:     fresh (rebuilt 2 min ago)

Today's budget:
  Recall calls: 2 / 50
  Tokens used:  4,120 / 50,000
  USD spent:    $0.018 / $0.500

Recent sessions:
  [a3f2b1c0] 2026-05-02  46 chunks  python-api-fix
  [9e1d8c44] 2026-05-01  82 chunks  refactor-auth-mod
  [771ab2e9] 2026-04-30  31 chunks  initial-spec
  ...
=========================================================
```

If `--verbose` is set, also include:

```
Top tags:
  refactor (212 chunks)
  bugfix (143 chunks)
  ...

Top file refs:
  src/auth.py (87 mentions)
  src/api/routes.py (52 mentions)
  ...
```

### 3. Health hints

If anything looks wrong, end with a hint:

- If `chunks=0`: "Store is empty. Memory accrues automatically when sessions end. Use `/remember <fact>` to seed."
- If `last_ingest > 30 days ago`: "Memory is stale. Either the project hasn't had Claude Code activity, or the Stop hook isn't firing — check `.kos-memory/ingest_log.jsonl`."
- If `budget exceeded`: "Today's recall budget is spent. Resets at midnight UTC, or use `/memory-status --user` to query the cross-project store which has its own budget."
- If `contradicted_chunks > 100`: "High contradiction count. Consider archiving or pruning old chunks via `/memory-export`."

## Constraints

- Read-only, no mutations.
- Sub-second latency target — don't run any LLM call.
- If the DB is locked (very rare, only during active ingest), retry once after 200ms then surface the error gracefully.
