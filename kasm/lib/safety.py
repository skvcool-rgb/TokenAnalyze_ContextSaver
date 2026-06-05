"""Safety primitives — panic switch, diagnostic logging, watchdog wrapper.

Every kos-memory hook should be wrapped via `run_safely(...)` so that:
  1. KOS_MEMORY_DISABLE=1 short-circuits to no-op (instant kill switch).
  2. Any unhandled exception is caught, logged, and the hook exits 0 —
     a buggy plugin must NEVER disrupt Claude Code's session lifecycle.
  3. Hard timeout via signal-based watchdog (POSIX) or thread-based
     watchdog (Windows). The hook process itself enforces this so even
     if Claude Code's plugin runner doesn't.
  4. Diagnostic events are appended to <kos-dir>/diagnostic.log with
     structured JSON (1 event per line). Log auto-rotates at 1 MB.

The cardinal rule: a hook can fail silently, but it can never raise
into Claude Code or hang past its timeout budget.

Pure stdlib. Importing this module has zero side effects beyond
reading env vars.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

# Public env vars
ENV_DISABLE = "KOS_MEMORY_DISABLE"           # "1" => instant no-op everywhere
ENV_DEBUG = "KOS_MEMORY_DEBUG"               # "1" => verbose diagnostic logging

# Diagnostic log file inside the kos-memory dir for the project being processed
DIAGNOSTIC_FILENAME = "diagnostic.log"
MAX_LOG_BYTES = 1_000_000                    # rotate at 1 MB
MAX_LOG_BACKUPS = 3                          # keep .1, .2, .3


def is_disabled() -> bool:
    """Returns True if KOS_MEMORY_DISABLE=1. All hooks should check this
    first and exit 0 if disabled."""
    return os.environ.get(ENV_DISABLE, "").strip() == "1"


def is_debug() -> bool:
    return os.environ.get(ENV_DEBUG, "").strip() == "1"


def _resolve_log_path(project_root: str | None = None) -> Path | None:
    """Find the diagnostic log path, creating the kos-dir if needed.
    Returns None on any failure — logging must never crash the hook."""
    try:
        from .paths import ensure_kos_dir
    except Exception:
        return None
    try:
        proj = (
            project_root
            or os.environ.get("CLAUDE_PROJECT_DIR")
            or os.getcwd()
        )
        kos = ensure_kos_dir(proj, user_level=False)
        return kos / DIAGNOSTIC_FILENAME
    except Exception:
        return None


def _rotate_if_needed(path: Path) -> None:
    """Roll the log if it exceeds MAX_LOG_BYTES. Best-effort; ignore errors."""
    try:
        if not path.exists():
            return
        if path.stat().st_size < MAX_LOG_BYTES:
            return
        # Push existing backups down: .2 → .3, .1 → .2, current → .1
        for i in range(MAX_LOG_BACKUPS, 0, -1):
            src = path.with_suffix(f".log.{i}")
            dst = path.with_suffix(f".log.{i + 1}")
            if dst.exists():
                try:
                    dst.unlink()
                except Exception:
                    pass
            if src.exists():
                try:
                    src.rename(dst)
                except Exception:
                    pass
        try:
            path.rename(path.with_suffix(".log.1"))
        except Exception:
            pass
    except Exception:
        pass


def log_event(
    event: str,
    *,
    level: str = "info",
    project_root: str | None = None,
    **fields,
) -> None:
    """Append a structured diagnostic event. Never raises.

    `event`  short verb, e.g. "hook_start", "hook_exception", "watchdog_fire"
    `level`  "debug" | "info" | "warn" | "error"
    extra fields go into the JSON payload.
    """
    # Suppress debug events when KOS_MEMORY_DEBUG isn't set
    if level == "debug" and not is_debug():
        return

    log_path = _resolve_log_path(project_root)
    if log_path is None:
        return

    record = {
        "ts": int(time.time()),
        "level": level,
        "event": event,
        "pid": os.getpid(),
        **fields,
    }
    try:
        _rotate_if_needed(log_path)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
            f.flush()
    except Exception:
        pass  # never raise from logging


class _Watchdog:
    """Cross-platform timeout watchdog. Sets a flag when fired; the
    monitored function should poll the flag (or be lucky and finish).
    On expiry we also force-exit the process to guarantee hook latency.

    NOTE: this is a hard upper bound — Claude Code itself may also
    enforce hook timeouts via the plugin manifest's `timeout` field;
    we layer this on top so cwd-only fault modes can't hang us.
    """

    def __init__(self, seconds: float, name: str = "hook"):
        self.seconds = seconds
        self.name = name
        self._timer: threading.Timer | None = None
        self.fired = False

    def __enter__(self):
        if self.seconds <= 0:
            return self

        def fire():
            self.fired = True
            log_event(
                "watchdog_fire",
                level="error",
                hook_name=self.name,
                timeout_s=self.seconds,
            )
            # Hard exit — Claude Code's hook runner sees exit code, can't
            # be left waiting on a hung subprocess.
            os._exit(0)

        self._timer = threading.Timer(self.seconds, fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


def run_safely(
    hook_main: Callable[[], int | None],
    *,
    hook_name: str,
    timeout_s: float = 30.0,
    project_root: str | None = None,
) -> int:
    """Wrap a hook's main() with the full safety stack:

      1. KOS_MEMORY_DISABLE check — instant no-op exit 0.
      2. Watchdog — guarantees process exits within timeout_s + small grace.
      3. Top-level try/except — any uncaught exception logged, exit 0.
      4. Diagnostic logging — start, end (with elapsed_ms), exceptions.

    Returns the exit code the script should pass to sys.exit().
    By contract this is ALWAYS 0 — hooks never disturb Claude Code's
    session even when broken. Errors go to diagnostic.log instead.
    """
    if is_disabled():
        return 0

    started_at = time.time()
    log_event("hook_start", level="debug",
              hook_name=hook_name, timeout_s=timeout_s,
              project_root=project_root)

    try:
        with _Watchdog(seconds=timeout_s, name=hook_name):
            rc = hook_main()
    except SystemExit as e:
        # Hooks frequently call sys.exit(0) or sys.exit(N). Honor the code
        # but don't escalate to disruption.
        code = e.code if isinstance(e.code, int) else 0
        elapsed_ms = int((time.time() - started_at) * 1000)
        log_event("hook_exit", level="debug",
                  hook_name=hook_name, code=code, elapsed_ms=elapsed_ms)
        return 0  # always 0 to Claude Code
    except KeyboardInterrupt:
        log_event("hook_interrupted", level="warn", hook_name=hook_name)
        return 0
    except Exception as e:
        elapsed_ms = int((time.time() - started_at) * 1000)
        log_event(
            "hook_exception", level="error",
            hook_name=hook_name,
            elapsed_ms=elapsed_ms,
            exc_type=type(e).__name__,
            exc_msg=str(e)[:500],
            traceback=traceback.format_exc(limit=10)[-2000:],
        )
        return 0  # SWALLOW — hook failure must not disturb session

    elapsed_ms = int((time.time() - started_at) * 1000)
    log_event("hook_end", level="debug",
              hook_name=hook_name, elapsed_ms=elapsed_ms,
              rc=rc if isinstance(rc, int) else None)
    return 0
