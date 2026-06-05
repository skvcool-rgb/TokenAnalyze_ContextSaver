"""kos-memory HTTP API server — local-only, loopback-bound REST surface.

Exposes recall / remember / status / state / bootstrap / sync over plain
JSON-over-HTTP for tools that don't speak MCP (Aider, Continue.dev, shell
scripts, custom integrations).

================================================================
SECURITY MODEL — read this before changing anything.
================================================================

This server is INTENTIONALLY local-only. Three layers of defense:

1. **Bind only to 127.0.0.1.** Never 0.0.0.0, never a public IP. The
   socket is constructed with HOST="127.0.0.1" so it is unreachable
   from any other machine on the LAN.

2. **Bearer token authentication.** When started with --token <T>,
   every endpoint EXCEPT `/healthz` requires
   `Authorization: Bearer <T>` on the request. Tokens should be
   generated with `secrets.token_urlsafe(32)` (>= 256 bits of entropy).
   No token = no auth (suitable for tightly-scoped local dev only).

3. **Host header check (DNS rebinding defense).** Browsers can be
   tricked into making cross-origin requests to 127.0.0.1 if a
   malicious site resolves a hostname to 127.0.0.1. We reject any
   request whose Host header is not `127.0.0.1[:port]` or
   `localhost[:port]`. This blocks the rebinding attack chain
   regardless of CORS configuration.

CORS is enabled but limited to the same trusted origins. We never set
`Access-Control-Allow-Origin: *`.

If you need to expose this beyond loopback, DO NOT just change HOST —
put it behind an auth proxy (nginx + mTLS, Tailscale, etc.) and audit
the threat model.

================================================================

## Endpoints

    GET  /healthz                         - 200 OK, no auth required
    GET  /v1/status                       - store stats
    GET  /v1/state                        - live state + reconciliation
    POST /v1/recall    {query, window_days, user}
    POST /v1/remember  {fact, tags, user}
    POST /v1/bootstrap                    - defensive (lib.bootstrap)
    POST /v1/sync/push                    - defensive (lib.sync)
    POST /v1/sync/pull                    - defensive (lib.sync)

All responses share an envelope:
    {"ok": bool, "data": ..., "error": str | null}

## Run

    python -m mcp.http_server                     # default 127.0.0.1:7621, no token
    python -m mcp.http_server --port 7777
    python -m mcp.http_server --token mysecret    # require Bearer auth
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.stdio_utf8 import force_utf8_io
force_utf8_io()

from lib.budget import Budget
from lib.chunker import chunk_text, extract_file_refs
from lib.codebase_survey import survey_project
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

HOST = "127.0.0.1"          # NEVER change to 0.0.0.0
DEFAULT_PORT = 7621
SERVER_NAME = "kos-memory-http"
SERVER_VERSION = "6.0.0"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def _resolve_project_dir() -> str:
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("KOS_MEMORY_PROJECT")
        or os.getcwd()
    )


def _resolve_kos_dir(user: bool = False) -> Path:
    return ensure_kos_dir(_resolve_project_dir(), user_level=bool(user))


def _envelope(ok: bool, data=None, error: str | None = None) -> bytes:
    return json.dumps({"ok": ok, "data": data, "error": error}).encode("utf-8")


# ── Handler implementations (return (status_code, envelope_dict)) ──────

def _h_status(_body: dict) -> tuple[int, dict]:
    kos_dir = _resolve_kos_dir(user=False)
    db = kos_dir / FILE_CHUNKS_DB
    if not db.exists():
        return 200, {"ok": True, "data": {
            "kos_dir": str(kos_dir), "chunks": 0, "sessions": 0, "empty": True,
        }, "error": None}
    store = Store(db)
    try:
        n_chunks = store.count()
        n_user = store.count(asserted_by_user=True)
        n_contra = store.count(contradicted=True)
        latest = store.latest_ts()
        sessions_total = len(store.list_sessions())
    finally:
        store.close()
    budget_state = Budget(kos_dir / FILE_BUDGET).status()
    return 200, {"ok": True, "data": {
        "kos_dir": str(kos_dir),
        "chunks": n_chunks, "user_asserted": n_user, "contradicted": n_contra,
        "sessions_total": sessions_total, "latest_ts": latest,
        "db_size_bytes": db.stat().st_size, "budget": budget_state,
    }, "error": None}


def _h_state(_body: dict) -> tuple[int, dict]:
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
    return 200, {"ok": True, "data": {
        "project_root": project,
        "is_git_repo": survey.is_git_repo,
        "branch": survey.branch, "head_sha": survey.head_sha,
        "dirty": survey.dirty, "versions": survey.versions,
        "tags": survey.tags[:5],
        "memory_md_files": [str(f.path) for f in files],
        "reconciliation": {
            "confirmed": rep.confirmed,
            "claimed_but_missing": rep.claimed_but_missing,
            "version_skew": rep.version_skew,
            "built_but_undocumented": rep.built_but_undocumented,
        },
        "reconciliation_text": render_reconciliation(rep),
    }, "error": None}


def _h_recall(body: dict) -> tuple[int, dict]:
    query = (body.get("query") or "").strip()
    if not query:
        return 400, {"ok": False, "data": None, "error": "empty query"}
    user = bool(body.get("user", False))
    window_days = int(body.get("window_days", 30))
    kos_dir = _resolve_kos_dir(user)
    budget = Budget(kos_dir / FILE_BUDGET)
    allowed, reason = budget.can_recall(estimated_tokens=4000)
    if not allowed:
        return 429, {"ok": False, "data": None, "error": f"throttled: {reason}"}
    rc = RecallContext(
        query=query, window_days=window_days,
        project_root=_resolve_project_dir(), user_level=user,
    )
    stage_0_local_expansion(rc, kos_dir)
    stage_1_catalog(rc, kos_dir)
    stage_2_grep(rc, kos_dir)
    est_tokens = sum(len(p["text"]) // 4 for p in rc.passages) + len(rc.catalog_text) // 4
    budget.record_recall(tokens=est_tokens, cost_usd=0.0)
    return 200, {"ok": True, "data": {
        "query": query, "window_days": window_days,
        "expanded_terms": rc.expanded_terms,
        "catalog_text": rc.catalog_text,
        "passages": rc.passages,
        "n_passages": len(rc.passages),
    }, "error": None}


def _h_remember(body: dict) -> tuple[int, dict]:
    fact = (body.get("fact") or "").strip()
    if not fact:
        return 400, {"ok": False, "data": None, "error": "empty fact"}
    low = fact.lower()
    secret_markers = ("api key", "apikey", "password", "secret_key", "bearer ", "ghp_", "sk-")
    if any(m in low for m in secret_markers) and len(fact) < 200:
        return 400, {"ok": False, "data": None, "error": "refusing to store potential secret"}
    user = bool(body.get("user", False))
    tags = body.get("tags") or []
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
            sid, started_at=now, ended_at=now, project=project,
            chunk_count=len(inserted), tags=tags, summary=fact[:120],
        )
    finally:
        store.close()
    return 200, {"ok": True, "data": {
        "chunk_ids": inserted, "session_id": sid,
        "kos_dir": str(kos_dir),
    }, "error": None}


def _defensive_call(module_path: str, attr: str, kwargs: dict) -> tuple[int, dict]:
    try:
        mod = __import__(module_path, fromlist=[attr])
    except ImportError as e:
        return 503, {"ok": False, "data": None,
                     "error": f"{module_path} not available: {e}"}
    fn = getattr(mod, attr, None)
    if fn is None:
        return 503, {"ok": False, "data": None,
                     "error": f"{module_path}.{attr} not found"}
    try:
        result = fn(**kwargs)
    except Exception as e:
        return 500, {"ok": False, "data": None, "error": f"tool error: {e}"}
    return 200, {"ok": True, "data": result, "error": None}


def _h_bootstrap(_body: dict) -> tuple[int, dict]:
    return _defensive_call("lib.bootstrap", "bootstrap_project",
                           {"project_root": _resolve_project_dir()})


def _h_sync_push(_body: dict) -> tuple[int, dict]:
    return _defensive_call("lib.sync", "push",
                           {"project_root": _resolve_project_dir()})


def _h_sync_pull(_body: dict) -> tuple[int, dict]:
    return _defensive_call("lib.sync", "pull",
                           {"project_root": _resolve_project_dir()})


# Routes: (method, path) -> handler
ROUTES = {
    ("GET",  "/v1/status"):     _h_status,
    ("GET",  "/v1/state"):      _h_state,
    ("POST", "/v1/recall"):     _h_recall,
    ("POST", "/v1/remember"):   _h_remember,
    ("POST", "/v1/bootstrap"):  _h_bootstrap,
    ("POST", "/v1/sync/push"):  _h_sync_push,
    ("POST", "/v1/sync/pull"):  _h_sync_pull,
}


class _Handler(BaseHTTPRequestHandler):
    server_token: str | None = None

    def log_message(self, fmt, *args):  # silence default noisy log
        return

    # ── security checks ───────────────────────────────────────
    def _check_host(self) -> bool:
        host_hdr = (self.headers.get("Host") or "").lower().strip()
        if not host_hdr:
            return False
        # strip :port for compare
        bare = host_hdr.split(":", 1)[0]
        return bare in ALLOWED_HOSTS

    def _check_token(self) -> bool:
        if self.server_token is None:
            return True
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return False
        provided = auth[len("Bearer "):].strip()
        return secrets.compare_digest(provided, self.server_token)

    def _send_envelope(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        # Limit CORS to localhost origins only — matches host check.
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            try:
                p = urlparse(origin)
                if p.hostname in ALLOWED_HOSTS:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Credentials", "true")
            except Exception:
                pass
        self.send_header("X-Server", f"{SERVER_NAME}/{SERVER_VERSION}")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ── HTTP verbs ────────────────────────────────────────────
    def do_OPTIONS(self) -> None:
        # CORS preflight — only honor for localhost origins
        origin = (self.headers.get("Origin") or "").strip()
        allow_origin = ""
        try:
            p = urlparse(origin)
            if p.hostname in ALLOWED_HOSTS:
                allow_origin = origin
        except Exception:
            allow_origin = ""
        self.send_response(204)
        if allow_origin:
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        # /healthz: no host check, no token check — pure liveness
        if method == "GET" and path == "/healthz":
            self._send_envelope(200, {"ok": True, "data": {
                "service": SERVER_NAME, "version": SERVER_VERSION,
                "ts": int(time.time()),
            }, "error": None})
            return

        if not self._check_host():
            self._send_envelope(400, {"ok": False, "data": None,
                                       "error": "bad Host header"})
            return
        if not self._check_token():
            self._send_envelope(401, {"ok": False, "data": None,
                                       "error": "auth required"})
            return

        handler = ROUTES.get((method, path))
        if handler is None:
            self._send_envelope(404, {"ok": False, "data": None,
                                       "error": f"no route {method} {path}"})
            return
        body = self._read_body() if method == "POST" else {}
        try:
            code, env = handler(body)
        except Exception as e:
            code, env = 500, {"ok": False, "data": None, "error": str(e)}
        self._send_envelope(code, env)


def make_server(port: int, token: str | None) -> ThreadingHTTPServer:
    """Build the HTTP server. Bound to loopback only."""
    handler_cls = type("BoundHandler", (_Handler,), {"server_token": token})
    httpd = ThreadingHTTPServer((HOST, port), handler_cls)
    return httpd


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kos-memory-http",
                                description="kos-memory local HTTP API")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--token", default=None,
                   help="Bearer token. Generate via secrets.token_urlsafe(32).")
    p.add_argument("--print-token-hint", action="store_true",
                   help="Print a sample token for copy-paste.")
    args = p.parse_args(argv)

    if args.print_token_hint and not args.token:
        sample = secrets.token_urlsafe(32)
        print(f"Suggested token: {sample}")
        print("Pass with --token <T>; clients send Authorization: Bearer <T>.")
        return 0

    server = make_server(args.port, args.token)
    bound_host, bound_port = server.server_address
    sys.stderr.write(
        f"[{SERVER_NAME}] listening on http://{bound_host}:{bound_port}"
        f" (auth: {'on' if args.token else 'OFF'})\n"
    )
    sys.stderr.flush()

    stop_event = threading.Event()

    def _stop(*_a):
        stop_event.set()
        try:
            server.shutdown()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, AttributeError):
        pass
    try:
        signal.signal(signal.SIGINT, _stop)
    except (ValueError, AttributeError):
        pass

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
