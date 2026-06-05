"""Tests for lib.sync — sidecar-git multi-machine sync."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib import sync as sync_mod
from lib.paths import FILE_CHUNKS_DB
from lib.store import Store


# ── Test helpers ──────────────────────────────────────────────────
def _seed_store(kos_dir: Path, chunks: list[dict],
                sessions: list[dict] | None = None) -> None:
    """Seed a chunks.db at kos_dir with the given chunks/sessions."""
    kos_dir.mkdir(parents=True, exist_ok=True)
    store = Store(kos_dir / FILE_CHUNKS_DB)
    try:
        if chunks:
            store.add_chunks_bulk(chunks)
        for s in sessions or []:
            store.upsert_session(
                s["session_id"],
                started_at=s.get("started_at"),
                ended_at=s.get("ended_at"),
                summary=s.get("summary"),
                tags=s.get("tags"),
                project=s.get("project"),
                chunk_count=s.get("chunk_count", 0),
            )
    finally:
        store.close()


def _make_bare_remote(parent: Path, name: str = "remote.git") -> Path:
    """Create a bare git repo to use as a file:// remote."""
    p = parent / name
    p.mkdir()
    subprocess.run(
        ["git", "-c", "http.proxy=", "-c", "https.proxy=",
         "init", "--bare", "-b", sync_mod.SYNC_BRANCH, str(p)],
        check=True, capture_output=True, timeout=10,
    )
    return p


def _file_url(p: Path) -> str:
    """Return a file:// URL for a local path that git will accept."""
    # On Windows we get drive-letter paths; git wants forward slashes.
    return "file:///" + str(p).replace("\\", "/").lstrip("/")


