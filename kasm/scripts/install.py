#!/usr/bin/env python
"""kos-memory v4 installer.

Registers the plugin with Claude Code by:
1. Verifying Python 3.9+ is available.
2. Adding the plugin directory to ~/.claude/settings.json under
   `enabledPlugins` (or equivalent), and the MCP server under `mcpServers`.
3. Smoke-testing the install with a temp .kos-memory store.

Pure stdlib. Safe to re-run (idempotent).

Usage:
    python scripts/install.py [--dry-run] [--user-settings PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_DEFAULT = Path.home() / ".claude" / "settings.json"

# Force UTF-8 stdout — installer prints non-ASCII characters
sys.path.insert(0, str(PLUGIN_ROOT))
try:
    from lib.stdio_utf8 import force_utf8_io
    force_utf8_io()
except Exception:
    pass


def check_python() -> tuple[bool, str]:
    v = sys.version_info
    if (v.major, v.minor) < (3, 9):
        return False, f"Python 3.9+ required, got {v.major}.{v.minor}.{v.micro}"
    return True, f"Python {v.major}.{v.minor}.{v.micro} OK"


def check_files() -> tuple[bool, list[str]]:
    """Verify all expected plugin files exist."""
    required = [
        ".claude-plugin/plugin.json",
        "lib/__init__.py",
        "lib/store.py",
        "lib/chunker.py",
        "lib/search.py",
        "lib/budget.py",
        "lib/catalog.py",
        "lib/recall.py",
        "lib/paths.py",
        "hooks/SessionStart.py",
        "hooks/Stop.py",
        "hooks/PreCompact.py",
        "hooks/UserPromptSubmit.py",
        "mcp/server.py",
        "mcp/cli.py",
        "commands/recall.md",
        "commands/remember.md",
        "commands/memory-status.md",
        "skills/memory-recovery/SKILL.md",
    ]
    missing = []
    for r in required:
        if not (PLUGIN_ROOT / r).exists():
            missing.append(r)
    return (len(missing) == 0), missing


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    # utf-8-sig transparently strips a BOM if present (common on Windows)
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            text = path.read_text(encoding=encoding)
            return json.loads(text)
        except UnicodeDecodeError:
            continue
        except json.JSONDecodeError as e:
            print(f"  WARN: existing settings.json is not valid JSON: {e}",
                  file=sys.stderr)
            return {}
        except Exception as e:
            print(f"  WARN: existing settings.json unreadable: {e}",
                  file=sys.stderr)
            return {}
    print("  WARN: settings.json had encoding we couldn't decode", file=sys.stderr)
    return {}


def merge_plugin_into_settings(
    settings: dict, plugin_root: Path, python_exe: str
) -> dict:
    """Add kos-memory plugin block + MCP server. Idempotent."""
    plugin_root_str = str(plugin_root.resolve()).replace("\\", "/")

    # Block 1: enabledPlugins (Claude Code plugin auto-load convention)
    enabled = settings.setdefault("enabledPlugins", {})
    enabled["kos-memory"] = {
        "path": plugin_root_str,
        "version": "6.0.1",
    }

    # Block 2: mcpServers (so MCP server is registered globally too).
    # Use the absolute path of the Python that ran the installer — avoids
    # PATH ambiguity (Mac/Linux often have only python3, not python).
    mcp = settings.setdefault("mcpServers", {})
    mcp["kos-memory"] = {
        "command": python_exe,
        "args": [str(plugin_root / "mcp" / "server.py").replace("\\", "/")],
    }

    return settings


def patch_plugin_manifest(plugin_root: Path, python_exe: str,
                          dry_run: bool = False) -> tuple[bool, str]:
    """Rewrite hook + mcpServer command strings in plugin.json to use the
    detected Python interpreter. Without this, Claude Code's hook runner
    invokes `python` which may not exist on Mac/Linux (only `python3`).

    Idempotent — safe to re-run."""
    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        return False, f"manifest missing at {manifest_path}"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"manifest unreadable: {e}"

    # Quote the python path for shells (Windows paths often have spaces)
    needs_quoting = " " in python_exe and not (
        python_exe.startswith('"') and python_exe.endswith('"')
    )
    py_token = f'"{python_exe}"' if needs_quoting else python_exe

    # v6.0.1: idempotent — replace WHATEVER interpreter token is currently
    # in the command (could be "python", an absolute path, a quoted path,
    # or a different python from a previous install). Split on the
    # template marker "${CLAUDE_PLUGIN_ROOT}" since it's stable.
    PLUGIN_MARKER = "${CLAUDE_PLUGIN_ROOT}"
    changed = False
    for hook_name, entries in (manifest.get("hooks") or {}).items():
        for entry in entries:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if PLUGIN_MARKER not in cmd:
                    continue
                # Everything before the marker is the interpreter prefix
                idx = cmd.index(PLUGIN_MARKER)
                tail = cmd[idx:]
                desired = f"{py_token} {tail}"
                if cmd != desired:
                    h["command"] = desired
                    changed = True

    # Patch mcpServers — replace any interpreter token (not just literal "python")
    mcp = manifest.get("mcpServers") or {}
    server = mcp.get("kos-memory")
    if server and "command" in server and server["command"] != python_exe:
        server["command"] = python_exe
        changed = True

    if not changed:
        return True, "manifest already patched (no changes)"
    if dry_run:
        return True, f"DRY-RUN: would patch manifest with python={python_exe!r}"

    # Atomic write
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(manifest_path)
    return True, f"patched manifest with python={python_exe!r}"


def write_settings(path: Path, settings: dict) -> None:
    """Atomic write of settings.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    if path.exists():
        bak = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
        shutil.copy2(path, bak)
        print(f"  backed up existing settings to {bak.name}")
    tmp.replace(path)


