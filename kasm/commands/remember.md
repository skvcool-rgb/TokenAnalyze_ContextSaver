---
description: Pin a fact to long-term project memory — user-asserted, weighted higher than auto-extracted
argument-hint: "<fact text> [--user] [--tags=tag1,tag2]"
allowed-tools: ["Bash"]
---

# /remember — Pin a fact to memory

The user wants to deliberately commit a fact to project memory. This is **user-asserted** content — it gets `asserted_by_user=true` and is weighted higher than auto-extracted chunks during recall synthesis.

## What this command does

Inserts a single chunk into the project (or user-level) `.kos-memory/chunks.db` with:
- `asserted_by_user=true`
- `kind="user_assertion"`
- `session_id="user_pin_<timestamp>"`
- `ts=now`
- file_refs auto-extracted from the text

## Steps

### 1. Parse arguments

The user invoked: `/remember $ARGUMENTS`

- If `--user` is present, scope is `~/.config/kos-memory/user/` (cross-project).
- Otherwise scope is this project's `.kos-memory/`.
- `--tags=foo,bar` adds tags (used by catalog clustering later).
- The remaining text is the fact.

If the user gave no fact text, ask them once: "What would you like me to remember? (a single concise sentence works best — long facts get chunked)"

### 2. Sanity check the fact

- If it's < 20 chars, ask: "That's quite short. Should I record it as-is, or did you want to add detail?"
- If it's > 2000 chars, warn: "That's long — I'll chunk it into ~400-char pieces, all marked user-asserted."
- If the fact contains literal credentials (looks like an API key, password, or token), refuse: "This looks like a secret. I won't write secrets to memory. Rephrase without the credential."

### 3. Insert into the store

```bash
python -m mcp.cli remember --fact "<text>" [--user] [--tags "<csv>"]
```

This returns JSON: `{"ok": true, "chunk_ids": [...], "kos_dir": "..."}`.

### 4. Confirm to the user

Reply with:
```
✓ Pinned to <project|user> memory: "<first 80 chars of fact>..."
  chunk_id: <id>
  tags: <list or "none">
  Recall via: /recall <relevant query>
```

## Constraints

- One command = one logical fact. If the user pastes a list, ask whether to split into multiple `/remember` calls or store as one chunk.
- Never overwrite existing chunks. `INSERT OR IGNORE` deduplicates by content hash, but always-add-as-new is the policy.
- Don't mutate sessions table — `/remember` doesn't create a real session.
