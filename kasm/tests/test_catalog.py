"""Unit tests for lib.catalog — hierarchical session catalog."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.catalog import (
    MAX_ARCHIVE_TAGS,
    MAX_MID_TAGS,
    MAX_RECENT_ENTRIES,
    MID_WINDOW,
    RECENT_WINDOW,
    build_catalog,
    load_catalog,
    render_catalog_for_claude,
    save_catalog,
    session_ids_matching_tags,
)
from lib.store import Store


class CatalogBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "chunks.db")
        self.now = int(time.time())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _add_session(self, sid, days_ago, tags=None, summary="", chunks=1, project="/p"):
        ts = self.now - days_ago * 24 * 3600
        self.store.upsert_session(
            sid, started_at=ts, ended_at=ts + 60,
            tags=tags or [], summary=summary, project=project, chunk_count=chunks,
        )

    def test_recent_window_30d(self):
        self._add_session("recent", days_ago=10, summary="recent work", tags=["x"])
        self._add_session("midold", days_ago=60, summary="mid work", tags=["x"])
        self._add_session("ancient", days_ago=400, summary="old work", tags=["y"])
        cat = build_catalog(self.store, project="/p", now_ts=self.now)
        self.assertEqual(len(cat["recent"]), 1)
        self.assertEqual(cat["recent"][0]["session_id"], "recent")

    def test_mid_window_30_to_180(self):
        self._add_session("midA", days_ago=60, tags=["auth"])
        self._add_session("midB", days_ago=100, tags=["auth"])
        self._add_session("midC", days_ago=120, tags=["bug"])
        cat = build_catalog(self.store, project="/p", now_ts=self.now)
        # Mid is tag-clustered
        tag_to_count = {m["tag"]: m["session_count"] for m in cat["mid"]}
        self.assertEqual(tag_to_count.get("auth"), 2)
        self.assertEqual(tag_to_count.get("bug"), 1)

    def test_archive_window_above_180d(self):
        self._add_session("ancient", days_ago=400, tags=["legacy"])
        self._add_session("ancient2", days_ago=500, tags=["legacy"])
        cat = build_catalog(self.store, project="/p", now_ts=self.now)
        self.assertTrue(any(a["tag"] == "legacy" for a in cat["archive"]))

    def test_recent_capped_at_max(self):
        for i in range(MAX_RECENT_ENTRIES + 10):
            self._add_session(f"r{i}", days_ago=i % 30, tags=[f"t{i}"])
        cat = build_catalog(self.store, project="/p", now_ts=self.now)
        self.assertLessEqual(len(cat["recent"]), MAX_RECENT_ENTRIES)

    def test_uncategorized_for_no_tags(self):
        self._add_session("notag", days_ago=60, tags=[])
        cat = build_catalog(self.store, project="/p", now_ts=self.now)
        tags = [m["tag"] for m in cat["mid"]]
        self.assertIn("uncategorized", tags)

    def test_project_filter(self):
        self._add_session("a", days_ago=10, project="/proj-a")
        self._add_session("b", days_ago=10, project="/proj-b")
        cat = build_catalog(self.store, project="/proj-a", now_ts=self.now)
        self.assertEqual(len(cat["recent"]), 1)
        self.assertEqual(cat["recent"][0]["session_id"], "a")

    def test_no_project_returns_all(self):
        self._add_session("a", days_ago=10, project="/x")
        self._add_session("b", days_ago=10, project="/y")
        cat = build_catalog(self.store, project=None, now_ts=self.now)
        self.assertEqual(len(cat["recent"]), 2)

    def test_total_sessions_counted(self):
        for i in range(5):
            self._add_session(f"s{i}", days_ago=i, project="/p")
        cat = build_catalog(self.store, project="/p", now_ts=self.now)
        self.assertEqual(cat["total_sessions"], 5)

    def test_schema_version_in_output(self):
        cat = build_catalog(self.store, now_ts=self.now)
        self.assertEqual(cat["schema_version"], 1)


class CatalogPersistenceTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cat.json"
            data = {"schema_version": 1, "recent": [], "mid": [], "archive": []}
            save_catalog(path, data)
            self.assertTrue(path.exists())
            loaded = load_catalog(path)
            self.assertEqual(loaded["schema_version"], 1)

    def test_load_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_catalog(Path(tmp) / "missing.json"))

    def test_load_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_catalog(p))

    def test_save_atomic_no_tmp_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cat.json"
            save_catalog(path, {"x": 1})
            tmp_path = path.with_suffix(".json.tmp")
            self.assertFalse(tmp_path.exists())


class CatalogRenderTests(unittest.TestCase):
    def test_render_includes_section_headers(self):
        cat = {
            "total_sessions": 3,
            "recent": [
                {"session_id": "abc12345", "date": "2026-05-01",
                 "summary": "fix auth", "tags": ["auth", "fix"], "chunks": 5},
            ],
            "mid": [
                {"tag": "refactor", "session_count": 4, "example": "cleaned router"},
            ],
            "archive": [
                {"tag": "legacy", "session_count": 12, "example": ""},
            ],
        }
        out = render_catalog_for_claude(cat)
        self.assertIn("## Recent", out)
        self.assertIn("## Mid", out)
        self.assertIn("## Archive", out)
        self.assertIn("abc12345", out)
        self.assertIn("refactor", out)
        self.assertIn("legacy", out)

    def test_render_empty_catalog(self):
        cat = {"total_sessions": 0, "recent": [], "mid": [], "archive": []}
        out = render_catalog_for_claude(cat)
        self.assertIn("0 sessions", out)


class CatalogTagSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "chunks.db")
        now = int(time.time())
        self.store.upsert_session("s1", started_at=now, tags=["auth", "fix"])
        self.store.upsert_session("s2", started_at=now, tags=["docs"])
        self.store.upsert_session("s3", started_at=now, tags=["auth"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_tag_intersection(self):
        sids = session_ids_matching_tags(self.store, ["auth"])
        self.assertEqual(set(sids), {"s1", "s3"})

    def test_tag_case_insensitive(self):
        sids = session_ids_matching_tags(self.store, ["AUTH"])
        self.assertEqual(set(sids), {"s1", "s3"})

    def test_no_match_returns_empty(self):
        sids = session_ids_matching_tags(self.store, ["nonexistent"])
        self.assertEqual(sids, [])


if __name__ == "__main__":
    unittest.main()
