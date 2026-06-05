"""Unit tests for lib.test_runner — framework detection + bounded
test execution.

These tests use real subprocesses (no monkey-patching of subprocess.run)
so we verify the actual security model: timeouts honored, never raises,
parses real tool output."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.test_runner import (
    DEFAULT_COLLECT_TIMEOUT_S,
    FRAMEWORK_CARGO,
    FRAMEWORK_GO,
    FRAMEWORK_JEST,
    FRAMEWORK_MOCHA,
    FRAMEWORK_PYTEST,
    FRAMEWORK_UNITTEST,
    FRAMEWORK_VITEST,
    CollectResult,
    Framework,
    RunResult,
    detect_framework,
    is_run_tests_enabled,
    run_collect_only,
    run_full_suite,
)


# ---- Detection -----------------------------------------------------------

class DetectFrameworkTests(unittest.TestCase):
    def test_pytest_from_pytest_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pytest.ini").write_text(
                "[pytest]\n", encoding="utf-8"
            )
            fw = detect_framework(tmp)
            self.assertIsNotNone(fw)
            self.assertEqual(fw.name, FRAMEWORK_PYTEST)
            self.assertIn("--collect-only", fw.command)

    def test_pytest_from_pyproject_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_text(textwrap.dedent("""
                [project]
                name = "demo"

                [tool.pytest.ini_options]
                testpaths = ["tests"]
            """), encoding="utf-8")
            fw = detect_framework(tmp)
            self.assertIsNotNone(fw)
            self.assertEqual(fw.name, FRAMEWORK_PYTEST)

    def test_pytest_from_conftest(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "conftest.py").write_text("", encoding="utf-8")
            fw = detect_framework(tmp)
            self.assertIsNotNone(fw)
            self.assertEqual(fw.name, FRAMEWORK_PYTEST)

    def test_jest_from_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text(json.dumps({
                "name": "demo",
                "scripts": {"test": "jest --ci"},
            }), encoding="utf-8")
            fw = detect_framework(tmp)
            self.assertIsNotNone(fw)
            self.assertEqual(fw.name, FRAMEWORK_JEST)

    def test_vitest_from_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text(json.dumps({
                "scripts": {"test": "vitest run"},
            }), encoding="utf-8")
            fw = detect_framework(tmp)
            self.assertEqual(fw.name, FRAMEWORK_VITEST)

    def test_mocha_from_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text(json.dumps({
                "scripts": {"test": "mocha test/**/*.js"},
            }), encoding="utf-8")
            fw = detect_framework(tmp)
            self.assertEqual(fw.name, FRAMEWORK_MOCHA)

    def test_cargo_from_cargo_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Cargo.toml").write_text(
                '[package]\nname = "demo"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            fw = detect_framework(tmp)
            self.assertEqual(fw.name, FRAMEWORK_CARGO)
            self.assertIn("--no-run", fw.command)

    def test_go_from_go_mod(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "go.mod").write_text(
                "module demo\n\ngo 1.22\n", encoding="utf-8"
            )
            fw = detect_framework(tmp)
            self.assertEqual(fw.name, FRAMEWORK_GO)

    def test_unittest_fallback_when_tests_dir_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / "tests"
            tests.mkdir()
            (tests / "test_thing.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_a(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            fw = detect_framework(tmp)
            self.assertIsNotNone(fw)
            self.assertEqual(fw.name, FRAMEWORK_UNITTEST)

    def test_pytest_wins_over_unittest_when_both_signals_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / "tests"
            tests.mkdir()
            (tests / "test_thing.py").write_text("", encoding="utf-8")
            (Path(tmp) / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            fw = detect_framework(tmp)
            self.assertEqual(fw.name, FRAMEWORK_PYTEST)

    def test_returns_none_for_empty_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = detect_framework(tmp)
            self.assertIsNone(fw)

    def test_returns_none_for_nonexistent_path(self):
        fw = detect_framework("/path/that/does/not/exist/abc123")
        self.assertIsNone(fw)


# ---- Collect-only --------------------------------------------------------

class CollectOnlyTests(unittest.TestCase):
    def test_collect_on_kos_memory_repo_returns_273_plus(self):
        """Smoke test: drive collect-only against this repo itself."""
        fw = detect_framework(PLUGIN_ROOT)
        self.assertIsNotNone(fw)
        # kos-memory-v4 has no pytest config — should detect as unittest
        self.assertEqual(fw.name, FRAMEWORK_UNITTEST)
        result = run_collect_only(PLUGIN_ROOT, fw, timeout_s=30)
        self.assertEqual(result.framework_name, FRAMEWORK_UNITTEST)
        # The repo currently has 273 passing tests; discovery counts
        # at least that many test ids.
        self.assertGreaterEqual(
            result.test_count, 273,
            f"expected >=273 tests, got {result.test_count}; "
            f"errors={result.parse_errors}; tail={result.raw_output_tail[-200:]}"
        )
        self.assertEqual(result.parse_errors, [])

    def test_collect_no_framework_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_collect_only(tmp, None, timeout_s=5)
        self.assertIsNone(result.framework_name)
        self.assertEqual(result.test_count, 0)
        self.assertEqual(result.parse_errors, [])

    def test_collect_handles_nonexistent_command(self):
        """A Framework whose `command` points at a missing binary must
        not raise — we surface the error in parse_errors."""
        bad_fw = Framework(
            name="cargo",
            command=["definitely-not-a-real-binary-zxqwop"],
            full_command=["definitely-not-a-real-binary-zxqwop"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run_collect_only(tmp, bad_fw, timeout_s=5)
        self.assertEqual(result.framework_name, "cargo")
        self.assertEqual(result.test_count, 0)
        self.assertTrue(result.parse_errors,
                        "expected error in parse_errors, got empty")

    def test_collect_respects_timeout(self):
        """Point a Framework at a slow Python script and verify timeout
        is honored within a small grace margin."""
        slow_fw = Framework(
            name=FRAMEWORK_PYTEST,
            command=[sys.executable, "-c", "import time; time.sleep(10)"],
            full_command=[sys.executable, "-c", "import time; time.sleep(10)"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            t0 = time.monotonic()
            result = run_collect_only(tmp, slow_fw, timeout_s=1)
            elapsed = time.monotonic() - t0
        # Honor the bound; allow 5s slack for subprocess teardown on
        # slow CI runners.
        self.assertLess(elapsed, 6.0,
                        f"timeout not honored, took {elapsed:.2f}s")
        self.assertTrue(any("timeout" in e.lower()
                            for e in result.parse_errors),
                        f"expected timeout error, got {result.parse_errors}")


# ---- Full-suite ----------------------------------------------------------

class FullSuiteTests(unittest.TestCase):
    def test_run_full_suite_never_raises_on_subprocess_crash(self):
        """Pointing run_full_suite at a non-existent binary must not
        raise — the failure flows through RunResult."""
        bad_fw = Framework(
            name=FRAMEWORK_PYTEST,
            command=["nope-bin-xyzzy"],
            full_command=["nope-bin-xyzzy"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run_full_suite(tmp, bad_fw, timeout_s=5)
        self.assertIsInstance(result, RunResult)
        # Either parsed as 1 failed (exit_code != 0 path) or carried an
        # error string. Either way: it didn't raise.
        self.assertNotEqual(result.exit_code, 0)

    def test_run_full_suite_no_framework_returns_error_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_full_suite(tmp, None, timeout_s=2)
        self.assertIsNone(result.framework_name)
        self.assertTrue(result.errors)

    def test_run_full_suite_unittest_on_synthetic_passing_project(self):
        """End-to-end: build a tiny passing unittest project, run
        full-suite, parse pass/fail counts."""
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_pass.py").write_text(textwrap.dedent("""
                import unittest
                class P(unittest.TestCase):
                    def test_a(self): self.assertEqual(1+1, 2)
                    def test_b(self): self.assertTrue(True)
            """).strip(), encoding="utf-8")
            fw = detect_framework(tmp)
            result = run_full_suite(tmp, fw, timeout_s=15)
        self.assertEqual(result.framework_name, FRAMEWORK_UNITTEST)
        self.assertEqual(result.exit_code, 0)
        self.assertGreaterEqual(result.passed, 2)
        self.assertEqual(result.failed, 0)


# ---- Opt-in gate ---------------------------------------------------------

class IsRunTestsEnabledTests(unittest.TestCase):
    def test_env_var_truthy_values(self):
        truthy = ("1", "true", "yes", "on", "TRUE", "Yes")
        for v in truthy:
            with self.subTest(v=v):
                old = os.environ.get("KOS_MEMORY_RUN_TESTS")
                os.environ["KOS_MEMORY_RUN_TESTS"] = v
                try:
                    self.assertTrue(is_run_tests_enabled())
                finally:
                    if old is None:
                        os.environ.pop("KOS_MEMORY_RUN_TESTS", None)
                    else:
                        os.environ["KOS_MEMORY_RUN_TESTS"] = old

    def test_env_var_falsy_values(self):
        falsy = ("", "0", "false", "no", "off")
        for v in falsy:
            with self.subTest(v=v):
                old = os.environ.get("KOS_MEMORY_RUN_TESTS")
                os.environ["KOS_MEMORY_RUN_TESTS"] = v
                try:
                    self.assertFalse(is_run_tests_enabled())
                finally:
                    if old is None:
                        os.environ.pop("KOS_MEMORY_RUN_TESTS", None)
                    else:
                        os.environ["KOS_MEMORY_RUN_TESTS"] = old

    def test_config_json_run_tests_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            from lib.paths import FILE_CONFIG, ensure_kos_dir
            kos = ensure_kos_dir(tmp, user_level=False)
            cfg = kos / FILE_CONFIG
            cfg.write_text(json.dumps({"run_tests": True}), encoding="utf-8")
            old = os.environ.pop("KOS_MEMORY_RUN_TESTS", None)
            try:
                self.assertTrue(is_run_tests_enabled(tmp))
            finally:
                if old is not None:
                    os.environ["KOS_MEMORY_RUN_TESTS"] = old

    def test_config_json_run_tests_missing_defaults_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            from lib.paths import FILE_CONFIG, ensure_kos_dir
            kos = ensure_kos_dir(tmp, user_level=False)
            (kos / FILE_CONFIG).write_text(json.dumps({}), encoding="utf-8")
            old = os.environ.pop("KOS_MEMORY_RUN_TESTS", None)
            try:
                self.assertFalse(is_run_tests_enabled(tmp))
            finally:
                if old is not None:
                    os.environ["KOS_MEMORY_RUN_TESTS"] = old

    def test_no_project_root_no_env_returns_false(self):
        old = os.environ.pop("KOS_MEMORY_RUN_TESTS", None)
        try:
            self.assertFalse(is_run_tests_enabled())
        finally:
            if old is not None:
                os.environ["KOS_MEMORY_RUN_TESTS"] = old


# ---- Empty-project edge case --------------------------------------------

class EmptyProjectTests(unittest.TestCase):
    def test_empty_project_detect_then_collect_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = detect_framework(tmp)
            self.assertIsNone(fw)
            result = run_collect_only(tmp, fw, timeout_s=5)
        self.assertIsNone(result.framework_name)
        self.assertEqual(result.test_count, 0)
        self.assertEqual(result.parse_errors, [])
        self.assertEqual(result.raw_output_tail, "")


if __name__ == "__main__":
    unittest.main()
