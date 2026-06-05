---
description: Sync kos-memory across machines via sidecar git repo (push|pull|init|status)
argument-hint: "<push|pull|init|status> [--remote URL] [--message MSG] [--user]"
allowed-tools: ["Bash"]
---

# /memory-sync — sidecar-git multi-machine sync

`chunks.db` is SQLite — git can't merge it cleanly. So kos-memory keeps
a sidecar git repo at `<kos-dir>/sync/` that tracks a deterministic JSON
snapshot of the store. Conflict resolution happens at the chunk-id
level when the snapshot is re-imported on another machine.

## How it works

```
machine A             remote git              machine B
──────────            ──────────              ──────────
chunks.db   ─export──> snapshot.json ──pull──> chunks.db
                        (sidecar branch:
                         kos-memory-sync)
```

- Chunks are immutable once written (dedup by id, INSERT OR IGNORE).
- Sessions upsert preferring most-recent `ended_at`.
- The sidecar lives on its own branch, never the user's main branch.
- `--force` is never used.

## Subcommands

### `init`

```bash
python -m mcp.cli sync init [--remote URL] [--user]
```

Creates `<kos-dir>/sync/` as a git repo on the `kos-memory-sync`
branch, writes a `.gitignore` that tracks only `snapshot.json`, and
optionally adds a remote.

### `push`

```bash
python -m mcp.cli sync push [--message MSG] [--user]
```

1. Exports current `chunks.db` to `<kos-dir>/sync/snapshot.json`
   (deterministic JSON: sorted keys, sorted chunks).
2. `git add snapshot.json`
3. `git commit -m "<message or default>"`
4. `git push origin kos-memory-sync` (skipped if no remote).

### `pull`

```bash
python -m mcp.cli sync pull [--user]
```

1. `git pull --ff-only origin kos-memory-sync` (skipped if no remote).
2. Re-imports the snapshot into the local store with the conflict
   rules above.

### `status`

```bash
python -m mcp.cli sync status [--user]
```

Reports whether the sync repo exists, current branch, remote URL, and
last snapshot commit sha.

## Steps

### 1. Parse arguments

The user invoked: `/memory-sync $ARGUMENTS`

- First positional argument: `push`, `pull`, `init`, or `status`.
- `--remote URL` → only meaningful for `init`.
- `--message MSG` → only meaningful for `push`.
- `--user` → operate on user-level memory store (vs project).

### 2. Run the CLI

```bash
python -m mcp.cli sync <subcommand> [flags]
```

Returns JSON describing the outcome.

### 3. Confirm to the user

For `push`:
```
Sync push:
  Snapshot: <N> chunks, <M> sessions, <bytes>
  Commit:   <sha or "no changes">
  Pushed:   <yes|no remote configured>
```

For `pull`:
```
Sync pull:
  Pulled: <yes|no remote configured>
  Merged: <chunks_imported> new chunks, <chunks_skipped> deduped,
          <sessions_upserted> sessions
```

For `init`:
```
Sync repo: <kos-dir>/sync (branch: kos-memory-sync)
Remote:    <url or "none">
```

## Constraints

- The sync repo lives at `<kos-dir>/sync/` only — never the project root.
- Git invocations always use `-c http.proxy="" -c https.proxy=""` to
  bypass the deployment env's proxy vars.
- Push/pull have a 60s timeout; local ops have a 5s timeout.
- Never `git push --force`.
- Never modify the user's main project git history.
