"""Codebase survey — read live ground truth (git + tree + package versions
+ test cache) so SessionStart and UserPromptSubmit reconciliation have
hard facts to compare against the chunks.db claims.

Pure stdlib. Uses subprocess for git (cap 2s). Cached at
<kos-dir>/survey_cache.json with 60s TTL so we don't re-walk the
repo on every UserPromptSubmit fire.

Public:
    survey_project(root) -> Survey       # cached, fast
    invalidate_cache(root)               # bust the cache
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Bounds — keeps survey fast and output small. v5.1: overridable per-project
# via <kos-dir>/config.json keys: tree_depth, tree_max_entries, max_commits,
# max_tags, git_timeout_s, cache_ttl_s.
MAX_COMMITS = 20
MAX_TAGS = 10
MAX_TREE_ENTRIES = 60
MAX_TREE_DEPTH = 3
GIT_TIMEOUT_S = 2.0
CACHE_TTL_S = 60


def _load_overrides(project_root: str) -> dict:
    """Read survey-related overrides from <kos-dir>/config.json.
    Empty dict on any failure — overrides are best-effort."""
    try:
        from .paths import FILE_CONFIG, ensure_kos_dir
        kos = ensure_kos_dir(project_root, user_level=False)
        cfg = kos / FILE_CONFIG
        if not cfg.exists():
            return {}
        data = json.loads(cfg.read_text(encoding="utf-8"))
        out = {}
        for key in ("tree_depth", "tree_max_entries", "max_commits",
                    "max_tags", "git_timeout_s", "cache_ttl_s"):
            if key in data and isinstance(data[key], (int, float)):
                out[key] = data[key]
        return out
    except Exception:
        return {}

# Files to extract version info from
VERSION_FILES = (
    "package.json",
    "Cargo.toml",
    "pyproject.toml",
    "setup.py",
    ".claude-plugin/plugin.json",
    "lib/__init__.py",
    "VERSION",
)

# Test artifact locations (read-only, never invoke tests)
TEST_CACHE_PATHS = (
    ".pytest_cache/v/cache/lastfailed",
    ".pytest_cache/v/cache/nodeids",
    ".test_cache/last_run.json",
    "node_modules/.cache/jest",
    "target/test-results",
)


@dataclass
class Survey:
    """Snapshot of the project's ground-truth state."""
    project_root: str = ""
    surveyed_at: int = 0
    is_git_repo: bool = False

    # Git state
    branch: str = ""
    head_sha: str = ""          # short
    head_subject: str = ""
    dirty: bool = False
    dirty_count: int = 0
    last_commits: list[dict] = field(default_factory=list)   # [{sha, subject, ts}]
    tags: list[str] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    upstream: str = ""

    # Tree (top-level + 1 level deep)
    tree_summary: list[str] = field(default_factory=list)    # ["lib/ (10 .py)", ...]
    file_count: int = 0

    # Package versions
    versions: dict = field(default_factory=dict)             # {"plugin.json": "4.1.0", ...}

    # Test status (last run, if any)
    test_status: str = "unknown"                             # "pass" | "fail" | "unknown"
    test_artifact_paths: list[str] = field(default_factory=list)

    # v6.0 — live test_runner dispatch. test_collect_* always populated
    # when a framework is detected; test_run_* only when opt-in.
    test_framework: str | None = None
    test_collect_count: int = 0
    test_collect_errors: list[str] = field(default_factory=list)
    test_run_status: str = "skipped"          # skipped | pass | fail | error
    test_run_elapsed_ms: int = 0

    # Errors (don't crash on these — survey is best-effort)
    errors: list[str] = field(default_factory=list)


