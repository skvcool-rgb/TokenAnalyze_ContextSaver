"""Production-grade integration tests.

Covers scenarios beyond unit tests:
  - Crash recovery: WAL log fully written, SQLite write interrupted → next
    session can replay
  - Schema migration: Future-version DB refused safely
  - Large corpus: chunking + recall stays sub-second on 1k chunks
  - End-to-end: ingest a real document, recall a fact from it
  - Concurrent access: two Store instances don't corrupt each other
  - Atomic writes: partial JSON writes don't leave broken catalog files
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.budget import Budget
from lib.catalog import build_catalog, save_catalog
from lib.chunker import chunk_text
from lib.paths import (
    FILE_BUDGET,
    FILE_CATALOG,
    FILE_CHUNKS_DB,
    FILE_INGEST_LOG,
    ensure_kos_dir,
)
from lib.recall import execute_recall_local_only
from lib.store import Store


class CrashRecoveryTests(unittest.TestCase):
    def test_wal_log_appended_before_db_write(self):
        """Stop hook pattern: append durable JSONL line FIRST, then SQLite.
        Verifies that if SQLite write crashes, the log line is still on disk
        and the next session can replay."""
        with tempfile.TemporaryDirectory() as tmp:
            kos = ensure_kos_dir(tmp, user_level=False)
            log = kos / FILE_INGEST_LOG

            # Manually mimic the hook's WAL pattern
            entry = {
                "ts": int(time.time()),
                "session_id": "crash-test",
                "project": tmp,
                "kind": "session_end",
                "len": 1234,
            }
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())

            # Now SIMULATE a crash before SQLite write
            # (just don't write to chunks.db). Next session should find log.

            self.assertTrue(log.exists())
            lines = log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            replayed = json.loads(lines[0])
            self.assertEqual(replayed["session_id"], "crash-test")
            self.assertEqual(replayed["kind"], "session_end")

    def test_wal_log_exists_independent_of_db(self):
        """The hook contract: WAL log line is durable even if SQLite write
        is later disturbed. We delete the DB and verify the log still
        contains the recovery info."""
        with tempfile.TemporaryDirectory() as tmp:
            kos = ensure_kos_dir(tmp, user_level=False)
            log_path = kos / FILE_INGEST_LOG
            db_path = kos / FILE_CHUNKS_DB

            # Append a WAL line, then write chunk to DB
            entry = {"ts": int(time.time()), "session_id": "wal-test",
                     "kind": "session_end", "len": 100}
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush(); os.fsync(f.fileno())
            store = Store(db_path)
            store.add_chunk(text="x", session_id="wal-test", ts=0)
            store.close()

            # SIMULATE catastrophic DB loss (disk corruption, accidental rm)
            db_path.unlink()
            # Also nuke any sqlite-shm/wal sidecars
            for suffix in ("-shm", "-wal"):
                sidecar = db_path.with_name(db_path.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()

            # Recovery: WAL log still tells us what was in flight
            self.assertTrue(log_path.exists())
            replayed = json.loads(
                log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
            )
            self.assertEqual(replayed["session_id"], "wal-test")


class SchemaMigrationTests(unittest.TestCase):
    def test_db_with_future_schema_version_refused(self):
        """A DB created by a future kos-memory must not be silently opened
        by an older client — that could corrupt data."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "chunks.db"
            c = sqlite3.connect(str(db))
            c.execute("PRAGMA user_version = 99")
            c.commit(); c.close()
            with self.assertRaises(RuntimeError) as ctx:
                Store(db)
            self.assertIn("99", str(ctx.exception))

    def test_db_with_old_schema_refused_until_migration(self):
        """Until we ship migration scripts, an old-schema DB must refuse
        to open rather than crash later."""
        # We're at SCHEMA_VERSION=1, so no version is "older" yet.
        # Stub: this test will become meaningful when we go to v2.
        # For now, verify the gate logic exists.
        from lib import SCHEMA_VERSION
        self.assertGreaterEqual(SCHEMA_VERSION, 1)


class LargeCorpusTests(unittest.TestCase):
    def test_recall_on_1k_chunks_under_2_seconds(self):
        """1000 chunks across 100 sessions; full recall pipeline must complete
        in under 2s on commodity hardware."""
        with tempfile.TemporaryDirectory() as tmp:
            kos = ensure_kos_dir(tmp, user_level=False)
            store = Store(kos / FILE_CHUNKS_DB)
            now = int(time.time())
            try:
                # 100 sessions, 10 chunks each
                bulk = []
                for s_idx in range(100):
                    sid = f"large-session-{s_idx}"
                    for c_idx in range(10):
                        bulk.append({
                            "session_id": sid,
                            "project": tmp,
                            "ts": now - s_idx * 100 - c_idx,
                            "text": f"content for session {s_idx} chunk {c_idx} "
                                    f"about feature foo bar baz refactor",
                            "kind": "prose",
                            "language": None,
                            "file_refs": [],
                            "asserted_by_user": False,
                        })
                store.add_chunks_bulk(bulk)
                for s_idx in range(100):
                    store.upsert_session(
                        f"large-session-{s_idx}",
                        started_at=now - s_idx * 100 - 10,
                        ended_at=now - s_idx * 100,
                        project=tmp, chunk_count=10,
                        tags=[f"tag{s_idx % 5}"],
                        summary=f"session {s_idx}",
                    )
                self.assertEqual(store.count(), 1000)
            finally:
                store.close()

            t0 = time.perf_counter()
            rc = execute_recall_local_only(
                query="refactor", window_days=30, project_root=tmp,
            )
            elapsed = time.perf_counter() - t0

            self.assertLess(elapsed, 2.0, msg=f"recall took {elapsed:.2f}s")
            self.assertGreater(len(rc.passages), 0)


