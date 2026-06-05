# kos-memory v4 — Deployment Guide

This document is for operators installing kos-memory v4 in a Claude Code environment.

## Prerequisites

- **Python 3.9+** on `PATH` (check: `python --version`)
- **Claude Code** with hook + plugin + MCP server support (any recent version)
- Write access to `~/.claude/settings.json` and the project's working directory
- ~10 MB disk for plugin code; per-project store starts at ~50 KB and grows ~30 KB per session

No other dependencies. No package manager required.

## Install paths

### Path A — automated (recommended)

```bash
cd /path/to/kos-memory-v4
python scripts/install.py
```

The installer will:
1. Verify Python ≥ 3.9
2. Verify all 19 required plugin files exist
3. Backup existing `~/.claude/settings.json` (timestamp suffix)
4. Merge `kos-memory` into `enabledPlugins` and `mcpServers` blocks
5. Run a smoke test (creates a temp store, ingests + recalls a chunk)

**Dry run first if uncertain:**
```bash
python scripts/install.py --dry-run
```

### Path B — manual settings edit

Edit `~/.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "kos-memory": {
      "path": "/absolute/path/to/kos-memory-v4",
      "version": "4.0.0"
    }
  },
  "mcpServers": {
    "kos-memory": {
      "command": "python",
      "args": ["/absolute/path/to/kos-memory-v4/mcp/server.py"]
    }
  }
}
```

Then restart Claude Code.

### Path C — Claude Code plugin marketplace

Once published to a marketplace, install via:
```
> /plugin install kos-memory
```
(Format depends on Claude Code's plugin marketplace conventions.)

## Verify install

After restart, in any project:

```
> /memory-status
```

Expected output (empty store):
```
kos-memory status — <project_path>
Chunks: 0
Sessions: 0
Last ingest: never
DB size: 0 bytes
Catalog: not built
```

Then start a normal Claude Code session. After it ends (Stop hook fires), check again — chunks should be > 0.

## Verify hooks fired

Each project gets `<project>/.kos-memory/`. After a session:

```
.kos-memory/
├── chunks.db          # SQLite, contains session chunks
├── ingest_log.jsonl   # WAL — at least one line per session
├── last_ingest_marker # plain Unix ts of last write
└── (optionally) catalog.json, synonyms.json, budget.json
```

If `chunks.db` exists but is empty, check:
- `ingest_log.jsonl` has lines (hook fired but DB write failed)
- `~/.claude/projects/<encoded>/<session>.jsonl` exists (transcript readable)
- `python /path/to/kos-memory-v4/hooks/Stop.py < /dev/null` exits cleanly

If `ingest_log.jsonl` is missing entirely, the hook is not registered. Check `~/.claude/settings.json`.

## Operational notes

### Disk growth

- Per session: ~10–50 chunks at ~400 chars each = ~5–20 KB after dedup.
- Per project: 1 year heavy use ~ 50 MB.
- Per user-level store: typically smaller; only `/remember --user` writes here.
- Compaction: SQLite WAL is auto-checkpointed; no manual maintenance.

### Pruning old data

```bash
# Keep last 90 days only
python -m mcp.cli export --since 2026-02-01 --out backup.json
# Then manually rebuild from backup if you need a wipe + restore
```

A dedicated prune subcommand is on the v4.1 roadmap.

### Backup

```bash
# Per-project
python -m mcp.cli export --out ~/backups/kos-mem-$(date +%F).json

# User-level
python -m mcp.cli export --user --out ~/backups/kos-mem-user-$(date +%F).json
```

Schedule via cron / Task Scheduler for periodic backups.

### Restore

```bash
python -m mcp.cli import_export --path ~/backups/kos-mem-2026-05-02.json
```

Or interactively in chat: `/memory-import ~/backups/kos-mem-2026-05-02.json`.

### Disabling

Per-project, temporarily:
```bash
mv .kos-memory .kos-memory.disabled
```

Hooks still fire but produce no output (DB doesn't exist → SessionStart/Stop early-exit).

System-wide:
```json
// ~/.claude/settings.json — remove or comment-out:
"enabledPlugins": {
  "kos-memory": {...}  // <-- delete this entry
}
```

### Throttling overrides

Default daily caps (in `lib/budget.py`):
- 50 recall calls
- 50,000 tokens
- $0.50 spend

To raise, edit `lib/budget.py`:
```python
DEFAULT_DAILY_CALL_CAP = 100      # was 50
DEFAULT_DAILY_TOKEN_CAP = 100_000 # was 50_000
DEFAULT_DAILY_USD_CAP = 1.00      # was 0.50
```

Restart Claude Code after editing.

### Logging / debugging

Hooks print to stderr on failure (visible in Claude Code's hook output panel). For deeper diagnostics:

```bash
# Manually replay Stop hook with a test transcript
CLAUDE_TRANSCRIPT_PATH=/path/to/test.jsonl \
CLAUDE_PROJECT_DIR=$(pwd) \
CLAUDE_SESSION_ID=test123 \
python hooks/Stop.py < /dev/null
```

Inspect the WAL log to see what was attempted:
```bash
tail -20 .kos-memory/ingest_log.jsonl
```

### Schema upgrades

Schema version is tracked in SQLite via `PRAGMA user_version`. Current = 1. The `Store` class refuses to open a DB with a version mismatch; on upgrade, future versions will provide a migration script under `scripts/migrate_v<n>.py`.

To force-rebuild from scratch: delete `.kos-memory/chunks.db`, restart Claude Code, the next Stop hook will recreate it.

## Security

- **Local only.** No network calls in any module under `lib/`, `hooks/`, or `mcp/`.
- **No daemon.** Zero processes between sessions. MCP server is stdio-only and only runs when Claude Code spawns it.
- **Secrets guard.** `/remember` and `remember_fact` reject text matching API-key/password/token patterns under 200 chars. Long secrets aren't currently filtered — operator should review what's pinned.
- **File permissions.** SQLite DB inherits umask. On multi-user systems, restrict via `chmod 600 .kos-memory/chunks.db`.
- **Backup files.** Export JSONs may contain sensitive snippets. Treat them as code artifacts; don't share publicly.

## Uninstall

```bash
# 1. Remove from settings
# Edit ~/.claude/settings.json — delete `enabledPlugins.kos-memory` and `mcpServers.kos-memory`.

# 2. (Optional) Remove all per-project data
find . -type d -name ".kos-memory" -exec rm -rf {} +

# 3. (Optional) Remove user-level store
rm -rf ~/.config/kos-memory  # or %APPDATA%\kos-memory on Windows

# 4. (Optional) Remove plugin code
rm -rf /path/to/kos-memory-v4
```

Backup files (`settings.json.bak-*`) are preserved by the installer and may need manual cleanup.

## Support

- Repo: https://github.com/skvcool-rgb/KOS-MemoryV4
- Architecture rationale: see `README.md` and the v3 → v4 migration section.
- Build-time decisions: see `HANDOVER.md`.
