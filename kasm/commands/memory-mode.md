---
description: Inspect or change kos-memory mode (primary | backup)
argument-hint: "[primary|backup] [--user]"
allowed-tools: ["Bash"]
---

# /memory-mode — toggle primary vs backup mode

kos-memory v4.1+ runs in one of two modes:

- **primary** (default): SessionStart auto-emits the Stage-1 catalog +
  MEMORY.md anchor skeleton + drift warnings; UserPromptSubmit on trigger
  phrases auto-runs Stage 0+1+2 and emits top passages inline.
- **backup**: 1-line marker on SessionStart, 1-line hint on
  UserPromptSubmit. Claude must explicitly invoke `/recall` or
  `recall_project_memory` to see content.

## Steps

### 1. Parse arguments

The user invoked: `/memory-mode $ARGUMENTS`

- No args → show current mode for project + user scope.
- `primary` or `backup` → set the mode.
- `--user` → write to user-level config instead of per-project.

### 2. Run the CLI

```bash
python -m mcp.cli memory_mode [--mode primary|backup] [--user]
```

Returns JSON: `{"ok": true, "mode": "primary", "scope": "project",
"config_path": "..."}`.

### 3. Confirm to the user

```
✓ kos-memory mode for <project|user> = <primary|backup>
  Config: <path>
  Effect: <what changes — see table above>
```

If just inspecting (no mode arg), show both project and user modes side
by side.
