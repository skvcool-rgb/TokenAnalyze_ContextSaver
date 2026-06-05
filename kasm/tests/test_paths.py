"""Unit tests for lib.paths — cross-platform kos-memory directory resolution."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.paths import (
    FILE_BUDGET,
    FILE_CATALOG,
    FILE_CHUNKS_DB,
    FILE_INGEST_LOG,
    FILE_LAST_INGEST,
    FILE_SYNONYMS,
    ensure_kos_dir,
    project_cache_dir,
    user_cache_dir,
)


class FileNameConstantsTests(unittest.TestCase):
    def test_constants_are_strings(self):
        for c in (FILE_CHUNKS_DB, FILE_CATALOG, FILE_SYNONYMS, FILE_LAST_INGEST,
                  FILE_BUDGET, FILE_INGEST_LOG):
            self.assertIsInstance(c, str)
            self.assertGreater(len(c), 0)


class ProjectCacheDirTests(unittest.TestCase):
    def test_returns_dot_kos_memory_under_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = project_cache_dir(tmp)
            self.assertEqual(d.name, ".kos-memory")
            self.assertEqual(d.parent, Path(tmp).resolve())

    def test_works_with_pathlib_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = project_cache_dir(Path(tmp))
            self.assertTrue(d.name == ".kos-memory")


class UserCacheDirTests(unittest.TestCase):
    def test_returns_a_path(self):
        d = user_cache_dir()
        self.assertIsInstance(d, Path)
        # Should NOT collide with v3's ~/.kos-memory/ (per design)
        # On Unix → ~/.config/kos-memory/user/
        # On Windows → %APPDATA%\kos-memory\user\
        if sys.platform == "win32":
            self.assertIn("kos-memory", str(d).replace("\\", "/").lower())
        else:
            self.assertIn(".config/kos-memory", str(d))


class EnsureKosDirTests(unittest.TestCase):
    def test_project_level_creates_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = ensure_kos_dir(tmp, user_level=False)
            self.assertTrue(d.exists())
            self.assertTrue(d.is_dir())
            self.assertEqual(d.name, ".kos-memory")

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d1 = ensure_kos_dir(tmp, user_level=False)
            d2 = ensure_kos_dir(tmp, user_level=False)
            self.assertEqual(d1, d2)

    def test_user_level_creates_dir(self):
        # Use a fake home to avoid touching the real one
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("HOME")
            old_appdata = os.environ.get("APPDATA")
            os.environ["HOME"] = tmp
            os.environ["APPDATA"] = tmp
            try:
                d = ensure_kos_dir(None, user_level=True)
                self.assertTrue(d.exists())
                self.assertIn("kos-memory", str(d))
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)
                if old_appdata is not None:
                    os.environ["APPDATA"] = old_appdata
                else:
                    os.environ.pop("APPDATA", None)


if __name__ == "__main__":
    unittest.main()
