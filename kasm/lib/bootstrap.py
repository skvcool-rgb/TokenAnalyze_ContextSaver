"""First-session bootstrap — seed chunks.db from sources the user already has.

Closes gap #6 ("first-session bootstrap is empty") in kos-memory v6. On a
brand-new project, MEMORY.md / catalog / chunks.db are all empty, so the
SessionStart reconstruction block has nothing to surface. This module does
a one-shot, idempotent ingest from two source types the operator usually
already has on disk:

  1. Top-level project docs:  README.md, CHANGELOG.md, ARCHITECTURE.md,
                              DEPLOYMENT.md, HANDOVER.md, ROADMAP.md
  2. Existing Claude Code transcripts:
       ~/.claude/projects/<encoded>/*.jsonl
     where <encoded> is `lib.memory_md.encode_project_path(project_root)`.

Idempotency: chunk_ids are derived from a sha1 of the chunk text, so
re-running bootstrap produces zero new chunks. No timestamps in the id.

Safety guards:

  - Per-doc read cap:        200 KB  (anything over is truncated)
  - Per-transcript read cap:  60 KB  (last-N bytes — same as Stop hook)
  - Max transcripts:          10 most-recent JSONLs
  - Secret skip:              chunks <200 chars matching common credential
                              patterns (api[_ ]key, password, sk-, ghp_) are
                              dropped entirely

Pure stdlib. Hook-safe (degrades to empty BootstrapReport on any failure).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .chunker import chunk_text, extract_file_refs
from .memory_md import encode_project_path
from .paths import FILE_CHUNKS_DB, ensure_kos_dir
from .store import Store

# ── Public configuration ────────────────────────────────────────────────
# Top-level project docs we ingest, in priority/listing order.
DOC_FILENAMES: tuple[str, ...] = (
    "README.md",
    "CHANGELOG.md",
    "ARCHITECTURE.md",
    "DEPLOYMENT.md",
    "HANDOVER.md",
    "ROADMAP.md",
)

# Per-source read caps (bytes). Keep bootstrap bounded — a 5 MB README
# would otherwise dominate the store on first run.
MAX_DOC_BYTES = 200_000
MAX_TRANSCRIPT_BYTES = 60_000

# Default ceiling on how many transcripts we walk on a single bootstrap.
DEFAULT_MAX_TRANSCRIPTS = 10

# Tags written onto chunks (read by catalog clustering downstream).
KIND_DOC = "bootstrap_doc"
KIND_TRANSCRIPT = "bootstrap_transcript"

# Secret-detection: if a chunk is short AND matches one of these patterns
# we drop it. Long chunks (>=200 chars) are allowed because the surrounding
# prose dilutes the credential signal — but we still err on the safe side
# below.
_SECRET_RE = re.compile(
    r"(?i)(?:api[_ ]?key|password|secret[_ ]?key|sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,})"
)
_SECRET_SHORT_THRESHOLD = 200  # chars

# Hard upper bound on the body of any single chunk we send to the secret
# scanner — pathological multi-megabyte single lines should not block the
# regex engine.
_SECRET_SCAN_CAP = 4_000


# ── Dataclasses ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BootstrapSource:
    """A discovered ingest source — doc or transcript."""
    kind: str          # "doc" | "transcript"
    path: Path
    size_bytes: int
    mtime: int


@dataclass
class BootstrapReport:
    """Outcome of a bootstrap_chunks() run."""
    docs_ingested: int = 0
    transcripts_ingested: int = 0
    chunks_added: int = 0
    chunks_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "docs_ingested": self.docs_ingested,
            "transcripts_ingested": self.transcripts_ingested,
            "chunks_added": self.chunks_added,
            "chunks_skipped": self.chunks_skipped,
            "errors": list(self.errors),
        }


# ── Discovery ───────────────────────────────────────────────────────────
def _stat_source(path: Path, kind: str) -> BootstrapSource | None:
    try:
        st = path.stat()
        return BootstrapSource(
            kind=kind, path=path, size_bytes=int(st.st_size),
            mtime=int(st.st_mtime),
        )
    except Exception:
        return None


def _find_docs(project_root: Path) -> list[BootstrapSource]:
    out: list[BootstrapSource] = []
    for name in DOC_FILENAMES:
        p = project_root / name
        try:
            if p.exists() and p.is_file():
                src = _stat_source(p, "doc")
                if src:
                    out.append(src)
        except Exception:
            continue
    return out


def _transcripts_dir(project_root: Path) -> Path | None:
    """Return ~/.claude/projects/<encoded>/ for this project, or None."""
    home = Path(
        os.environ.get("USERPROFILE")
        or os.environ.get("HOME")
        or str(Path.home())
    )
    encoded = encode_project_path(project_root)
    d = home / ".claude" / "projects" / encoded
    try:
        if d.exists() and d.is_dir():
            return d
    except Exception:
        return None
    return None


def _find_transcripts(
    project_root: Path,
    max_transcripts: int = DEFAULT_MAX_TRANSCRIPTS,
) -> list[BootstrapSource]:
    d = _transcripts_dir(project_root)
    if d is None:
        return []
    try:
        jsonls = list(d.glob("*.jsonl"))
    except Exception:
        return []
    # Most-recent first
    jsonls.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True)
    out: list[BootstrapSource] = []
    for p in jsonls[: max(0, max_transcripts)]:
        src = _stat_source(p, "transcript")
        if src:
            out.append(src)
    return out


def find_bootstrap_sources(
    project_root: str | Path,
    *,
    max_transcripts: int = DEFAULT_MAX_TRANSCRIPTS,
) -> list[BootstrapSource]:
    """Discover all bootstrap sources for a given project root.

    Always-safe: returns [] on any error rather than raising. Order is
    docs first (deterministic by DOC_FILENAMES), then transcripts (most-
    recent first, capped at max_transcripts).
    """
    proj = Path(project_root).resolve()
    out: list[BootstrapSource] = []
    out.extend(_find_docs(proj))
    out.extend(_find_transcripts(proj, max_transcripts=max_transcripts))
    return out


# ── Reading ─────────────────────────────────────────────────────────────
def _read_capped(path: Path, cap: int) -> str | None:
    """Read up to `cap` bytes from path. Returns None on failure."""
    try:
        with open(path, "rb") as f:
            data = f.read(cap)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def _read_transcript_tail(path: Path, cap: int) -> str | None:
    """Read the LAST `cap` bytes of a JSONL transcript (mirrors Stop hook)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - cap))
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_transcript_messages(transcript: str) -> str:
    """Pull conversational text from JSONL — same shape as Stop._extract_messages."""
    out: list[str] = []
    for line in transcript.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message", rec)
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or rec.get("type", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                (c.get("text", "") if isinstance(c, dict) else str(c))
                for c in content
            )
        if not isinstance(content, str) or not content.strip():
            continue
        if role in ("user", "assistant"):
            out.append(f"[{role}] {content[:2000]}")
    # Cap to last 30 turns — same as Stop hook
    return "\n\n".join(out[-30:])


