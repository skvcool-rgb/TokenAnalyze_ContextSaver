"""Tests for the mcp.standalone_server stdio JSON-RPC interface.

Same shape as test_mcp_server.py: spawn the server as a subprocess and
drive it via line-framed JSON-RPC.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PLUGIN_ROOT / "mcp" / "standalone_server.py"


class _MCPClient:
    """Minimal stdio JSON-RPC client."""

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

    def close(self) -> None:
        for s in ("stdin", "stdout", "stderr"):
            x = getattr(self.proc, s, None)
            if x is not None:
                try:
                    x.close()
                except Exception:
                    pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait()


class StandaloneServerInitializeTests(unittest.TestCase):
    def test_initialize_handshake(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                resp = cli.call("initialize", {})
                self.assertIn("result", resp)
                self.assertEqual(resp["result"]["serverInfo"]["name"],
                                 "kos-memory-standalone")
                self.assertIn("protocolVersion", resp["result"])
            finally:
                cli.close()


class StandaloneServerToolsListTests(unittest.TestCase):
    def test_tools_list_includes_standard_and_extended(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/list", {})
                names = [t["name"] for t in resp["result"]["tools"]]
                self.assertIn("recall_project_memory", names)
                self.assertIn("remember_fact", names)
                self.assertIn("get_project_state", names)
                self.assertIn("bootstrap_project", names)
                self.assertIn("sync_push", names)
                self.assertIn("sync_pull", names)
                self.assertIn("curate_memory", names)
            finally:
                cli.close()


class StandaloneServerRememberTests(unittest.TestCase):
    def test_remember_accepts_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "remember_fact",
                    "arguments": {"fact": "Standalone test fact."},
                })
                self.assertNotIn("error", resp)
                txt = resp["result"]["content"][0]["text"]
                self.assertIn("pinned", txt)
            finally:
                cli.close()

    def test_remember_rejects_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "remember_fact",
                    "arguments": {"fact": "use api key sk-abc12345"},
                })
                self.assertTrue(resp["result"].get("isError"))
            finally:
                cli.close()


class StandaloneServerProjectStateTests(unittest.TestCase):
    def test_get_project_state_returns_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "get_project_state",
                    "arguments": {},
                })
                self.assertNotIn("error", resp)
                txt = resp["result"]["content"][0]["text"]
                # Body should be JSON parseable
                payload = json.loads(txt)
                self.assertIn("project_root", payload)
                self.assertIn("reconciliation", payload)
            finally:
                cli.close()


class StandaloneServerDefensiveTests(unittest.TestCase):
    def test_bootstrap_runs_when_module_available(self):
        # lib.bootstrap currently exists; the tool should succeed (or fail
        # with a tool-error envelope, never crash the server). If a future
        # build removes lib.bootstrap, the tool should return a 503-style
        # error envelope. Either branch keeps the wire alive.
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "bootstrap_project",
                    "arguments": {},
                })
                # Wire-level: must always have a result (no error transport)
                self.assertIn("result", resp)
                # If errored, the message should reference the module path
                if resp["result"].get("isError"):
                    txt = resp["result"]["content"][0]["text"]
                    self.assertTrue("lib.bootstrap" in txt or "tool error" in txt)
            finally:
                cli.close()

    def test_sync_push_returns_503_when_module_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "sync_push", "arguments": {},
                })
                self.assertTrue(resp["result"].get("isError"))
                self.assertIn("lib.sync",
                              resp["result"]["content"][0]["text"])
            finally:
                cli.close()


class StandaloneServerUnknownToolTests(unittest.TestCase):
    def test_unknown_tool_returns_jsonrpc_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = _MCPClient(project_dir=tmp)
            try:
                cli.call("initialize", {})
                resp = cli.call("tools/call", {
                    "name": "no_such_tool", "arguments": {},
                })
                self.assertIn("error", resp)
                self.assertEqual(resp["error"]["code"], -32601)
            finally:
                cli.close()


if __name__ == "__main__":
    unittest.main()
