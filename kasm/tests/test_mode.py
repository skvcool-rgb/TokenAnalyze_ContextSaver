"""Unit tests for paths.get_mode / set_mode and CLI memory_mode."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.paths import (
    DEFAULT_MODE,
    FILE_CONFIG,
    MODE_BACKUP,
    MODE_PRIMARY,
    VALID_MODES,
    ensure_kos_dir,
    get_mode,
    set_mode,
)


class ModeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        # Save & clear env
        self._old_env = os.environ.pop("KOS_MEMORY_MODE", None)

    def tearDown(self):
        if self._old_env is not None:
            os.environ["KOS_MEMORY_MODE"] = self._old_env
        else:
            os.environ.pop("KOS_MEMORY_MODE", None)
        self.tmp.cleanup()

    def test_default_is_primary(self):
        self.assertEqual(DEFAULT_MODE, MODE_PRIMARY)
        self.assertEqual(get_mode(self.project), MODE_PRIMARY)

    def test_env_var_overrides_default(self):
        os.environ["KOS_MEMORY_MODE"] = "backup"
        self.assertEqual(get_mode(self.project), MODE_BACKUP)

    def test_invalid_env_var_falls_through(self):
        os.environ["KOS_MEMORY_MODE"] = "garbage"
        # Falls through to default
        self.assertEqual(get_mode(self.project), DEFAULT_MODE)

    def test_env_var_case_insensitive(self):
        os.environ["KOS_MEMORY_MODE"] = "PRIMARY"
        self.assertEqual(get_mode(self.project), MODE_PRIMARY)

    def test_set_mode_persists_to_config(self):
        cfg = set_mode(MODE_BACKUP, project_root=self.project)
        self.assertTrue(cfg.exists())
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], MODE_BACKUP)
        # Re-resolve picks up the persisted value
        self.assertEqual(get_mode(self.project), MODE_BACKUP)

    def test_env_overrides_config_file(self):
        set_mode(MODE_BACKUP, project_root=self.project)
        os.environ["KOS_MEMORY_MODE"] = "primary"
        self.assertEqual(get_mode(self.project), MODE_PRIMARY)

    def test_set_mode_invalid_raises(self):
        with self.assertRaises(ValueError):
            set_mode("invalid", project_root=self.project)

    def test_set_mode_preserves_other_config_keys(self):
        kos = ensure_kos_dir(self.project, user_level=False)
        cfg = kos / FILE_CONFIG
        cfg.write_text(json.dumps({"other_key": "kept", "mode": "backup"}),
                       encoding="utf-8")
        set_mode(MODE_PRIMARY, project_root=self.project)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], MODE_PRIMARY)
        self.assertEqual(data["other_key"], "kept")


class CLIMemoryModeTests(unittest.TestCase):
    """End-to-end: invoke `python -m mcp.cli memory_mode` and verify output."""

    def _run_cli(self, *args, project_dir):
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = project_dir
        env.pop("KOS_MEMORY_MODE", None)
        r = subprocess.run(
            [sys.executable, "-m", "mcp.cli", "memory_mode", *args],
            capture_output=True, text=True, env=env, timeout=30,
            cwd=str(PLUGIN_ROOT),
        )
        if r.returncode != 0:
            raise RuntimeError(f"cli failed: {r.stdout} {r.stderr}")
        return json.loads(r.stdout.strip())

    def test_inspect_returns_active_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_cli(project_dir=tmp)
            self.assertTrue(out["ok"])
            self.assertEqual(out["active_mode"], MODE_PRIMARY)
            self.assertIn("primary", out["valid_modes"])
            self.assertIn("backup", out["valid_modes"])

    def test_set_to_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_cli("--mode", "backup", project_dir=tmp)
            self.assertTrue(out["ok"])
            self.assertEqual(out["mode"], "backup")
            self.assertEqual(out["scope"], "project")
            # Re-inspect — should show backup now
            out2 = self._run_cli(project_dir=tmp)
            self.assertEqual(out2["active_mode"], "backup")
            self.assertEqual(out2["project_mode"], "backup")

    def test_set_to_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_cli("--mode", "backup", project_dir=tmp)
            out = self._run_cli("--mode", "primary", project_dir=tmp)
            self.assertEqual(out["mode"], "primary")


if __name__ == "__main__":
    unittest.main()
