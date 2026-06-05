"""Tests for the mcp.server JSON-RPC stdio interface.

Drives the server via subprocess: write JSON-RPC requests on stdin,
read responses on stdout. Verifies initialize handshake, tools/list,
and tools/call paths."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PLUGIN_ROOT / "mcp" / "server.py"


class _MCPClient:
    """Spawn the MCP server, send requests, read responses."""

    def __init__(self, project_dir: str | None = None):
        env = os.environ.copy()
        if project_dir:
            env["CLAUDE_PROJECT_DIR"] = project_dir
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(PLUGIN_ROOT),
            bufsize=1,
        )
        self._next_id = 1

    def call(self, method: str, params: dict | None = None) -> dict:
        rid = self._next_id
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": rid, "method": method,
               "params": params or {}}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read()
            raise RuntimeError(f"server died. stderr={stderr!r}")
        return json.loads(line)

    def notify(self, method: str, params: dict | None = None) -> None:
        req = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

    def close(self):
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(self.proc, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait()


class MCPInitializeTests(unittest.TestCase):
    def test_initialize_returns_protocol_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                resp = cli.call("initialize", {})
                self.assertIn("result", resp)
                self.assertIn("protocolVersion", resp["result"])
                self.assertEqual(
                    resp["result"]["serverInfo"]["name"], "kos-memory")
            finally:
                cli.close()

    def test_initialized_notification_no_response(self):
        # Notifications must not produce output. After sending the
        # notification, the next request should still get its response,
        # not the notification's reply.
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                cli.notify("notifications/initialized", {})
                resp = cli.call("tools/list", {})
                self.assertEqual(resp["id"], 2)
                self.assertIn("tools", resp["result"])
            finally:
                cli.close()


class MCPToolsListTests(unittest.TestCase):
    def test_tools_list_includes_recall_and_remember(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/list", {})
                names = [t["name"] for t in resp["result"]["tools"]]
                self.assertIn("recall_project_memory", names)
                self.assertIn("remember_fact", names)
            finally:
                cli.close()

    def test_tool_descriptions_mention_use_when(self):
        # Defensive: the deliberate descriptions should tell Claude when
        # to use vs. not use each tool. If we ever drop that, test fails.
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/list", {})
                for tool in resp["result"]["tools"]:
                    self.assertIn("USE WHEN", tool["description"])
                    self.assertIn("DO NOT USE", tool["description"])
            finally:
                cli.close()


class MCPRememberToolTests(unittest.TestCase):
    def test_remember_inserts_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "remember_fact",
                    "arguments": {"fact": "The team chose Go over Rust."},
                })
                self.assertNotIn("error", resp)
                content = resp["result"]["content"][0]["text"]
                self.assertIn("pinned", content)
            finally:
                cli.close()

    def test_remember_rejects_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "remember_fact",
                    "arguments": {"fact": "use api key sk-abc123def456ghi789"},
                })
                # Should be marked as error in MCP content with isError flag
                self.assertTrue(resp["result"].get("isError"))
                self.assertIn("secret", resp["result"]["content"][0]["text"].lower())
            finally:
                cli.close()

    def test_remember_empty_fact_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "remember_fact",
                    "arguments": {"fact": ""},
                })
                self.assertTrue(resp["result"].get("isError"))
            finally:
                cli.close()


class MCPRecallToolTests(unittest.TestCase):
    def test_recall_after_remember_finds_passage(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                cli.call("tools/call", {
                    "name": "remember_fact",
                    "arguments": {"fact": "OAuth2 with PKCE was chosen for auth."},
                })
                resp = cli.call("tools/call", {
                    "name": "recall_project_memory",
                    "arguments": {"query": "oauth auth", "window_days": 30},
                })
                self.assertNotIn("error", resp)
                text = resp["result"]["content"][0]["text"]
                self.assertIn("kos-memory recall", text)
                self.assertIn("Catalog", text)
                self.assertIn("Passages", text)

            finally:
                cli.close()

    def test_recall_empty_query_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "recall_project_memory",
                    "arguments": {"query": ""},
                })
                self.assertTrue(resp["result"].get("isError"))
            finally:
                cli.close()

    def test_recall_session_throttle_after_5_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                cli.call("tools/call", {
                    "name": "remember_fact",
                    "arguments": {"fact": "seed for throttle test"},
                })
                # Server has _MAX_RECALLS_PER_SESSION = 5
                for i in range(5):
                    resp = cli.call("tools/call", {
                        "name": "recall_project_memory",
                        "arguments": {"query": f"seed {i}"},
                    })
                    self.assertFalse(
                        resp["result"].get("isError", False),
                        msg=f"call {i} unexpectedly errored: {resp}",
                    )
                # 6th must be throttled
                resp = cli.call("tools/call", {
                    "name": "recall_project_memory",
                    "arguments": {"query": "seed throttled"},
                })
                self.assertTrue(resp["result"].get("isError"))
                self.assertIn("throttled", resp["result"]["content"][0]["text"].lower())
            finally:
                cli.close()


class MCPUnknownMethodTests(unittest.TestCase):
    def test_unknown_method_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                resp = cli.call("does_not_exist", {})
                self.assertIn("error", resp)
                self.assertEqual(resp["error"]["code"], -32601)
            finally:
                cli.close()

    def test_unknown_tool_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "nonexistent_tool", "arguments": {},
                })
                self.assertIn("error", resp)
            finally:
                cli.close()


if __name__ == "__main__":
    unittest.main()