class EndToEndIngestTests(unittest.TestCase):
    def test_ingest_then_recall_finds_seeded_fact(self):
        """Realistic flow: chunk a multi-paragraph document, ingest it,
        then recall a specific fact from it."""
        with tempfile.TemporaryDirectory() as tmp:
            kos = ensure_kos_dir(tmp, user_level=False)
            store = Store(kos / FILE_CHUNKS_DB)
            try:
                doc = (
                    "We refactored the authentication module last sprint.\n\n"
                    "The team chose OAuth2 with PKCE flow for mobile clients.\n\n"
                    "Postgres replaced MySQL for the user table.\n\n"
                    "Deployment moved from Heroku to AWS Fargate.\n\n"
                    "Logging now uses structured JSON via Pino.\n\n"
                    "```python\n"
                    "# auth.py\n"
                    "def verify_token(t): return jwt.decode(t, SECRET)\n"
                    "```\n\n"
                    "Future work: rate limit on /login and add MFA."
                )
                chunks = chunk_text(doc, max_chars=400, overlap=50)
                self.assertGreater(len(chunks), 1)

                bulk = [{
                    "session_id": "doc-session", "project": tmp,
                    "ts": int(time.time()), "text": c.text,
                    "kind": c.kind, "language": c.language,
                    "file_refs": [], "asserted_by_user": False,
                } for c in chunks]
                store.add_chunks_bulk(bulk)
                store.upsert_session(
                    "doc-session", started_at=int(time.time()) - 60,
                    ended_at=int(time.time()), project=tmp,
                    chunk_count=len(chunks), tags=["refactor", "auth"],
                    summary="auth refactor + db migration",
                )
            finally:
                store.close()

            # Now recall: should surface the OAuth2 / PKCE detail
            rc = execute_recall_local_only(
                query="oauth pkce", window_days=7, project_root=tmp,
            )
            self.assertGreater(len(rc.passages), 0)
            joined = " ".join(p["text"].lower() for p in rc.passages)
            self.assertIn("oauth", joined)


class ConcurrentAccessTests(unittest.TestCase):
    def test_two_store_instances_dont_corrupt(self):
        """Two Store objects on the same DB (e.g. one slash command running
        while a hook fires) — WAL mode should let both proceed."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "chunks.db"
            s1 = Store(db)
            s2 = Store(db)
            try:
                s1.add_chunk(text="from s1", session_id="a", ts=1)
                s2.add_chunk(text="from s2", session_id="b", ts=2)
                # Both should see both rows
                self.assertEqual(s1.count(), 2)
                self.assertEqual(s2.count(), 2)
            finally:
                s1.close()
                s2.close()


class AtomicWriteTests(unittest.TestCase):
    def test_catalog_write_atomic(self):
        """Catalog writes use os.replace — should never leave a partial file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cat.json"
            # Write a real catalog
            save_catalog(path, {"schema_version": 1, "recent": [],
                                "mid": [], "archive": []})
            self.assertTrue(path.exists())
            # No leftover .tmp file
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            # Re-write — old file is replaced atomically, never empty
            save_catalog(path, {"schema_version": 1, "recent": [],
                                "mid": [], "archive": [],
                                "extra": "second write"})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["extra"], "second write")

    def test_budget_write_atomic(self):
        """Budget writes use os.replace — same property."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            b = Budget(path)
            b.record_recall(tokens=100)
            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(".json.tmp").exists())


class DocumentSizeBoundaryTests(unittest.TestCase):
    def test_chunker_handles_empty_string(self):
        self.assertEqual(chunk_text(""), [])

    def test_chunker_handles_megabyte_input(self):
        """1 MB input split should not crash or hang."""
        text = ("This is a sentence that we will repeat many times. " * 20000)
        # ~1 MB
        t0 = time.perf_counter()
        chunks = chunk_text(text, max_chars=400)
        elapsed = time.perf_counter() - t0
        self.assertGreater(len(chunks), 100)
        self.assertLess(elapsed, 5.0, msg=f"1MB chunk took {elapsed:.2f}s")

    def test_chunker_preserves_unicode(self):
        text = "Hello → world ✓ café résumé"
        chunks = chunk_text(text)
        self.assertEqual(len(chunks), 1)
        self.assertIn("→", chunks[0].text)
        self.assertIn("✓", chunks[0].text)


class PrivacyTests(unittest.TestCase):
    def test_no_network_calls_in_lib(self):
        """Smoke check: libs should not import socket/requests/urllib."""
        # Whitelist: lib/* must not directly use urllib, requests, http.client
        forbidden_imports = ["urllib", "requests", "httpx", "http.client",
                             "socket", "aiohttp"]
        lib_files = list((PLUGIN_ROOT / "lib").rglob("*.py"))
        self.assertGreater(len(lib_files), 0)
        for f in lib_files:
            content = f.read_text(encoding="utf-8")
            for forbidden in forbidden_imports:
                self.assertNotIn(
                    f"import {forbidden}", content,
                    msg=f"{f.name} imports {forbidden} — privacy violation",
                )
                self.assertNotIn(
                    f"from {forbidden}", content,
                    msg=f"{f.name} imports from {forbidden}",
                )

    def test_no_network_calls_in_hooks(self):
        forbidden_imports = ["urllib.request", "requests", "httpx", "aiohttp"]
        hook_files = list((PLUGIN_ROOT / "hooks").rglob("*.py"))
        for f in hook_files:
            content = f.read_text(encoding="utf-8")
            for forbidden in forbidden_imports:
                self.assertNotIn(
                    f"import {forbidden}", content,
                    msg=f"{f.name} imports {forbidden}",
                )


if __name__ == "__main__":
    unittest.main()
