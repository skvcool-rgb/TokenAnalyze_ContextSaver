"""Auto-suggestions for MEMORY.md curation — append-only, marker-fenced.

MEMORY.md is **operator-curated truth**. We never rewrite or reflow it.
Instead we mine the chunks store for high-value candidates and append
them inside a clearly-marked block that the operator reviews on their
own schedule:

    <!-- KOS-AUTO-START v1 -->
    ## Auto-extracted suggestions (operator review)
    ...rendered suggestions...
    <!-- KOS-AUTO-END -->

The contract this module enforces:

  1. Anything *outside* the markers is preserved byte-for-byte.
  2. If markers are missing the block is appended at EOF (with one
     surrounding blank line for readability) — never inserted in the
     middle of operator content.
  3. Writes are atomic via tmp + os.replace.
  4. Empty / no-suggestions runs still produce a well-formed (empty)
     block so the operator can see the marker exists.

Pure stdlib. No I/O failures escape — the WriteReport reports them.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ── Marker contract (versioned so we can evolve the format later) ───
MARKER_START = "<!-- KOS-AUTO-START v1 -->"
MARKER_END = "<!-- KOS-AUTO-END -->"
SECTION_HEADING = "## Auto-extracted suggestions (operator review)"

# ── Tunables ─────────────────────────────────────────────────────────
MIN_AGE_SECS = 7 * 24 * 3600       # < 7 days — still in flux, skip
MAX_AGE_SECS = 180 * 24 * 3600     # > 180 days — archive territory, skip
TEXT_PREVIEW_CHARS = 200
DEFAULT_MAX_SUGGESTIONS = 20

# Score multipliers
BOOST_USER_ASSERTED = 3.0
BOOST_DECISION_PATTERN = 2.0
BOOST_VERSION_TOKEN = 1.5

# Decision-pattern phrases (case-insensitive). Compiled once.
_DECISION_PATTERNS = [
    re.compile(r"\bwe\s+chose\s+\w+", re.IGNORECASE),
    re.compile(r"\bwe\s+decided\s+to\s+\w+", re.IGNORECASE),
    re.compile(r"\bdecided\s+to\s+\w+", re.IGNORECASE),
    re.compile(r"\bswitched\s+from\s+\w+\s+to\s+\w+", re.IGNORECASE),
    re.compile(r"\bmigrat(?:ed|ing)\s+from\s+\w+\s+to\s+\w+", re.IGNORECASE),
    re.compile(r"\bpicked\s+\w+\s+over\s+\w+", re.IGNORECASE),
    re.compile(r"\bchose\s+\w+\s+over\s+\w+", re.IGNORECASE),
    re.compile(r"\bsettled\s+on\s+\w+", re.IGNORECASE),
]

_VERSION_TOKEN = re.compile(r"\bv\d+\.\d+(?:\.\d+)?", re.IGNORECASE)


@dataclass
class Suggestion:
    """A high-value chunk surfaced for operator review."""
    chunk_id: str
    ts: int
    text_preview: str           # capped at TEXT_PREVIEW_CHARS
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class WriteReport:
    """Outcome of an append_to_memory_md call."""
    path: str
    was_appended: bool = False    # True if markers were absent and we added them
    was_replaced: bool = False    # True if existing markers had their body replaced
    bytes_written: int = 0
    suggestion_count: int = 0
    errors: list[str] = field(default_factory=list)


# ── Scoring ──────────────────────────────────────────────────────────
def _score_chunk(row, now: int) -> tuple[float, list[str]]:
    """Compute heuristic score + human-readable reasons for one row.

    `row` is a sqlite3.Row or any mapping with the chunks columns.
    Returns (0.0, []) if the chunk should be filtered out entirely
    (too young / too old / contradicted)."""
    try:
        ts = int(row["ts"])
    except Exception:
        return (0.0, [])

    age = now - ts
    if age < MIN_AGE_SECS:
        return (0.0, [])  # < 7 days: still in flux
    if age > MAX_AGE_SECS:
        return (0.0, [])  # > 180 days: archive

    # Skip contradicted chunks — operator already superseded them
    try:
        if int(row["contradicted_by_later_session"] or 0):
            return (0.0, [])
    except Exception:
        pass

    text = row["text"] or ""
    if not text.strip():
        return (0.0, [])

    score = 1.0
    reasons: list[str] = []

    # User-asserted: 3x
    try:
        if int(row["asserted_by_user"] or 0):
            score *= BOOST_USER_ASSERTED
            reasons.append("user-asserted")
    except Exception:
        pass

    # Decision pattern: 2x (only count once even if multiple patterns hit)
    for pat in _DECISION_PATTERNS:
        if pat.search(text):
            score *= BOOST_DECISION_PATTERN
            reasons.append("decision-phrase")
            break

    # Version token: 1.5x (e.g. "v1.0", "v0.7.27")
    if _VERSION_TOKEN.search(text):
        score *= BOOST_VERSION_TOKEN
        reasons.append("version-token")

    return (score, reasons)


def extract_high_value_chunks(
    chunks_iter: Iterable,
    max_n: int = DEFAULT_MAX_SUGGESTIONS,
    now: int | None = None,
) -> list[Suggestion]:
    """Heuristically rank chunks; return at most max_n suggestions.

    Boosts:
      * 3x user-asserted
      * 2x decision-pattern phrases (we chose / decided to / switched from)
      * 1.5x version-bump tokens (v\\d+\\.\\d+...)

    Filters chunks aged < 7 days (still in flux) and > 180 days (archive),
    plus contradicted chunks. Stable ordering: score desc, ts desc.
    """
    if now is None:
        now = int(time.time())

    scored: list[Suggestion] = []
    for row in chunks_iter:
        try:
            score, reasons = _score_chunk(row, now)
        except Exception:
            continue
        if score <= 0.0:
            continue

        text = row["text"] or ""
        preview = text[:TEXT_PREVIEW_CHARS]
        if len(text) > TEXT_PREVIEW_CHARS:
            preview = preview.rstrip() + "..."

        scored.append(Suggestion(
            chunk_id=str(row["id"]),
            ts=int(row["ts"]),
            text_preview=preview,
            score=score,
            reasons=reasons,
        ))

    # Stable sort: higher score first, then more recent first, then chunk_id
    scored.sort(key=lambda s: (-s.score, -s.ts, s.chunk_id))
    return scored[: max(0, int(max_n))]


# ── Block formatting ─────────────────────────────────────────────────
def _fmt_date(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))
    except Exception:
        return "?"


def format_suggestions_block(
    suggestions: list[Suggestion],
    project_name: str = "",
) -> str:
    """Produce the marker-fenced block. Idempotent for the same input."""
    project_label = project_name.strip() or "this project"
    lines: list[str] = []
    lines.append(MARKER_START)
    lines.append(SECTION_HEADING)
    lines.append("")
    lines.append(
        f"Auto-extracted from chunks store for {project_label}. "
        "Operator-curated content above/below these markers is "
        "preserved untouched. Edit / promote / delete entries below as "
        "you see fit — the next /memory-curate run replaces only the "
        "fenced region."
    )
    lines.append("")

    if not suggestions:
        lines.append("_No high-value candidates this run._")
    else:
        lines.append(f"Top {len(suggestions)} candidates "
                     "(score, date, reasons):")
        lines.append("")
        for s in suggestions:
            reasons = ", ".join(s.reasons) if s.reasons else "baseline"
            lines.append(
                f"- **[{_fmt_date(s.ts)}]** "
                f"`score={s.score:.2f}` `{reasons}` "
                f"(chunk `{s.chunk_id[:8]}`)"
            )
            # Indent the preview so it visually attaches to the bullet
            preview = s.text_preview.replace("\n", " ").strip()
            if preview:
                lines.append(f"  > {preview}")

    lines.append("")
    lines.append(MARKER_END)
    return "\n".join(lines)


# ── Atomic append / replace ──────────────────────────────────────────
def _find_marker_span(text: str) -> tuple[int, int] | None:
    """Return (start_idx_of_MARKER_START, end_idx_after_MARKER_END) or None.

    Indexes are byte-positions in `text` suitable for slicing. Both
    markers must be present and in the correct order. Searches use
    plain string matching (no regex) — operators may have legitimate
    HTML comments elsewhere in the file but our marker pair is unique.
    """
    s = text.find(MARKER_START)
    if s < 0:
        return None
    e = text.find(MARKER_END, s + len(MARKER_START))
    if e < 0:
        return None
    e_end = e + len(MARKER_END)
    return (s, e_end)


def append_to_memory_md(
    memory_md_path: str | Path,
    block: str,
    suggestion_count: int = 0,
) -> WriteReport:
    """Idempotently install/replace the marker-fenced block.

    Behavior:
      * If MEMORY.md does not exist: create it containing only the block.
      * If markers are present: replace ONLY the substring from
        MARKER_START through MARKER_END (inclusive). Content before
        the start marker and after the end marker is preserved
        byte-for-byte.
      * If markers are absent: append the block at end-of-file with a
        leading blank-line separator. Operator content stays untouched.

    Atomicity: write to <path>.tmp then os.replace onto the target.
    The .tmp is removed on any error so no leftovers persist.
    """
    p = Path(memory_md_path)
    report = WriteReport(path=str(p), suggestion_count=suggestion_count)

    # Sanity: the block we were handed must contain our markers, otherwise
    # we'd corrupt the file. Refuse rather than silently mis-write.
    if MARKER_START not in block or MARKER_END not in block:
        report.errors.append(
            "block missing required markers; refusing to write"
        )
        return report

    # Read as raw bytes so platform line-ending translation never silently
    # rewrites operator content. The byte-for-byte preservation contract
    # depends on this.
    try:
        if p.exists():
            original_bytes = p.read_bytes()
        else:
            original_bytes = b""
    except Exception as e:
        report.errors.append(f"read failed: {e}")
        return report

    try:
        original = original_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Fallback: decode with replacement so we can still locate markers,
        # but re-emit using the replaced text. Operator should fix encoding.
        original = original_bytes.decode("utf-8", errors="replace")

    span = _find_marker_span(original)
    if span is None:
        # No markers — append at EOF preserving everything above
        if original and not original.endswith("\n"):
            new_text = original + "\n\n" + block + "\n"
        elif original:
            # already ends with newline; add one more blank-line separator
            sep = "" if original.endswith("\n\n") else "\n"
            new_text = original + sep + block + "\n"
        else:
            new_text = block + "\n"
        report.was_appended = True
    else:
        # Markers present — splice the block in, preserve outside content.
        # We work in bytes here so any \r\n the operator authored survives
        # untouched outside the marker region.
        marker_start_b = MARKER_START.encode("utf-8")
        marker_end_b = MARKER_END.encode("utf-8")
        s_b = original_bytes.find(marker_start_b)
        e_b = original_bytes.find(marker_end_b, s_b + len(marker_start_b))
        if s_b < 0 or e_b < 0:
            # Marker layout shifted between text/bytes views (only possible
            # under exotic encodings) — fall back to text splice.
            before = original[: span[0]]
            after = original[span[1]:]
            new_text = before + block + after
            new_bytes = new_text.encode("utf-8")
        else:
            before_b = original_bytes[:s_b]
            after_b = original_bytes[e_b + len(marker_end_b):]
            new_bytes = before_b + block.encode("utf-8") + after_b
            new_text = new_bytes.decode("utf-8", errors="replace")
        report.was_replaced = True

    # Atomic write via tmp + os.replace, in BINARY mode (no line-ending
    # translation; preserves whatever \n / \r\n the slice produced).
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        # Ensure parent dir exists (for first-write case)
        p.parent.mkdir(parents=True, exist_ok=True)
        if report.was_replaced:
            payload_bytes = new_bytes  # byte-faithful splice
        else:
            payload_bytes = new_text.encode("utf-8")
        tmp.write_bytes(payload_bytes)
        os.replace(str(tmp), str(p))
        report.bytes_written = len(payload_bytes)
    except Exception as e:
        report.errors.append(f"write failed: {e}")
        # Best-effort cleanup of any tmp leftover
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return report

    # Belt-and-suspenders: confirm no .tmp leftover
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    return report
