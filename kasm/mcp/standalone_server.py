"""kos-memory standalone MCP stdio server — cross-tool entry point.

Self-contained MCP JSON-RPC 2.0 stdio server designed to be invoked from
*any* MCP-compatible client (Claude Desktop, Cursor, Cline, Zed AI, generic
MCP runners). Wire-compatible with mcp/server.py but ALSO exposes additional
tools for codebase bootstrap, sync, project state, and curation.

## How tools resolve the project

In priority order:
    1. CLAUDE_PROJECT_DIR     (Claude Code convention; set by the harness)
    2. KOS_MEMORY_PROJECT     (this server's portable convention; let other
                               clients pin a project explicitly)
    3. os.getcwd()            (fallback for shells / generic MCP clients)

## Defensive imports

Tools whose backing library may not yet be merged (Agent A/B/C/D modules
under lib/bootstrap.py, lib/sync.py, lib/auto_suggestions.py) are gated
behind try/except ImportError. If the module is missing, the tool still
shows in tools/list, but tools/call returns a graceful 503-style error in
the MCP envelope rather than crashing the server.

## Differences vs mcp/server.py

- No per-session recall throttle (those exist on the other server too;
  this one assumes the client manages its own pace)
- Adds: bootstrap_project, sync_push, sync_pull, get_project_state,
  curate_memory
- Same protocol version (2024-11-05) and JSON-RPC framing — drop-in
  replacement at the wire level

Pure stdlib. Implements JSON-RPC 2.0 over stdin/stdout per MCP spec.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.stdio_utf8 import force_utf8_io
force_utf8_io()

from lib.budget import Budget
from lib.chunker import chunk_text, extract_file_refs
from lib.codebase_survey import render_live_state, survey_project
from lib.memory_md import find_memory_files, parse_memory_file
from lib.paths import FILE_BUDGET, FILE_CHUNKS_DB, ensure_kos_dir
from lib.reality_sync import reconcile, render_reconciliation
from lib.recall import (
    RecallContext,
    stage_0_local_expansion,
    stage_1_catalog,
    stage_2_grep,
)
from lib.store import Store

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "kos-memory-standalone"
SERVER_VERSION = "6.0.0"


def _resolve_project_dir() -> str:
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("KOS_MEMORY_PROJECT")
        or os.getcwd()
    )


def _resolve_kos_dir(user: bool) -> Path:
    return ensure_kos_dir(_resolve_project_dir(), user_level=bool(user))


def _err(text: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": text}]}


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


# ── Tool definitions ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "recall_project_memory",
        "description": (
            "Recall past context for the active project from the kos-memory store. "
            "Runs Stage 0+1+2 of the recall pipeline and returns catalog + passages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "window_days": {"type": "integer", "default": 30},
                "user": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember_fact",
        "description": "Pin a user-asserted fact to the long-term memory store.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "user": {"type": "boolean", "default": False},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "get_project_state",
        "description": (
            "Return live filesystem + git survey + reconciliation for the active "
            "project as structured JSON. No LLM calls."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bootstrap_project",
        "description": (
            "Bootstrap kos-memory for a project: scan code, ingest initial chunks, "
            "build catalog. Calls lib.bootstrap if available."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sync_push",
        "description": "Push local store to a remote sync target. Calls lib.sync if available.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sync_pull",
        "description": "Pull remote store into local. Calls lib.sync if available.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "curate_memory",
        "description": (
            "Suggest memory curations (consolidate, prune, contradict). Calls "
            "lib.auto_suggestions if available."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ── Tool handlers ──────────────────────────────────────────────────────

def _run_recall(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return _err("empty query")
    user = bool(args.get("user", False))
    window_days = int(args.get("window_days", 30))
    kos_dir = _resolve_kos_dir(user)

    budget = Budget(kos_dir / FILE_BUDGET)
    allowed, reason = budget.can_recall(estimated_tokens=4000)
    if not allowed:
        return _err(f"recall throttled: {reason}")

    rc = RecallContext(
        query=query, window_days=window_days,
        project_root=_resolve_project_dir(), user_level=user,
    )
    stage_0_local_expansion(rc, kos_dir)
    stage_1_catalog(rc, kos_dir)
    stage_2_grep(rc, kos_dir)

    est_tokens = sum(len(p["text"]) // 4 for p in rc.passages) + len(rc.catalog_text) // 4
    budget.record_recall(tokens=est_tokens, cost_usd=0.0)

    passages_block = "\n\n---\n\n".join(
        f"[{p['session_id'][:8]}, {time.strftime('%Y-%m-%d', time.gmtime(p['ts']))}]\n{p['text']}"
        for p in rc.passages
    ) or "(no matching passages)"

    return _ok(
        f"## kos-memory recall — query: {query}\n"
        f"### Catalog\n{rc.catalog_text}\n\n"
        f"### Passages ({len(rc.passages)})\n{passages_block}"
    )


def _run_remember(args: dict) -> dict:
    fact = (args.get("fact") or "").strip()
    if not fact:
        return _err("empty fact")
    low = fact.lower()
    secret_markers = ("api key", "apikey", "password", "secret_key", "bearer ", "ghp_", "sk-")
    if any(m in low for m in secret_markers) and len(fact) < 200:
        return _err("refusing to write potential secret to memory")

    user = bool(args.get("user", False))
    tags = args.get("tags") or []
    kos_dir = _resolve_kos_dir(user)
    project = _resolve_project_dir()
    chunks = chunk_text(fact, max_chars=400, overlap=50)
    file_refs = extract_file_refs(fact)
    now = int(time.time())
    sid = f"user_pin_{now}"

    store = Store(kos_dir / FILE_CHUNKS_DB)
    inserted = []
    try:
        for c in chunks:
            cid = store.add_chunk(
                text=c.text, session_id=sid,
                project=project if not user else "user",
                ts=now, kind="user_assertion", language=c.language,
                file_refs=file_refs, asserted_by_user=True,
            )
            inserted.append(cid)
        store.upsert_session(
            sid, started_at=now, ended_at=now,
            project=project, chunk_count=len(inserted),
            tags=tags, summary=fact[:120],
        )
    finally:
        store.close()

    return _ok(f"pinned {len(inserted)} chunk(s); session_id={sid}")


def _run_get_project_state(args: dict) -> dict:
    project = _resolve_project_dir()
    kos_dir = _resolve_kos_dir(user=False)
    survey = survey_project(project)
    files = find_memory_files(project)
    parsed = [parse_memory_file(f) for f in files]
    db = kos_dir / FILE_CHUNKS_DB
    chunks_data = []
    if db.exists():
        store = Store(db)
        try:
            chunks_data = [dict(r) for r in store.iter_chunks()]
        finally:
            store.close()
    rep = reconcile(chunks_data, survey, parsed)
    payload = {
        "project_root": project,
        "is_git_repo": survey.is_git_repo,
        "branch": survey.branch,
        "head_sha": survey.head_sha,
        "dirty": survey.dirty,
        "versions": survey.versions,
        "tags": survey.tags[:5],
        "live_state": render_live_state(survey),
        "memory_md_files": [str(f.path) for f in files],
        "reconciliation": {
            "confirmed": rep.confirmed,
            "claimed_but_missing": rep.claimed_but_missing,
            "version_skew": rep.version_skew,
            "built_but_undocumented": rep.built_but_undocumented,
        },
        "reconciliation_text": render_reconciliation(rep),
    }
    return _ok(json.dumps(payload, indent=2))


def _defensive_call(module_path: str, attr: str, kwargs: dict) -> dict:
    """Invoke an optional lib.* function. Return MCP envelope.
    Returns 503-style error if module missing."""
    try:
        mod = __import__(module_path, fromlist=[attr])
    except ImportError as e:
        return _err(f"503: {module_path} not available ({e}); feature not yet merged")
    fn = getattr(mod, attr, None)
    if fn is None:
        return _err(f"503: {module_path}.{attr} not found")
    try:
        result = fn(**kwargs)
    except Exception as e:
        return _err(f"tool error: {e}")
    return _ok(json.dumps(result, indent=2, default=str) if not isinstance(result, str) else result)


# ── JSON-RPC dispatch ──────────────────────────────────────────────────

def _make_response(req_id, result=None, error=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def _handle_request(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    is_notification = req_id is None

    if method == "initialize":
        return _make_response(req_id, result={
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _make_response(req_id, result={"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "recall_project_memory":
                result = _run_recall(args)
            elif name == "remember_fact":
                result = _run_remember(args)
            elif name == "get_project_state":
                result = _run_get_project_state(args)
            elif name == "bootstrap_project":
                project = _resolve_project_dir()
                result = _defensive_call("lib.bootstrap", "bootstrap_project",
                                         {"project_root": project})
            elif name == "sync_push":
                project = _resolve_project_dir()
                result = _defensive_call("lib.sync", "push",
                                         {"project_root": project})
            elif name == "sync_pull":
                project = _resolve_project_dir()
                result = _defensive_call("lib.sync", "pull",
                                         {"project_root": project})
            elif name == "curate_memory":
                project = _resolve_project_dir()
                result = _defensive_call("lib.auto_suggestions", "curate",
                                         {"project_root": project})
            else:
                return _make_response(req_id, error={
                    "code": -32601, "message": f"unknown tool: {name}",
                })
            return _make_response(req_id, result=result)
        except Exception as e:
            return _make_response(req_id, error={
                "code": -32000, "message": f"tool error: {e}",
            })

    if is_notification:
        return None
    return _make_response(req_id, error={
        "code": -32601, "message": f"unknown method: {method}",
    })


def main() -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except Exception:
            continue
        resp = _handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
