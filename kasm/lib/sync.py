"""Sidecar-git sync for kos-memory.

Architecture
------------
The store is `chunks.db` (SQLite) — git can't merge it. So we keep an
adjacent git repo at <kos-dir>/sync/ that tracks a deterministic JSON
snapshot of the store. Conflict resolution happens at the chunk-id level
when we re-import:

    chunks   — INSERT OR IGNORE (immutable once created, by id).
    sessions — upsert preferring most-recent ended_at.
    asserted_by_user contradictions on different chunk-ids — keep both.

The sidecar lives on a dedicated branch ("kos-memory-sync") so we never
touch the user's main project history.

Proxy bypass
------------
The deployment machine has HTTP_PROXY/HTTPS_PROXY env vars from a
security tool whose proxy is not running. Every git invocation uses
`-c http.proxy="" -c https.proxy=""` to override the env-driven proxy
inheritance for that single command — no global config mutation.

Pure stdlib. unittest-friendly. No mutation of external state outside
<kos-dir>/sync/ and the snapshot file.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import FILE_CHUNKS_DB, ensure_kos_dir

# ── Constants ───────────────────────────────────────────────
SYNC_DIR_NAME = "sync"
SNAPSHOT_FILENAME = "snapshot.json"
SYNC_BRANCH = "kos-memory-sync"
SCHEMA_VERSION = 1

TIMEOUT_LOCAL_SEC = 5
TIMEOUT_REMOTE_SEC = 60

GITIGNORE_CONTENT = """\
# kos-memory sidecar — only the snapshot is tracked.
*
!.gitignore
!snapshot.json
"""


# ── Dataclasses ─────────────────────────────────────────────
@dataclass
class SyncRepo:
    sync_dir: Path
    branch: str
    remote_url: str | None
    initialized: bool


@dataclass
class SnapshotInfo:
    path: Path
    chunks: int
    sessions: int
    bytes: int
    schema_version: int


@dataclass
class MergeReport:
    ok: bool
    chunks_imported: int = 0
    chunks_skipped: int = 0
    sessions_upserted: int = 0
    error: str | None = None


@dataclass
class PushReport:
    ok: bool
    snapshot: SnapshotInfo | None = None
    committed: bool = False
    pushed: bool = False
    commit_sha: str | None = None
    message: str | None = None
    error: str | None = None
    stderr: str = ""


@dataclass
class PullReport:
    ok: bool
    pulled: bool = False
    merge: MergeReport | None = None
    error: str | None = None
    stderr: str = ""


# ── Internal helpers ────────────────────────────────────────
def _git(
    sync_dir: Path,
    *args: str,
    timeout: int = TIMEOUT_LOCAL_SEC,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Invoke git with proxy-bypass flags and an isolated env.

    The -c http.proxy="" -c https.proxy="" pair neutralizes any
    HTTP_PROXY / HTTPS_PROXY env vars for this single command without
    touching git's persistent config.
    """
    cmd = [
        "git",
        "-c", "http.proxy=",
        "-c", "https.proxy=",
        "-C", str(sync_dir),
        *args,
    ]
    # Inherit env so user creds work, but git's internal proxy resolution
    # will see the empty -c overrides and short-circuit env lookup.
    env = os.environ.copy()
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=check,
    )


def _sync_dir_for(kos_dir: str | Path) -> Path:
    return Path(kos_dir) / SYNC_DIR_NAME


def _snapshot_path_for(kos_dir: str | Path) -> Path:
    return _sync_dir_for(kos_dir) / SNAPSHOT_FILENAME


