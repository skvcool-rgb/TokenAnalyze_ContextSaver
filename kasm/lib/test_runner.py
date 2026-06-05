"""Test framework detection + bounded test execution.

Closes survey gap #4 ("doesn't run tests"). Two layers:

1. Detection (free, no execution) — `detect_framework()` inspects
   well-known config files. Inspection order:
       1. pytest.ini, pyproject.toml [tool.pytest], conftest.py -> "pytest"
       2. package.json scripts.test mentioning jest/vitest/mocha   -> "jest"/"vitest"/"mocha"
       3. Cargo.toml                                                -> "cargo"
       4. go.mod                                                    -> "go"
       5. tests/ dir with test_*.py files (no pytest config)        -> "unittest"

2. Execution (bounded subprocess, never raises):
   - `run_collect_only()` — enumerate tests, no test code runs.
     Default-on for SessionStart-class hooks because it catches
     "test file syntax broke" without executing user code.
   - `run_full_suite()` — actually runs the suite. Opt-in only,
     gated by env var KOS_MEMORY_RUN_TESTS=1 or config.json
     {"run_tests": true}. Memory-bounded by capturing only the
     output tail (500 chars).

Security model:
    * Subprocess sandbox: every external invocation goes through
      `subprocess.run(..., timeout=...)`. No shell=True. Argument
      vectors are list[str]; no string interpolation into a shell.
    * No env mutation: parent env is forwarded as-is. We do NOT set,
      unset, or rewrite any env var.
    * No fs writes outside the subprocess sandbox: this module never
      writes to disk. The subprocess child may write inside its own
      cwd (project_root) per its own logic — we do not redirect or
      duplicate that I/O.
    * All I/O timeouts: every subprocess.run gets a hard timeout in
      seconds. TimeoutExpired is caught and surfaced in the result.
    * Error containment: every public function returns a result
      dataclass; nothing raises out of the public API. Background
      stdout/stderr is truncated to 500 chars (`raw_output_tail` /
      `output_tail`) so a chatty framework cannot blow the caller's
      memory budget.
    * No network: detection reads files; execution invokes the
      project's local test command. We do not make network calls.

Public API:
    detect_framework(project_root) -> Framework | None
    run_collect_only(project_root, framework, timeout_s=10) -> CollectResult
    run_full_suite(project_root, framework, timeout_s=120) -> RunResult
    is_run_tests_enabled(project_root) -> bool
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---- Constants -----------------------------------------------------------

DEFAULT_COLLECT_TIMEOUT_S = 10
DEFAULT_RUN_TIMEOUT_S = 120
OUTPUT_TAIL_CHARS = 500

# Frameworks we know how to drive
FRAMEWORK_PYTEST = "pytest"
FRAMEWORK_UNITTEST = "unittest"
FRAMEWORK_JEST = "jest"
FRAMEWORK_VITEST = "vitest"
FRAMEWORK_MOCHA = "mocha"
FRAMEWORK_CARGO = "cargo"
FRAMEWORK_GO = "go"


# ---- Dataclasses ---------------------------------------------------------

@dataclass
class Framework:
    """A detected test framework + the commands needed to drive it."""
    name: str
    command: list[str] = field(default_factory=list)        # collect-only
    full_command: list[str] = field(default_factory=list)   # full run
    version: str = ""                                        # if detectable


@dataclass
class CollectResult:
    """Outcome of `run_collect_only`. Never raises."""
    framework_name: str | None = None
    test_count: int = 0
    parse_errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    raw_output_tail: str = ""


@dataclass
class RunResult:
    """Outcome of `run_full_suite`. Never raises."""
    framework_name: str | None = None
    passed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    exit_code: int = -1
    output_tail: str = ""


# ---- Helpers -------------------------------------------------------------

def _tail(text: str, n: int = OUTPUT_TAIL_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[-n:]


def _read_text_safe(p: Path, max_bytes: int = 200_000) -> str:
    try:
        if not p.exists() or not p.is_file():
            return ""
        if p.stat().st_size > max_bytes:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _has_pytest_config(root: Path) -> bool:
    """pytest.ini, pyproject.toml [tool.pytest], or conftest.py at the
    project root all signal pytest is the chosen framework."""
    if (root / "pytest.ini").exists():
        return True
    if (root / "conftest.py").exists():
        return True
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = _read_text_safe(pyproject)
        # Match [tool.pytest], [tool.pytest.ini_options], etc.
        if re.search(r"(?m)^\s*\[tool\.pytest", text):
            return True
    return False


def _detect_node_runner(root: Path) -> str | None:
    """Inspect package.json scripts.test for jest/vitest/mocha keywords."""
    pkg = root / "package.json"
    text = _read_text_safe(pkg)
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    scripts = data.get("scripts") or {}
    test_cmd = (scripts.get("test") or "").lower()
    if not test_cmd:
        return None
    # Order: jest, vitest, mocha (specificity / popularity)
    if "jest" in test_cmd:
        return FRAMEWORK_JEST
    if "vitest" in test_cmd:
        return FRAMEWORK_VITEST
    if "mocha" in test_cmd:
        return FRAMEWORK_MOCHA
    return None


def _has_unittest_layout(root: Path) -> bool:
    """A `tests/` (or `test/`) directory containing test_*.py files,
    used as the unittest fallback when no pytest config is present."""
    for tests_name in ("tests", "test"):
        d = root / tests_name
        if not d.is_dir():
            continue
        try:
            for p in d.iterdir():
                if p.is_file() and p.name.startswith("test_") and p.suffix == ".py":
                    return True
        except (PermissionError, OSError):
            continue
    return False


def _tests_dir(root: Path) -> Path | None:
    """Resolve the unittest discovery root. Prefer `tests/`, fall back
    to `test/`."""
    for n in ("tests", "test"):
        p = root / n
        if p.is_dir():
            return p
    return None


def _python_exe() -> str:
    """The interpreter to drive python-based subprocesses with."""
    return sys.executable or "python"


def _run_subprocess(
    cmd: list[str],
    cwd: str,
    timeout_s: float,
) -> tuple[int, str, str, bool]:
    """Run a subprocess with a hard timeout. Returns
    (exit_code, stdout, stderr, timed_out). Never raises."""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return r.returncode, r.stdout or "", r.stderr or "", False
    except subprocess.TimeoutExpired as e:
        # Try to retrieve any partial output the process produced
        out = ""
        err = ""
        if e.stdout is not None:
            out = e.stdout if isinstance(e.stdout, str) else e.stdout.decode(
                "utf-8", errors="replace")
        if e.stderr is not None:
            err = e.stderr if isinstance(e.stderr, str) else e.stderr.decode(
                "utf-8", errors="replace")
        return -1, out, err, True
    except FileNotFoundError as e:
        return -1, "", f"command not found: {e}", False
    except Exception as e:
        return -1, "", f"subprocess error: {e}", False


# ---- Detection -----------------------------------------------------------

def detect_framework(project_root: str | Path) -> Framework | None:
    """Inspect `project_root` and return the detected Framework, or None.

    Inspection order matches the module docstring. We never execute
    anything during detection — purely file-system inspection.
    """
    try:
        root = Path(project_root).resolve()
    except Exception:
        return None

    if not root.exists() or not root.is_dir():
        return None

    # 1. pytest signals
    if _has_pytest_config(root):
        return Framework(
            name=FRAMEWORK_PYTEST,
            command=[_python_exe(), "-m", "pytest", "--collect-only", "-q"],
            full_command=[_python_exe(), "-m", "pytest", "-q"],
        )

    # 2. node runners
    node = _detect_node_runner(root)
    if node == FRAMEWORK_JEST:
        return Framework(
            name=FRAMEWORK_JEST,
            command=["npx", "jest", "--listTests"],
            full_command=["npm", "test", "--silent"],
        )
    if node == FRAMEWORK_VITEST:
        return Framework(
            name=FRAMEWORK_VITEST,
            command=["npx", "vitest", "list"],
            full_command=["npm", "test", "--silent"],
        )
    if node == FRAMEWORK_MOCHA:
        return Framework(
            name=FRAMEWORK_MOCHA,
            command=["npx", "mocha", "--dry-run"],
            full_command=["npm", "test", "--silent"],
        )

    # 3. cargo
    if (root / "Cargo.toml").exists():
        return Framework(
            name=FRAMEWORK_CARGO,
            command=["cargo", "test", "--no-run"],
            full_command=["cargo", "test"],
        )

    # 4. go
    if (root / "go.mod").exists():
        return Framework(
            name=FRAMEWORK_GO,
            command=["go", "test", "-list", ".*", "./..."],
            full_command=["go", "test", "./..."],
        )

    # 5. unittest fallback (tests/ dir present, no pytest config)
    if _has_unittest_layout(root):
        tdir = _tests_dir(root)
        start = str(tdir) if tdir is not None else "tests"
        # `python -m unittest discover -v` actually RUNS the tests — it
        # has no real --collect-only mode. To enumerate without execution
        # we drive `unittest.TestLoader.discover()` via a tiny -c script
        # which only imports test modules (loads classes / methods) and
        # counts them. No test bodies execute. The script uses a stack
        # iterator instead of a recursive `def` so it fits in a -c arg
        # (no semicolon-separated function definitions).
        collect_script = (
            "import sys, unittest\n"
            "loader = unittest.TestLoader()\n"
            "suite = loader.discover(sys.argv[1], top_level_dir=sys.argv[2])\n"
            "stack = [suite]\n"
            "n = 0\n"
            "while stack:\n"
            "    cur = stack.pop()\n"
            "    if isinstance(cur, unittest.TestSuite):\n"
            "        stack.extend(cur)\n"
            "    else:\n"
            "        n += 1\n"
            "print('TESTS_COLLECTED', n)\n"
            "for e in getattr(loader, 'errors', []) or []:\n"
            "    print('LOAD_ERROR', e)\n"
        )
        return Framework(
            name=FRAMEWORK_UNITTEST,
            command=[_python_exe(), "-c", collect_script, start, str(root)],
            full_command=[_python_exe(), "-m", "unittest", "discover",
                          "--start-directory", start],
        )

    return None


# ---- Output parsers ------------------------------------------------------

# pytest: `--collect-only -q` ends with a line like
#   "127 tests collected in 0.42s"
# or with errors like "ERRORS" headers + "errors during collection".
_PYTEST_COUNT_RE = re.compile(
    r"(?m)^(?:\s*)(\d+)\s+test[s]?\s+collected"
)
_PYTEST_ERROR_RE = re.compile(
    r"(?m)^(ERROR|E\s+).+", re.IGNORECASE
)


def _parse_pytest_collect(stdout: str, stderr: str) -> tuple[int, list[str]]:
    text = stdout + "\n" + stderr
    m = _PYTEST_COUNT_RE.search(text)
    n = int(m.group(1)) if m else 0
    errors: list[str] = []
    # Surface any "errors during collection" / "ERROR" lines, capped at 5
    for line in text.splitlines():
        if "errors during collection" in line.lower():
            errors.append(line.strip())
        elif line.strip().startswith(("ERROR", "E ")):
            errors.append(line.strip())
        if len(errors) >= 5:
            break
    return n, errors


# unittest full-run footer: "Ran N tests in Xs" — used by `_parse_unittest_run`.
_UNITTEST_RAN_RE = re.compile(r"(?m)^Ran\s+(\d+)\s+test[s]?\s+in\b")
# Verbose-line shape (kept for diagnostics, not used by collect parser).
_UNITTEST_VERBOSE_LINE_RE = re.compile(
    r"^\s*\S+\s+\([^)]+\)\s+\.\.\."
)
# Our collect-only one-liner emits "TESTS_COLLECTED N" + zero or more
# "LOAD_ERROR <msg>" lines.
_UNITTEST_COLLECT_COUNT_RE = re.compile(r"(?m)^TESTS_COLLECTED\s+(\d+)\s*$")


def _parse_unittest_collect(stdout: str, stderr: str) -> tuple[int, list[str]]:
    """Parse the output of our unittest collect one-liner. We drive
    TestLoader.discover() in a child process and the child prints
    `TESTS_COLLECTED <n>` plus optional `LOAD_ERROR` lines."""
    text = stdout + "\n" + stderr
    m = _UNITTEST_COLLECT_COUNT_RE.search(text)
    n = int(m.group(1)) if m else 0
    errors: list[str] = []
    for line in text.splitlines():
        if line.startswith("LOAD_ERROR"):
            errors.append(line.strip())
        elif "ImportError" in line or "ModuleNotFoundError" in line:
            errors.append(line.strip())
        elif line.startswith("Traceback"):
            errors.append(line.strip())
        if len(errors) >= 5:
            break
    return n, errors


# Node runners — best-effort. jest --listTests prints one path per line;
# vitest list prints test ids; mocha --dry-run prints test descriptions.
def _parse_node_collect(stdout: str, stderr: str) -> tuple[int, list[str]]:
    lines = [l for l in stdout.splitlines() if l.strip()]
    return len(lines), []


# cargo test --no-run prints "Compiling ..." + counts at end ("test result: ok. 0 passed")
# but the *actual* test count requires cargo test --list. We approximate
# by counting compiled test binaries. Best-effort.
def _parse_cargo_collect(stdout: str, stderr: str) -> tuple[int, list[str]]:
    text = stdout + "\n" + stderr
    # Look for any "running N tests" lines or count "Running" entries
    n = 0
    for line in text.splitlines():
        m = re.search(r"running\s+(\d+)\s+test", line)
        if m:
            n += int(m.group(1))
    return n, []


# go test -list .* ./... prints one test name per line.
def _parse_go_collect(stdout: str, stderr: str) -> tuple[int, list[str]]:
    lines = [l for l in stdout.splitlines() if l.strip() and l.startswith("Test")]
    return len(lines), []


# unittest full run: parse "Ran N tests in Xs" + "OK" / "FAILED (failures=A, errors=B)"
_UNITTEST_FAIL_RE = re.compile(
    r"FAILED\s*\((?:failures=(\d+))?(?:,\s*)?(?:errors=(\d+))?\)"
)


def _parse_unittest_run(
    stdout: str, stderr: str, exit_code: int,
) -> tuple[int, int, list[str]]:
    text = stdout + "\n" + stderr
    m = _UNITTEST_RAN_RE.search(text)
    total = int(m.group(1)) if m else 0
    fail_m = _UNITTEST_FAIL_RE.search(text)
    failures = 0
    errors = 0
    if fail_m:
        failures = int(fail_m.group(1) or 0)
        errors = int(fail_m.group(2) or 0)
    failed = failures + errors
    passed = max(0, total - failed)
    err_list: list[str] = []
    for line in text.splitlines():
        if line.startswith(("FAIL:", "ERROR:")):
            err_list.append(line.strip())
        if len(err_list) >= 10:
            break
    return passed, failed, err_list


# pytest full run: "X passed, Y failed in Zs" — variable wording.
_PYTEST_RUN_RE = re.compile(
    r"(?m)(?:(\d+)\s+passed)?[^\n]*?(?:(\d+)\s+failed)?[^\n]*?in\s+[\d.]+s"
)


def _parse_pytest_run(
    stdout: str, stderr: str, exit_code: int,
) -> tuple[int, int, list[str]]:
    text = stdout + "\n" + stderr
    passed = 0
    failed = 0
    # Iterate matches; take last hit since pytest may print summary twice
    for m in _PYTEST_RUN_RE.finditer(text):
        if m.group(1):
            passed = int(m.group(1))
        if m.group(2):
            failed = int(m.group(2))
    err_list: list[str] = []
    for line in text.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            err_list.append(line.strip())
        if len(err_list) >= 10:
            break
    return passed, failed, err_list


# ---- Public execution API ------------------------------------------------

def run_collect_only(
    project_root: str | Path,
    framework: Framework | None,
    timeout_s: float = DEFAULT_COLLECT_TIMEOUT_S,
) -> CollectResult:
    """Enumerate the test suite without executing any test code. Bounded
    by `timeout_s`. Never raises."""
    started = time.monotonic()

    if framework is None:
        return CollectResult(
            framework_name=None,
            test_count=0,
            parse_errors=[],
            elapsed_ms=0,
            raw_output_tail="",
        )

    try:
        cwd = str(Path(project_root).resolve())
    except Exception:
        return CollectResult(
            framework_name=framework.name,
            parse_errors=["bad project_root"],
        )

    code, out, err, timed_out = _run_subprocess(
        framework.command, cwd=cwd, timeout_s=timeout_s,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # Dispatch parser by framework
    parse_errors: list[str] = []
    test_count = 0
    if framework.name == FRAMEWORK_PYTEST:
        test_count, parse_errors = _parse_pytest_collect(out, err)
    elif framework.name == FRAMEWORK_UNITTEST:
        test_count, parse_errors = _parse_unittest_collect(out, err)
    elif framework.name in (FRAMEWORK_JEST, FRAMEWORK_VITEST, FRAMEWORK_MOCHA):
        test_count, parse_errors = _parse_node_collect(out, err)
    elif framework.name == FRAMEWORK_CARGO:
        test_count, parse_errors = _parse_cargo_collect(out, err)
    elif framework.name == FRAMEWORK_GO:
        test_count, parse_errors = _parse_go_collect(out, err)

    if timed_out:
        parse_errors.append(f"collect timeout after {timeout_s}s")
    if code not in (0, 5) and not test_count:
        # pytest exits 5 when no tests collected; not necessarily an error
        msg = (err or out or "").strip().splitlines()
        if msg:
            parse_errors.append(f"non-zero exit {code}: {msg[-1][:120]}")

    return CollectResult(
        framework_name=framework.name,
        test_count=test_count,
        parse_errors=parse_errors[:10],
        elapsed_ms=elapsed_ms,
        raw_output_tail=_tail(out + err),
    )


def run_full_suite(
    project_root: str | Path,
    framework: Framework | None,
    timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
) -> RunResult:
    """Run the full test suite. Bounded by `timeout_s`. Never raises."""
    started = time.monotonic()

    if framework is None:
        return RunResult(
            framework_name=None,
            errors=["no framework detected"],
        )

    try:
        cwd = str(Path(project_root).resolve())
    except Exception:
        return RunResult(
            framework_name=framework.name,
            errors=["bad project_root"],
        )

    code, out, err, timed_out = _run_subprocess(
        framework.full_command, cwd=cwd, timeout_s=timeout_s,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    passed = 0
    failed = 0
    err_list: list[str] = []
    if framework.name == FRAMEWORK_UNITTEST:
        passed, failed, err_list = _parse_unittest_run(out, err, code)
    elif framework.name == FRAMEWORK_PYTEST:
        passed, failed, err_list = _parse_pytest_run(out, err, code)
    else:
        # For node/cargo/go — fall back to exit code as truth value;
        # parsing varies too much to be reliable in stdlib alone.
        if code == 0:
            passed = 1  # signal "suite passed" without per-test count
        else:
            failed = 1

    if timed_out:
        err_list.append(f"run timeout after {timeout_s}s")

    return RunResult(
        framework_name=framework.name,
        passed=passed,
        failed=failed,
        errors=err_list[:10],
        elapsed_ms=elapsed_ms,
        exit_code=code,
        output_tail=_tail(out + err),
    )


# ---- Opt-in gate ---------------------------------------------------------

def is_run_tests_enabled(project_root: str | Path | None = None) -> bool:
    """True iff full-suite execution is opt-in-enabled.

    Resolution order:
      1. KOS_MEMORY_RUN_TESTS env var (non-empty + truthy)
      2. <kos-dir>/config.json key {"run_tests": true}
      3. False (default — collect-only is the safe behavior)
    """
    env_val = (os.environ.get("KOS_MEMORY_RUN_TESTS") or "").strip().lower()
    if env_val in ("1", "true", "yes", "on"):
        return True

    if project_root is None:
        return False

    # Read config.json — best-effort
    try:
        from .paths import FILE_CONFIG, ensure_kos_dir
    except Exception:
        return False

    try:
        kos = ensure_kos_dir(project_root, user_level=False)
        cfg = kos / FILE_CONFIG
        if not cfg.exists():
            return False
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return bool(data.get("run_tests", False))
    except Exception:
        return False
