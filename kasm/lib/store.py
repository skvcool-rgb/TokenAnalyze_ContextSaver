"""SQLite-backed chunk store. Pure stdlib.

Schema v1:
  chunks(
    id TEXT PRIMARY KEY,           -- uuid4
    session_id TEXT,
    project TEXT,
    ts INTEGER,                    -- unix epoch
    text TEXT NOT NULL,
    kind TEXT,                     -- 'prose' | 'code'
    language TEXT,                 -- e.g. 'python' if kind=code
    file_refs TEXT,                -- JSON array of file paths mentioned
    asserted_by_user INTEGER,      -- 0/1, true if explicitly /remember'd
    contradicted_by_later_session INTEGER  -- 0/1, set lazily by recall pipeline
  )

Crash safety: WAL mode, INSERT OR IGNORE for idempotent re-ingest, atomic
commit. Schema versioning via PRAGMA user_version.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from . import SCHEMA_VERSION

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS chunks (
    id                            TEXT PRIMARY KEY,
    session_id                    TEXT,
    project                       TEXT,
    ts                            INTEGER NOT NULL,
    text                          TEXT NOT NULL,
    kind                          TEXT,
    language                      TEXT,
    file_refs                     TEXT,
    asserted_by_user              INTEGER NOT NULL DEFAULT 0,
    contradicted_by_later_session INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chunks_ts ON chunks(ts);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    summary     TEXT,                   -- 1-line (Claude-generated, optional)
    tags        TEXT,                   -- JSON array of topic tags
    project     TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(started_at);
"""


class Store:
    """Thread-unsafe; one Store per process."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._open_and_migrate()

    def _open_and_migrate(self) -> None:
        c = sqlite3.connect(str(self.db_path), timeout=10.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        v = c.execute("PRAGMA user_version").fetchone()[0]
        if v == 0:
            c.executescript(SCHEMA_V1)
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            c.commit()
        elif v > SCHEMA_VERSION:
            c.close()
            raise RuntimeError(
                f"chunks.db schema {v} is newer than this kos-memory ({SCHEMA_VERSION}). Upgrade."
            )
        elif v < SCHEMA_VERSION:
            c.close()
            raise RuntimeError(
                f"chunks.db schema {v} needs migration to {SCHEMA_VERSION}."
            )
        self._conn = c

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open_and_migrate()
        return self._conn  # type: ignore[return-value]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN")
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ── chunk ops ───────────────────────────────────────────
    def add_chunk(
        self,
        text: str,
        *,
        chunk_id: str | None = None,
        session_id: str | None = None,
        project: str | None = None,
        ts: int | None = None,
        kind: str = "prose",
        language: str | None = None,
        file_refs: list[str] | None = None,
        asserted_by_user: bool = False,
    ) -> str:
        cid = chunk_id or str(uuid.uuid4())
        self.conn.execute(
            """INSERT OR IGNORE INTO chunks
               (id, session_id, project, ts, text, kind, language,
                file_refs, asserted_by_user, contradicted_by_later_session)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (cid, session_id, project, ts or int(time.time()), text, kind,
             language, json.dumps(file_refs or []), int(asserted_by_user)),
        )
        self.conn.commit()
        return cid

    def add_chunks_bulk(self, records: list[dict]) -> int:
        inserted = 0
        with self.transaction():
            for r in records:
                cid = r.get("chunk_id") or str(uuid.uuid4())
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO chunks
                       (id, session_id, project, ts, text, kind, language,
                        file_refs, asserted_by_user,
                        contradicted_by_later_session)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                    (cid, r.get("session_id"), r.get("project"),
                     r.get("ts") or int(time.time()), r["text"],
                     r.get("kind", "prose"), r.get("language"),
                     json.dumps(r.get("file_refs") or []),
                     int(r.get("asserted_by_user", False))),
                )
                if cur.rowcount > 0:
                    inserted += 1
        return inserted

    def mark_contradicted(self, chunk_ids: Iterable[str]) -> int:
        ids = list(chunk_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"UPDATE chunks SET contradicted_by_later_session = 1 "
            f"WHERE id IN ({placeholders})",
            ids,
        )
        self.conn.commit()
        return cur.rowcount

    def get_chunks(self, ids: list[str]) -> list[sqlite3.Row]:
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        return list(self.conn.execute(
            f"SELECT * FROM chunks WHERE id IN ({ph})", ids
        ))

    def iter_chunks(self, since_ts: int | None = None) -> Iterable[sqlite3.Row]:
        if since_ts is None:
            cur = self.conn.execute("SELECT * FROM chunks ORDER BY ts ASC")
        else:
            cur = self.conn.execute(
                "SELECT * FROM chunks WHERE ts >= ? ORDER BY ts ASC", (since_ts,)
            )
        for row in cur:
            yield row

    def count(
        self,
        *,
        asserted_by_user: bool | None = None,
        contradicted: bool | None = None,
    ) -> int:
        """Total chunk count, optionally filtered by user-asserted /
        contradicted booleans."""
        clauses: list[str] = []
        params: list = []
        if asserted_by_user is not None:
            clauses.append("asserted_by_user = ?")
            params.append(1 if asserted_by_user else 0)
        if contradicted is not None:
            clauses.append("contradicted_by_later_session = ?")
            params.append(1 if contradicted else 0)
        sql = "SELECT COUNT(*) FROM chunks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(sql, params).fetchone()[0]

    def latest_ts(self) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(ts) FROM chunks"
        ).fetchone()
        return row[0] if row and row[0] else None

    # ── session ops ─────────────────────────────────────────
    def upsert_session(
        self,
        session_id: str,
        *,
        started_at: int | None = None,
        ended_at: int | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        chunk_count: int | None = None,
    ) -> None:
        existing = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if existing:
            new_summary = summary if summary is not None else existing["summary"]
            new_tags = json.dumps(tags) if tags is not None else existing["tags"]
            new_ended = ended_at if ended_at is not None else existing["ended_at"]
            new_count = chunk_count if chunk_count is not None else existing["chunk_count"]
            self.conn.execute(
                """UPDATE sessions SET
                   summary=?, tags=?, ended_at=?, chunk_count=?, project=COALESCE(?, project)
                   WHERE session_id=?""",
                (new_summary, new_tags, new_ended, new_count, project, session_id),
            )
        else:
            self.conn.execute(
                """INSERT INTO sessions
                   (session_id, started_at, ended_at, summary, tags, project, chunk_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, started_at or int(time.time()), ended_at,
                 summary, json.dumps(tags or []), project, chunk_count or 0),
            )
        self.conn.commit()

    def list_sessions(
        self, since_ts: int | None = None, limit: int | None = None
    ) -> list[sqlite3.Row]:
        if since_ts is None:
            sql = "SELECT * FROM sessions ORDER BY started_at DESC"
            params: tuple = ()
        else:
            sql = (
                "SELECT * FROM sessions WHERE started_at >= ? "
                "ORDER BY started_at DESC"
            )
            params = (since_ts,)
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params = params + (limit,)
        return list(self.conn.execute(sql, params))

    def session(self, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
