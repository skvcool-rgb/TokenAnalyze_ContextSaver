# Cursor

Cursor speaks MCP via its `Settings → Features → MCP Servers` panel
(or by editing `~/.cursor/mcp.json` directly).

```json
{
  "mcpServers": {
    "kos-memory": {
      "command": "C:/Path/To/python.exe",
      "args": ["-m", "mcp.standalone_server"],
      "cwd": "C:/Path/To/kos-memory-v4",
      "env": {
        "KOS_MEMORY_PROJECT": "${workspaceFolder}"
      }
    }
  }
}
```

`${workspaceFolder}` is Cursor's variable for the currently-open
project. With it set, kos-memory recall/remember always operates on
the project Cursor has focus on.

Reload Cursor (Cmd/Ctrl+Shift+P → "MCP: Reload servers"). The kos-memory
tools should appear in the tool palette.

## Verifying

In Cursor's chat, ask:

> Use `recall_project_memory` to find anything about the auth refactor.

Cursor should call the tool and display the catalog + passages.

## Easy mode

Run `python scripts/install.py` once for Claude Code; for Cursor copy
the snippet above into `~/.cursor/mcp.json`.
