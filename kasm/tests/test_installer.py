"""Tests for scripts/install.py — installer + manifest patcher.

Critical for fresh-clone install path on Mac/Linux where `python` may not
exist (only `python3`). The installer must detect sys.executable and bake
it into both plugin.json and settings.json so hooks don't fail with
'python: command not found' on first session."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.install import (
    SETTINGS_DEFAULT,
    check_files,
    check_python,
    load_settings,
    merge_plugin_into_settings,
    patch_plugin_manifest,
    smoke_test,
)


class CheckPythonTests(unittest.TestCase):
    def test_current_interpreter_passes(self):
        ok, msg = check_python()
        self.assertTrue(ok, msg=msg)
        self.assertIn("OK", msg)


class CheckFilesTests(unittest.TestCase):
    def test_all_required_files_present(self):
        ok, missing = check_files()
        self.assertTrue(ok, msg=f"missing: {missing}")
        self.assertEqual(missing, [])


class LoadSettingsTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_settings(Path(tmp) / "nope.json"), {})

    def test_valid_utf8_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            p.write_text('{"k": 1}', encoding="utf-8")
            self.assertEqual(load_settings(p), {"k": 1})

    def test_utf8_bom_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            p.write_text('{"k": 2}', encoding="utf-8-sig")
            self.assertEqual(load_settings(p), {"k": 2})

    def test_corrupt_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_settings(p), {})


class MergeIntoSettingsTests(unittest.TestCase):
    def test_creates_blocks_when_missing(self):
        out = merge_plugin_into_settings({}, PLUGIN_ROOT, "/usr/bin/python3")
        self.assertIn("kos-memory", out["enabledPlugins"])
        self.assertIn("kos-memory", out["mcpServers"])
        self.assertEqual(out["mcpServers"]["kos-memory"]["command"],
                         "/usr/bin/python3")

    def test_preserves_existing_settings(self):
        existing = {"theme": "dark", "alwaysThinkingEnabled": True,
                    "mcpServers": {"other": {"command": "node"}}}
        out = merge_plugin_into_settings(existing, PLUGIN_ROOT, "py")
        self.assertEqual(out["theme"], "dark")
        self.assertTrue(out["alwaysThinkingEnabled"])
        # Existing other MCP server preserved
        self.assertEqual(out["mcpServers"]["other"]["command"], "node")
        # kos-memory added alongside
        self.assertIn("kos-memory", out["mcpServers"])

    def test_idempotent_overwrite(self):
        out = merge_plugin_into_settings({}, PLUGIN_ROOT, "py-old")
        out = merge_plugin_into_settings(out, PLUGIN_ROOT, "py-new")
        # Latest python wins
        self.assertEqual(out["mcpServers"]["kos-memory"]["command"], "py-new")
        self.assertEqual(len(out["mcpServers"]), 1)


class PatchPluginManifestTests(unittest.TestCase):
    """The most important new behavior: rewriting plugin.json hook commands
    to use the detected Python interpreter so Mac/Linux users (where
    `python` may not be on PATH) get working hooks out of the box."""

    def setUp(self):
        # Work on a copy so we don't mutate the real repo
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin_root = Path(self.tmp.name) / "plugin"
        shutil.copytree(PLUGIN_ROOT / ".claude-plugin",
                        self.plugin_root / ".claude-plugin")
        self.manifest = self.plugin_root / ".claude-plugin" / "plugin.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_patches_all_hook_commands(self):
        ok, msg = patch_plugin_manifest(self.plugin_root, "/usr/bin/python3")
        self.assertTrue(ok, msg=msg)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        for entries in manifest["hooks"].values():
            for entry in entries:
                for h in entry["hooks"]:
                    self.assertTrue(
                        h["command"].startswith("/usr/bin/python3"),
                        msg=f"hook not patched: {h['command']!r}",
                    )

    def test_patches_mcp_server_command(self):
        patch_plugin_manifest(self.plugin_root, "/opt/python")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["mcpServers"]["kos-memory"]["command"], "/opt/python",
        )

    def test_quotes_path_with_spaces(self):
        windows_path = "C:/Program Files/Python/python.exe"
        patch_plugin_manifest(self.plugin_root, windows_path)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        # The hook command (a shell string) needs the path quoted
        for entries in manifest["hooks"].values():
            for entry in entries:
                for h in entry["hooks"]:
                    self.assertTrue(
                        h["command"].startswith(f'"{windows_path}"'),
                        msg=f"path with spaces not quoted: {h['command']!r}",
                    )
        # The mcpServers command goes through subprocess as argv[0] —
        # no shell quoting needed there
        self.assertEqual(
            manifest["mcpServers"]["kos-memory"]["command"], windows_path,
        )

    def test_idempotent(self):
        ok1, msg1 = patch_plugin_manifest(self.plugin_root, "py-abs")
        ok2, msg2 = patch_plugin_manifest(self.plugin_root, "py-abs")
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertIn("already patched", msg2)

    def test_dry_run_doesnt_write(self):
        before = self.manifest.read_text(encoding="utf-8")
        ok, msg = patch_plugin_manifest(
            self.plugin_root, "py-test", dry_run=True,
        )
        self.assertTrue(ok)
        self.assertIn("DRY-RUN", msg)
        after = self.manifest.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_missing_manifest_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, msg = patch_plugin_manifest(Path(tmp), "py")
            self.assertFalse(ok)
            self.assertIn("missing", msg.lower())


class SmokeTestTests(unittest.TestCase):
    def test_smoke_test_passes(self):
        ok, msg = smoke_test()
        self.assertTrue(ok, msg=msg)
        self.assertIn("OK", msg)


class FreshCloneInstallTests(unittest.TestCase):
    """Highest-value test: simulate a fresh user's clone-and-install flow.
    Copy the repo to a temp dir, run the installer with --user-settings
    pointed at a temp settings.json, verify everything ends up wired
    correctly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fresh_repo = Path(self.tmp.name) / "fresh-clone"
        # Copy the whole plugin (skipping git + caches)
        shutil.copytree(
            PLUGIN_ROOT, self.fresh_repo,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".kos-memory", "*.pyc",
            ),
        )
        self.settings_path = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_to_temp_settings(self):
        result = subprocess.run(
            [sys.executable,
             str(self.fresh_repo / "scripts" / "install.py"),
             "--user-settings", str(self.settings_path),
             "--skip-smoke"],  # smoke test runs in a separate sandbox
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0,
                         msg=f"installer failed:\n"
                             f"stdout: {result.stdout}\n"
                             f"stderr: {result.stderr}")

        # settings.json should now exist with kos-memory blocks
        self.assertTrue(self.settings_path.exists())
        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertIn("kos-memory", settings.get("enabledPlugins", {}))
        self.assertIn("kos-memory", settings.get("mcpServers", {}))
        # The MCP server command should be sys.executable (absolute path),
        # not the literal string "python"
        cmd = settings["mcpServers"]["kos-memory"]["command"]
        self.assertNotEqual(cmd, "python")
        self.assertTrue(Path(cmd.replace("/", str(Path("/")))).exists()
                        or Path(cmd).exists(),
                        msg=f"resolved python path doesn't exist: {cmd}")

        # The plugin manifest should have been patched too
        manifest = json.loads(
            (self.fresh_repo / ".claude-plugin" / "plugin.json")
            .read_text(encoding="utf-8")
        )
        for entries in manifest["hooks"].values():
            for entry in entries:
                for h in entry["hooks"]:
                    self.assertNotEqual(
                        h["command"].split()[0], "python",
                        msg="hook command still uses bare 'python' — "
                            "patch step didn't run",
                    )

    def test_install_idempotent(self):
        """Running the installer twice must not duplicate or break anything."""
        for _ in range(2):
            r = subprocess.run(
                [sys.executable,
                 str(self.fresh_repo / "scripts" / "install.py"),
                 "--user-settings", str(self.settings_path),
                 "--skip-smoke"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(r.returncode, 0)
        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        # Still exactly one kos-memory entry (not duplicated)
        self.assertEqual(
            list(settings["mcpServers"].keys()).count("kos-memory"), 1,
        )

    def test_install_preserves_other_settings(self):
        """User's existing settings (theme, model, other MCP servers) must
        survive the install."""
        self.settings_path.write_text(json.dumps({
            "theme": "dark",
            "alwaysThinkingEnabled": True,
            "mcpServers": {"other-server": {"command": "node"}},
        }), encoding="utf-8")

        r = subprocess.run(
            [sys.executable,
             str(self.fresh_repo / "scripts" / "install.py"),
             "--user-settings", str(self.settings_path),
             "--skip-smoke"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 0)

        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "dark")
        self.assertTrue(settings["alwaysThinkingEnabled"])
        self.assertEqual(settings["mcpServers"]["other-server"]["command"],
                         "node")
        self.assertIn("kos-memory", settings["mcpServers"])

    def test_install_with_explicit_python_flag(self):
        """--python override should be respected end-to-end."""
        r = subprocess.run(
            [sys.executable,
             str(self.fresh_repo / "scripts" / "install.py"),
             "--user-settings", str(self.settings_path),
             "--python", "/custom/python",
             "--skip-smoke"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 0)
        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(
            settings["mcpServers"]["kos-memory"]["command"], "/custom/python",
        )

    def test_install_creates_settings_dir_if_missing(self):
        nested = Path(self.tmp.name) / "deep" / "nested" / "settings.json"
        r = subprocess.run(
            [sys.executable,
             str(self.fresh_repo / "scripts" / "install.py"),
             "--user-settings", str(nested),
             "--skip-smoke"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertTrue(nested.exists())


if __name__ == "__main__":
    unittest.main()
