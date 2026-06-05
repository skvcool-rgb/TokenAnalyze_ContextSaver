---
description: Export project memory to portable JSON — for backup, migration, or sharing
argument-hint: "[--out=path.json] [--user] [--since=YYYY-MM-DD] [--include-contradicted]"
allowed-tools: ["Bash"]
---

# /memory-export — Export memory to JSON

Dumps the kos-memory store to a portable JSON file. Use for backups, machine migration, or sharing project memory with a teammate.

## What this command does

Reads `chunks.db` and writes a JSON document with:
- `version`: schema version
- `exported_at`: timestamp
- `scope`: project path or "user"
- `chunks`: list of all chunks (or filtered subset)
- `sessions`: list of all sessions

Default behavior **excludes** chunks marked `contradicted_by_later_session=true` (use `--include-contradicted` to keep them).

## Steps

### 1. Parse arguments

- `--out=path.json` → write to this path. Default: `./kos-memory-export-<YYYYMMDD>.json` in cwd.
- `--user` → scope is user store
- `--since=YYYY-MM-DD` → only chunks with `ts >= that date`
- `--include-contradicted` → keep superseded chunks (default: filter them out)

### 2. Run the export

```bash
python -m mcp.cli export --out "<path>" [--user] [--since "<date>"] [--include-contradicted]
```

Returns JSON: `{"ok": true, "path": "...", "chunks_written": N, "sessions_written": M, "bytes": ...}`.

### 3. Confirm to the user

```
✓ Exported <N> chunks, <M> sessions to: <path>
  Size: <human bytes>
  Filter: <since date or "all"> | <"contradicted included" or "contradicted excluded">

Re-import with: /memory-import <path>
```

### 4. Safety reminder

If the export contains user-asserted facts or file paths, mention:
> "This export may contain file paths and user-pinned notes. Treat it like a code artifact — don't share publicly without review."

## Format spec (portable across kos-memory installs)

```json
{
  "version": 1,
  "exported_at": "2026-05-02T14:30:00Z",
  "scope": "C:/Users/me/myproj",
  "chunks": [
    {
      "id": "abc123...",
      "session_id": "<sid>",
      "project": "<path>",
      "ts": 1746201600,
      "text": "...",
      "kind": "prose|code|user_assertion",
      "language": "python|null",
      "file_refs": ["src/foo.py"],
      "asserted_by_user": false,
      "contradicted_by_later_session": false
    }
  ],
  "sessions": [
    {
      "id": "<sid>",
      "started_at": 1746198000,
      "ended_at": 1746201600,
      "project": "<path>",
      "summary": "...",
      "tags": ["refactor", "auth"],
      "chunk_count": 46
    }
  ]
}
```

## Constraints

- Read-only on the source store.
- Output file is overwritten if it exists — confirm with the user if the path already exists and is non-empty.
- Atomic write: write to `<path>.tmp` then `os.replace`. No partial files on crash.
- For large stores (>100 MB), warn the user before writing.
