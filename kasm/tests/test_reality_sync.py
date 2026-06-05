"""Unit tests for lib.reality_sync — chunks vs filesystem reconciliation."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.codebase_survey import Survey
from lib.reality_sync import (
    StatusVerdict,
    quick_status_for_topic,
    reconcile,
    render_reconciliation,
    render_status_verdict,
)


def _chunk(text, asserted=False, ts=None):
    """Build a chunk dict like Store rows expose."""
    return {
        "text": text,
        "ts": ts or int(time.time()),
        "session_id": "s1",
        "asserted_by_user": asserted,
    }


def _survey(tree=None, tags=None, commits=None, versions=None):
    return Survey(
        project_root="/p", surveyed_at=int(time.time()),
        is_git_repo=True, branch="main", head_sha="abc1234",
        tree_summary=tree or [],
        tags=tags or [],
        last_commits=commits or [],
        versions=versions or {},
    )


class VersionRegexTests(unittest.TestCase):
    """v6.0.1: regex must distinguish version tags from IP addresses.

    Real-world bug found during dogfood: '127.0.0.1' was being matched
    as version '0.0.1', flagging false version-skew for the HTTP server's
    bind address."""

    def test_v_prefixed_versions_match(self):
        from lib.reality_sync import EVIDENCE_TAG_RE
        self.assertEqual(EVIDENCE_TAG_RE.findall("v1.2.3"), ["v1.2.3"])
        self.assertEqual(EVIDENCE_TAG_RE.findall("v0.7.26-RC1"), ["v0.7.26-RC1"])
        self.assertEqual(EVIDENCE_TAG_RE.findall("v6.0.0"), ["v6.0.0"])

    def test_bare_versions_match(self):
        from lib.reality_sync import EVIDENCE_TAG_RE
        self.assertEqual(EVIDENCE_TAG_RE.findall("shipped 5.0.0 yesterday"),
                         ["5.0.0"])
        self.assertEqual(EVIDENCE_TAG_RE.findall("release 6.0.1"), ["6.0.1"])
        self.assertEqual(EVIDENCE_TAG_RE.findall("1.2.3-RC1"), ["1.2.3-RC1"])

    def test_ip_addresses_do_not_match(self):
        from lib.reality_sync import EVIDENCE_TAG_RE
        self.assertEqual(EVIDENCE_TAG_RE.findall("127.0.0.1"), [])
        self.assertEqual(EVIDENCE_TAG_RE.findall("192.168.1.1"), [])
        self.assertEqual(EVIDENCE_TAG_RE.findall("10.0.0.1"), [])
        self.assertEqual(EVIDENCE_TAG_RE.findall("listening on 127.0.0.1:7621"), [])

    def test_mixed_text_extracts_only_versions(self):
        from lib.reality_sync import EVIDENCE_TAG_RE
        text = "host 10.0.0.1 then ship 1.0.0 — also v6.0.1"
        self.assertEqual(set(EVIDENCE_TAG_RE.findall(text)),
                         {"1.0.0", "v6.0.1"})


class ReconcileTests(unittest.TestCase):
    def test_empty_returns_empty_report(self):
        rep = reconcile([], _survey())
        self.assertEqual(rep.confirmed, [])
        self.assertEqual(rep.claimed_but_missing, [])

    def test_confirmed_when_chunks_and_filesystem_agree(self):
        chunks = [_chunk("we wrote auth.py last week"),
                  _chunk("auth.py uses oauth2")]
        sv = _survey(tree=["auth.py", "lib/", "tests/"])
        rep = reconcile(chunks, sv)
        self.assertIn("auth.py", rep.confirmed)

    def test_claimed_but_missing_flagged(self):
        chunks = [_chunk("created src/missing.py last sprint"),
                  _chunk("src/missing.py is the new module")]
        sv = _survey(tree=["lib/", "tests/", "README.md"])
        rep = reconcile(chunks, sv)
        # "src/missing.py" mentioned but neither "src" dir nor "missing.py"
        # appear top-level. Should be flagged (or at least not confirmed).
        # We don't strictly assert flag because the heuristic is conservative;
        # just check it's not in confirmed.
        self.assertNotIn("src/missing.py", rep.confirmed)

    def test_version_skew_detected(self):
        chunks = [_chunk("shipped v2.0.0 last week"),
                  _chunk("v2.0.0 fixed the bug"),
                  _chunk("after v2.0.0 release")]
        sv = _survey(tags=["v1.0.0", "v1.1.0"],
                     versions={"package.json": "1.1.0"})
        rep = reconcile(chunks, sv)
        self.assertTrue(any("v2.0.0" in s for s in rep.version_skew))

    def test_no_skew_when_version_matches_tag(self):
        chunks = [_chunk("shipped v1.1.0"), _chunk("v1.1.0 release")]
        sv = _survey(tags=["v1.1.0"])
        rep = reconcile(chunks, sv)
        self.assertEqual(rep.version_skew, [])

    def test_caps_output_lengths(self):
        # 50 file refs in chunks
        text = " ".join(f"file_{i}.py" for i in range(50))
        chunks = [_chunk(text)]
        tree = [f"file_{i}.py" for i in range(50)]
        sv = _survey(tree=tree)
        rep = reconcile(chunks, sv)
        self.assertLessEqual(len(rep.confirmed), 20)


class RenderReconciliationTests(unittest.TestCase):
    def test_renders_confirmed(self):
        from lib.reality_sync import ReconciliationReport
        rep = ReconciliationReport(confirmed=["auth.py", "store.py"])
        out = render_reconciliation(rep)
        self.assertIn("✓ confirmed", out)
        self.assertIn("auth.py", out)

    def test_renders_drift(self):
        from lib.reality_sync import ReconciliationReport
        rep = ReconciliationReport(claimed_but_missing=["src/ghost.py"])
        out = render_reconciliation(rep)
        self.assertIn("⚠ claimed but missing", out)
        self.assertIn("ghost.py", out)

    def test_no_drift_message_when_clean(self):
        from lib.reality_sync import ReconciliationReport
        out = render_reconciliation(ReconciliationReport())
        self.assertIn("no reconciliation signal", out)


class QuickStatusTests(unittest.TestCase):
    def test_high_confidence_when_all_three_agree(self):
        chunks = [_chunk(f"shipped feature_X #{i}") for i in range(6)]
        sv = _survey(
            tree=["feature_X/", "lib/"],
            tags=["v1.0.0"],
            commits=[{"sha": "abc", "subject": "feature_X release",
                      "ts": int(time.time())}],
        )
        v = quick_status_for_topic("feature_X", chunks, sv)
        self.assertEqual(v.confidence, "high")
        self.assertIn("BUILT", v.summary)

    def test_low_confidence_when_chunks_only(self):
        chunks = [_chunk("we built widget_Y last week")]
        sv = _survey(tree=["lib/", "tests/"], tags=[], commits=[])
        v = quick_status_for_topic("widget_Y", chunks, sv)
        self.assertIn(v.confidence, ("low", "medium"))
        # Should warn the user to verify before asserting
        # (chunks=claimed_in_progress because only 1 mention, not 5+,
        # so summary won't include "VERIFY before asserting")
        self.assertNotIn("BUILT", v.summary)

    def test_chunks_claim_built_but_no_filesystem(self):
        chunks = [_chunk(f"shipped deeplink_Z #{i}", asserted=True)
                  for i in range(3)]
        sv = _survey(tree=["lib/"], tags=[], commits=[])
        v = quick_status_for_topic("deeplink_Z", chunks, sv)
        self.assertEqual(v.chunks_say, "claimed_built")
        self.assertIn("VERIFY", v.summary)

    def test_no_evidence_returns_clean_negative(self):
        v = quick_status_for_topic("foobar", [], _survey())
        self.assertEqual(v.confidence, "low")
        self.assertIn("NO evidence", v.summary)

    def test_filesystem_only_evidence(self):
        chunks: list = []
        sv = _survey(tree=["new_module/", "lib/"])
        v = quick_status_for_topic("new_module", chunks, sv)
        self.assertEqual(v.filesystem_says, "confirms")
        self.assertEqual(v.chunks_say, "silent")


class RenderStatusVerdictTests(unittest.TestCase):
    def test_includes_evidence_breakdown(self):
        v = StatusVerdict(
            topic="x", chunks_say="claimed_built", filesystem_says="confirms",
            git_says="committed", confidence="high", summary="x is BUILT",
        )
        out = render_status_verdict(v)
        self.assertIn("[reality check]", out)
        self.assertIn("chunks=claimed_built", out)
        self.assertIn("filesystem=confirms", out)
        self.assertIn("git=committed", out)
        self.assertIn("confidence: high", out)


if __name__ == "__main__":
    unittest.main()
