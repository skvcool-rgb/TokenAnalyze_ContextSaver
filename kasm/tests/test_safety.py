"""Unit tests for lib.safety — panic switch, diagnostic logging, watchdog."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.safety import (
    DIAGNOSTIC_FILENAME,
    ENV_DEBUG,
    ENV_DISABLE,
    MAX_LOG_BYTES,
    is_debug,
    is_disabled,
    log_event,
    run_safely,
)


class _EnvSandbox:
    """Save and restore env vars between tests."""

    def __init__(self, *keys):
        self._keys = keys
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for k in self._keys:
            self._saved[k] = os.environ.get(k)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class IsDisabledTests(unittest.TestCase):
    def test_default_off(self):
        with _EnvSandbox(ENV_DISABLE):
            os.environ.pop(ENV_DISABLE, None)
            self.assertFalse(is_disabled())

    def test_set_to_1(self):
        with _EnvSandbox(ENV_DISABLE):
            os.environ[ENV_DISABLE] = "1"
            self.assertTrue(is_disabled())

    def test_other_truthy_value_doesnt_disable(self):
        # Strict "1" only — avoid surprising operators with truthy strings
        with _EnvSandbox(ENV_DISABLE):
            os.environ[ENV_DISABLE] = "true"
            self.assertFalse(is_disabled())
            os.environ[ENV_DISABLE] = "yes"
            self.assertFalse(is_disabled())
            os.environ[ENV_DISABLE] = "0"
            self.assertFalse(is_disabled())


class IsDebugTests(unittest.TestCase):
    def test_default_off(self):
        with _EnvSandbox(ENV_DEBUG):
            os.environ.pop(ENV_DEBUG, None)
            self.assertFalse(is_debug())

    def test_on_when_set(self):
        with _EnvSandbox(ENV_DEBUG):
            os.environ[ENV_DEBUG] = "1"
            self.assertTrue(is_debug())


class LogEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_proj = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old_proj is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._old_proj
        self.tmp.cleanup()

    def _log_path(self) -> Path:
        return Path(self.tmp.name) / ".kos-memory" / DIAGNOSTIC_FILENAME

    def test_writes_jsonl_record(self):
        log_event("test_event", level="info", foo="bar")
        path = self._log_path()
        self.assertTrue(path.exists())
        line = path.read_text(encoding="utf-8").strip().splitlines()[0]
        rec = json.loads(line)
        self.assertEqual(rec["event"], "test_event")
        self.assertEqual(rec["level"], "info")
        self.assertEqual(rec["foo"], "bar")
        self.assertIn("ts", rec)
        self.assertIn("pid", rec)

    def test_debug_suppressed_when_env_unset(self):
        with _EnvSandbox(ENV_DEBUG):
            os.environ.pop(ENV_DEBUG, None)
            log_event("debug_event", level="debug")
        path = self._log_path()
        if path.exists():
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("debug_event", content)

    def test_debug_emitted_when_env_set(self):
        with _EnvSandbox(ENV_DEBUG):
            os.environ[ENV_DEBUG] = "1"
            log_event("debug_event", level="debug")
        path = self._log_path()
        self.assertTrue(path.exists())
        self.assertIn("debug_event", path.read_text(encoding="utf-8"))

    def test_never_raises_on_unwriteable_dir(self):
        # Point project to a path we can't write to (an existing FILE)
        bad = Path(self.tmp.name) / "not-a-dir.txt"
        bad.write_text("x", encoding="utf-8")
        os.environ["CLAUDE_PROJECT_DIR"] = str(bad)
        # Must not raise
        log_event("should_not_raise", level="info")


class RunSafelyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_proj = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old_proj is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._old_proj
        self.tmp.cleanup()

    def _log_path(self) -> Path:
        return Path(self.tmp.name) / ".kos-memory" / DIAGNOSTIC_FILENAME

    def test_disabled_short_circuits(self):
        called = []

        def main():
            called.append(True)
            return 0

        with _EnvSandbox(ENV_DISABLE):
            os.environ[ENV_DISABLE] = "1"
            rc = run_safely(main, hook_name="testhook", timeout_s=5.0)
        self.assertEqual(rc, 0)
        self.assertEqual(called, [])  # never invoked

    def test_returns_0_on_success(self):
        rc = run_safely(lambda: 0, hook_name="ok_hook", timeout_s=5.0)
        self.assertEqual(rc, 0)

    def test_returns_0_on_exception(self):
        def boom():
            raise RuntimeError("synthetic boom")

        rc = run_safely(boom, hook_name="boom_hook", timeout_s=5.0)
        self.assertEqual(rc, 0)
        # Exception should be logged
        path = self._log_path()
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("hook_exception", content)
        self.assertIn("synthetic boom", content)
        self.assertIn("boom_hook", content)

    def test_returns_0_on_keyboard_interrupt(self):
        def interrupt():
            raise KeyboardInterrupt()

        rc = run_safely(interrupt, hook_name="kb_hook", timeout_s=5.0)
        self.assertEqual(rc, 0)

    def test_returns_0_when_main_returns_non_zero(self):
        # Hook conventions allow returning non-zero from main() but we
        # guarantee Claude Code sees exit 0 regardless.
        rc = run_safely(lambda: 42, hook_name="rc_hook", timeout_s=5.0)
        self.assertEqual(rc, 0)

    def test_systemexit_swallowed(self):
        def sysex():
            sys.exit(7)

        rc = run_safely(sysex, hook_name="sysexit_hook", timeout_s=5.0)
        self.assertEqual(rc, 0)

    def test_logs_elapsed_time(self):
        with _EnvSandbox(ENV_DEBUG):
            os.environ[ENV_DEBUG] = "1"  # hook_end is debug-level

            def slow_main():
                time.sleep(0.05)
                return 0

            run_safely(slow_main, hook_name="slow_hook", timeout_s=5.0)

        path = self._log_path()
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        self.assertIn("slow_hook", content)


class WatchdogIntegrationTests(unittest.TestCase):
    """Watchdog uses os._exit on fire which makes it untestable in
    process. We verify the timer setup itself works without firing."""

    def test_watchdog_does_not_fire_for_fast_main(self):
        # 5s timeout, main returns immediately — watchdog cancels cleanly
        rc = run_safely(lambda: 0, hook_name="fast", timeout_s=5.0)
        self.assertEqual(rc, 0)

    def test_zero_timeout_skips_watchdog(self):
        # timeout_s <= 0 should skip watchdog setup entirely
        rc = run_safely(lambda: 0, hook_name="notimer", timeout_s=0)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
