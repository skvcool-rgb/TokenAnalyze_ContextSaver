"""Unit tests for lib.auto_suggestions — append-only MEMORY.md curation.

Safety contract under test:

  1. extract_high_value_chunks ranks user-asserted above auto-extracted.
  2. extract_high_value_chunks detects decision-pattern phrases.
  3. extract_high_value_chunks filters chunks < 7 days old and > 180 days.
  4. format_suggestions_block emits the marker pair every time.
  5. format_suggestions_block is deterministic for the same input.
  6. append_to_memory_md creates a new section when markers are absent.
  7. append_to_memory_md REPLACES the body between markers when present.
  8. ★ append_to_memory_md does NOT touch content outside markers.
  9. Atomic write produces no .tmp leftover.
 10. Empty suggestion list still produces a well-formed (empty) block.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.auto_suggestions import (
    MARKER_END,
    MARKER_START,
    SECTION_HEADING,
    Suggestion,
    WriteReport,
    append_to_memory_md,
    extract_high_value_chunks,
    format_suggestions_block,
)


# ── Test helpers ────────────────────────────────────────────────────
class _FakeRow(dict):
    """sqlite3.Row stand-in: supports row['col'] access."""

    def __init__(self, **kw):
        super().__init__(**kw)


def _mk_row(
    *,
    chunk_id: str,
    text: str,
    ts: int,
    asserted_by_user: int = 0,
    contradicted: int = 0,
) -> _FakeRow:
    return _FakeRow(
        id=chunk_id,
        text=text,
        ts=ts,
        asserted_by_user=asserted_by_user,
        contradicted_by_later_session=contradicted,
    )


# ── Ranking ──────────────────────────────────────────────────────────
class ExtractHighValueChunksTests(unittest.TestCase):
    def setUp(self):
        # Anchor "now" so age windows are deterministic across slow CI
        self.now = 2_000_000_000
        self.day = 24 * 3600

    def test_user_asserted_ranks_above_auto(self):
        rows = [
            _mk_row(chunk_id="auto1",
                    text="The team is using Postgres.",
                    ts=self.now - 30 * self.day,
                    asserted_by_user=0),
            _mk_row(chunk_id="user1",
                    text="The team is using Postgres.",
                    ts=self.now - 30 * self.day,
                    asserted_by_user=1),
        ]
        out = extract_high_value_chunks(rows, max_n=10, now=self.now)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].chunk_id, "user1")
        self.assertIn("user-asserted", out[0].reasons)

    def test_detects_decision_pattern(self):
        rows = [
            _mk_row(chunk_id="c1",
                    text="We chose Postgres over MySQL for the audit log.",
                    ts=self.now - 30 * self.day),
            _mk_row(chunk_id="c2",
                    text="The audit log is in a database somewhere.",
                    ts=self.now - 30 * self.day),
        ]
        out = extract_high_value_chunks(rows, max_n=10, now=self.now)
        # "we chose" boosted vs the bland baseline
        ids = [s.chunk_id for s in out]
        self.assertEqual(ids[0], "c1")
        self.assertIn("decision-phrase", out[0].reasons)

    def test_decision_pattern_variants(self):
        # Each phrase should boost a chunk above a bland baseline
        cases = [
            "we decided to migrate to Rust",
            "decided to keep the current schema",
            "switched from MySQL to Postgres",
            "migrated from sqlite to postgres",
            "picked Postgres over MySQL",
            "chose Rust over Go",
            "settled on Postgres for v1",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                rows = [
                    _mk_row(chunk_id="boost",
                            text=phrase,
                            ts=self.now - 30 * self.day),
                    _mk_row(chunk_id="bland",
                            text="some unrelated text about the database",
                            ts=self.now - 30 * self.day),
                ]
                out = extract_high_value_chunks(rows, max_n=10,
                                                now=self.now)
                self.assertEqual(out[0].chunk_id, "boost")
                self.assertIn("decision-phrase", out[0].reasons)

    def test_filters_chunks_younger_than_7_days(self):
        rows = [
            _mk_row(chunk_id="too_new",
                    text="We chose Postgres",
                    ts=self.now - 3 * self.day,         # 3d old → dropped
                    asserted_by_user=1),
            _mk_row(chunk_id="ok",
                    text="We chose Postgres",
                    ts=self.now - 30 * self.day,
                    asserted_by_user=1),
        ]
        out = extract_high_value_chunks(rows, max_n=10, now=self.now)
        ids = [s.chunk_id for s in out]
        self.assertNotIn("too_new", ids)
        self.assertIn("ok", ids)

    def test_filters_chunks_older_than_180_days(self):
        rows = [
            _mk_row(chunk_id="too_old",
                    text="We chose Postgres",
                    ts=self.now - 200 * self.day,
                    asserted_by_user=1),
            _mk_row(chunk_id="ok",
                    text="We chose Postgres",
                    ts=self.now - 30 * self.day,
                    asserted_by_user=1),
        ]
        out = extract_high_value_chunks(rows, max_n=10, now=self.now)
        ids = [s.chunk_id for s in out]
        self.assertNotIn("too_old", ids)
        self.assertIn("ok", ids)

    def test_skips_contradicted_chunks(self):
        rows = [
            _mk_row(chunk_id="superseded",
                    text="We chose Postgres",
                    ts=self.now - 30 * self.day,
                    asserted_by_user=1,
                    contradicted=1),
        ]
        out = extract_high_value_chunks(rows, max_n=10, now=self.now)
        self.assertEqual(out, [])

    def test_version_token_boost(self):
        rows = [
            _mk_row(chunk_id="versioned",
                    text="Shipped v0.7.27 to production today.",
                    ts=self.now - 30 * self.day),
            _mk_row(chunk_id="bland",
                    text="Shipped to production today.",
                    ts=self.now - 30 * self.day),
        ]
        out = extract_high_value_chunks(rows, max_n=10, now=self.now)
        self.assertEqual(out[0].chunk_id, "versioned")
        self.assertIn("version-token", out[0].reasons)

    def test_text_preview_capped(self):
        long_text = "x " * 500  # 1000 chars
        rows = [_mk_row(chunk_id="big", text=long_text,
                        ts=self.now - 30 * self.day,
                        asserted_by_user=1)]
        out = extract_high_value_chunks(rows, max_n=10, now=self.now)
        # 200-char preview + ellipsis, no full 1000-char dump
        self.assertLessEqual(len(out[0].text_preview), 210)
        self.assertTrue(out[0].text_preview.endswith("..."))

    def test_max_n_caps_output(self):
        rows = [
            _mk_row(chunk_id=f"c{i}",
                    text="we chose option " + str(i),
                    ts=self.now - 30 * self.day)
            for i in range(50)
        ]
        out = extract_high_value_chunks(rows, max_n=5, now=self.now)
        self.assertEqual(len(out), 5)


# ── Block formatting ────────────────────────────────────────────────
class FormatSuggestionsBlockTests(unittest.TestCase):
    def test_block_has_both_markers(self):
        block = format_suggestions_block([], project_name="demo")
        self.assertIn(MARKER_START, block)
        self.assertIn(MARKER_END, block)
        self.assertLess(block.index(MARKER_START), block.index(MARKER_END))
        self.assertIn(SECTION_HEADING, block)

    def test_block_is_deterministic(self):
        s = [
            Suggestion(chunk_id="aaa", ts=1_700_000_000,
                       text_preview="we chose Postgres",
                       score=6.0, reasons=["user-asserted",
                                            "decision-phrase"]),
            Suggestion(chunk_id="bbb", ts=1_700_000_000,
                       text_preview="shipped v0.7.27",
                       score=4.5, reasons=["version-token"]),
        ]
        a = format_suggestions_block(s, project_name="demo")
        b = format_suggestions_block(s, project_name="demo")
        self.assertEqual(a, b)

    def test_empty_block_is_well_formed(self):
        block = format_suggestions_block([], project_name="demo")
        self.assertIn(MARKER_START, block)
        self.assertIn(MARKER_END, block)
        # Has the friendly empty marker
        self.assertIn("No high-value candidates", block)


# ── Atomic write — REPLACE / APPEND / preserve outside ──────────────
class AppendToMemoryMdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "MEMORY.md"

    def tearDown(self):
        self.tmp.cleanup()

    def _block(self, body: str = "TEST_BODY") -> str:
        return f"{MARKER_START}\n{SECTION_HEADING}\n\n{body}\n{MARKER_END}"

    def test_creates_section_when_markers_absent(self):
        self.path.write_text("# Operator notes\n\nhello\n", encoding="utf-8")
        report = append_to_memory_md(self.path, self._block(), 3)
        self.assertTrue(report.was_appended)
        self.assertFalse(report.was_replaced)
        self.assertEqual(report.errors, [])
        self.assertGreater(report.bytes_written, 0)

        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# Operator notes", text)
        self.assertIn("hello", text)
        self.assertIn(MARKER_START, text)
        self.assertIn("TEST_BODY", text)

    def test_creates_file_when_missing(self):
        # No file at all → create it containing only the block
        self.assertFalse(self.path.exists())
        report = append_to_memory_md(self.path, self._block(), 0)
        self.assertTrue(report.was_appended)
        self.assertTrue(self.path.exists())
        text = self.path.read_text(encoding="utf-8")
        self.assertIn(MARKER_START, text)

    def test_replaces_content_between_markers(self):
        # Pre-existing markers with old auto content
        original = (
            "# Header\n\n"
            f"{MARKER_START}\nold auto content\n{MARKER_END}\n"
            "# Footer\n"
        )
        self.path.write_text(original, encoding="utf-8")

        report = append_to_memory_md(self.path, self._block("NEW_BODY"), 1)
        self.assertTrue(report.was_replaced)
        self.assertFalse(report.was_appended)

        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("old auto content", text)
        self.assertIn("NEW_BODY", text)
        # Header + footer still there
        self.assertIn("# Header", text)
        self.assertIn("# Footer", text)

    def test_does_not_touch_content_outside_markers(self):
        # ★ CRITICAL SAFETY TEST ★
        # MEMORY.md = header + markers w/ old auto + operator-curated footer.
        # After append, header and footer must be byte-for-byte preserved.
        header = (
            "# Project Bible (DO NOT TOUCH)\n"
            "\n"
            "## Section 1: load-bearing decisions\n"
            "- We chose Postgres on 2026-01-01.\n"
            "- We migrated to Rust on 2026-04-01.\n"
            "\n"
        )
        old_auto = (
            f"{MARKER_START}\n"
            "## Auto-extracted suggestions (operator review)\n"
            "\n"
            "- old line 1\n"
            "- old line 2\n"
            "\n"
            f"{MARKER_END}\n"
        )
        footer = (
            "\n"
            "## Section 99: post-marker operator notes\n"
            "- This footer must survive every /memory-curate run.\n"
            "- Special chars preserved: < > & \" ' \n"
            "- Trailing whitespace at EOL    \n"
            "- Final line, no trailing newline"
        )
        original = header + old_auto + footer
        # Write raw bytes so our line endings survive the round-trip
        # exactly as authored — Path.write_text on Windows would
        # translate \n → \r\n via the text layer, defeating the test.
        self.path.write_bytes(original.encode("utf-8"))
        original_bytes = self.path.read_bytes()

        # Compute byte-positions of the marker pair in the *original*
        # so we can compare what survives outside that region against
        # the actual original bytes (not the test's variable concat,
        # whose newline accounting is easy to get wrong).
        original_text = original_bytes.decode("utf-8")
        orig_marker_start_idx = original_text.index(MARKER_START)
        orig_marker_end_idx = (
            original_text.index(MARKER_END) + len(MARKER_END)
        )
        original_header_bytes = original_bytes[:orig_marker_start_idx]
        original_footer_bytes = original_bytes[orig_marker_end_idx:]

        # Run an append with new content
        new_block = self._block("BRAND_NEW_AUTO_BODY")
        report = append_to_memory_md(self.path, new_block, 7)
        self.assertTrue(report.was_replaced)
        self.assertEqual(report.errors, [])

        result_bytes = self.path.read_bytes()
        result_text = result_bytes.decode("utf-8")

        # Header preserved byte-for-byte (everything before MARKER_START)
        marker_start_idx = result_text.index(MARKER_START)
        self.assertEqual(
            result_bytes[:marker_start_idx],
            original_header_bytes,
            "header content was modified outside markers",
        )

        # Footer preserved byte-for-byte (everything after MARKER_END)
        marker_end_idx = result_text.index(MARKER_END) + len(MARKER_END)
        self.assertEqual(
            result_bytes[marker_end_idx:],
            original_footer_bytes,
            "footer content was modified outside markers",
        )

        # And the body between markers WAS swapped
        self.assertIn("BRAND_NEW_AUTO_BODY", result_text)
        self.assertNotIn("old line 1", result_text)
        self.assertNotIn("old line 2", result_text)

        # Sanity: original and result differ only in the marker region
        self.assertNotEqual(original_bytes, result_bytes)

    def test_idempotent_when_block_unchanged(self):
        # Two runs of the SAME block → same final bytes
        self.path.write_text("# Top\n", encoding="utf-8")
        block = self._block("STABLE")
        append_to_memory_md(self.path, block, 1)
        bytes_a = self.path.read_bytes()
        append_to_memory_md(self.path, block, 1)
        bytes_b = self.path.read_bytes()
        self.assertEqual(bytes_a, bytes_b)

    def test_atomic_write_no_tmp_leftover(self):
        self.path.write_text("# Top\n", encoding="utf-8")
        append_to_memory_md(self.path, self._block(), 0)

        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(
            leftovers, [],
            f"unexpected .tmp leftovers: {leftovers}",
        )

    def test_empty_suggestions_produces_well_formed_block(self):
        # End-to-end: extract → format → write, with zero candidates.
        block = format_suggestions_block([], project_name="demo")
        self.assertIn(MARKER_START, block)
        self.assertIn(MARKER_END, block)

        self.path.write_text("# Top\n", encoding="utf-8")
        report = append_to_memory_md(self.path, block, 0)
        self.assertTrue(report.was_appended)
        self.assertEqual(report.errors, [])

        text = self.path.read_text(encoding="utf-8")
        self.assertIn(MARKER_START, text)
        self.assertIn(MARKER_END, text)
        self.assertIn("# Top", text)
        self.assertIn("No high-value candidates", text)

    def test_refuses_to_write_block_without_markers(self):
        # Defensive: if caller hands us a malformed block, refuse rather
        # than silently corrupt the file.
        self.path.write_text("# Top\n", encoding="utf-8")
        original = self.path.read_bytes()
        report = append_to_memory_md(
            self.path, "no markers here at all", 0,
        )
        self.assertGreater(len(report.errors), 0)
        self.assertFalse(report.was_appended)
        self.assertFalse(report.was_replaced)
        # File unchanged
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
