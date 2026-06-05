"""Unit tests for lib.codebase_survey."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.codebase_survey import (
    CACHE_TTL_S,
    Survey,
    invalidate_cache,
    render_live_state,
    survey_project,
)


def _git(args, cwd):
    """Run git in cwd, raise on failure."""
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, check=True)


def _make_repo(path: Path) -> None:
    """Initialize a small git repo with 2 commits + 1 tag."""
    _git(["init", "-b", "main"], cwd=str(path))
    _git(["config", "user.email", "test@example.com"], cwd=str(path))
    _git(["config", "user.name", "Test"], cwd=str(path))
    (path / "README.md").write_text("# repo", encoding="utf-8")
    (path / "lib").mkdir()
    (path / "lib" / "core.py").write_text("def x(): pass\n", encoding="utf-8")
    (path / "package.json").write_text(
        '{"name": "x", "version": "1.2.3"}', encoding="utf-8"
    )
    _git(["add", "."], cwd=str(path))
    _git(["commit", "-m", "initial commit"], cwd=str(path))
    _git(["tag", "v1.0.0"], cwd=str(path))
    (path / "CHANGES.md").write_text("# changes", encoding="utf-8")
    _git(["add", "CHANGES.md"], cwd=str(path))
    _git(["commit", "-m", "add changelog"], cwd=str(path))


class GitSurveyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _make_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detects_git_repo(self):
        s = survey_project(self.repo, use_cache=False)
        self.assertTrue(s.is_git_repo)

    def test_extracts_branch_and_head(self):
        s = survey_project(self.repo, use_cache=False)
        self.assertEqual(s.branch, "main")
        self.assertTrue(s.head_sha)
        self.assertEqual(len(s.head_sha), 7)  # short SHA
        self.assertEqual(s.head_subject, "add changelog")

    def test_extracts_last_commits(self):
        s = survey_project(self.repo, use_cache=False)
        self.assertEqual(len(s.last_commits), 2)
        # Most recent first
        self.assertEqual(s.last_commits[0]["subject"], "add changelog")
        self.assertEqual(s.last_commits[1]["subject"], "initial commit")

    def test_extracts_tags(self):
        s = survey_project(self.repo, use_cache=False)
        self.assertIn("v1.0.0", s.tags)

    def test_clean_state(self):
        s = survey_project(self.repo, use_cache=False)
        self.assertFalse(s.dirty)
        self.assertEqual(s.dirty_count, 0)

    def test_dirty_state(self):
        (self.repo / "uncommitted.txt").write_text("x", encoding="utf-8")
        s = survey_project(self.repo, use_cache=False)
        self.assertTrue(s.dirty)
        self.assertGreater(s.dirty_count, 0)

    def test_handles_non_git_dir(self):
        with tempfile.TemporaryDirectory() as plain:
            s = survey_project(plain, use_cache=False)
            self.assertFalse(s.is_git_repo)


class TreeSurveyTests(unittest.TestCase):
    def test_tree_summary_lists_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib").mkdir()
            (root / "lib" / "a.py").write_text("", encoding="utf-8")
            (root / "lib" / "b.py").write_text("", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "t.py").write_text("", encoding="utf-8")
            (root / "README.md").write_text("", encoding="utf-8")
            s = survey_project(root, use_cache=False)
            tree = " ".join(s.tree_summary)
            self.assertIn("lib/", tree)
            self.assertIn("tests/", tree)
            self.assertIn("README.md", tree)

    def test_skips_well_known_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
            (root / ".git").mkdir()
            (root / "real").mkdir()
            (root / "real" / "f.py").write_text("", encoding="utf-8")
            s = survey_project(root, use_cache=False)
            tree = " ".join(s.tree_summary)
            self.assertNotIn("__pycache__", tree)
            self.assertNotIn(".git", tree)
            self.assertIn("real/", tree)


class VersionSurveyTests(unittest.TestCase):
    def test_extracts_from_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text(
                '{"name": "x", "version": "2.5.0"}', encoding="utf-8"
            )
            s = survey_project(tmp, use_cache=False)
            self.assertEqual(s.versions.get("package.json"), "2.5.0")

    def test_extracts_from_pyproject_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_text(
                'version = "0.9.1"\n', encoding="utf-8"
            )
            s = survey_project(tmp, use_cache=False)
            self.assertEqual(s.versions.get("pyproject.toml"), "0.9.1")

    def test_extracts_from_python_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "lib"
            lib.mkdir()
            (lib / "__init__.py").write_text(
                '__version__ = "3.1.4"\n', encoding="utf-8"
            )
            s = survey_project(tmp, use_cache=False)
            self.assertEqual(s.versions.get("lib/__init__.py"), "3.1.4")


class TreeDepthOverrideTests(unittest.TestCase):
    """v5.1: tree depth + max-entries are configurable per-project."""

    def _config(self, root: Path, **kwargs) -> None:
        from lib.paths import FILE_CONFIG, ensure_kos_dir
        kos = ensure_kos_dir(root, user_level=False)
        cfg = kos / FILE_CONFIG
        import json as _json
        existing = {}
        if cfg.exists():
            try:
                existing = _json.loads(cfg.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(kwargs)
        cfg.write_text(_json.dumps(existing), encoding="utf-8")

    def test_default_depth_one_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib").mkdir()
            (root / "lib" / "deep").mkdir()
            (root / "lib" / "deep" / "core.py").write_text("", encoding="utf-8")
            s = survey_project(root, use_cache=False)
            tree = " ".join(s.tree_summary)
            # At depth=1 (default), we should NOT see "deep/" as its own entry
            self.assertIn("lib/", tree)
            self.assertNotIn("  deep/", tree)

    def test_depth_two_recurses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib").mkdir()
            (root / "lib" / "subpkg").mkdir()
            (root / "lib" / "subpkg" / "core.py").write_text("", encoding="utf-8")
            self._config(root, tree_depth=2)
            s = survey_project(root, use_cache=False)
            tree = "\n".join(s.tree_summary)
            self.assertIn("lib/", tree)
            self.assertIn("subpkg/", tree)

    def test_max_entries_caps_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(50):
                (root / f"f{i:02d}.py").write_text("", encoding="utf-8")
            self._config(root, tree_max_entries=10)
            s = survey_project(root, use_cache=False)
            self.assertLessEqual(len(s.tree_summary), 10)

    def test_cache_ttl_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._config(root, cache_ttl_s=1)
            s1 = survey_project(root, use_cache=True)
            time.sleep(1.5)
            s2 = survey_project(root, use_cache=True)
            # TTL=1s expired, so re-surveyed
            self.assertGreaterEqual(s2.surveyed_at, s1.surveyed_at)

    def test_bad_config_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from lib.paths import FILE_CONFIG, ensure_kos_dir
            kos = ensure_kos_dir(root, user_level=False)
            (kos / FILE_CONFIG).write_text("{not json}", encoding="utf-8")
            # Should still produce a survey, just falls back to defaults
            s = survey_project(root, use_cache=False)
            self.assertIsNotNone(s)


class CacheTests(unittest.TestCase):
    def test_uses_cache_within_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            s1 = survey_project(tmp, use_cache=True)
            ts1 = s1.surveyed_at
            time.sleep(0.05)
            s2 = survey_project(tmp, use_cache=True)
            self.assertEqual(s2.surveyed_at, ts1)  # cached, didn't re-survey

    def test_invalidate_forces_re_survey(self):
        with tempfile.TemporaryDirectory() as tmp:
            s1 = survey_project(tmp, use_cache=True)
            invalidate_cache(tmp)
            time.sleep(1.1)  # ensure ts moves
            s2 = survey_project(tmp, use_cache=True)
            self.assertGreater(s2.surveyed_at, s1.surveyed_at)


class RenderLiveStateTests(unittest.TestCase):
    def test_renders_branch_and_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(Path(tmp))
            s = survey_project(tmp, use_cache=False)
            out = render_live_state(s)
            self.assertIn("branch:", out)
            self.assertIn("main", out)
            self.assertIn("head:", out)
            self.assertIn("tags:", out)

    def test_empty_when_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = survey_project(tmp, use_cache=False)
            out = render_live_state(s)
            # Empty dir should produce empty output
            # (no git, no tree, no versions)
            self.assertEqual(out, "")

    def test_truncates_to_max_chars(self):
        s = Survey(project_root="/x", surveyed_at=int(time.time()),
                   is_git_repo=True, branch="main",
                   tree_summary=["a/" + "x" * 5000])
        out = render_live_state(s, max_chars=200)
        self.assertLessEqual(len(out), 250)
        self.assertIn("[truncated]", out)


if __name__ == "__main__":
    unittest.main()