def smoke_test() -> tuple[bool, str]:
    """Create a temp store, insert chunk, recall it. Verifies imports + SQLite + grep."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        from lib.chunker import chunk_text
        from lib.paths import FILE_CHUNKS_DB, ensure_kos_dir
        from lib.recall import execute_recall_local_only
        from lib.store import Store
    except Exception as e:
        return False, f"import failed: {e}"

    with tempfile.TemporaryDirectory() as td:
        try:
            kos = ensure_kos_dir(td, user_level=False)
        except Exception as e:
            return False, f"ensure_kos_dir failed: {e}"

        store = Store(kos / FILE_CHUNKS_DB)
        try:
            chunks = chunk_text(
                "The quick brown fox jumps over the lazy dog. "
                "We refactored the auth module to use OAuth2 with PKCE.",
                max_chars=400,
                overlap=50,
            )
            for c in chunks:
                store.add_chunk(
                    session_id="smoke_session",
                    project=td,
                    ts=int(time.time()),
                    text=c.text,
                    kind=c.kind,
                    language=c.language,
                    file_refs=[],
                    asserted_by_user=False,
                )
            store.upsert_session(
                "smoke_session",
                started_at=int(time.time()) - 60,
                ended_at=int(time.time()),
                project=td,
                chunk_count=len(chunks),
            )
        finally:
            store.close()

        rc = execute_recall_local_only(query="auth oauth", window_days=1, project_root=td)
        if not rc.passages:
            return False, "recall returned 0 passages on smoke data"

        return True, f"recall returned {len(rc.passages)} passages, OK"


def main() -> int:
    parser = argparse.ArgumentParser(prog="kos-memory-install")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing.")
    parser.add_argument("--user-settings", default=str(SETTINGS_DEFAULT),
                        help=f"Path to Claude settings.json (default: {SETTINGS_DEFAULT})")
    parser.add_argument("--skip-smoke", action="store_true",
                        help="Skip the post-install smoke test.")
    parser.add_argument("--python", default=None,
                        help="Override Python interpreter to use for hooks "
                             "and MCP server (default: the one running this "
                             "script, i.e. sys.executable).")
    args = parser.parse_args()

    python_exe = args.python or sys.executable
    # Normalize to forward slashes on Windows for portability in JSON
    python_exe = python_exe.replace("\\", "/")

    print("=" * 60)
    print("kos-memory v4 installer")
    print("=" * 60)
    print(f"Plugin root: {PLUGIN_ROOT}")
    print(f"Python:      {python_exe}")
    print(f"Settings:    {args.user_settings}")
    print(f"Mode:        {'DRY-RUN' if args.dry_run else 'INSTALL'}")
    print()

    # Step 1: Python version
    ok, msg = check_python()
    print(f"[1/6] Python check: {msg}")
    if not ok:
        print("  ABORT: incompatible Python.")
        return 1

    # Step 2: Plugin file integrity
    ok, missing = check_files()
    if not ok:
        print(f"[2/6] File check: MISSING {len(missing)} files:")
        for m in missing:
            print(f"      - {m}")
        return 1
    print(f"[2/6] File check: all required files present")

    # Step 3: Patch plugin.json with detected Python interpreter
    ok, msg = patch_plugin_manifest(PLUGIN_ROOT, python_exe,
                                    dry_run=args.dry_run)
    print(f"[3/6] Manifest patch: {msg}")
    if not ok:
        print("  ABORT: cannot patch manifest.")
        return 1

    # Step 4: Load settings
    settings_path = Path(args.user_settings).expanduser()
    settings = load_settings(settings_path)
    print(f"[4/6] Loaded settings ({len(settings)} top-level keys)")

    # Step 5: Merge into settings
    settings = merge_plugin_into_settings(settings, PLUGIN_ROOT, python_exe)
    if args.dry_run:
        print(f"[5/6] DRY-RUN: would write the following blocks to settings:")
        preview = {
            "enabledPlugins": settings.get("enabledPlugins", {}),
            "mcpServers": {"kos-memory": settings.get("mcpServers", {}).get("kos-memory")},
        }
        print(json.dumps(preview, indent=2))
    else:
        write_settings(settings_path, settings)
        print(f"[5/6] Wrote settings to {settings_path}")

    # Step 6: Smoke test
    if args.skip_smoke or args.dry_run:
        print(f"[6/6] Smoke test: SKIPPED")
    else:
        ok, msg = smoke_test()
        print(f"[6/6] Smoke test: {msg}")
        if not ok:
            print("  WARNING: install completed but smoke test failed. Investigate.")
            return 2

    print()
    print("=" * 60)
    print("Done. Restart Claude Code, then try:  /memory-status")
    print("=" * 60)

    # v6.0: cross-tool nudge — kos-memory storage is reachable from non-CC
    # tools too. Don't auto-modify other tools' configs (intrusive); just
    # point the operator at the integration docs.
    if not args.dry_run:
        print()
        print("[cross-tool] kos-memory storage is now reachable from non-CC tools:")
        print("  Claude Desktop / Cursor / Cline / Zed   "
              "→ mcp.standalone_server (see docs/integrations/)")
        print("  Aider / Continue.dev / shell scripts    "
              "→ python -m mcp.http_server  (see docs/integrations/)")
        print("  Any local CLI                           "
              "→ python -m mcp.standalone_cli status")

    # Friendly nag for contributors: don't accidentally commit the per-user
    # python path baked into plugin.json
    if not args.dry_run and (PLUGIN_ROOT / ".git").exists():
        print()
        print("Note for contributors: this install mutated "
              ".claude-plugin/plugin.json")
        print("with your local Python path. If you plan to commit, run:")
        print("  git restore .claude-plugin/plugin.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
