"""Tests for the mcp.cli subcommand interface."""
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


def _run_cli(*args: str, project_dir: str | None = None,
             env_overrides: dict | None = None) -> dict:
    """Run `python -m mcp.cli <args>` and return parsed JSON output."""
    env = os.environ.copy()
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    if env_overrides:
        env.update(env_overrides)
    r = subprocess.run(
        [sys.executable, "-m", "mcp.cli", *args],
        capture_output=True, text=True, env=env, timeout=30,
        cwd=str(PLUGIN_ROOT),
    )
    if r.returncode not in (0, 2):
        raise RuntimeError(
            f"cli failed (code={r.returncode}): "
            f"stdout={r.stdout!r} stderr={r.stderr!r}"
        )
    return json.loads(r.stdout.strip())


class CLIRememberTests(unittest.TestCase):
    def test_remember_inserts_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = _run_cli(
                "remember",
                "--fact", "The team chose Postgres over MySQL.",
                "--tags", "decision,db",
                project_dir=tmp,
            )
            self.assertTrue(out["ok"])
            self.assertGreaterEqual(len(out["chunk_ids"]), 1)
            self.assertIn("user_pin_", out["session_id"])
            self.assertTrue(Path(out["kos_dir"]).exists())


class CLIStatusTests(unittest.TestCase):
    def test_status_on_empty_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = _run_cli("status", project_dir=tmp)
            self.assertTrue(out["ok"])
            self.assertEqual(out["chunks"], 0)
            self.assertTrue(out.get("empty", False))

    def test_status_after_remember(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_cli("remember", "--fact", "The fact about auth.",
                     project_dir=tmp)
            out = _run_cli("status", project_dir=tmp)
            self.assertTrue(out["ok"])
            self.assertGreaterEqual(out["chunks"], 1)
            self.assertGreaterEqual(out["user_asserted"], 1)
            self.assertEqual(out["contradicted"], 0)


class CLIRecallStagesTests(unittest.TestCase):
    def test_recall_stage_a_returns_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_cli("remember", "--fact", "The auth refactor used OAuth2.",
                     project_dir=tmp)
            out = _run_cli(
                "recall_stage_a",
                "--query", "auth oauth",
                "--window-days", "30",
                project_dir=tmp,
            )
            self.assertTrue(out["ok"])
            self.assertIn("auth", out["expanded_terms"])
            self.assertIn("oauth", out["expanded_terms"])
            # SynonymCache seed expands "auth" → "authentication"
            self.assertIn("authentication", out["expanded_terms"])
            self.assertIn("catalog_text", out)
            self.assertIn("kos_dir", out)

    def test_recall_stage_b_returns_passages(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_cli("remember", "--fact", "The auth refactor used OAuth2.",
                     project_dir=tmp)
            stage_a = _run_cli(
                "recall_stage_a", "--query", "auth", "--window-days", "30",
                project_dir=tmp,
            )
            kos_dir = stage_a["kos_dir"]
            terms_csv = ",".join(stage_a["expanded_terms"])
            stage_b = _run_cli(
                "recall_stage_b",
                "--kos-dir", kos_dir,
                "--query", "auth",
                "--terms", terms_csv,
                "--window-days", "30",
                project_dir=tmp,
            )
            self.assertTrue(stage_b["ok"])
            self.assertGreaterEqual(stage_b["n_passages"], 1)


class CLIExportImportRoundtripTests(unittest.TestCase):
    def test_export_then_import_preserves_chunk(self):
        with tempfile.TemporaryDirectory() as src, \
             tempfile.TemporaryDirectory() as dst, \
             tempfile.TemporaryDirectory() as outdir:
            # Seed source
            _run_cli("remember", "--fact", "Important fact for export.",
                     project_dir=src)

            # Export
            export_path = Path(outdir) / "export.json"
            r_export = _run_cli(
                "export", "--out", str(export_path),
                project_dir=src,
            )
            self.assertTrue(r_export["ok"])
            self.assertGreaterEqual(r_export["chunks_written"], 1)
            self.assertTrue(export_path.exists())

            # Import into a fresh project
            r_import = _run_cli(
                "import_export", "--path", str(export_path),
                project_dir=dst,
            )
            self.assertTrue(r_import["ok"])
            self.assertGreaterEqual(r_import["chunks_imported"], 1)

            # Verify dst has the chunk
            r_status = _run_cli("status", project_dir=dst)
            self.assertGreaterEqual(r_status["chunks"], 1)

    def test_import_dedupe_idempotent(self):
        with tempfile.TemporaryDirectory() as src, \
             tempfile.TemporaryDirectory() as dst, \
             tempfile.TemporaryDirectory() as outdir:
            _run_cli("remember", "--fact", "Dedupe test fact.", project_dir=src)
            export_path = Path(outdir) / "export.json"
            _run_cli("export", "--out", str(export_path), project_dir=src)
            _run_cli("import_export", "--path", str(export_path), project_dir=dst)
            # Second import should skip
            r_second = _run_cli(
                "import_export", "--path", str(export_path), project_dir=dst,
            )
            self.assertEqual(r_second["chunks_imported"], 0)
            self.assertGreaterEqual(r_second["chunks_skipped"], 1)


class CLIRebuildCatalogTests(unittest.TestCase):
    def test_rebuild_catalog_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_cli("remember", "--fact", "for catalog test", project_dir=tmp)
            out = _run_cli("rebuild_catalog", project_dir=tmp)
            self.assertTrue(out["ok"])
            self.assertTrue(Path(out["catalog_path"]).exists())


class CLIMarkContradictedTests(unittest.TestCase):
    def test_mark_contradicted_flips_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            r_remember = _run_cli(
                "remember", "--fact", "to be contradicted later",
                project_dir=tmp,
            )
            cid = r_remember["chunk_ids"][0]
            from lib.paths import ensure_kos_dir
            kos_dir = str(ensure_kos_dir(tmp, user_level=False))
            r_mark = _run_cli(
                "mark_contradicted",
                "--kos-dir", kos_dir,
                "--ids", cid,
                project_dir=tmp,
            )
            self.assertTrue(r_mark["ok"])
            self.assertEqual(r_mark["marked"], 1)
            r_status = _run_cli("status", project_dir=tmp)
            self.assertGreaterEqual(r_status["contradicted"], 1)


if __name__ == "__main__":
    unittest.main()
