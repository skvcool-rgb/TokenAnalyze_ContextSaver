"""kos-memory CLI — subcommands invoked by slash commands.

NOT an MCP server. This is a stdlib-only argparse interface that the
markdown slash commands shell out to. Returns JSON on stdout.

Usage:
    python -m mcp.cli <subcommand> [args...]

Subcommands (v5.0):
    recall_stage_a    — Stage 0 (local expansion) + Stage 1 (catalog)
    recall_stage_b    — Stage 2 (grep over selected sessions)
    remember          — Pin user-asserted fact
    status            — Store stats
    export            — Dump store to JSON
    import_export     — Load JSON into store
    rebuild_catalog   — Force catalog refresh
    memory_mode       — Toggle primary/backup mode
    mark_contradicted — Flag chunks as superseded

Subcommands (v6.0):
    bootstrap         — Seed store from project docs + Claude Code transcripts
    curate            — Append/refresh marker-fenced suggestions in MEMORY.md
    sync              — Sidecar-git multi-machine sync (push|pull|init|status)
    test_status       — Detect framework, run collect-only (+ full run if opt-in)
    serve             — Start local HTTP API for non-CC tools (delegates to mcp.http_server)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Add plugin root to path
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.stdio_utf8 import force_utf8_io
force_utf8_io()

from lib.budget import Budget
from lib.catalog import (
    build_catalog,
    render_catalog_for_claude,
    save_catalog,
    session_ids_matching_tags,
)
from lib.chunker import chunk_text, extract_file_refs
from lib.paths import (
    FILE_BUDGET,
    FILE_CATALOG,
    FILE_CHUNKS_DB,
    FILE_INGEST_LOG,
    FILE_LAST_INGEST,
    FILE_SYNONYMS,
    ensure_kos_dir,
)
from lib.recall import (
    RecallContext,
    stage_0_local_expansion,
    stage_1_catalog,
    stage_2_grep,
)
from lib.search import SynonymCache, tokenize
from lib.store import Store


def _emit(data: dict) -> None:
    """Print JSON on stdout (machine-readable)."""
    sys.stdout.write(json.dumps(data, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _err(msg: str, code: int = 1) -> int:
    _emit({"ok": False, "error": msg})
    return code


def _resolve_kos_dir(args) -> Path:
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return ensure_kos_dir(project, user_level=bool(getattr(args, "user", False)))


def cmd_recall_stage_a(args) -> int:
    """Stage 0 (expansion) + Stage 1 (catalog). No LLM."""
    kos_dir = _resolve_kos_dir(args)

    # Throttle check
    budget = Budget(kos_dir / FILE_BUDGET)
    allowed, reason = budget.can_recall(estimated_tokens=2000)
    if not allowed:
        return _err(f"recall throttled: {reason}", code=2)

    rc = RecallContext(
        query=args.query,
        window_days=args.window_days,
        project_root=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
        user_level=bool(args.user),
    )
    stage_0_local_expansion(rc, kos_dir)
    stage_1_catalog(rc, kos_dir)

    _emit({
        "ok": True,
        "kos_dir": str(kos_dir),
        "expanded_terms": rc.expanded_terms,
        "catalog_text": rc.catalog_text,
        "catalog": rc.catalog,
        "stage": "a",
    })
    return 0


def cmd_recall_stage_b(args) -> int:
    """Stage 2 grep over selected sessions."""
    kos_dir = Path(args.kos_dir)
    if not (kos_dir / FILE_CHUNKS_DB).exists():
        return _err(f"no DB at {kos_dir / FILE_CHUNKS_DB}")

    terms = [t.strip() for t in (args.terms or "").split(",") if t.strip()]
    sessions = [s.strip() for s in (args.sessions or "").split(",") if s.strip()]

    rc = RecallContext(
        query=args.query,
        window_days=args.window_days,
        expanded_terms=terms,
        selected_session_ids=sessions,
    )
    stage_2_grep(rc, kos_dir)

    # Record budget usage (estimate tokens by passage size)
    budget = Budget(kos_dir / FILE_BUDGET)
    est_tokens = sum(len(p["text"]) // 4 for p in rc.passages) + 500
    budget.record_recall(tokens=est_tokens, cost_usd=0.0)

    _emit({
        "ok": True,
        "passages": rc.passages,
        "n_passages": len(rc.passages),
        "n_sessions": len({p["session_id"] for p in rc.passages}),
        "stage": "b",
        "timings_ms": rc.timings_ms,
    })
    return 0


def cmd_remember(args) -> int:
    """Pin a user-asserted fact."""
    kos_dir = _resolve_kos_dir(args)
    text = (args.fact or "").strip()
    if not text:
        return _err("empty fact text")

    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    chunks = chunk_text(text, max_chars=400, overlap=50)
    if not chunks:
        return _err("chunking produced no output")

    file_refs = extract_file_refs(text)
    now = int(time.time())
    sid = f"user_pin_{now}"

    store = Store(kos_dir / FILE_CHUNKS_DB)
    inserted_ids = []
    try:
        for c in chunks:
            cid = store.add_chunk(
                text=c.text,
                session_id=sid,
                project=project if not args.user else "user",
                ts=now,
                kind="user_assertion",
                language=c.language,
                file_refs=file_refs,
                asserted_by_user=True,
            )
            inserted_ids.append(cid)
        store.upsert_session(
            sid,
            started_at=now,
            ended_at=now,
            project=project,
            chunk_count=len(inserted_ids),
            tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
            summary=text[:120],
        )
    finally:
        store.close()

    _emit({
        "ok": True,
        "chunk_ids": inserted_ids,
        "kos_dir": str(kos_dir),
        "session_id": sid,
    })
    return 0


def cmd_status(args) -> int:
    """Store stats."""
    kos_dir = _resolve_kos_dir(args)
    db = kos_dir / FILE_CHUNKS_DB
    if not db.exists():
        _emit({
            "ok": True,
            "kos_dir": str(kos_dir),
            "chunks": 0,
            "sessions": 0,
            "last_ingest": None,
            "db_size_bytes": 0,
            "empty": True,
        })
        return 0

    store = Store(db)
    try:
        n_chunks = store.count()
        n_user_assert = store.count(asserted_by_user=True)
        n_contra = store.count(contradicted=True)
        latest = store.latest_ts()
        sessions_total = len(store.list_sessions())
        recent_sessions = [dict(s) for s in store.list_sessions(limit=5)]
    finally:
        store.close()

    budget_state = Budget(kos_dir / FILE_BUDGET).status()

    _emit({
        "ok": True,
        "kos_dir": str(kos_dir),
        "chunks": n_chunks,
        "user_asserted": n_user_assert,
        "contradicted": n_contra,
        "sessions_total": sessions_total,
        "recent_sessions": recent_sessions,
        "latest_ts": latest,
        "db_size_bytes": db.stat().st_size,
        "budget": budget_state,
        "verbose": bool(args.verbose),
    })
    return 0


def cmd_export(args) -> int:
    """Dump store to JSON."""
    kos_dir = _resolve_kos_dir(args)
    db = kos_dir / FILE_CHUNKS_DB
    if not db.exists():
        return _err(f"no DB at {db}")

    out_path = Path(args.out) if args.out else Path.cwd() / (
        f"kos-memory-export-{time.strftime('%Y%m%d')}.json"
    )

    since_ts = None
    if args.since:
        try:
            since_ts = int(time.mktime(time.strptime(args.since, "%Y-%m-%d")))
        except Exception:
            return _err(f"bad --since date: {args.since} (use YYYY-MM-DD)")

    store = Store(db)
    try:
        # Convert Row objects to dicts for JSON serialization
        chunks = [dict(r) for r in store.iter_chunks(since_ts=since_ts)]
        if not args.include_contradicted:
            chunks = [c for c in chunks if not c["contradicted_by_later_session"]]
        # Re-decode JSON-encoded fields so the export is human-readable
        for c in chunks:
            try:
                c["file_refs"] = json.loads(c.get("file_refs") or "[]")
            except Exception:
                c["file_refs"] = []
        sessions = [dict(s) for s in store.list_sessions()]
        for s in sessions:
            try:
                s["tags"] = json.loads(s.get("tags") or "[]")
            except Exception:
                s["tags"] = []
    finally:
        store.close()

    payload = {
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": str(kos_dir),
        "chunks": chunks,
        "sessions": sessions,
    }

    # Atomic write
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)

    _emit({
        "ok": True,
        "path": str(out_path),
        "chunks_written": len(chunks),
        "sessions_written": len(sessions),
        "bytes": out_path.stat().st_size,
    })
    return 0


def cmd_import_export(args) -> int:
    """Load JSON export into store."""
    src = Path(args.path)
    if not src.exists():
        return _err(f"no such file: {src}")

    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        return _err(f"bad JSON: {e}")

    if payload.get("version") != 1:
        return _err(f"version mismatch: got {payload.get('version')}, need 1")

    kos_dir = _resolve_kos_dir(args)
    db_path = kos_dir / FILE_CHUNKS_DB

    if args.replace and db_path.exists():
        # Destructive — caller should have confirmed
        db_path.unlink()

    store = Store(db_path)
    skipped = 0
    imported = 0
    try:
        # Use bulk insert so rowcount reflects ACTUAL inserts (INSERT OR IGNORE
        # returns rowcount=0 on duplicate). Per-chunk add returns the id
        # regardless, so we'd lose the dedup signal otherwise.
        records = []
        for c in payload.get("chunks", []):
            if c.get("contradicted_by_later_session") and not args.include_contradicted:
                skipped += 1
                continue
            file_refs = c.get("file_refs", [])
            if isinstance(file_refs, str):
                try:
                    file_refs = json.loads(file_refs)
                except Exception:
                    file_refs = []
            records.append({
                "chunk_id": c.get("id"),
                "session_id": c.get("session_id"),
                "project": c.get("project", ""),
                "ts": int(c.get("ts") or time.time()),
                "text": c["text"],
                "kind": c.get("kind", "prose"),
                "language": c.get("language"),
                "file_refs": file_refs,
                "asserted_by_user": bool(c.get("asserted_by_user", False)),
            })
        if records:
            imported = store.add_chunks_bulk(records)
            skipped += len(records) - imported

        sessions_upserted = 0
        for s in payload.get("sessions", []):
            tags = s.get("tags", [])
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            sid = s.get("session_id") or s.get("id")
            if not sid:
                continue
            store.upsert_session(
                sid,
                started_at=s.get("started_at"),
                ended_at=s.get("ended_at"),
                project=s.get("project", ""),
                summary=s.get("summary", ""),
                tags=tags,
                chunk_count=s.get("chunk_count", 0),
            )
            sessions_upserted += 1
    finally:
        store.close()

    _emit({
        "ok": True,
        "chunks_imported": imported,
        "chunks_skipped": skipped,
        "sessions_upserted": sessions_upserted,
    })
    return 0


def cmd_rebuild_catalog(args) -> int:
    """Force catalog rebuild."""
    kos_dir = _resolve_kos_dir(args)
    db = kos_dir / FILE_CHUNKS_DB
    if not db.exists():
        return _err("no DB")

    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    store = Store(db)
    try:
        catalog = build_catalog(store, project=project)
        save_catalog(kos_dir / FILE_CATALOG, catalog)
    finally:
        store.close()

    _emit({"ok": True, "catalog_path": str(kos_dir / FILE_CATALOG)})
    return 0


def cmd_memory_mode(args) -> int:
    """Inspect or set the active kos-memory mode."""
    from lib.paths import (
        FILE_CONFIG,
        VALID_MODES,
        get_mode,
        set_mode,
    )

    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if args.mode:
        if args.mode not in VALID_MODES:
            return _err(f"invalid mode {args.mode!r}, must be one of {VALID_MODES}")
        cfg = set_mode(args.mode, project_root=project,
                       user_level=bool(args.user))
        _emit({
            "ok": True,
            "mode": args.mode,
            "scope": "user" if args.user else "project",
            "config_path": str(cfg),
        })
        return 0

    # Inspect both scopes
    proj_mode = get_mode(project_root=project)
    # User-level resolution: bypass project config to reveal user scope only
    import os as _os
    saved = _os.environ.pop("KOS_MEMORY_MODE", None)
    try:
        from lib.paths import ensure_kos_dir
        user_kos = ensure_kos_dir(None, user_level=True)
        user_cfg = user_kos / FILE_CONFIG
        user_mode = None
        if user_cfg.exists():
            try:
                import json as _json
                user_mode = _json.loads(user_cfg.read_text(encoding="utf-8")).get("mode")
            except Exception:
                user_mode = None
    finally:
        if saved is not None:
            _os.environ["KOS_MEMORY_MODE"] = saved

    _emit({
        "ok": True,
        "active_mode": proj_mode,
        "project_mode": proj_mode,
        "user_mode": user_mode,
        "env_override": _os.environ.get("KOS_MEMORY_MODE"),
        "valid_modes": list(VALID_MODES),
    })
    return 0


def cmd_bootstrap(args) -> int:
    """v6.0: one-shot seed of chunks.db from project docs + CC transcripts."""
    from lib.bootstrap import DEFAULT_MAX_TRANSCRIPTS, bootstrap_project
    kos_dir = _resolve_kos_dir(args)
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    report = bootstrap_project(project, max_transcripts=int(args.max_transcripts))
    _emit({
        "ok": (not report.errors) or (report.chunks_added > 0),
        "kos_dir": str(kos_dir),
        **report.as_dict(),
    })
    return 0


def cmd_curate(args) -> int:
    """v6.0: refresh marker-fenced auto-suggestions block in MEMORY.md."""
    from lib.auto_suggestions import (
        append_to_memory_md,
        extract_high_value_chunks,
        format_suggestions_block,
    )
    from lib.memory_md import find_memory_files
    kos_dir = _resolve_kos_dir(args)
    db = kos_dir / FILE_CHUNKS_DB
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    suggestions: list = []
    if db.exists():
        store = Store(db)
        try:
            suggestions = extract_high_value_chunks(
                store.iter_chunks(), max_n=20,
            )
        finally:
            store.close()

    block = format_suggestions_block(suggestions, project_name=project)

    if args.target:
        target = Path(args.target)
    else:
        files = find_memory_files(project)
        target = files[0].path if files else Path(project) / "MEMORY.md"

    if args.write:
        report = append_to_memory_md(
            target, block, suggestion_count=len(suggestions),
        )
        _emit({
            "ok": not report.errors,
            "mode": "write",
            "target": str(target),
            "suggestion_count": len(suggestions),
            "block": block,
            "report": {
                "path": report.path, "was_appended": report.was_appended,
                "was_replaced": report.was_replaced,
                "bytes_written": report.bytes_written,
                "suggestion_count": report.suggestion_count,
                "errors": report.errors,
            },
        })
        return 0 if not report.errors else 1

    _emit({
        "ok": True, "mode": "preview", "target": str(target),
        "suggestion_count": len(suggestions), "block": block, "report": None,
    })
    return 0


def cmd_sync(args) -> int:
    """v6.0: sidecar-git multi-machine sync."""
    from lib.sync import (
        SYNC_BRANCH,
        _sync_dir_for,
        prepare_sync_repo,
        sync_pull,
        sync_push,
    )
    kos_dir = _resolve_kos_dir(args)
    sub = args.sync_cmd
    if sub == "init":
        repo = prepare_sync_repo(kos_dir, remote_url=args.remote)
        _emit({
            "ok": True, "sync_dir": str(repo.sync_dir),
            "branch": repo.branch, "remote": repo.remote_url,
            "initialized": repo.initialized,
        })
        return 0
    if sub == "push":
        r = sync_push(kos_dir, message=args.message)
        _emit({
            "ok": r.ok, "committed": r.committed, "pushed": r.pushed,
            "commit_sha": r.commit_sha, "error": r.error,
            "snapshot": (
                {"chunks": r.snapshot.chunks,
                 "sessions": r.snapshot.sessions,
                 "bytes": r.snapshot.bytes}
                if r.snapshot else None
            ),
        })
        return 0 if r.ok else 1
    if sub == "pull":
        r = sync_pull(kos_dir)
        merge = r.merge
        _emit({
            "ok": r.ok, "pulled": r.pulled, "error": r.error,
            "merge": (
                {"chunks_imported": merge.chunks_imported,
                 "chunks_skipped": merge.chunks_skipped,
                 "sessions_upserted": merge.sessions_upserted}
                if merge else None
            ),
        })
        return 0 if r.ok else 1
    if sub == "status":
        sd = _sync_dir_for(kos_dir)
        _emit({
            "ok": True, "sync_dir": str(sd),
            "exists": (sd / ".git").exists(), "branch": SYNC_BRANCH,
        })
        return 0
    return _err(f"unknown sync subcommand: {sub}")


def cmd_test_status(args) -> int:
    """v6.0: detect framework, run collect-only, optionally run full suite."""
    from dataclasses import asdict
    from lib.test_runner import (
        detect_framework,
        is_run_tests_enabled,
        run_collect_only,
        run_full_suite,
    )
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    fw = detect_framework(project)
    payload: dict = {
        "ok": True,
        "framework": fw.name if fw else None,
        "framework_command": fw.command if fw else None,
    }
    collect = run_collect_only(project, fw, timeout_s=args.collect_timeout_s)
    payload["collect"] = asdict(collect)
    if args.run or is_run_tests_enabled(project):
        run = run_full_suite(project, fw, timeout_s=args.run_timeout_s)
        payload["run"] = asdict(run)
    _emit(payload)
    return 0


def cmd_serve(args) -> int:
    """v6.0: start local HTTP API (delegates to mcp.http_server)."""
    from mcp import http_server as _hs
    argv = ["--port", str(args.port)]
    if args.token:
        argv += ["--token", args.token]
    return _hs.main(argv)


def cmd_mark_contradicted(args) -> int:
    """Flag chunks as superseded by later session."""
    kos_dir = _resolve_kos_dir(args)
    db = kos_dir / FILE_CHUNKS_DB
    if not db.exists():
        return _err("no DB")

    ids = [i.strip() for i in (args.ids or "").split(",") if i.strip()]
    if not ids:
        return _err("no chunk ids supplied")

    store = Store(db)
    try:
        # mark_contradicted takes an iterable and returns the count of rows updated
        n = store.mark_contradicted(ids)
    finally:
        store.close()

    _emit({"ok": True, "marked": n, "requested": len(ids)})
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kos-memory-cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("recall_stage_a", help="Stage 0+1: expansion + catalog")
    pa.add_argument("--query", required=True)
    pa.add_argument("--window-days", type=int, default=30)
    pa.add_argument("--user", action="store_true")
    pa.set_defaults(func=cmd_recall_stage_a)

    pb = sub.add_parser("recall_stage_b", help="Stage 2: grep selected sessions")
    pb.add_argument("--kos-dir", required=True)
    pb.add_argument("--query", required=True)
    pb.add_argument("--terms", default="")
    pb.add_argument("--sessions", default="")
    pb.add_argument("--window-days", type=int, default=30)
    pb.set_defaults(func=cmd_recall_stage_b)

    pr = sub.add_parser("remember", help="Pin user-asserted fact")
    pr.add_argument("--fact", required=True)
    pr.add_argument("--tags", default="")
    pr.add_argument("--user", action="store_true")
    pr.set_defaults(func=cmd_remember)

    ps = sub.add_parser("status", help="Store stats")
    ps.add_argument("--user", action="store_true")
    ps.add_argument("--verbose", action="store_true")
    ps.set_defaults(func=cmd_status)

    pe = sub.add_parser("export", help="Dump to JSON")
    pe.add_argument("--out", default=None)
    pe.add_argument("--user", action="store_true")
    pe.add_argument("--since", default=None)
    pe.add_argument("--include-contradicted", action="store_true")
    pe.set_defaults(func=cmd_export)

    pi = sub.add_parser("import_export", help="Load JSON into store")
    pi.add_argument("--path", required=True)
    pi.add_argument("--user", action="store_true")
    pi.add_argument("--replace", action="store_true")
    pi.add_argument("--include-contradicted", action="store_true")
    pi.set_defaults(func=cmd_import_export)

    prc = sub.add_parser("rebuild_catalog", help="Force catalog refresh")
    prc.add_argument("--user", action="store_true")
    prc.set_defaults(func=cmd_rebuild_catalog)

    pmode = sub.add_parser("memory_mode", help="Inspect or set primary/backup mode")
    pmode.add_argument("--mode", choices=("primary", "backup"), default=None)
    pmode.add_argument("--user", action="store_true")
    pmode.set_defaults(func=cmd_memory_mode)

    pm = sub.add_parser("mark_contradicted", help="Flag chunks superseded")
    pm.add_argument("--kos-dir", default=None)
    pm.add_argument("--user", action="store_true")
    pm.add_argument("--ids", required=True)
    pm.set_defaults(func=cmd_mark_contradicted)

    # ── v6.0 subcommands ─────────────────────────────────────
    pbs = sub.add_parser("bootstrap",
                         help="Seed store from project docs + CC transcripts")
    pbs.add_argument("--max-transcripts", type=int, default=10)
    pbs.add_argument("--user", action="store_true")  # accepted; project-scoped
    pbs.set_defaults(func=cmd_bootstrap)

    pcu = sub.add_parser(
        "curate",
        help="Append/refresh marker-fenced suggestions in MEMORY.md",
    )
    pcu.add_argument("--preview", action="store_true")
    pcu.add_argument("--write", action="store_true")
    pcu.add_argument("--target", default=None)
    pcu.add_argument("--user", action="store_true")
    pcu.set_defaults(func=cmd_curate)

    psy = sub.add_parser("sync",
                         help="Sidecar-git multi-machine sync")
    psy.add_argument("sync_cmd", choices=("init", "push", "pull", "status"))
    psy.add_argument("--remote", default=None)
    psy.add_argument("--message", default=None)
    psy.add_argument("--user", action="store_true")
    psy.set_defaults(func=cmd_sync)

    pts = sub.add_parser(
        "test_status",
        help="Detect framework + collect-only (+ run if opt-in)",
    )
    pts.add_argument("--run", action="store_true",
                     help="Force full-suite run (overrides config gate)")
    pts.add_argument("--collect-timeout-s", type=int, default=10)
    pts.add_argument("--run-timeout-s", type=int, default=120)
    pts.set_defaults(func=cmd_test_status)

    psv = sub.add_parser(
        "serve",
        help="Start local HTTP API (delegates to mcp.http_server)",
    )
    psv.add_argument("--port", type=int, default=7621)
    psv.add_argument("--token", default=None)
    psv.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
