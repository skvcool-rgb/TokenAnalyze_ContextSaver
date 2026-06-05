---
description: Seed kos-memory from existing project docs + Claude Code transcripts (one-shot, idempotent)
argument-hint: "[--max-transcripts=10]"
allowed-tools: ["Bash"]
---

# /memory-bootstrap — Seed first-session memory

The user is starting kos-memory on a project that already has on-disk
context — top-level docs (README, CHANGELOG, ARCHITECTURE, DEPLOYMENT,
HANDOVER, ROADMAP) and/or prior Claude Code transcripts in
`~/.claude/projects/<encoded>/*.jsonl`.

This command does a **one-shot ingest** so the next `/recall` and the
SessionStart reconstruction block are not empty.

## What this command does

1. Discovers up to 6 top-level project docs (capped at 200 KB each).
2. Discovers up to 10 most-recent transcripts (capped at 60 KB each).
3. Chunks every source (400 chars, 50 overlap — same as Stop hook).
4. Drops chunks that look like literal credentials (api_key / password /
   sk- / ghp_) when they're under 200 chars.
5. Bulk-inserts with content-hash chunk_ids — so re-running adds 0
   chunks. Safe to invoke multiple times.

Bootstrap chunks are tagged `kind="bootstrap_doc"` or `kind="bootstrap_transcript"`
and grouped under a single session `bootstrap_<unix_ts>`.

## Steps

### 1. Parse arguments

The user invoked: `/memory-bootstrap $ARGUMENTS`

- `--max-transcripts=N` overrides the default cap of 10. Pass `0` to
  ingest only docs.
- No other arguments are accepted.

### 2. Run the bootstrap

```bash
python -m mcp.cli bootstrap [--max-transcripts <N>]
```

This returns JSON:

```json
{
  "ok": true,
  "kos_dir": "<path>",
  "docs_ingested": 4,
  "transcripts_ingested": 8,
  "chunks_added": 312,
  "chunks_skipped": 6,
  "errors": []
}
```

### 3. Confirm to the user

Reply with:

```
✓ Bootstrap complete
  docs ingested:        <docs_ingested>
  transcripts ingested: <transcripts_ingested>
  chunks added:         <chunks_added>   (skipped <chunks_skipped> as duplicates / secrets)
  store:                <kos_dir>
```

Then suggest: "Try `/recall <topic>` to verify the seed worked, or
`/memory-status` to see the new counts."

## Constraints

- This is a **one-shot** seed — running it twice is a no-op (idempotent
  via content-hash chunk ids), but you should not invoke it on every
  session start. The empty-store nudge in SessionStart already prompts
  for it once.
- If `chunks_added == 0` AND `docs_ingested == 0` AND `transcripts_ingested == 0`,
  the project genuinely has nothing to bootstrap from — tell the user:
  "No top-level docs and no prior transcripts found. Use `/remember <fact>`
  to start the store manually."
- Never run with `--user` scope: bootstrap is project-scoped by design.
- If `errors[]` is non-empty, surface the FIRST error verbatim — usually
  it's a permission issue or a corrupt JSONL line.