# ── Secret filtering ────────────────────────────────────────────────────
def _looks_like_secret(text: str) -> bool:
    """True if a chunk is short AND matches a credential pattern."""
    if len(text) >= _SECRET_SHORT_THRESHOLD:
        return False
    sample = text if len(text) <= _SECRET_SCAN_CAP else text[:_SECRET_SCAN_CAP]
    return _SECRET_RE.search(sample) is not None


# ── Idempotent chunk_id ─────────────────────────────────────────────────
def _content_chunk_id(text: str) -> str:
    """Deterministic 16-char id derived from the chunk text. Re-running
    bootstrap on the same content produces the same id, so INSERT OR IGNORE
    in the store dedupes naturally."""
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# ── Ingest ──────────────────────────────────────────────────────────────
def _ingest_one_source(
    src: BootstrapSource,
    *,
    project: str,
    session_id: str,
    now: int,
    store: Store,
    report: BootstrapReport,
) -> None:
    """Read one source, chunk it, secret-filter, bulk-insert."""
    if src.kind == "doc":
        text = _read_capped(src.path, MAX_DOC_BYTES)
        chunk_kind = KIND_DOC
    elif src.kind == "transcript":
        raw = _read_transcript_tail(src.path, MAX_TRANSCRIPT_BYTES)
        text = _extract_transcript_messages(raw) if raw else None
        chunk_kind = KIND_TRANSCRIPT
    else:
        report.errors.append(f"unknown source kind: {src.kind}")
        return

    if not text or not text.strip():
        return

    chunks = chunk_text(text, max_chars=400, overlap=50)
    if not chunks:
        return

    file_refs = extract_file_refs(text)
    records: list[dict] = []
    for c in chunks:
        if _looks_like_secret(c.text):
            report.chunks_skipped += 1
            continue
        records.append({
            "chunk_id": _content_chunk_id(c.text),
            "session_id": session_id,
            "project": project,
            "ts": now,
            "text": c.text,
            "kind": chunk_kind,
            "language": c.language,
            "file_refs": file_refs,
            "asserted_by_user": False,
        })

    if not records:
        return

    try:
        added = store.add_chunks_bulk(records)
    except Exception as e:
        report.errors.append(f"store insert failed for {src.path.name}: {e}")
        return

    skipped_dupes = len(records) - added
    if skipped_dupes > 0:
        report.chunks_skipped += skipped_dupes
    report.chunks_added += added

    if added > 0 or skipped_dupes > 0:
        if src.kind == "doc":
            report.docs_ingested += 1
        else:
            report.transcripts_ingested += 1