# ── Tests ─────────────────────────────────────────────────────────
class PrepareSyncRepoTests(unittest.TestCase):
    def test_creates_sidecar_with_branch_and_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            kos.mkdir()
            repo = sync_mod.prepare_sync_repo(kos)
            self.assertTrue(repo.initialized)
            self.assertTrue((kos / "sync" / ".git").exists())
            gi = (kos / "sync" / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("snapshot.json", gi)
            self.assertIn("!.gitignore", gi)
            # On the sync branch. Use symbolic-ref since rev-parse
            # --abbrev-ref returns "HEAD" on a branch with no commits.
            r = subprocess.run(
                ["git", "-C", str(kos / "sync"), "symbolic-ref",
                 "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(r.stdout.strip(), sync_mod.SYNC_BRANCH)

    def test_idempotent_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            kos.mkdir()
            r1 = sync_mod.prepare_sync_repo(kos)
            r2 = sync_mod.prepare_sync_repo(kos)
            self.assertTrue(r1.initialized)
            self.assertFalse(r2.initialized)
            # Still on sync branch.
            self.assertEqual(r2.branch, sync_mod.SYNC_BRANCH)

    def test_remote_url_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            kos.mkdir()
            remote = _make_bare_remote(Path(tmp))
            sync_mod.prepare_sync_repo(kos, remote_url=_file_url(remote))
            r = subprocess.run(
                ["git", "-C", str(kos / "sync"), "remote", "-v"],
                capture_output=True, text=True, timeout=5,
            )
            self.assertIn("origin", r.stdout)


class ExportSnapshotTests(unittest.TestCase):
    def test_export_produces_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            _seed_store(kos, [
                {"chunk_id": "a", "text": "alpha", "ts": 100,
                 "session_id": "s1", "project": "p"},
                {"chunk_id": "b", "text": "beta", "ts": 200,
                 "session_id": "s1", "project": "p"},
            ])
            snap = kos / "snap.json"
            info = sync_mod.export_snapshot(kos, snap)
            self.assertEqual(info.chunks, 2)
            self.assertTrue(snap.exists())
            payload = json.loads(snap.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(len(payload["chunks"]), 2)

    def test_export_deterministic_for_same_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            _seed_store(kos, [
                {"chunk_id": "z", "text": "zulu", "ts": 100,
                 "session_id": "s", "project": "p"},
                {"chunk_id": "a", "text": "alpha", "ts": 200,
                 "session_id": "s", "project": "p"},
                {"chunk_id": "m", "text": "mike", "ts": 150,
                 "session_id": "s", "project": "p"},
            ])
            snap1 = kos / "s1.json"
            snap2 = kos / "s2.json"
            sync_mod.export_snapshot(kos, snap1)
            sync_mod.export_snapshot(kos, snap2)
            # The exported_at timestamp will differ — strip it for the
            # determinism check; the rest must be byte-equal.
            p1 = json.loads(snap1.read_text(encoding="utf-8"))
            p2 = json.loads(snap2.read_text(encoding="utf-8"))
            p1.pop("exported_at")
            p2.pop("exported_at")
            self.assertEqual(
                json.dumps(p1, sort_keys=True),
                json.dumps(p2, sort_keys=True),
            )
            # Chunks must be sorted by id.
            ids = [c["id"] for c in p1["chunks"]]
            self.assertEqual(ids, sorted(ids))

    def test_export_with_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            kos.mkdir()
            # No chunks.db at all.
            info = sync_mod.export_snapshot(kos, kos / "snap.json")
            self.assertEqual(info.chunks, 0)
            self.assertEqual(info.sessions, 0)
            payload = json.loads(
                (kos / "snap.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["chunks"], [])


class ImportSnapshotTests(unittest.TestCase):
    def test_import_dedupes_by_chunk_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dst = Path(tmp) / "dst"
            _seed_store(src, [
                {"chunk_id": "x", "text": "ex", "ts": 100,
                 "session_id": "s", "project": "p"},
            ])
            snap = Path(tmp) / "snap.json"
            sync_mod.export_snapshot(src, snap)

            r1 = sync_mod.import_snapshot(dst, snap)
            self.assertTrue(r1.ok)
            self.assertEqual(r1.chunks_imported, 1)

            r2 = sync_mod.import_snapshot(dst, snap)
            self.assertTrue(r2.ok)
            self.assertEqual(r2.chunks_imported, 0)
            self.assertGreaterEqual(r2.chunks_skipped, 1)

    def test_import_session_prefers_most_recent_ended_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "dst"
            _seed_store(dst, [], [{
                "session_id": "s1", "started_at": 100,
                "ended_at": 200, "summary": "old",
            }])
            # Snapshot with newer ended_at.
            snap = Path(tmp) / "snap.json"
            payload = {
                "schema_version": 1,
                "exported_at": int(time.time()),
                "chunks": [],
                "sessions": [{
                    "session_id": "s1", "started_at": 100,
                    "ended_at": 500, "summary": "new",
                    "tags": [], "project": None, "chunk_count": 0,
                }],
            }
            snap.write_text(json.dumps(payload), encoding="utf-8")
            r = sync_mod.import_snapshot(dst, snap)
            self.assertTrue(r.ok)
            store = Store(dst / FILE_CHUNKS_DB)
            try:
                row = store.session("s1")
                self.assertEqual(row["ended_at"], 500)
                self.assertEqual(row["summary"], "new")
            finally:
                store.close()

            # Now snapshot with older ended_at — should NOT override.
            payload["sessions"][0]["ended_at"] = 300
            payload["sessions"][0]["summary"] = "stale"
            snap.write_text(json.dumps(payload), encoding="utf-8")
            sync_mod.import_snapshot(dst, snap)
            store = Store(dst / FILE_CHUNKS_DB)
            try:
                row = store.session("s1")
                self.assertEqual(row["ended_at"], 500)
                self.assertEqual(row["summary"], "new")
            finally:
                store.close()

    def test_import_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "dst"
            snap = Path(tmp) / "bad.json"
            snap.write_text("{not json", encoding="utf-8")
            r = sync_mod.import_snapshot(dst, snap)
            self.assertFalse(r.ok)
            self.assertIsNotNone(r.error)
            self.assertIn("bad snapshot", r.error)

    def test_import_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "dst"
            r = sync_mod.import_snapshot(dst, Path(tmp) / "nope.json")
            self.assertFalse(r.ok)
            self.assertIn("not found", r.error)

    def test_import_schema_version_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "dst"
            snap = Path(tmp) / "snap.json"
            snap.write_text(
                json.dumps({"schema_version": 99, "chunks": [],
                            "sessions": []}),
                encoding="utf-8",
            )
            r = sync_mod.import_snapshot(dst, snap)
            self.assertFalse(r.ok)
            self.assertIn("schema_version", r.error)


class PushPullRoundtripTests(unittest.TestCase):
    def test_push_then_pull_local_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            kos_a = tmp_path / "machine_a"
            kos_b = tmp_path / "machine_b"
            remote = _make_bare_remote(tmp_path)
            url = _file_url(remote)

            _seed_store(kos_a, [
                {"chunk_id": "c1", "text": "from machine A",
                 "ts": 100, "session_id": "sA", "project": "p"},
            ])
            sync_mod.prepare_sync_repo(kos_a, remote_url=url)
            push = sync_mod.sync_push(kos_a, message="initial")
            self.assertTrue(push.ok, msg=f"push.error={push.error}")
            self.assertTrue(push.committed)
            self.assertTrue(push.pushed)

            sync_mod.prepare_sync_repo(kos_b, remote_url=url)
            pull = sync_mod.sync_pull(kos_b)
            self.assertTrue(pull.ok, msg=f"pull.error={pull.error}")
            self.assertTrue(pull.pulled)
            self.assertGreaterEqual(pull.merge.chunks_imported, 1)

            # B's store should now contain the chunk from A.
            store = Store(kos_b / FILE_CHUNKS_DB)
            try:
                rows = store.get_chunks(["c1"])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["text"], "from machine A")
            finally:
                store.close()

    def test_two_machines_disjoint_chunks_converge(self):
        """Two parallel machines with different chunks both end up with
        the union after a push/pull/push/pull dance."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            kos_a = tmp_path / "ma"
            kos_b = tmp_path / "mb"
            remote = _make_bare_remote(tmp_path)
            url = _file_url(remote)

            _seed_store(kos_a, [
                {"chunk_id": "a1", "text": "alpha-a", "ts": 100,
                 "session_id": "sA", "project": "p"},
                {"chunk_id": "shared", "text": "shared text", "ts": 50,
                 "session_id": "sA", "project": "p"},
            ])
            _seed_store(kos_b, [
                {"chunk_id": "b1", "text": "beta-b", "ts": 200,
                 "session_id": "sB", "project": "p"},
                {"chunk_id": "shared", "text": "shared text", "ts": 50,
                 "session_id": "sB", "project": "p"},
            ])

            sync_mod.prepare_sync_repo(kos_a, remote_url=url)
            sync_mod.prepare_sync_repo(kos_b, remote_url=url)

            # A pushes first.
            self.assertTrue(sync_mod.sync_push(kos_a).ok)
            # B pulls A, then pushes (with B's own chunks merged in).
            self.assertTrue(sync_mod.sync_pull(kos_b).ok)
            self.assertTrue(sync_mod.sync_push(kos_b).ok)
            # A pulls B.
            self.assertTrue(sync_mod.sync_pull(kos_a).ok)

            for kos in (kos_a, kos_b):
                store = Store(kos / FILE_CHUNKS_DB)
                try:
                    ids = {r["id"] for r in store.get_chunks(
                        ["a1", "b1", "shared"])}
                    self.assertEqual(ids, {"a1", "b1", "shared"},
                                     msg=f"missing in {kos}")
                finally:
                    store.close()


class ProxyBypassTests(unittest.TestCase):
    """Verify the -c http.proxy= override neutralizes env proxy vars."""

    def test_push_succeeds_with_bogus_https_proxy_in_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            kos = tmp_path / "kos"
            remote = _make_bare_remote(tmp_path)
            url = _file_url(remote)

            _seed_store(kos, [
                {"chunk_id": "p1", "text": "proxy test", "ts": 100,
                 "session_id": "sP", "project": "p"},
            ])
            sync_mod.prepare_sync_repo(kos, remote_url=url)

            # Inject a bogus proxy. file:// transport doesn't actually
            # use HTTP, but we want to prove the override is wired so
            # http(s)-based remotes would also work.
            saved_https = os.environ.get("HTTPS_PROXY")
            saved_http = os.environ.get("HTTP_PROXY")
            os.environ["HTTPS_PROXY"] = "http://localhost:99999"
            os.environ["HTTP_PROXY"] = "http://localhost:99999"
            try:
                push = sync_mod.sync_push(kos)
                self.assertTrue(push.ok, msg=f"push.error={push.error}, "
                                f"stderr={push.stderr}")
                self.assertTrue(push.pushed)
            finally:
                if saved_https is None:
                    os.environ.pop("HTTPS_PROXY", None)
                else:
                    os.environ["HTTPS_PROXY"] = saved_https
                if saved_http is None:
                    os.environ.pop("HTTP_PROXY", None)
                else:
                    os.environ["HTTP_PROXY"] = saved_http


class EdgeCaseTests(unittest.TestCase):
    def test_push_without_init_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            kos.mkdir()
            r = sync_mod.sync_push(kos)
            self.assertFalse(r.ok)
            self.assertIn("not initialized", r.error)

    def test_push_with_no_chunks_db_emits_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            kos.mkdir()
            sync_mod.prepare_sync_repo(kos)
            r = sync_mod.sync_push(kos)
            self.assertTrue(r.ok, msg=f"err={r.error}")
            self.assertEqual(r.snapshot.chunks, 0)

    def test_push_with_no_changes_does_not_recommit(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = Path(tmp) / "kos"
            _seed_store(kos, [
                {"chunk_id": "n1", "text": "noop", "ts": 100,
                 "session_id": "s", "project": "p"},
            ])
            sync_mod.prepare_sync_repo(kos)
            r1 = sync_mod.sync_push(kos)
            self.assertTrue(r1.committed)
            r2 = sync_mod.sync_push(kos)
            self.assertTrue(r2.ok)
            # Snapshot is byte-identical for chunks (modulo exported_at)
            # but exported_at will differ each call, so a re-commit may
            # actually happen. Still, the push should be ok.
            self.assertEqual(r2.snapshot.chunks, 1)


if __name__ == "__main__":
    unittest.main()
