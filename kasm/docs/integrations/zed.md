# Zed AI

Zed reads context-server config from `~/.config/zed/settings.json`
(Linux/macOS) or `%APPDATA%\Zed\settings.json` (Windows).

```json
{
  "context_servers": {
    "kos-memory": {
      "command": {
        "path": "C:/Path/To/python.exe",
        "args": ["-m", "mcp.standalone_server"],
        "env": {
          "KOS_MEMORY_PROJECT": "$ZED_WORKTREE_ROOT"
        }
      }
    }
  }
}
```

Zed's worktree root variable is `$ZED_WORKTREE_ROOT` — substituted at
spawn time so kos-memory always sees Zed's current working project.

The `cwd` is Zed's working directory by default; if Python's `-m`
resolution can't find the `mcp` package from that cwd, set it
explicitly:

```json
"command": {
  "path": "C:/Path/To/python.exe",
  "args": [
    "-m",
    "mcp.standalone_server"
  ],
  "env": {
    "PYTHONPATH": "C:/Path/To/kos-memory-v4"
  }
}
```

## Verifying

Open Zed's assistant panel → Context Servers list should show
`kos-memory`. Ask the assistant:

> Use `get_project_state` to show me what's tracked.

## Easy mode

`scripts/install.py` does not write Zed config; copy the snippet above
in by hand once.
