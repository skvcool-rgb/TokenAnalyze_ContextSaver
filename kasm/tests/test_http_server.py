"""Tests for the mcp.http_server local HTTP API.

Verifies:
- Loopback-only bind (no 0.0.0.0)
- /healthz works without auth
- All other endpoints require Bearer token when configured
- Wrong token returns 401
- Host header check rejects non-localhost
- Recall + remember roundtrip works
- Defensive imports return 503 when module missing
- OPTIONS preflight returns CORS for localhost origin
- SIGTERM cleanup
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_ready(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = HTTPConnection("127.0.0.1", port, timeout=0.5)
            c.request("GET", "/healthz")
            r = c.getresponse()
            r.read()
            if r.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def _start_server(port: int, token: str | None = None,
                  project_dir: str | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    args = [sys.executable, "-m", "mcp.http_server", "--port", str(port)]
    if token:
        args += ["--token", token]
    proc = subprocess.Popen(
        args, env=env, cwd=str(PLUGIN_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if not _wait_ready(port, timeout=8.0):
        try:
            proc.terminate()
        except Exception:
            pass
        out, err = proc.communicate(timeout=2)
        raise RuntimeError(f"server did not become ready: stderr={err!r}")
    return proc


def _stop(proc: subprocess.Popen) -> None:
    """Terminate + drain pipes to silence ResourceWarnings."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass
    for s in ("stdin", "stdout", "stderr"):
        x = getattr(proc, s, None)
        if x is not None:
            try:
                x.close()
            except Exception:
                pass


def _request(port: int, method: str, path: str,
             body: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
    c = HTTPConnection("127.0.0.1", port, timeout=5.0)
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    payload = None
    if body is not None:
        payload = json.dumps(body)
    c.request(method, path, body=payload, headers=h)
    r = c.getresponse()
    raw = r.read().decode("utf-8")
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {"_raw": raw}
    c.close()
    return r.status, data


class HTTPHealthTests(unittest.TestCase):
    def test_healthz_returns_200_no_auth(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, token="x", project_dir=tmp)
            try:
                code, data = _request(port, "GET", "/healthz")
                self.assertEqual(code, 200)
                self.assertTrue(data.get("ok"))
                self.assertIn("service", data["data"])
            finally:
                _stop(proc)


class HTTPLoopbackOnlyTests(unittest.TestCase):
    def test_server_does_not_bind_0_0_0_0(self):
        from mcp.http_server import HOST
        self.assertEqual(HOST, "127.0.0.1")

    def test_make_server_binds_loopback(self):
        from mcp.http_server import make_server
        port = _free_port()
        srv = make_server(port, None)
        try:
            host, _bound = srv.server_address
            self.assertEqual(host, "127.0.0.1")
            # Family should be IPv4 in this build
            self.assertEqual(srv.socket.family, socket.AF_INET)
        finally:
            srv.server_close()


class HTTPStatusTests(unittest.TestCase):
    def test_status_returns_chunks_count(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, project_dir=tmp)
            try:
                code, data = _request(port, "GET", "/v1/status")
                self.assertEqual(code, 200)
                self.assertTrue(data.get("ok"))
                self.assertIn("chunks", data["data"])
                self.assertEqual(data["data"]["chunks"], 0)
            finally:
                _stop(proc)


class HTTPRecallEmptyTests(unittest.TestCase):
    def test_empty_query_returns_error_envelope(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, project_dir=tmp)
            try:
                code, data = _request(port, "POST", "/v1/recall",
                                      body={"query": ""})
                self.assertEqual(code, 400)
                self.assertFalse(data.get("ok"))
                self.assertIn("empty", data.get("error", "").lower())
            finally:
                _stop(proc)


class HTTPRememberRoundtripTests(unittest.TestCase):
    def test_remember_then_status_shows_chunk(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, project_dir=tmp)
            try:
                code, data = _request(port, "POST", "/v1/remember",
                                      body={"fact": "Test fact via HTTP."})
                self.assertEqual(code, 200, data)
                self.assertTrue(data.get("ok"))
                self.assertGreaterEqual(len(data["data"]["chunk_ids"]), 1)
                code, st = _request(port, "GET", "/v1/status")
                self.assertGreaterEqual(st["data"]["chunks"], 1)
            finally:
                _stop(proc)


class HTTPAuthTests(unittest.TestCase):
    def test_missing_token_returns_401(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, token="secret123", project_dir=tmp)
            try:
                code, data = _request(port, "GET", "/v1/status")
                self.assertEqual(code, 401)
                self.assertFalse(data.get("ok"))
            finally:
                _stop(proc)

    def test_correct_token_accepted(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, token="secret123", project_dir=tmp)
            try:
                code, data = _request(
                    port, "GET", "/v1/status",
                    headers={"Authorization": "Bearer secret123"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(data.get("ok"))
            finally:
                _stop(proc)

    def test_wrong_token_returns_401(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, token="secret123", project_dir=tmp)
            try:
                code, _ = _request(
                    port, "GET", "/v1/status",
                    headers={"Authorization": "Bearer wrong"},
                )
                self.assertEqual(code, 401)
            finally:
                _stop(proc)


class HTTPHostHeaderTests(unittest.TestCase):
    def test_bad_host_header_rejected(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, project_dir=tmp)
            try:
                code, data = _request(
                    port, "GET", "/v1/status",
                    headers={"Host": "evil.attacker.example"},
                )
                self.assertEqual(code, 400)
                self.assertIn("host", data.get("error", "").lower())
            finally:
                _stop(proc)

    def test_localhost_host_header_ok(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, project_dir=tmp)
            try:
                code, _ = _request(
                    port, "GET", "/v1/status",
                    headers={"Host": f"localhost:{port}"},
                )
                self.assertEqual(code, 200)
            finally:
                _stop(proc)


class HTTPCorsPreflightTests(unittest.TestCase):
    def test_options_preflight_returns_cors_for_localhost(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, project_dir=tmp)
            try:
                c = HTTPConnection("127.0.0.1", port, timeout=5.0)
                c.request("OPTIONS", "/v1/recall", headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                })
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 204)
                allow = r.getheader("Access-Control-Allow-Origin") or ""
                self.assertEqual(allow, "http://localhost:3000")
                methods = r.getheader("Access-Control-Allow-Methods") or ""
                self.assertIn("POST", methods)
            finally:
                _stop(proc)


class HTTPDefensiveImportsTests(unittest.TestCase):
    def test_sync_push_returns_503_when_lib_missing(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, project_dir=tmp)
            try:
                code, data = _request(port, "POST", "/v1/sync/push", body={})
                self.assertEqual(code, 503)
                self.assertFalse(data.get("ok"))
                self.assertIn("lib.sync", data.get("error", ""))
            finally:
                _stop(proc)


class HTTPSigtermTests(unittest.TestCase):
    def test_server_exits_cleanly_on_terminate(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            proc = _start_server(port, project_dir=tmp)
            try:
                # Give the server one good request to confirm it's alive
                code, _ = _request(port, "GET", "/healthz")
                self.assertEqual(code, 200)
                proc.terminate()
                try:
                    rc = proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    self.fail("server did not exit on terminate")
                # On Windows, terminate() = TerminateProcess; rc may be
                # nonzero. All we need is that the process exited.
                self.assertIsNotNone(rc)
            finally:
                _stop(proc)


if __name__ == "__main__":
    unittest.main()
