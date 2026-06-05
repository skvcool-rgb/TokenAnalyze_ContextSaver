# Cline (VS Code)

Cline (formerly Claude Dev) reads MCP server config from its settings
panel: **Cline icon → MCP Servers → Edit MCP settings**, or directly
from `~/.config/cline/cline_mcp_settings.json` (Linux/macOS) /
`%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` (Windows).

```json
{
  "mcpServers": {
    "kos-memory": {
      "command": "C:/Path/To/python.exe",
      "args": ["-m", "mcp.standalone_server"],
      "cwd": "C:/Path/To/kos-memory-v4",
      "env": {
        "KOS_MEMORY_PROJECT": "${workspaceFolder}"
      },
      "disabled": false,
      "alwaysAllow": ["recall_project_memory", "get_project_state"]
    }
  }
}
```

`alwaysAllow` is Cline-specific — read-only tools like
`recall_project_memory` and `get_project_state` are safe to add there
so Cline doesn't prompt every call. Leave write-side tools
(`remember_fact`, `bootstrap_project`, `sync_push`) out so the user
sees them.

After saving, click the refresh icon next to "kos-memory" in the
MCP Servers panel.

## Easy mode

Run `python scripts/install.py` for Claude Code; copy the JSON above
into the Cline settings file for VS Code.
