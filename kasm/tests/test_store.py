"""Unit tests for lib.store — SQLite chunk store."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.store import Store


class StoreBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "chunks.db"
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_init_creates_db_with_schema(self):
        self.assertTrue(self.db.exists())
        # Schema version should be 1
        c = sqlite3.connect(str(self.db))
        v = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        self.assertEqual(v, 1)

    def test_add_chunk_returns_id(self):
        cid = self.store.add_chunk(
            text="hello world", session_id="s1", project="/p", ts=1000
        )
        self.assertIsInstance(cid, str)
        self.assertEqual(self.store.count(), 1)

    def test_add_chunk_with_explicit_id_is_idempotent(self):
        cid = "fixed-id"
        self.store.add_chunk(text="first", chunk_id=cid, session_id="s", ts=1)
        self.store.add_chunk(text="duplicate", chunk_id=cid, session_id="s", ts=2)
        self.assertEqual(self.store.count(), 1)  # second insert ignored

    def test_add_chunks_bulk_returns_inserted_count(self):
        records = [
            {"chunk_id": f"id-{i}", "text": f"chunk {i}",
             "session_id": "s", "ts": 1000 + i}
            for i in range(5)
        ]
        n = self.store.add_chunks_bulk(records)
        self.assertEqual(n, 5)
        # Re-inserting with same ids = 0 new rows
        n2 = self.store.add_chunks_bulk(records)
        self.assertEqual(n2, 0)
        self.assertEqual(self.store.count(), 5)

    def test_count_with_filters(self):
        self.store.add_chunk(text="user", session_id="s", ts=1, asserted_by_user=True)
        self.store.add_chunk(text="auto", session_id="s", ts=2, asserted_by_user=False)
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.count(asserted_by_user=True), 1)
        self.assertEqual(self.store.count(asserted_by_user=False), 1)
        self.assertEqual(self.store.count(contradicted=True), 0)

    def test_mark_contradicted_flips_flag(self):
        cid = self.store.add_chunk(text="t", session_id="s", ts=1)
        n = self.store.mark_contradicted([cid])
        self.assertEqual(n, 1)
        self.assertEqual(self.store.count(contradicted=True), 1)

    def test_mark_contradicted_empty_returns_zero(self):
        self.assertEqual(self.store.mark_contradicted([]), 0)

    def test_iter_chunks_orders_by_ts_asc(self):
        for i, ts in enumerate([300, 100, 200]):
            self.store.add_chunk(text=f"t{i}", session_id="s", ts=ts)
        timestamps = [r["ts"] for r in self.store.iter_chunks()]
        self.assertEqual(timestamps, [100, 200, 300])

    def test_iter_chunks_with_since_filters(self):
        for ts in [100, 200, 300]:
            self.store.add_chunk(text=f"t{ts}", session_id="s", ts=ts)
        rows = list(self.store.iter_chunks(since_ts=200))
        self.assertEqual(len(rows), 2)

    def test_get_chunks_by_id(self):
        ids = [self.store.add_chunk(text=f"t{i}", session_id="s", ts=i)
               for i in range(3)]
        rows = self.store.get_chunks(ids[:2])
        self.assertEqual(len(rows), 2)

    def test_get_chunks_empty_input(self):
        self.assertEqual(self.store.get_chunks([]), [])

    def test_latest_ts_returns_max(self):
        for ts in [100, 50, 200]:
            self.store.add_chunk(text=f"t{ts}", session_id="s", ts=ts)
        self.assertEqual(self.store.latest_ts(), 200)

    def test_latest_ts_empty_db_returns_none(self):
        self.assertIsNone(self.store.latest_ts())


class StoreSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "chunks.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_upsert_session_creates(self):
        self.store.upsert_session("s1", started_at=100, project="/p", chunk_count=3)
        rows = self.store.list_sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chunk_count"], 3)

    def test_upsert_session_updates(self):
        self.store.upsert_session("s1", started_at=100, project="/p", chunk_count=3)
        self.store.upsert_session("s1", ended_at=200, chunk_count=5,
                                  summary="fixed bug", tags=["bug", "fix"])
        sess = self.store.session("s1")
        self.assertEqual(sess["ended_at"], 200)
        self.assertEqual(sess["chunk_count"], 5)
        self.assertEqual(sess["summary"], "fixed bug")
        # tags persisted as JSON
        import json
        self.assertEqual(json.loads(sess["tags"]), ["bug", "fix"])

    def test_list_sessions_with_limit(self):
        for i in range(5):
            self.store.upsert_session(f"s{i}", started_at=100 + i)
        rows = self.store.list_sessions(limit=2)
        self.assertEqual(len(rows), 2)
        # Most recent first
        self.assertEqual(rows[0]["session_id"], "s4")

    def test_list_sessions_with_since_ts(self):
        for i, ts in enumerate([100, 200, 300]):
            self.store.upsert_session(f"s{i}", started_at=ts)
        rows = self.store.list_sessions(since_ts=200)
        self.assertEqual(len(rows), 2)


class StoreSchemaTests(unittest.TestCase):
    def test_existing_db_with_correct_schema_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "chunks.db"
            s1 = Store(db); s1.add_chunk(text="x", ts=1); s1.close()
            s2 = Store(db)  # second open; should not crash
            self.assertEqual(s2.count(), 1)
            s2.close()

    def test_future_schema_refuses_to_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "chunks.db"
            c = sqlite3.connect(str(db))
            c.execute("PRAGMA user_version = 999")
            c.commit(); c.close()
            with self.assertRaises(RuntimeError):
                Store(db)


class StoreTransactionTests(unittest.TestCase):
    def test_transaction_rollback_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "chunks.db")
            try:
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.conn.execute(
                            """INSERT INTO chunks
                            (id, ts, text, asserted_by_user,
                             contradicted_by_later_session)
                            VALUES (?, ?, ?, ?, ?)""",
                            ("rollback-id", 1, "test", 0, 0),
                        )
                        raise RuntimeError("boom")
                # Insert was rolled back
                self.assertEqual(store.count(), 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