def bootstrap_chunks(
    project_root: str | Path,
    store: Store,
    *,
    sources: list[BootstrapSource] | None = None,
    max_transcripts: int = DEFAULT_MAX_TRANSCRIPTS,
) -> BootstrapReport:
    """Seed `store` from project docs + Claude Code transcripts.

    Idempotent: chunk_ids are content-hashed, so re-running on the same
    sources produces 0 new chunks (skipped duplicates show up in
    BootstrapReport.chunks_skipped).

    Args:
      project_root:   Project directory whose docs we read AND whose
                      Claude-Code transcript dir we look up.
      store:          Open Store. Caller owns lifecycle (do not close inside).
      sources:        Optional pre-discovered list (skip discovery). When
                      None, calls find_bootstrap_sources() with
                      max_transcripts.
      max_transcripts: Cap on how many *.jsonl files we ingest. Ignored
                      if `sources` is supplied.

    Returns: BootstrapReport — counts + errors.
    """
    proj = Path(project_root).resolve()
    project = str(proj)
    report = BootstrapReport()

    if sources is None:
        sources = find_bootstrap_sources(proj, max_transcripts=max_transcripts)

    if not sources:
        return report

    now = int(time.time())
    session_id = f"bootstrap_{now}"

    for src in sources:
        try:
            _ingest_one_source(
                src,
                project=project,
                session_id=session_id,
                now=now,
                store=store,
                report=report,
            )
        except Exception as e:
            report.errors.append(f"{src.path.name}: {e}")

    # Record a single "session" entry so catalog clustering can see
    # bootstrap activity. Only do this if at least one chunk was added —
    # we don't want phantom sessions on empty re-runs.
    if report.chunks_added > 0:
        try:
            store.upsert_session(
                session_id,
                started_at=now,
                ended_at=now,
                project=project,
                chunk_count=report.chunks_added,
                summary="kos-memory bootstrap (docs + transcripts)",
                tags=["bootstrap"],
            )
        except Exception as e:
            report.errors.append(f"upsert_session failed: {e}")

    return report


# ── Convenience entrypoint for CLI / hooks ──────────────────────────────
def bootstrap_project(
    project_root: str | Path | None = None,
    *,
    max_transcripts: int = DEFAULT_MAX_TRANSCRIPTS,
) -> BootstrapReport:
    """High-level: open the project's chunks.db and run bootstrap_chunks().

    Useful for the slash command and the empty-store nudge. Always opens
    + closes the store itself; never raises (errors land in the report).
    """
    proj = Path(project_root).resolve() if project_root else Path.cwd()
    try:
        kos_dir = ensure_kos_dir(proj, user_level=False)
    except Exception as e:
        r = BootstrapReport()
        r.errors.append(f"ensure_kos_dir failed: {e}")
        return r

    store = Store(kos_dir / FILE_CHUNKS_DB)
    try:
        return bootstrap_chunks(
            proj, store, max_transcripts=max_transcripts,
        )
    finally:
        store.close()
