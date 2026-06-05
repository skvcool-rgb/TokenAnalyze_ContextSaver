# Generic MCP

For any MCP-compatible client not covered by Claude Desktop / Cursor /
Cline / Zed: invoke `mcp.standalone_server` over stdio. The server
implements MCP protocol version `2024-11-05`.

## Spawn the server

The host launches:

```
<absolute-path-to-python> -m mcp.standalone_server
```

with `cwd = <absolute-path-to-kos-memory-v4>` and the project pinned
via env var:

```
KOS_MEMORY_PROJECT=<absolute-path-to-the-project-this-instance-serves>
```

(Or fall back to `CLAUDE_PROJECT_DIR`, or to the cwd.)

## Wire format

Standard MCP JSON-RPC 2.0 over stdin/stdout, line-framed. Each request is
one line of JSON; each response is one line of JSON. Notifications
(no `id`) get no response.

### Sample handshake

```json
> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
< {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"kos-memory-standalone","version":"6.0.0"}}}

> {"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
(no response)

> {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
< {"jsonrpc":"2.0","id":2,"result":{"tools":[...]}}

> {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"recall_project_memory","arguments":{"query":"auth"}}}
< {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"..."}]}}
```

## Tools exposed

| Name | Purpose |
|---|---|
| `recall_project_memory` | Stage 0+1+2 recall over the active project |
| `remember_fact` | Pin user-asserted chunk |
| `get_project_state` | Live filesystem + git survey + reconciliation |
| `bootstrap_project` | (defensive — needs lib.bootstrap) |
| `sync_push` / `sync_pull` | (defensive — needs lib.sync) |
| `curate_memory` | (defensive — needs lib.auto_suggestions) |

Defensive tools return a 503-style error envelope (`isError: true`,
text starts with `503:`) when the backing module is not yet merged.

## Easy mode

`python scripts/install.py` registers Claude Code. For other MCP hosts,
use the snippet above directly in their config.