def _atomic_write_text(path: Path, content: str) -> None:
    """Write atomically via tmp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _row_to_chunk_dict(row: sqlite3.Row) -> dict:
    """Convert a chunks row into a deterministic JSON-friendly dict."""
    file_refs_raw = row["file_refs"] or "[]"
    try:
        file_refs = json.loads(file_refs_raw)
    except Exception:
        file_refs = []
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "project": row["project"],
        "ts": row["ts"],
        "text": row["text"],
        "kind": row["kind"],
        "language": row["language"],
        "file_refs": file_refs,
        "asserted_by_user": bool(row["asserted_by_user"]),
        "contradicted_by_later_session": bool(row["contradicted_by_later_session"]),
    }


def _row_to_session_dict(row: sqlite3.Row) -> dict:
    tags_raw = row["tags"] or "[]"
    try:
        tags = json.loads(tags_raw)
    except Exception:
        tags = []
    return {
        "session_id": row["session_id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "summary": row["summary"],
        "tags": tags,
        "project": row["project"],
        "chunk_count": row["chunk_count"],
    }


# ── Public API ──────────────────────────────────────────────
def prepare_sync_repo(
    kos_dir: str | Path,
    remote_url: str | None = None,
) -> SyncRepo:
    """Initialize <kos-dir>/sync/ as a git repo on the sync branch.

    Idempotent — calling on an existing repo just ensures the branch is
    checked out and the .gitignore exists. If `remote_url` is supplied,
    sets it as `origin` (replacing any prior origin).
    """
    sync_dir = _sync_dir_for(kos_dir)
    sync_dir.mkdir(parents=True, exist_ok=True)
    git_dir = sync_dir / ".git"
    initialized_now = False

    if not git_dir.exists():
        # Init with our branch as default to avoid a stray "main" or
        # "master" floating around.
        subprocess.run(
            ["git", "-c", "http.proxy=", "-c", "https.proxy=",
             "-C", str(sync_dir), "init", "-b", SYNC_BRANCH],
            capture_output=True, text=True,
            env=os.environ.copy(), timeout=TIMEOUT_LOCAL_SEC, check=True,
        )
        # Set a local identity so commits work even with no global config.
        _git(sync_dir, "config", "user.email", "kos-memory-sync@local")
        _git(sync_dir, "config", "user.name", "kos-memory-sync")
        initialized_now = True
    else:
        # Ensure we're on the sync branch. If the branch doesn't exist
        # yet (legacy init), create it.
        r = _git(sync_dir, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        current = r.stdout.strip()
        if current != SYNC_BRANCH:
            # Try checkout, fall back to creating it.
            r2 = _git(sync_dir, "checkout", SYNC_BRANCH, check=False)
            if r2.returncode != 0:
                _git(sync_dir, "checkout", "-b", SYNC_BRANCH, check=False)

    # Ensure .gitignore is in place. The * + !snapshot.json rule keeps
    # the working tree clean. We DO NOT commit .gitignore eagerly here:
    # eager commits would create divergent root commits across machines
    # (each preparing its own sidecar before the first pull) and break
    # fast-forward pulls. Instead we let the first sync_push commit
    # .gitignore alongside the snapshot, OR a pull will populate the
    # tree from origin (and the remote's .gitignore will land via
    # checkout).
    gitignore = sync_dir / ".gitignore"
    if not gitignore.exists() or gitignore.read_text(encoding="utf-8") != GITIGNORE_CONTENT:
        gitignore.write_text(GITIGNORE_CONTENT, encoding="utf-8")

    if remote_url is not None:
        # Replace any existing origin to keep state predictable.
        _git(sync_dir, "remote", "remove", "origin", check=False)
        _git(sync_dir, "remote", "add", "origin", remote_url, check=False)

    return SyncRepo(
        sync_dir=sync_dir,
        branch=SYNC_BRANCH,
        remote_url=remote_url,
        initialized=initialized_now,
    )


def export_snapshot(
    kos_dir: str | Path,
    snapshot_path: str | Path,
) -> SnapshotInfo:
    """Read chunks.db + sessions, write deterministic JSON snapshot.

    Determinism: sorted keys, chunks sorted by id, sessions sorted by
    session_id. `exported_at` is included for human triage but does NOT
    affect git diff stability (it'll churn each push, that's fine — the
    bulk of the file stays stable).

    Missing or empty chunks.db → emits an empty-but-valid snapshot.
    """
    kos_dir = Path(kos_dir)
    snapshot_path = Path(snapshot_path)
    db_path = kos_dir / FILE_CHUNKS_DB

    chunks: list[dict] = []
    sessions: list[dict] = []

    if db_path.exists():
        # Open read-only to avoid migration/upgrade side-effects on the
        # main store. We bypass Store() because we don't want to touch
        # WAL or trigger schema checks during sync.
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.execute(
                    "SELECT id, session_id, project, ts, text, kind, "
                    "language, file_refs, asserted_by_user, "
                    "contradicted_by_later_session FROM chunks"
                )
                chunks = [_row_to_chunk_dict(r) for r in cur]
                cur = conn.execute(
                    "SELECT session_id, started_at, ended_at, summary, "
                    "tags, project, chunk_count FROM sessions"
                )
                sessions = [_row_to_session_dict(r) for r in cur]
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            # Corrupt or non-store DB — emit empty snapshot rather than
            # crashing the sync.
            chunks = []
            sessions = []

    chunks.sort(key=lambda c: c["id"] or "")
    sessions.sort(key=lambda s: s["session_id"] or "")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": int(time.time()),
        "chunks": chunks,
        "sessions": sessions,
    }
    # sort_keys + indent for human-diff-friendly output. Determinism for
    # the same chunk set is what makes git diffs minimal.
    rendered = json.dumps(
        payload, sort_keys=True, indent=2, ensure_ascii=False,
    )
    _atomic_write_text(snapshot_path, rendered)

    return SnapshotInfo(
        path=snapshot_path,
        chunks=len(chunks),
        sessions=len(sessions),
        bytes=snapshot_path.stat().st_size,
        schema_version=SCHEMA_VERSION,
    )


def import_snapshot(
    kos_dir: str | Path,
    snapshot_path: str | Path,
) -> MergeReport:
    """Merge a snapshot JSON into the local store.

    Conflict rules:
      - chunks: INSERT OR IGNORE keyed by id (immutable once written).
      - sessions: upsert preferring the most-recent ended_at when both
        sides claim the same session_id.
      - asserted_by_user contradictions on different chunk-ids: both are
        kept (the dedup is purely chunk-id based).
    """
    kos_dir = Path(kos_dir)
    snapshot_path = Path(snapshot_path)

    if not snapshot_path.exists():
        return MergeReport(ok=False, error=f"snapshot not found: {snapshot_path}")

    try:
        raw = snapshot_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        return MergeReport(ok=False, error=f"bad snapshot json: {e}")

    sv = payload.get("schema_version")
    if sv != SCHEMA_VERSION:
        return MergeReport(
            ok=False,
            error=f"schema_version mismatch: got {sv!r}, need {SCHEMA_VERSION}",
        )

    # Lazily import Store so this module stays useful even if Store gets
    # refactored. This also avoids a circular if Store ever pulls sync.
    from .store import Store

    db_path = kos_dir / FILE_CHUNKS_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)

    imported = 0
    skipped = 0
    sessions_upserted = 0
    try:
        records = []
        for c in payload.get("chunks", []):
            file_refs = c.get("file_refs") or []
            if isinstance(file_refs, str):
                try:
                    file_refs = json.loads(file_refs)
                except Exception:
                    file_refs = []
            if not c.get("text"):
                # Defensive: skip rows missing the only NOT NULL field
                # we care about. Still counts as "skipped" so the report
                # is honest.
                skipped += 1
                continue
            records.append({
                "chunk_id": c.get("id"),
                "session_id": c.get("session_id"),
                "project": c.get("project"),
                "ts": int(c.get("ts") or time.time()),
                "text": c["text"],
                "kind": c.get("kind") or "prose",
                "language": c.get("language"),
                "file_refs": file_refs,
                "asserted_by_user": bool(c.get("asserted_by_user", False)),
            })
        if records:
            imported = store.add_chunks_bulk(records)
            skipped += len(records) - imported

        for s in payload.get("sessions", []):
            sid = s.get("session_id") or s.get("id")
            if not sid:
                continue
            existing = store.session(sid)
            new_ended = s.get("ended_at")
            if existing is not None:
                # Most-recent-ended_at wins. None < anything.
                old_ended = existing["ended_at"]
                if old_ended is not None and (
                    new_ended is None or new_ended <= old_ended
                ):
                    # Local copy is already as-fresh-or-fresher; skip.
                    continue
            tags = s.get("tags") or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            store.upsert_session(
                sid,
                started_at=s.get("started_at"),
                ended_at=new_ended,
                project=s.get("project"),
                summary=s.get("summary"),
                tags=tags,
                chunk_count=s.get("chunk_count", 0),
            )
            sessions_upserted += 1
    finally:
        store.close()

    return MergeReport(
        ok=True,
        chunks_imported=imported,
        chunks_skipped=skipped,
        sessions_upserted=sessions_upserted,
    )


def sync_push(
    kos_dir: str | Path,
    message: str | None = None,
) -> PushReport:
    """Snapshot → commit → push to the sync branch."""
    kos_dir = Path(kos_dir)
    sync_dir = _sync_dir_for(kos_dir)

    if not (sync_dir / ".git").exists():
        return PushReport(
            ok=False,
            error="sync repo not initialized; run prepare_sync_repo first",
        )

    snapshot_path = _snapshot_path_for(kos_dir)
    try:
        info = export_snapshot(kos_dir, snapshot_path)
    except Exception as e:
        return PushReport(ok=False, error=f"export failed: {e}")

    msg = message or f"kos-memory-sync: {info.chunks} chunks, {info.sessions} sessions"

    try:
        # Stage .gitignore too if it isn't tracked yet — keeps the
        # initial commit self-contained.
        gitignore = sync_dir / ".gitignore"
        if gitignore.exists():
            _git(sync_dir, "add", "--", ".gitignore", check=False)
        _git(sync_dir, "add", SNAPSHOT_FILENAME)
        # Skip a no-op commit cleanly.
        st = _git(sync_dir, "status", "--porcelain", check=False)
        committed = False
        commit_sha: str | None = None
        if st.stdout.strip():
            _git(sync_dir, "commit", "-m", msg)
            committed = True
        # Always read HEAD sha (even if no new commit, helps callers).
        r = _git(sync_dir, "rev-parse", "HEAD", check=False)
        if r.returncode == 0:
            commit_sha = r.stdout.strip() or None
    except subprocess.CalledProcessError as e:
        return PushReport(
            ok=False,
            snapshot=info,
            error=f"git commit failed: {e}",
            stderr=(e.stderr or "") if hasattr(e, "stderr") else "",
        )
    except subprocess.TimeoutExpired:
        return PushReport(
            ok=False, snapshot=info, error="git commit timed out",
        )

    pushed = False
    # Remote push is optional — a local-only sync repo is still useful.
    has_remote = _git(sync_dir, "remote", check=False).stdout.strip()
    if has_remote:
        try:
            r = _git(
                sync_dir, "push", "origin", SYNC_BRANCH,
                timeout=TIMEOUT_REMOTE_SEC, check=False,
            )
            if r.returncode != 0:
                return PushReport(
                    ok=False, snapshot=info, committed=committed,
                    commit_sha=commit_sha, message=msg,
                    error=f"git push failed: rc={r.returncode}",
                    stderr=r.stderr,
                )
            pushed = True
        except subprocess.TimeoutExpired:
            return PushReport(
                ok=False, snapshot=info, committed=committed,
                commit_sha=commit_sha, message=msg,
                error="git push timed out",
            )

    return PushReport(
        ok=True, snapshot=info, committed=committed, pushed=pushed,
        commit_sha=commit_sha, message=msg,
    )


def sync_pull(kos_dir: str | Path) -> PullReport:
    """Pull from sync branch, then import the snapshot."""
    kos_dir = Path(kos_dir)
    sync_dir = _sync_dir_for(kos_dir)

    if not (sync_dir / ".git").exists():
        return PullReport(
            ok=False,
            error="sync repo not initialized; run prepare_sync_repo first",
        )

    pulled = False
    has_remote = _git(sync_dir, "remote", check=False).stdout.strip()
    if has_remote:
        try:
            # First, check whether we have any local commits. If the
            # local repo is empty (just-initialized, never pushed), do
            # a fetch + reset --hard to adopt origin cleanly — this
            # avoids "untracked .gitignore would be overwritten by
            # merge" errors that occur because prepare_sync_repo writes
            # an unstaged .gitignore.
            head_check = _git(
                sync_dir, "rev-parse", "--verify", "HEAD",
                check=False,
            )
            has_local_commits = head_check.returncode == 0

            r = _git(
                sync_dir, "fetch", "origin", SYNC_BRANCH,
                timeout=TIMEOUT_REMOTE_SEC, check=False,
            )
            if r.returncode != 0:
                return PullReport(
                    ok=False,
                    error=f"git fetch failed: rc={r.returncode}",
                    stderr=r.stderr,
                )

            if has_local_commits:
                # Normal flow — fast-forward the local branch.
                r = _git(
                    sync_dir, "merge", "--ff-only",
                    "FETCH_HEAD",
                    timeout=TIMEOUT_LOCAL_SEC, check=False,
                )
                if r.returncode != 0:
                    return PullReport(
                        ok=False,
                        error=f"git merge --ff-only failed: rc={r.returncode}",
                        stderr=r.stderr,
                    )
            else:
                # No local commits → adopt origin wholesale. The sync
                # tree is just data-in-flight; the source of truth is
                # the project's chunks.db. reset --hard is safe here.
                r = _git(
                    sync_dir, "reset", "--hard", "FETCH_HEAD",
                    timeout=TIMEOUT_LOCAL_SEC, check=False,
                )
                if r.returncode != 0:
                    return PullReport(
                        ok=False,
                        error=f"git reset --hard failed: rc={r.returncode}",
                        stderr=r.stderr,
                    )
            pulled = True
        except subprocess.TimeoutExpired:
            return PullReport(ok=False, error="git pull timed out")

    snapshot_path = _snapshot_path_for(kos_dir)
    if not snapshot_path.exists():
        # No snapshot to import — that's fine, just means nothing has
        # been pushed yet. Caller still gets ok=True so a freshly cloned
        # remote-less init doesn't look like an error.
        return PullReport(
            ok=True, pulled=pulled,
            merge=MergeReport(ok=True, error="no snapshot to import"),
        )

    merge = import_snapshot(kos_dir, snapshot_path)
    return PullReport(ok=merge.ok, pulled=pulled, merge=merge,
                      error=merge.error)
