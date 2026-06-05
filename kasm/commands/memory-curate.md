---
description: Mine high-value chunks and append/refresh a marker-fenced suggestions block in MEMORY.md
argument-hint: "[--preview] [--write] [--target PATH]"
allowed-tools: ["Bash", "Read"]
---

# /memory-curate — Auto-suggest MEMORY.md updates (operator-reviewed)

`MEMORY.md` is **operator-curated truth** — kos-memory does **not** rewrite
it. This command instead mines the chunks store for high-value
candidates (user-asserted facts, decision phrases, version bumps)
and writes them into a clearly-marked block:

```
<!-- KOS-AUTO-START v1 -->
## Auto-extracted suggestions (operator review)
... candidates ...
<!-- KOS-AUTO-END -->
```

Only the fenced region is ever touched. Anything you wrote above or
below the markers is preserved byte-for-byte. The next `/memory-curate`
run replaces only what's between markers.

## What this command does

1. Iterates the project's `chunks.db`, scoring each chunk:
   - 3x for user-asserted (`/remember` chunks)
   - 2x for decision-pattern phrases (`we chose X`, `decided to Y`,
     `switched from A to B`, ...)
   - 1.5x for version-bump tokens (`v1.0`, `v0.7.27`, ...)
2. Filters chunks aged < 7 days (still in flux) and > 180 days
   (archive territory) and any chunk already marked superseded.
3. Sorts by score desc, ts desc; takes top 20.
4. Renders a marker-fenced markdown block.
5. Either prints it (`--preview`) or atomically appends/replaces in
   the resolved MEMORY.md (`--write`).

## Steps

### 1. Parse arguments

The user invoked: `/memory-curate $ARGUMENTS`

- `--preview` (default if `--write` is absent) — print the block and the
  resolved target path, do **not** write.
- `--write` — perform the atomic write. Without `--target` this writes
  to the highest-priority MEMORY.md returned by `lib.memory_md.find_memory_files`
  for the current project. If none exist, write to `<project>/MEMORY.md`.
- `--target PATH` — explicit override (use when the user has multiple
  memory files and wants to curate a specific one).

### 2. Run the CLI

```bash
python -m mcp.cli curate [--preview] [--write] [--target /abs/path/MEMORY.md]
```

Returns JSON. Shape:

```json
{
  "ok": true,
  "mode": "preview" | "write",
  "target": "/abs/path/MEMORY.md",
  "suggestion_count": 12,
  "block": "<!-- KOS-AUTO-START v1 -->\n...",
  "report": {
    "path": "...",
    "was_appended": true | false,
    "was_replaced": true | false,
    "bytes_written": 1234,
    "suggestion_count": 12,
    "errors": []
  }
}
```

(`report` is null in `--preview` mode.)

### 3. Confirm to the user

**Preview mode:**

```
Preview of auto-suggestions for: <target>

(block)
---
<full block here>
---

Run /memory-curate --write to install. The block lands between the
KOS-AUTO-START / KOS-AUTO-END markers. Content outside markers is
never modified.
```

**Write mode:**

```
✓ MEMORY.md updated: <target>
  Mode: <appended new section | replaced existing block>
  Suggestions: <N>
  Bytes written: <bytes>

Operator review the block at the markers; promote, edit, or delete
entries as you see fit. Anything outside markers was untouched.
```

If `errors` is non-empty, surface them verbatim — likely permissions
or path issues.

## Constraints

- **Never edits content outside the markers.** This is the safety
  contract of this feature; the underlying `append_to_memory_md`
  refuses to write a block that doesn't contain both markers.
- One run = one write. Do not re-invoke automatically — let the
  operator decide when to refresh.
- Empty store / no candidates still produces a well-formed (empty)
  block so the operator can see the markers exist.
- Atomic: writes through `<path>.tmp` + `os.replace`. No partial
  files on crash.