def _run_git(args: list[str], cwd: str) -> tuple[bool, str]:
    """Run a git command, return (ok, output). Bounded by timeout."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=cwd, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S,
        )
        if r.returncode != 0:
            return False, (r.stderr or "").strip()
        return True, (r.stdout or "").strip()
    except FileNotFoundError:
        return False, "git not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"git timeout >{GIT_TIMEOUT_S}s"
    except Exception as e:
        return False, str(e)


def _survey_git(survey: Survey, overrides: dict | None = None) -> None:
    root = survey.project_root
    o = overrides or {}
    max_commits = int(o.get("max_commits", MAX_COMMITS))
    max_tags = int(o.get("max_tags", MAX_TAGS))

    ok, _ = _run_git(["rev-parse", "--is-inside-work-tree"], root)
    if not ok:
        survey.is_git_repo = False
        return
    survey.is_git_repo = True

    ok, branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if ok:
        survey.branch = branch

    ok, sha = _run_git(["rev-parse", "--short", "HEAD"], root)
    if ok:
        survey.head_sha = sha

    ok, subject = _run_git(["log", "-1", "--format=%s"], root)
    if ok:
        survey.head_subject = subject

    # Dirty state
    ok, status = _run_git(["status", "--porcelain"], root)
    if ok:
        lines = [l for l in status.splitlines() if l.strip()]
        survey.dirty = len(lines) > 0
        survey.dirty_count = len(lines)

    # Last N commits
    ok, log = _run_git([
        "log", f"-{max_commits}",
        "--format=%h\x1f%ct\x1f%s",
    ], root)
    if ok and log:
        for line in log.splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) == 3:
                survey.last_commits.append({
                    "sha": parts[0],
                    "ts": int(parts[1]) if parts[1].isdigit() else 0,
                    "subject": parts[2][:100],
                })

    # Tags
    ok, tags = _run_git([
        "tag", "-l", "--sort=-creatordate",
    ], root)
    if ok and tags:
        survey.tags = tags.splitlines()[:max_tags]

    # Ahead/behind upstream
    ok, upstream = _run_git([
        "rev-parse", "--abbrev-ref", "@{u}",
    ], root)
    if ok and upstream:
        survey.upstream = upstream
        ok, lr = _run_git([
            "rev-list", "--left-right", "--count", "HEAD...@{u}",
        ], root)
        if ok and lr:
            parts = lr.split()
            if len(parts) == 2:
                try:
                    survey.ahead = int(parts[0])
                    survey.behind = int(parts[1])
                except ValueError:
                    pass


_SKIP_DIRS = {".git", "__pycache__", ".kos-memory", "node_modules",
              ".pytest_cache", ".idea", ".vscode", "target",
              "dist", "build", ".tox", ".mypy_cache", ".ruff_cache",
              ".venv", "venv", "env", ".env"}


def _walk_tree(root: Path, max_depth: int, max_entries: int,
               survey: Survey, prefix: str = "", depth: int = 0) -> None:
    """Recursive tree walk to configurable depth. Capped at max_entries
    total. Top-level (depth=0) entries are full names; deeper entries
    use indented prefix for clarity."""
    if depth >= max_depth:
        return
    if len(survey.tree_summary) >= max_entries:
        return
    try:
        entries = sorted(root.iterdir(),
                         key=lambda p: (not p.is_dir(), p.name.lower()))
    except (PermissionError, OSError):
        return

    for entry in entries:
        if len(survey.tree_summary) >= max_entries:
            return
        if entry.name in _SKIP_DIRS or entry.name.startswith("."):
            if entry.name not in (".claude-plugin", ".github"):
                # Allow those two — they often hold meaningful config
                continue
        if entry.is_file():
            survey.tree_summary.append(f"{prefix}{entry.name}")
            survey.file_count += 1
            continue
        if not entry.is_dir():
            continue

        # Count files by ext at this level for compact summary
        ext_counts: dict[str, int] = {}
        try:
            for sub in entry.iterdir():
                if sub.is_file():
                    ext = sub.suffix or "(no-ext)"
                    ext_counts[ext] = ext_counts.get(ext, 0) + 1
                    survey.file_count += 1
        except (PermissionError, OSError):
            continue

        if ext_counts:
            ext_str = ", ".join(
                f"{n} {ext}"
                for ext, n in sorted(ext_counts.items(),
                                     key=lambda x: -x[1])[:3]
            )
            survey.tree_summary.append(f"{prefix}{entry.name}/ ({ext_str})")
        else:
            survey.tree_summary.append(f"{prefix}{entry.name}/ (empty)")

        # Recurse if depth budget allows
        if depth + 1 < max_depth:
            _walk_tree(entry, max_depth, max_entries, survey,
                       prefix=prefix + "  ", depth=depth + 1)


def _survey_tree(survey: Survey, overrides: dict | None = None) -> None:
    """Configurable tree walk. Defaults stay at 1-level for performance,
    operator can crank up via .kos-memory/config.json."""
    o = overrides or {}
    # Default tree_depth=1 means "top-level + count files inside one level"
    # which matches v5.0 behavior. Setting tree_depth=2+ enables deeper walk.
    max_depth = max(1, int(o.get("tree_depth", 1)))
    max_entries = max(10, int(o.get("tree_max_entries", MAX_TREE_ENTRIES)))

    root = Path(survey.project_root)
    if not root.exists():
        return
    try:
        _walk_tree(root, max_depth, max_entries, survey)
    except Exception as e:
        survey.errors.append(f"tree walk failed: {e}")


_VERSION_PATTERNS = (
    re.compile(r'"version"\s*:\s*"([^"]+)"'),                          # JSON
    re.compile(r'(?m)^\s*version\s*=\s*"([^"]+)"'),                    # toml/setup
    re.compile(r"(?m)^\s*__version__\s*=\s*['\"]([^'\"]+)['\"]"),      # python
    re.compile(r"(?m)^([0-9]+\.[0-9]+\.[0-9]+[^\s]*)\s*$"),             # bare VERSION
)


def _extract_version(text: str) -> str | None:
    for pat in _VERSION_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _survey_versions(survey: Survey) -> None:
    root = Path(survey.project_root)
    for rel in VERSION_FILES:
        p = root / rel
        try:
            if p.exists() and p.is_file() and p.stat().st_size < 200_000:
                text = p.read_text(encoding="utf-8", errors="replace")
                v = _extract_version(text)
                if v:
                    survey.versions[rel] = v
        except Exception:
            continue


def _survey_test_runner(survey: Survey) -> None:
    """v6.0: detect framework, run collect-only (always), full suite (opt-in)."""
    try:
        from .test_runner import (
            detect_framework,
            is_run_tests_enabled,
            run_collect_only,
            run_full_suite,
        )
    except Exception:
        return

    fw = detect_framework(survey.project_root)
    if fw is None:
        return
    survey.test_framework = fw.name
    collect = run_collect_only(survey.project_root, fw, timeout_s=10)
    survey.test_collect_count = collect.test_count
    survey.test_collect_errors = collect.parse_errors[:5]
    if is_run_tests_enabled(survey.project_root):
        run = run_full_suite(survey.project_root, fw, timeout_s=120)
        survey.test_run_elapsed_ms = run.elapsed_ms
        if run.errors:
            survey.test_run_status = "error"
        elif run.failed:
            survey.test_run_status = "fail"
        elif run.exit_code == 0:
            survey.test_run_status = "pass"
        else:
            survey.test_run_status = "error"


def _survey_test_status(survey: Survey) -> None:
    root = Path(survey.project_root)
    artifacts: list[str] = []
    for rel in TEST_CACHE_PATHS:
        p = root / rel
        if p.exists():
            artifacts.append(rel)
    survey.test_artifact_paths = artifacts

    # Heuristic: if .pytest_cache/v/cache/lastfailed exists and is non-empty
    # array, last run had failures. Empty array = all pass. None = unknown.
    lastfailed = root / ".pytest_cache" / "v" / "cache" / "lastfailed"
    if lastfailed.exists():
        try:
            data = json.loads(lastfailed.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                survey.test_status = "fail" if data else "pass"
            elif isinstance(data, list):
                survey.test_status = "fail" if data else "pass"
        except Exception:
            pass


def _cache_path(project_root: str) -> Path:
    """Survey cache lives next to chunks.db."""
    from .paths import ensure_kos_dir
    try:
        kos = ensure_kos_dir(project_root, user_level=False)
    except Exception:
        return Path(project_root) / ".kos-memory" / "survey_cache.json"
    return kos / "survey_cache.json"


def _read_cache(project_root: str, overrides: dict | None = None) -> Survey | None:
    ttl = int((overrides or {}).get("cache_ttl_s", CACHE_TTL_S))
    p = _cache_path(project_root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        s = Survey(**data)
        if s.surveyed_at + ttl < int(time.time()):
            return None
        if s.project_root != project_root:
            return None
        return s
    except Exception:
        return None


def _write_cache(survey: Survey) -> None:
    p = _cache_path(survey.project_root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(survey), indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def survey_project(project_root: str | None = None,
                   use_cache: bool = True) -> Survey:
    """Survey the project. Cached per overrides['cache_ttl_s'] (default 60s).
    All bounds (commits, tags, tree depth/breadth, git timeout) are
    overridable via <kos-dir>/config.json — see _load_overrides()."""
    if project_root is None:
        project_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project_root = str(Path(project_root).resolve())

    overrides = _load_overrides(project_root)

    if use_cache:
        cached = _read_cache(project_root, overrides)
        if cached is not None:
            return cached

    survey = Survey(
        project_root=project_root,
        surveyed_at=int(time.time()),
    )

    try:
        _survey_git(survey, overrides)
    except Exception as e:
        survey.errors.append(f"git: {e}")
    try:
        _survey_tree(survey, overrides)
    except Exception as e:
        survey.errors.append(f"tree: {e}")
    try:
        _survey_versions(survey)
    except Exception as e:
        survey.errors.append(f"versions: {e}")
    try:
        _survey_test_status(survey)
    except Exception as e:
        survey.errors.append(f"test: {e}")
    try:
        _survey_test_runner(survey)
    except Exception as e:
        survey.errors.append(f"test_runner: {e}")

    _write_cache(survey)
    return survey


def invalidate_cache(project_root: str) -> None:
    p = _cache_path(str(Path(project_root).resolve()))
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


def render_live_state(survey: Survey, max_chars: int = 2500) -> str:
    """Format the survey for inclusion in SessionStart hook output.
    Bounded to ~max_chars."""
    if not survey.is_git_repo and not survey.tree_summary and not survey.versions:
        return ""

    lines: list[str] = []
    if survey.is_git_repo:
        lines.append(f"  branch:        {survey.branch} "
                     f"({'dirty: ' + str(survey.dirty_count) if survey.dirty else 'clean'})")
        if survey.head_sha:
            lines.append(f"  head:          {survey.head_sha} \"{survey.head_subject[:80]}\"")
        if survey.upstream and (survey.ahead or survey.behind):
            lines.append(f"  vs upstream:   {survey.upstream} "
                         f"({survey.ahead} ahead, {survey.behind} behind)")
        if survey.tags:
            tag_str = ", ".join(survey.tags[:6])
            more = f", ... +{len(survey.tags) - 6} more" if len(survey.tags) > 6 else ""
            lines.append(f"  tags:          {tag_str}{more}")
        if survey.last_commits:
            lines.append("  last commits:")
            for c in survey.last_commits[:5]:
                lines.append(f"    {c['sha']}  {c['subject'][:80]}")

    if survey.versions:
        lines.append(f"  versions:")
        for path, v in list(survey.versions.items())[:5]:
            lines.append(f"    {path}: {v}")

    if survey.tree_summary:
        tree_str = ", ".join(survey.tree_summary[:8])
        more = (f", ... +{len(survey.tree_summary) - 8} more"
                if len(survey.tree_summary) > 8 else "")
        lines.append(f"  tree:          {tree_str}{more}")

    if survey.test_status != "unknown":
        lines.append(f"  test status:   last run = {survey.test_status} "
                     f"(via {len(survey.test_artifact_paths)} cached artifacts)")
    if survey.test_framework:
        line = (f"  test runner:   {survey.test_framework}, "
                f"collect={survey.test_collect_count}")
        if survey.test_collect_errors:
            line += f", parse_errors={len(survey.test_collect_errors)}"
        if survey.test_run_status != "skipped":
            line += (f", run={survey.test_run_status} "
                     f"({survey.test_run_elapsed_ms}ms)")
        lines.append(line)

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... [truncated]"
    return out
