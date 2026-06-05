"""Tests for lib.bootstrap — first-session seed of chunks.db."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.bootstrap import (  # noqa: E402
    BootstrapReport,
    BootstrapSource,
    DEFAULT_MAX_TRANSCRIPTS,
    DOC_FILENAMES,
    KIND_DOC,
    KIND_TRANSCRIPT,
    MAX_DOC_BYTES,
    bootstrap_chunks,
    bootstrap_project,
    find_bootstrap_sources,
)
from lib.memory_md import encode_project_path  # noqa: E402
from lib.paths import FILE_CHUNKS_DB, ensure_kos_dir  # noqa: E402
from lib.store import Store  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_jsonl(turns: list[tuple[str, str]]) -> str:
    """Build a minimal Claude Code transcript JSONL string."""
    lines = []
    for role, content in turns:
        lines.append(json.dumps({
            "type": role,
            "message": {"role": role, "content": content},
        }))
    return "\n".join(lines) + "\n"


class _HomeIsolatedTest(unittest.TestCase):
    """Base — pin HOME / USERPROFILE so encoded transcripts dir is sandboxed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir(parents=True, exist_ok=True)
        self.fake_home = Path(self.tmp.name) / "home"
        self.fake_home.mkdir(parents=True, exist_ok=True)

        self._old_env = {
            k: os.environ.get(k)
            for k in ("HOME", "USERPROFILE", "CLAUDE_PROJECT_DIR",
                      "KOS_MEMORY_MODE")
        }
        os.environ["HOME"] = str(self.fake_home)
        os.environ["USERPROFILE"] = str(self.fake_home)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    # Helpers
    def _transcript_dir(self) -> Path:
        encoded = encode_project_path(self.proj)
        d = self.fake_home / ".claude" / "projects" / encoded
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _open_store(self) -> Store:
        kos_dir = ensure_kos_dir(self.proj, user_level=False)
        return Store(kos_dir / FILE_CHUNKS_DB)


# ── Discovery ─────────────────────────────────────────────────────────
class FindBootstrapSourcesTests(_HomeIsolatedTest):
    def test_empty_project_returns_empty_list(self):
        srcs = find_bootstrap_sources(self.proj)
        self.assertEqual(srcs, [])

    def test_finds_all_known_doc_filenames(self):
        # Create one of each
        for name in DOC_FILENAMES:
            _write(self.proj / name, f"# {name}\n\nbody for {name}\n")
        srcs = find_bootstrap_sources(self.proj)
        kinds = [s.kind for s in srcs]
        self.assertEqual(kinds.count("doc"), len(DOC_FILENAMES))
        names = {s.path.name for s in srcs}
        for n in DOC_FILENAMES:
            self.assertIn(n, names)

    def test_finds_only_subset_when_some_missing(self):
        _write(self.proj / "README.md", "# r")
        _write(self.proj / "CHANGELOG.md", "# c")
        srcs = find_bootstrap_sources(self.proj)
        names = {s.path.name for s in srcs}
        self.assertEqual(names, {"README.md", "CHANGELOG.md"})

    def test_detects_transcript_via_encoded_project_path(self):
        # Confirms we use lib.memory_md.encode_project_path for the lookup
        td = self._transcript_dir()
        (td / "session-a.jsonl").write_text(
            _make_jsonl([("user", "hello"), ("assistant", "hi back")]),
            encoding="utf-8",
        )
        srcs = find_bootstrap_sources(self.proj)
        ts_srcs = [s for s in srcs if s.kind == "transcript"]
        self.assertEqual(len(ts_srcs), 1)
        self.assertTrue(str(ts_srcs[0].path).endswith("session-a.jsonl"))

    def test_max_transcripts_cap_respected(self):
        td = self._transcript_dir()
        # Create 15 transcripts with stagger mtimes
        now = time.time()
        for i in range(15):
            p = td / f"sess-{i:02d}.jsonl"
            p.write_text(_make_jsonl([("user", f"msg {i}")]), encoding="utf-8")
            os.utime(p, (now - i, now - i))  # i=0 newest
        srcs = find_bootstrap_sources(self.proj, max_transcripts=5)
        ts_srcs = [s for s in srcs if s.kind == "transcript"]
        self.assertEqual(len(ts_srcs), 5)
        # Most-recent first ordering
        self.assertTrue(
            ts_srcs[0].mtime >= ts_srcs[-1].mtime,
            msg=f"order broken: {[s.mtime for s in ts_srcs]}",
        )

    def test_missing_transcript_dir_handled_gracefully(self):
        # No ~/.claude/projects/<encoded> exists at all
        _write(self.proj / "README.md", "# r")
        srcs = find_bootstrap_sources(self.proj)
        # Doc still found, no crash
        self.assertEqual(len(srcs), 1)
        self.assertEqual(srcs[0].kind, "doc")


