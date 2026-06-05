---
description: Import a kos-memory JSON export into this project's store
argument-hint: "<path-to-export.json> [--user] [--merge|--replace]"
allowed-tools: ["Bash", "Read"]
---

# /memory-import — Import an export

Loads a kos-memory JSON export (produced by `/memory-export`) into this project's store, or the user-level store with `--user`.

## What this command does

Reads the JSON file and `INSERT OR IGNORE`s every chunk into the destination `chunks.db`. Sessions are upserted. Default mode is **merge** — existing chunks are kept, new ones added by content hash. `--replace` wipes the destination table first (destructive — confirm with user).

## Steps

### 1. Parse arguments

- First positional arg = path to the export JSON
- `--user` → import into user store (default: project store)
- `--merge` (default) → add to existing
- `--replace` → DROP and recreate destination tables, then import

If no path given, ask: "Which export file? Provide the path to a `.json` file produced by `/memory-export`."

### 2. Validate the file

Read the file (use `Read` tool to peek at first ~100 lines). Verify:
- Top-level keys: `version`, `exported_at`, `scope`, `chunks`, `sessions`
- `version` matches the current schema (currently 1)
- Chunks list is well-formed (sample 3 entries)

If validation fails, abort and tell the user what's wrong.

### 3. Pre-flight check

Show the user:
```
Import preview:
  Source:    <path>
  Exported:  <date>
  Scope:     <project|user>
  Chunks:    <N>
  Sessions:  <M>
  Mode:      <merge|REPLACE (destructive)>
  Target:    <destination kos-dir>

Proceed? (y/N)
```

For `--replace`, require explicit `y` confirmation in chat. For `--merge`, you can proceed if the count is <10,000; ask if larger.

### 4. Run the import

```bash
python -m mcp.cli import_export --path "<path>" [--user] [--replace]
```

Returns JSON: `{"ok": true, "chunks_imported": N, "chunks_skipped": M, "sessions_upserted": K}`.

### 5. Confirm

```
✓ Imported <N> chunks (<M> already present, skipped)
  <K> sessions upserted
  Catalog will rebuild on next /recall

Verify with: /memory-status
```

### 6. Trigger catalog rebuild

```bash
python -m mcp.cli rebuild_catalog [--user]
```

Best-effort — if it fails, the next `/recall` will rebuild lazily.

## Constraints

- Validate JSON before opening any DB connection. Bad JSON should fail fast and not touch the store.
- `--replace` is destructive — require confirmation in chat (not just CLI flag).
- Atomic: import inside a single SQLite transaction. On any error, roll back.
- Don't import chunks marked `contradicted_by_later_session=true` unless the export explicitly included them (they remain in the JSON for audit trail but the importer skips them by default — surface this to the user).
- After import, never call `/recall` automatically — let the user invoke it.
