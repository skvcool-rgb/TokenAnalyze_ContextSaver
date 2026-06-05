"""kos-memory standalone CLI — friendly, cross-tool entry point.

Wraps mcp/cli.py functions with subcommand names that read naturally
from any shell, default to the cwd as the project, and pretty-print
JSON output by default.

This is the recommended entry point for non-Claude-Code tools (Aider,
shell scripts, ad-hoc curl-via-shell pipelines, etc.). It does not
modify or replace mcp/cli.py — it is purely a convenience wrapper.

## Usage

    python -m mcp.standalone_cli status
    python -m mcp.standalone_cli recall "auth refactor"
    python -m mcp.standalone_cli remember "We picked Postgres" --tags db,decision
    python -m mcp.standalone_cli mode primary
    python -m mcp.standalone_cli export --out /tmp/export.json
    python -m mcp.standalone_cli import_ --path /tmp/export.json
    python -m mcp.standalone_cli bootstrap         # defensive
    python -m mcp.standalone_cli sync push         # defensive
    python -m mcp.standalone_cli curate            # defensive
    python -m mcp.standalone_cli test_status       # smoke test

Add --json for raw machine-readable output. Default: indent=2 pretty.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.stdio_utf8 import force_utf8_io
force_utf8_io()

from mcp import cli as _cli


def _resolve_project() -> str:
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("KOS_MEMORY_PROJECT")
        or os.getcwd()
    )


class _Capture:
    """Small helper: route _cli._emit() through a buffer so we can re-print."""
    def __init__(self):
        self.last: dict | None = None
        self._orig = _cli._emit

    def __enter__(self):
        def emit(data):
            self.last = data
        _cli._emit = emit
        return self

    def __exit__(self, *a):
        _cli._emit = self._orig


def _print(obj, raw: bool) -> None:
    if raw:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    else:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _set_project_env(project: str) -> None:
    os.environ["CLAUDE_PROJECT_DIR"] = project


def _build_args(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace pre-populated with defaults the
    underlying _cli.cmd_* expects."""
    base = {"user": False, "verbose": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def _run(fn, ns: argparse.Namespace, raw: bool) -> int:
    with _Capture() as cap:
        rc = fn(ns)
    out = cap.last or {"ok": False, "error": "no output captured"}
    _print(out, raw)
    return rc


def cmd_status(args) -> int:
    _set_project_env(args.project)
    ns = _build_args(user=args.user, verbose=args.verbose)
    return _run(_cli.cmd_status, ns, args.json)


def cmd_recall(args) -> int:
    _set_project_env(args.project)
    ns = _build_args(user=args.user, query=args.query,
                     window_days=args.window_days)
    return _run(_cli.cmd_recall_stage_a, ns, args.json)


def cmd_remember(args) -> int:
    _set_project_env(args.project)
    ns = _build_args(user=args.user, fact=args.fact, tags=args.tags or "")
    return _run(_cli.cmd_remember, ns, args.json)


def cmd_mode(args) -> int:
    _set_project_env(args.project)
    ns = _build_args(user=args.user, mode=args.mode)
    return _run(_cli.cmd_memory_mode, ns, args.json)


def cmd_export(args) -> int:
    _set_project_env(args.project)
    ns = _build_args(user=args.user, out=args.out, since=args.since,
                     include_contradicted=args.include_contradicted)
    return _run(_cli.cmd_export, ns, args.json)


def cmd_import(args) -> int:
    _set_project_env(args.project)
    ns = _build_args(user=args.user, path=args.path,
                     replace=args.replace,
                     include_contradicted=args.include_contradicted)
    return _run(_cli.cmd_import_export, ns, args.json)


def cmd_bootstrap(args) -> int:
    try:
        from lib import bootstrap as _b  # type: ignore
    except ImportError as e:
        _print({"ok": False, "error": f"503: lib.bootstrap not available ({e})"},
               args.json)
        return 1
    out = _b.bootstrap_project(project_root=args.project) if hasattr(_b, "bootstrap_project") else {"ok": False, "error": "no bootstrap_project()"}
    _print(out if isinstance(out, dict) else {"ok": True, "data": out}, args.json)
    return 0


def cmd_sync(args) -> int:
    try:
        from lib import sync as _s  # type: ignore
    except ImportError as e:
        _print({"ok": False, "error": f"503: lib.sync not available ({e})"},
               args.json)
        return 1
    fn = getattr(_s, args.direction, None)
    if fn is None:
        _print({"ok": False, "error": f"lib.sync.{args.direction}() not found"},
               args.json)
        return 1
    out = fn(project_root=args.project)
    _print(out if isinstance(out, dict) else {"ok": True, "data": out}, args.json)
    return 0


def cmd_curate(args) -> int:
    try:
        from lib import auto_suggestions as _a  # type: ignore
    except ImportError as e:
        _print({"ok": False, "error": f"503: lib.auto_suggestions not available ({e})"},
               args.json)
        return 1
    fn = getattr(_a, "curate", None)
    if fn is None:
        _print({"ok": False, "error": "lib.auto_suggestions.curate() not found"},
               args.json)
        return 1
    out = fn(project_root=args.project)
    _print(out if isinstance(out, dict) else {"ok": True, "data": out}, args.json)
    return 0


def cmd_test_status(args) -> int:
    """Smoke test: status -> remember -> status round-trip."""
    _set_project_env(args.project)
    ns_status = _build_args()
    with _Capture() as cap:
        _cli.cmd_status(ns_status)
    before = cap.last
    ns_rem = _build_args(fact="standalone smoke test", tags="")
    with _Capture() as cap:
        _cli.cmd_remember(ns_rem)
    rem = cap.last
    _print({"ok": True, "data": {
        "status_before": before, "remember_result": rem,
    }, "error": None}, args.json)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kos-memory-standalone")
    p.add_argument("--json", action="store_true",
                   help="Raw single-line JSON output (default is pretty).")
    p.add_argument("--project", default=_resolve_project(),
                   help="Project root (default: cwd / CLAUDE_PROJECT_DIR / KOS_MEMORY_PROJECT).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status"); s.set_defaults(func=cmd_status)
    s.add_argument("--user", action="store_true")
    s.add_argument("--verbose", action="store_true")

    r = sub.add_parser("recall"); r.set_defaults(func=cmd_recall)
    r.add_argument("query")
    r.add_argument("--window-days", type=int, default=30)
    r.add_argument("--user", action="store_true")

    rem = sub.add_parser("remember"); rem.set_defaults(func=cmd_remember)
    rem.add_argument("fact")
    rem.add_argument("--tags", default="")
    rem.add_argument("--user", action="store_true")

    m = sub.add_parser("mode"); m.set_defaults(func=cmd_mode)
    m.add_argument("mode", nargs="?", default=None,
                   choices=("primary", "backup", None))
    m.add_argument("--user", action="store_true")

    e = sub.add_parser("export"); e.set_defaults(func=cmd_export)
    e.add_argument("--out", default=None)
    e.add_argument("--since", default=None)
    e.add_argument("--include-contradicted", action="store_true")
    e.add_argument("--user", action="store_true")

    i = sub.add_parser("import_"); i.set_defaults(func=cmd_import)
    i.add_argument("--path", required=True)
    i.add_argument("--replace", action="store_true")
    i.add_argument("--include-contradicted", action="store_true")
    i.add_argument("--user", action="store_true")

    b = sub.add_parser("bootstrap"); b.set_defaults(func=cmd_bootstrap)

    sy = sub.add_parser("sync"); sy.set_defaults(func=cmd_sync)
    sy.add_argument("direction", choices=("push", "pull"))

    c = sub.add_parser("curate"); c.set_defaults(func=cmd_curate)

    t = sub.add_parser("test_status"); t.set_defaults(func=cmd_test_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