# ── bootstrap_chunks() ─────────────────────────────────────────────────
class BootstrapChunksTests(_HomeIsolatedTest):
    def test_empty_project_returns_empty_report(self):
        store = self._open_store()
        try:
            r = bootstrap_chunks(self.proj, store)
        finally:
            store.close()
        self.assertIsInstance(r, BootstrapReport)
        self.assertEqual(r.docs_ingested, 0)
        self.assertEqual(r.transcripts_ingested, 0)
        self.assertEqual(r.chunks_added, 0)
        self.assertEqual(r.errors, [])

    def test_ingests_readme_as_bootstrap_doc(self):
        body = (
            "# Project Title\n\n"
            "This is a substantial readme. " * 20
            + "\n\n## Section\n\nMore prose to ensure multiple chunks emit.\n"
        )
        _write(self.proj / "README.md", body)

        store = self._open_store()
        try:
            r = bootstrap_chunks(self.proj, store)
            self.assertEqual(r.docs_ingested, 1)
            self.assertGreaterEqual(r.chunks_added, 1)

            rows = list(store.iter_chunks())
            kinds = {row["kind"] for row in rows}
            self.assertIn(KIND_DOC, kinds)
        finally:
            store.close()

    def test_ingests_transcript_as_bootstrap_transcript(self):
        td = self._transcript_dir()
        jsonl = _make_jsonl([
            ("user", "Help me design auth."),
            ("assistant", "Sure — start with OAuth2 server-side flow."),
            ("user", "What about refresh tokens?"),
            ("assistant", "Rotate them per session and revoke on logout."),
        ])
        (td / "real-session.jsonl").write_text(jsonl, encoding="utf-8")

        store = self._open_store()
        try:
            r = bootstrap_chunks(self.proj, store)
            self.assertEqual(r.transcripts_ingested, 1)
            self.assertGreaterEqual(r.chunks_added, 1)

            rows = list(store.iter_chunks())
            kinds = {row["kind"] for row in rows}
            self.assertIn(KIND_TRANSCRIPT, kinds)
            joined = "\n".join(row["text"] for row in rows)
            self.assertIn("OAuth2", joined)
        finally:
            store.close()

    def test_idempotent_rerun_adds_zero_chunks(self):
        _write(self.proj / "README.md", "# Hello\n\n" + ("body line\n" * 50))
        store = self._open_store()
        try:
            r1 = bootstrap_chunks(self.proj, store)
            self.assertGreaterEqual(r1.chunks_added, 1)
            n_after_first = store.count()

            # Second run on identical content
            r2 = bootstrap_chunks(self.proj, store)
            self.assertEqual(
                r2.chunks_added, 0,
                msg=f"expected 0 added, got {r2.chunks_added}",
            )
            self.assertGreaterEqual(r2.chunks_skipped, 1)
            self.assertEqual(store.count(), n_after_first)
        finally:
            store.close()

    def test_max_transcripts_cap_passed_through(self):
        td = self._transcript_dir()
        now = time.time()
        for i in range(8):
            p = td / f"sess-{i}.jsonl"
            p.write_text(
                _make_jsonl([("user", f"hello {i} " * 20),
                             ("assistant", f"reply {i} " * 20)]),
                encoding="utf-8",
            )
            os.utime(p, (now - i, now - i))

        store = self._open_store()
        try:
            r = bootstrap_chunks(self.proj, store, max_transcripts=3)
            self.assertEqual(r.transcripts_ingested, 3)
        finally:
            store.close()

    def test_secret_in_short_chunk_skipped(self):
        # Build a README whose chunked output includes a tiny credential
        # snippet. With chunker max_chars=400, a small fenced block tends
        # to land in its own short chunk.
        readme = (
            "# Setup\n\n"
            "Add the API key to your env.\n\n"
            "```\n"
            "api_key=sk-1234567890abcdef\n"
            "```\n"
            "\nNothing else here.\n"
        )
        _write(self.proj / "README.md", readme)

        store = self._open_store()
        try:
            r = bootstrap_chunks(self.proj, store)
            rows = list(store.iter_chunks())
            joined = "\n".join(row["text"] for row in rows)
            # The literal secret string must NOT have been written
            self.assertNotIn(
                "sk-1234567890abcdef", joined,
                msg=f"secret leaked into store: {joined!r}",
            )
            self.assertGreaterEqual(r.chunks_skipped, 1)
        finally:
            store.close()

    def test_pre_supplied_sources_skip_discovery(self):
        # Caller passes its own source list — discovery is bypassed
        _write(self.proj / "README.md", "# header\n\nshould NOT be ingested")
        other = self.proj / "EXTRA.md"
        _write(other, "# extra\n\n" + ("real ingested body. " * 30))

        src = BootstrapSource(
            kind="doc", path=other,
            size_bytes=other.stat().st_size,
            mtime=int(other.stat().st_mtime),
        )

        store = self._open_store()
        try:
            r = bootstrap_chunks(self.proj, store, sources=[src])
            self.assertEqual(r.docs_ingested, 1)
            joined = "\n".join(row["text"] for row in store.iter_chunks())
            self.assertIn("real ingested body", joined)
            self.assertNotIn("should NOT be ingested", joined)
        finally:
            store.close()

    def test_doc_size_capped_at_max_doc_bytes(self):
        # Create a doc larger than the cap; we just confirm we don't crash
        # and we still ingest something.
        big = "# big\n\n" + ("X" * (MAX_DOC_BYTES + 50_000))
        _write(self.proj / "README.md", big)
        store = self._open_store()
        try:
            r = bootstrap_chunks(self.proj, store)
            self.assertEqual(r.docs_ingested, 1)
            self.assertGreaterEqual(r.chunks_added, 1)
        finally:
            store.close()

    def test_creates_bootstrap_session_when_chunks_added(self):
        _write(self.proj / "README.md", "# r\n\n" + ("body line. " * 50))
        store = self._open_store()
        try:
            r = bootstrap_chunks(self.proj, store)
            self.assertGreater(r.chunks_added, 0)
            sessions = store.list_sessions()
            sids = [s["session_id"] for s in sessions]
            self.assertTrue(
                any(sid.startswith("bootstrap_") for sid in sids),
                msg=f"no bootstrap session: {sids}",
            )
        finally:
            store.close()


# ── bootstrap_project() convenience entrypoint ─────────────────────────
class BootstrapProjectTests(_HomeIsolatedTest):
    def test_opens_and_closes_its_own_store(self):
        _write(self.proj / "README.md", "# r\n\n" + ("seed body. " * 30))
        r = bootstrap_project(self.proj)
        self.assertGreater(r.chunks_added, 0)
        # Reopen separately to confirm DB is intact
        store = self._open_store()
        try:
            self.assertGreater(store.count(), 0)
        finally:
            store.close()

    def test_no_sources_no_crash(self):
        r = bootstrap_project(self.proj)
        self.assertEqual(r.chunks_added, 0)
        self.assertEqual(r.errors, [])


if __name__ == "__main__":
    unittest.main()
