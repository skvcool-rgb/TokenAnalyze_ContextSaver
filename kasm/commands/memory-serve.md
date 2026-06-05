---
description: Start a local-only HTTP API for kos-memory (recall/remember/state) accessible to Aider, Continue.dev, and shell scripts
argument-hint: "[--port 7621] [--token X]"
allowed-tools: ["Bash"]
---

# /memory-serve — local HTTP API

Starts the kos-memory HTTP server, bound to `127.0.0.1` only. Other tools
on the same machine (Aider, Continue.dev, shell pipelines, custom
integrations) can call recall/remember/state without speaking MCP.

## What this command does

Spawns `python -m mcp.http_server` in the foreground (or backgrounded by
the operator's shell). Default port `7621`, default no-auth (loopback-only
is the primary defense). Use `--token <T>` for an extra layer.

## Steps

### 1. Parse arguments

- `--port N` — bind port (default 7621)
- `--token T` — require `Authorization: Bearer T` on every request except `/healthz`
- If no `--token` is given, suggest generating one with `secrets.token_urlsafe(32)`.

### 2. Run the server

```bash
python -m mcp.http_server --port 7621 [--token <T>]
```

Background it with `&` (POSIX) or `Start-Process` (PowerShell) if you want
the shell back. Stop with Ctrl-C or `kill <pid>`.

### 3. Quick test

```bash
curl http://127.0.0.1:7621/healthz
# -> {"ok": true, "data": {...}, "error": null}

curl -H "Authorization: Bearer <T>" http://127.0.0.1:7621/v1/status

curl -H "Authorization: Bearer <T>" \
     -H "Content-Type: application/json" \
     -d '{"query":"auth"}' \
     http://127.0.0.1:7621/v1/recall
```

## Endpoints

```
GET  /healthz                              200 OK, no auth
GET  /v1/status                            store stats
GET  /v1/state                             live state + reconciliation
POST /v1/recall    {query, window_days}    Stage 0+1+2
POST /v1/remember  {fact, tags}            pin chunk
POST /v1/bootstrap                         (defensive)
POST /v1/sync/push                         (defensive)
POST /v1/sync/pull                         (defensive)
```

All responses share the envelope `{"ok": bool, "data": ..., "error": ...}`.

## Security

- Bound to **127.0.0.1 only** — never 0.0.0.0, never a public IP.
- Token auth is optional but **strongly recommended** if any other process
  on the box runs untrusted code (browsers, IDE plugins).
- Host header check rejects DNS rebinding attempts.

## Constraints

- Pure stdlib `http.server` — no third-party web framework.
- Single process, threaded handler. Fine for local dev; not a production
  service.
- Daily/per-session budget caps still apply on `/v1/recall`.
