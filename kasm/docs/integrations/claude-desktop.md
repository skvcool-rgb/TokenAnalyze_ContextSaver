# Claude Desktop

Add kos-memory to `claude_desktop_config.json` (path varies by OS):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "kos-memory": {
      "command": "C:/Path/To/python.exe",
      "args": [
        "-m",
        "mcp.standalone_server"
      ],
      "cwd": "C:/Path/To/kos-memory-v4",
      "env": {
        "KOS_MEMORY_PROJECT": "C:/Path/To/your/active/project"
      }
    }
  }
}
```

Replace `C:/Path/To/python.exe` with the absolute path to a Python 3.9+
interpreter, and `C:/Path/To/kos-memory-v4` with the cloned repo. Set
`KOS_MEMORY_PROJECT` to whichever project you want recall/remember to
operate against (or omit it to use the cwd, which on Claude Desktop
is the cloned repo itself).

Restart Claude Desktop. The tools `recall_project_memory`,
`remember_fact`, `get_project_state`, `bootstrap_project`, `sync_push`,
`sync_pull`, `curate_memory` should appear in the tool picker.

## Easy mode

Run `python scripts/install.py` from the repo. It writes the user-level
Claude Code config; Claude Desktop config is currently a manual
copy-paste — see this file for the snippet.
