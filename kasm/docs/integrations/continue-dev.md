# Continue.dev

Continue.dev (VS Code / JetBrains) supports custom context providers
via its config file (`~/.continue/config.json` or
`~/.continue/config.yaml`). kos-memory plugs in via the HTTP server.

## Step 1: Start the HTTP server

```bash
python -m mcp.http_server --token "$KOS_MEMORY_TOKEN"
```

Set `KOS_MEMORY_TOKEN` to a generated value (e.g. `secrets.token_urlsafe(32)`)
and stash it in your shell rc file.

## Step 2: Add a custom context provider

In `~/.continue/config.json`:

```json
{
  "contextProviders": [
    {
      "name": "kos-memory",
      "description": "Recall past project decisions",
      "type": "http",
      "params": {
        "url": "http://127.0.0.1:7621/v1/recall",
        "method": "POST",
        "headers": {
          "Authorization": "Bearer YOUR_TOKEN_HERE",
          "Content-Type": "application/json"
        },
        "bodyTemplate": "{\"query\": \"{{ query }}\", \"window_days\": 30}",
        "responseJsonPath": "$.data.catalog_text"
      }
    }
  ]
}
```

Replace `YOUR_TOKEN_HERE` with the `--token` value used when starting
the HTTP server.

## Step 3: Use it in chat

In Continue, type `@kos-memory <query>`. Continue calls
`/v1/recall` and inlines the catalog as context for the next prompt.

## Pinning a fact from Continue

Continue's slash commands can be wired similarly:

```json
{
  "slashCommands": [
    {
      "name": "remember",
      "description": "Pin a fact to kos-memory",
      "type": "http",
      "params": {
        "url": "http://127.0.0.1:7621/v1/remember",
        "method": "POST",
        "headers": {
          "Authorization": "Bearer YOUR_TOKEN_HERE",
          "Content-Type": "application/json"
        },
        "bodyTemplate": "{\"fact\": \"{{ args }}\"}"
      }
    }
  ]
}
```

## Easy mode

`python scripts/install.py` handles the Claude Code side. The Continue
config is one copy-paste — keep this file alongside as a reference.
