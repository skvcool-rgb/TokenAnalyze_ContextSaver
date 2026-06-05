"""Filesystem path helpers — single source of truth.

Per-project memory:   <project>/.kos-memory/
User-level memory:    ~/.config/kos-memory/user/  (XDG; %APPDATA%/kos-memory/user/ on Windows)
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR_NAME = ".kos-memory"


def project_cache_dir(project_root: str | Path) -> Path:
    """Return absolute <project>/.kos-memory/ path. Creates if missing."""
    p = Path(project_root).resolve() / PROJECT_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_cache_dir() -> Path:
    """Return cross-project user-level memory dir.

    Uses %APPDATA% on Windows, ~/.config on POSIX (XDG-compliant).
    Distinct from any v3 path to avoid collision.
    """
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    p = base / "kos-memory" / "user"
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_kos_dir(project_root: str | Path | None = None, user_level: bool = False) -> Path:
    """Pick the correct cache dir for project- vs user-level operation."""
    if user_level:
        return user_cache_dir()
    if project_root is None:
        project_root = os.getcwd()
    return project_cache_dir(project_root)


# Standard files inside a kos-memory dir
FILE_CHUNKS_DB = "chunks.db"
FILE_CATALOG = "catalog.json"
FILE_SYNONYMS = "synonyms.json"
FILE_LAST_INGEST = "last_ingest_marker"
FILE_BUDGET = "budget.json"
FILE_INGEST_LOG = "ingest_log.jsonl"
FILE_CONFIG = "config.json"

# ── Mode resolution ──────────────────────────────────────────
# v4.1.0: kos-memory operates in either "primary" (auto-injects catalog
# + MEMORY.md TL;DR + auto-recall on triggers) or "backup" (markers only,
# explicit /recall required) mode. Default is "primary" — the building
# blocks were always there and operators almost always want the surfaced
# context.

MODE_PRIMARY = "primary"
MODE_BACKUP = "backup"
DEFAULT_MODE = MODE_PRIMARY
VALID_MODES = (MODE_PRIMARY, MODE_BACKUP)


def get_mode(project_root: str | Path | None = None) -> str:
    """Resolve the active mode in priority order:
       1. KOS_MEMORY_MODE env var (highest)
       2. <project>/.kos-memory/config.json {"mode": ...}
       3. user-level config.json {"mode": ...}
       4. DEFAULT_MODE (primary)
    """
    env = (os.environ.get("KOS_MEMORY_MODE") or "").strip().lower()
    if env in VALID_MODES:
        return env

    import json as _json
    for user_level in (False, True):
        try:
            kos_dir = ensure_kos_dir(project_root, user_level=user_level)
            cfg = kos_dir / FILE_CONFIG
            if cfg.exists():
                data = _json.loads(cfg.read_text(encoding="utf-8"))
                m = (data.get("mode") or "").strip().lower()
                if m in VALID_MODES:
                    return m
        except Exception:
            continue
    return DEFAULT_MODE


def set_mode(mode: str, project_root: str | Path | None = None,
             user_level: bool = False) -> Path:
    """Persist a mode to <kos-dir>/config.json. Returns the file path."""
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}, must be one of {VALID_MODES}")
    import json as _json
    kos_dir = ensure_kos_dir(project_root, user_level=user_level)
    cfg = kos_dir / FILE_CONFIG
    data: dict = {}
    if cfg.exists():
        try:
            data = _json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["mode"] = mode
    tmp = cfg.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, cfg)
    return cfg
