"""kos-memory MCP server — stdio JSON-RPC.

Exposes two tools to Claude:

  recall_project_memory(query, window_days=30, user=False)
    The expensive one. Runs Stage 0+1+2 of the recall pipeline and returns
    structured passages + catalog. Claude itself does Stage 3 (synthesis).

  remember_fact(fact, tags=None, user=False)
    Cheap. Pins a user-asserted chunk to the store.

Both tools enforce throttling per session and per day. The MCP server reads
session state from <kos-dir>/budget.json — same store the slash commands use.

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
from lib.catalog import build_catalog, render_catalog_for_claude, save_catalog
from lib.chunker import chunk_text, extract_file_refs
from lib.paths import (
    FILE_BUDGET,
    FILE_CATALOG,
    FILE_CHUNKS_DB,
    ensure_kos_dir,
)
from lib.recall import (
    RecallContext,
    stage_0_local_expansion,
    stage_1_catalog,
    stage_2_grep,
)
from lib.store import Store

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "kos-memory"
SERVER_VERSION = "4.0.0"

# Per-session in-memory throttle (resets when MCP server restarts)
_session_recall_count = 0
_MAX_RECALLS_PER_SESSION = 5


def _resolve_kos_dir(user: bool) -> Path:
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return ensure_kos_dir(project, user_level=bool(user))


# ---------------------------------------------------------------------------
# Tool definitions (with deliberate, opinionated descriptions)
# ---------------------------------------------------------------------------

TOOL_RECALL = {
    "name": "recall_project_memory",
    "description": (
        "Recover past context for THIS project from the kos-memory backup store. "
        "USE WHEN: the user asks about something the current chat doesn't know about, "
        "OR the current session feels like it's missing prior decisions, OR the user "
        "explicitly references 'earlier', 'last time', 'we discussed', 'where we left off'. "
        "DO NOT USE: as a routine search — this is a backup mode, not a primary lookup. "
        "Prefer reading files in the current workspace first. Cost: ~$0.01-0.03 per call. "
        "Hard caps: 5 calls per session, 50 per day, $0.50 per day. "
        "Returns: catalog of past sessions + selected passages. You synthesize the delta."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to recall. Be specific — 'auth refactor' beats 'stuff'.",
            },
            "window_days": {
                "type": "integer",
                "description": "How far back to search. Default 30. Use 90+ for archived projects.",
                "default": 30,
            },
            "user": {
                "type": "boolean",
                "description": "Search the cross-project user-level store instead of this project.",
                "default": False,
            },
        },
        "required": ["query"],
    },
}

TOOL_REMEMBER = {
    "name": "remember_fact",
    "description": (
        "Pin a fact to the long-term project memory store. USE WHEN: the user explicitly "
        "asks you to remember/note/save something for future sessions. DO NOT USE: to "
        "auto-summarize the current conversation (the Stop hook handles that). DO NOT USE: "
        "for sensitive data like API keys, passwords, or PII. Returns: chunk_ids inserted. "
        "Marked as user-asserted, weighted higher in future recalls."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The fact to remember. One concise sentence works best.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for catalog clustering (e.g. ['auth','decision']).",
                "default": [],
            },
            "user": {
                "type": "boolean",
                "description": "Pin to the cross-project user store instead of this project.",
                "default": False,
            },
        },
        "required": ["fact"],
    },
}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _run_recall(args: dict) -> dict:
    global _session_recall_count
    query = (args.get("query") or "").strip()
    if not query:
        return {"isError": True, "content": [{"type": "text", "text": "empty query"}]}

    if _session_recall_count >= _MAX_RECALLS_PER_SESSION:
        return {
            "isError": True,
            "content": [{
                "type": "text",
                "text": (
                    f"recall throttled: max {_MAX_RECALLS_PER_SESSION} per session "
                    f"reached. Use /recall <query> to bypass session cap "
                    f"(daily budget still applies)."
                ),
            }],
        }

    user = bool(args.get("user", False))
    window_days = int(args.get("window_days", 30))
    kos_dir = _resolve_kos_dir(user)

    # Daily budget gate
    budget = Budget(kos_dir / FILE_BUDGET)
    allowed, reason = budget.can_recall(estimated_tokens=4000)
    if not allowed:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"recall throttled: {reason}"}],
        }

    rc = RecallContext(
        query=query,
        window_days=window_days,
        project_root=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
        user_level=user,
    )
    stage_0_local_expansion(rc, kos_dir)
    stage_1_catalog(rc, kos_dir)
    stage_2_grep(rc, kos_dir)

    # Estimate token cost
    est_tokens = sum(len(p["text"]) // 4 for p in rc.passages) + len(rc.catalog_text) // 4
    budget.record_recall(tokens=est_tokens, cost_usd=0.0)
    _session_recall_count += 1

    # Build the response Claude will read
    passages_block = "\n\n---\n\n".join(
        f"[{p['session_id'][:8]}, {time.strftime('%Y-%m-%d', time.gmtime(p['ts']))}"
        f"{', user-asserted' if p['asserted_by_user'] else ''}"
        f"{', SUPERSEDED' if p['contradicted_by_later_session'] else ''}]\n{p['text']}"
        for p in rc.passages
    ) or "(no matching passages)"

    text = (
        f"## kos-memory recall — query: {query}\n"
        f"Window: past {window_days}d | Scope: {'user' if user else 'project'}\n"
        f"Sources: {len(rc.passages)} passages from "
        f"{len({p['session_id'] for p in rc.passages})} sessions\n\n"
        f"### Catalog (Stage 1)\n{rc.catalog_text}\n\n"
        f"### Passages (Stage 2)\n{passages_block}\n\n"
        f"### Synthesis instructions (you do this)\n"
        f"Compare passages above with current context. Produce sections "
        f"(a) NEW ITEMS, (b) POTENTIALLY STALE, (c) SUGGESTED STATE, "
        f"(d) UNCERTAINTY, (e) CONTRADICTIONS DETECTED. "
        f"Cite source dates. Do NOT reproduce raw passage text — synthesize.\n\n"
        f"Tokens consumed: ~{est_tokens} | Session calls: "
        f"{_session_recall_count}/{_MAX_RECALLS_PER_SESSION}"
    )

    return {"content": [{"type": "text", "text": text}]}


def _run_remember(args: dict) -> dict:
    fact = (args.get("fact") or "").strip()
    if not fact:
        return {"isError": True, "content": [{"type": "text", "text": "empty fact"}]}

    # Crude secret guard
    low = fact.lower()
    secret_markers = ("api key", "apikey", "password", "secret_key", "bearer ", "ghp_", "sk-")
    if any(m in low for m in secret_markers) and len(fact) < 200:
        return {
            "isError": True,
            "content": [{
                "type": "text",
                "text": (
                    "refusing to write potential secret to memory. "
                    "Rephrase without the credential, or call this tool with the "
                    "fact rewritten to omit the literal value."
                ),
            }],
        }

    user = bool(args.get("user", False))
    tags = args.get("tags") or []
    kos_dir = _resolve_kos_dir(user)
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    chunks = chunk_text(fact, max_chars=400, overlap=50)
    file_refs = extract_file_refs(fact)
    now = int(time.time())
    sid = f"user_pin_{now}"

    store = Store(kos_dir / FILE_CHUNKS_DB)
    inserted = []
    try:
        for c in chunks:
            cid = store.add_chunk(
                text=c.text,
                session_id=sid,
                project=project if not user else "user",
                ts=now,
                kind="user_assertion",
                language=c.language,
                file_refs=file_refs,
                asserted_by_user=True,
            )
            inserted.append(cid)
        store.upsert_session(
            sid,
            started_at=now,
            ended_at=now,
            project=project,
            chunk_count=len(inserted),
            tags=tags,
            summary=fact[:120],
        )
    finally:
        store.close()

    return {
        "content": [{
            "type": "text",
            "text": (
                f"✓ pinned {len(inserted)} chunk(s) as user-asserted.\n"
                f"  session_id: {sid}\n"
                f"  scope: {'user' if user else 'project'}\n"
                f"  tags: {tags or 'none'}\n"
                f"  chunk_ids: {inserted[:5]}{'...' if len(inserted) > 5 else ''}"
            ),
        }]
    }


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------

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

    # Notifications have no id; we don't reply
    is_notification = req_id is None

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return _make_response(req_id, result=result)

    if method == "notifications/initialized":
        return None  # no reply

    if method == "tools/list":
        return _make_response(req_id, result={"tools": [TOOL_RECALL, TOOL_REMEMBER]})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "recall_project_memory":
                result = _run_recall(args)
            elif name == "remember_fact":
                result = _run_remember(args)
            else:
                return _make_response(req_id, error={
                    "code": -32601,
                    "message": f"unknown tool: {name}",
                })
            return _make_response(req_id, result=result)
        except Exception as e:
            return _make_response(req_id, error={
                "code": -32000,
                "message": f"tool error: {e}",
            })

    if is_notification:
        return None

    return _make_response(req_id, error={
        "code": -32601,
        "message": f"unknown method: {method}",
    })


def main() -> int:
    """Run JSON-RPC stdio loop."""
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
