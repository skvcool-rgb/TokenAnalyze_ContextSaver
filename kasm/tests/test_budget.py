"""Unit tests for lib.budget — daily/session throttling."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.budget import (
    DEFAULT_DAILY_RECALL_CAP,
    DEFAULT_DAILY_TOKEN_CAP,
    DEFAULT_PER_SESSION_RECALL_CAP,
    Budget,
)


class BudgetBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "budget.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_budget_allows_recall(self):
        b = Budget(self.path)
        ok, reason = b.can_recall()
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_record_recall_increments_state(self):
        b = Budget(self.path)
        b.record_recall(tokens=1000)
        st = b.status()
        self.assertEqual(st["tokens_used"], 1000)
        self.assertEqual(st["recalls_today"], 1)

    def test_token_cap_blocks(self):
        b = Budget(self.path)
        b.record_recall(tokens=DEFAULT_DAILY_TOKEN_CAP)
        ok, reason = b.can_recall()
        self.assertFalse(ok)
        self.assertIn("token", reason.lower())

    def test_estimated_tokens_blocks_pre_call(self):
        b = Budget(self.path)
        b.record_recall(tokens=DEFAULT_DAILY_TOKEN_CAP - 100)
        ok, reason = b.can_recall(estimated_tokens=200)
        self.assertFalse(ok)
        self.assertIn("exceed", reason.lower())

    def test_recall_count_cap_blocks(self):
        b = Budget(self.path)
        for _ in range(DEFAULT_DAILY_RECALL_CAP):
            b.record_recall(tokens=1)
        ok, reason = b.can_recall()
        self.assertFalse(ok)
        self.assertIn("recall", reason.lower())

    def test_session_cap_blocks(self):
        b = Budget(self.path)
        for _ in range(DEFAULT_PER_SESSION_RECALL_CAP):
            b.record_recall(session_id="sess1", tokens=1)
        ok, reason = b.can_recall(session_id="sess1")
        self.assertFalse(ok)
        self.assertIn("sess1", reason)

    def test_session_cap_per_session_isolated(self):
        b = Budget(self.path)
        for _ in range(DEFAULT_PER_SESSION_RECALL_CAP):
            b.record_recall(session_id="sess1", tokens=1)
        ok, _ = b.can_recall(session_id="sess2")
        self.assertTrue(ok)

    def test_record_recall_accepts_both_tokens_and_tokens_used(self):
        b = Budget(self.path)
        b.record_recall(tokens_used=500)
        b.record_recall(tokens=500)
        self.assertEqual(b.status()["tokens_used"], 1000)

    def test_status_returns_dict_copy(self):
        b = Budget(self.path)
        b.record_recall(tokens=100)
        st = b.status()
        st["tokens_used"] = 99999  # mutate the copy
        # Re-read; original should be unchanged
        self.assertEqual(b.status()["tokens_used"], 100)


class BudgetPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "budget.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_persists_across_instances(self):
        b1 = Budget(self.path)
        b1.record_recall(tokens=2000)
        b2 = Budget(self.path)
        self.assertEqual(b2.status()["tokens_used"], 2000)

    def test_atomic_write_uses_tmp_file(self):
        b = Budget(self.path)
        b.record_recall(tokens=100)
        # No leftover .tmp file
        tmp = self.path.with_suffix(".json.tmp")
        self.assertFalse(tmp.exists())

    def test_corrupt_json_resets_to_empty(self):
        self.path.write_text("garbage{not json", encoding="utf-8")
        b = Budget(self.path)
        # Should silently reset for today
        st = b.status()
        self.assertEqual(st.get("tokens_used", 0), 0)


class BudgetRolloverTests(unittest.TestCase):
    def test_new_day_resets_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            # Manually write yesterday's state
            yesterday = "2020-01-01"
            path.write_text(json.dumps({
                "date": yesterday,
                "tokens_used": 99999,
                "recalls_today": 100,
                "sessions": {"old": {"recalls": 99}},
            }), encoding="utf-8")
            b = Budget(path)
            st = b.status()
            self.assertEqual(st["tokens_used"], 0)
            self.assertEqual(st["recalls_today"], 0)
            self.assertNotEqual(st["date"], yesterday)


if __name__ == "__main__":
    unittest.main()
