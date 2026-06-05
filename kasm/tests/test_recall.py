"""Unit tests for lib.recall — 4-stage recall pipeline."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.paths import FILE_CHUNKS_DB, ensure_kos_dir
from lib.recall import (
    RecallContext,
    build_synthesis_prompt,
    execute_recall_local_only,
    parse_synthesis_for_contradictions,
    render_recall_output,
    stage_0_local_expansion,
    stage_0_remember_llm_expansion,
    stage_1_catalog,
    stage_2_grep,
)
from lib.store import Store


class _StoreFixture:
    """Helper: spin up a kos-memory dir with seeded chunks."""

    def __init__(self, tmp: Path):
        self.kos_dir = ensure_kos_dir(str(tmp), user_level=False)
        self.store = Store(self.kos_dir / FILE_CHUNKS_DB)
        self.now = int(time.time())

    def add(self, *, sid, text, ts_offset_days=0, asserted=False, project=None):
        ts = self.now - ts_offset_days * 24 * 3600
        cid = self.store.add_chunk(
            text=text, session_id=sid, project=project,
            ts=ts, asserted_by_user=asserted,
        )
        # Also upsert a session record so the catalog can find it
        self.store.upsert_session(
            sid, started_at=ts, ended_at=ts + 60,
            project=project, chunk_count=1, summary=text[:80], tags=[],
        )
        return cid

    def close(self):
        self.store.close()


class Stage0Tests(unittest.TestCase):
    def test_local_expansion_uses_synonyms(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                rc = RecallContext(query="auth deploy")
                stage_0_local_expansion(rc, f.kos_dir)
                self.assertIn("auth", rc.expanded_terms)
                self.assertIn("deploy", rc.expanded_terms)
                self.assertIn("authentication", rc.expanded_terms)  # seed
                self.assertIn("deployment", rc.expanded_terms)  # seed
            finally:
                f.close()

    def test_remember_llm_expansion_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                rc = RecallContext(query="rare query")
                stage_0_local_expansion(rc, f.kos_dir)
                stage_0_remember_llm_expansion(rc, f.kos_dir, ["llm_term1", "llm_term2"])
                self.assertIn("llm_term1", rc.expanded_terms)
                # Re-expand from a fresh RecallContext — should hit cache
                rc2 = RecallContext(query="rare query")
                stage_0_local_expansion(rc2, f.kos_dir)
                self.assertIn("llm_term1", rc2.expanded_terms)
            finally:
                f.close()


class Stage1Tests(unittest.TestCase):
    def test_catalog_built_and_renderable(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                f.add(sid="s1", text="auth refactor", project=str(Path(tmp).resolve()))
                rc = RecallContext(query="auth", project_root=str(Path(tmp).resolve()))
                stage_1_catalog(rc, f.kos_dir)
                self.assertIn("recent", rc.catalog)
                self.assertGreater(len(rc.catalog_text), 0)
                self.assertGreater(rc.timings_ms.get("stage_1", 0), 0)
            finally:
                f.close()


class Stage2Tests(unittest.TestCase):
    def test_grep_returns_matching_passages(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                f.add(sid="s1", text="line 1\nthe oauth fix is here\nline 3")
                f.add(sid="s2", text="unrelated content about UI")
                rc = RecallContext(
                    query="oauth",
                    expanded_terms=["oauth"],
                    window_days=1,
                )
                stage_2_grep(rc, f.kos_dir)
                self.assertGreater(len(rc.passages), 0)
                self.assertTrue(any("oauth" in p["text"].lower() for p in rc.passages))
            finally:
                f.close()

    def test_user_asserted_ranks_higher(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                # Two equally-recent matches; one user-asserted
                f.add(sid="s1", text="oauth note A", asserted=False)
                f.add(sid="s2", text="oauth note B", asserted=True)
                rc = RecallContext(query="oauth",
                                   expanded_terms=["oauth"], window_days=1)
                stage_2_grep(rc, f.kos_dir)
                # First result must be the user-asserted one
                self.assertTrue(rc.passages[0]["asserted_by_user"])
            finally:
                f.close()

    def test_session_id_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                f.add(sid="keep", text="oauth note here")
                f.add(sid="skip", text="oauth note also here")
                rc = RecallContext(
                    query="oauth", expanded_terms=["oauth"],
                    window_days=1, selected_session_ids=["keep"],
                )
                stage_2_grep(rc, f.kos_dir)
                sids = {p["session_id"] for p in rc.passages}
                self.assertEqual(sids, {"keep"})
            finally:
                f.close()

    def test_window_days_filters_old_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                f.add(sid="recent", text="oauth recent", ts_offset_days=5)
                f.add(sid="ancient", text="oauth ancient", ts_offset_days=400)
                rc = RecallContext(query="oauth",
                                   expanded_terms=["oauth"], window_days=30)
                stage_2_grep(rc, f.kos_dir)
                sids = {p["session_id"] for p in rc.passages}
                self.assertIn("recent", sids)
                self.assertNotIn("ancient", sids)
            finally:
                f.close()

    def test_passages_capped_at_20(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                for i in range(50):
                    f.add(sid=f"s{i}", text=f"oauth match line {i}")
                rc = RecallContext(query="oauth",
                                   expanded_terms=["oauth"], window_days=1)
                stage_2_grep(rc, f.kos_dir)
                self.assertLessEqual(len(rc.passages), 20)
            finally:
                f.close()


class SynthesisPromptTests(unittest.TestCase):
    def test_prompt_contains_required_sections(self):
        rc = RecallContext(query="x", window_days=7)
        rc.passages = [{
            "session_id": "abc12345", "ts": 1700000000,
            "text": "passage text", "asserted_by_user": False,
            "contradicted_by_later_session": False,
        }]
        prompt = build_synthesis_prompt(rc, current_context="curr")
        self.assertIn("NEW ITEMS", prompt)
        self.assertIn("POTENTIALLY STALE", prompt)
        self.assertIn("SUGGESTED UPDATED STATE", prompt)
        self.assertIn("UNCERTAINTY", prompt)
        self.assertIn("CONTRADICTIONS DETECTED", prompt)
        self.assertIn("superseded_chunk_ids", prompt)
        self.assertIn("curr", prompt)
        self.assertIn("passage text", prompt)


class ContradictionParseTests(unittest.TestCase):
    def test_parses_simple_list(self):
        synth = "Lorem ipsum.\nsuperseded_chunk_ids: [abc, def, 123]\nDone."
        ids = parse_synthesis_for_contradictions(synth)
        self.assertEqual(ids, ["abc", "def", "123"])

    def test_parses_quoted_ids(self):
        synth = 'superseded_chunk_ids: ["uuid1", "uuid2"]'
        ids = parse_synthesis_for_contradictions(synth)
        self.assertEqual(ids, ["uuid1", "uuid2"])

    def test_empty_list(self):
        synth = "superseded_chunk_ids: []"
        ids = parse_synthesis_for_contradictions(synth)
        self.assertEqual(ids, [])

    def test_no_match_returns_empty(self):
        ids = parse_synthesis_for_contradictions("no marker here")
        self.assertEqual(ids, [])

    def test_case_insensitive_marker(self):
        synth = "SUPERSEDED_CHUNK_IDS: [a, b]"
        ids = parse_synthesis_for_contradictions(synth)
        self.assertEqual(ids, ["a", "b"])


class RenderRecallOutputTests(unittest.TestCase):
    def test_includes_session_count(self):
        rc = RecallContext(query="q", window_days=10)
        rc.passages = [
            {"session_id": "a", "ts": 1700000000, "text": "x",
             "asserted_by_user": False, "contradicted_by_later_session": False},
            {"session_id": "a", "ts": 1700000001, "text": "y",
             "asserted_by_user": False, "contradicted_by_later_session": False},
            {"session_id": "b", "ts": 1700000002, "text": "z",
             "asserted_by_user": False, "contradicted_by_later_session": False},
        ]
        rc.synthesis = "summary text"
        out = render_recall_output(rc)
        self.assertIn("3 passages", out)
        self.assertIn("2 sessions", out)
        self.assertIn("summary text", out)
        self.assertIn("Confirm or correct", out)

    def test_empty_passages_falls_back(self):
        rc = RecallContext(query="q")
        out = render_recall_output(rc)
        self.assertIn("no synthesis", out)


class FullPipelineTests(unittest.TestCase):
    def test_execute_recall_local_only_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _StoreFixture(Path(tmp))
            try:
                f.add(sid="s1", text="oauth refactor done", project=str(Path(tmp).resolve()))
                rc = execute_recall_local_only(
                    query="oauth",
                    window_days=7,
                    project_root=str(Path(tmp).resolve()),
                )
                self.assertIsNotNone(rc.catalog)
                self.assertGreaterEqual(len(rc.passages), 1)
                self.assertEqual(rc.synthesis, "")  # local-only doesn't synthesize
            finally:
                f.close()


if __name__ == "__main__":
    unittest.main()
