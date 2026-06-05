"""MEMORY.md / CLAUDE.md integration — truth-anchor for primary-mode recall.

Locates and parses Claude-Code-convention memory files for the active
project, exposes their content for inclusion in recall output, and
detects drift between MEMORY.md (operator-curated truth) and chunks.db
(auto-extracted history).

Search order (first match wins for each kind):

    1. <project>/MEMORY.md
    2. <project>/.claude/MEMORY.md
    3. <project>/CLAUDE.md
    4. ~/.claude/projects/<encoded>/memory/MEMORY.md   ← Claude Code auto-memory
    5. ~/.claude/CLAUDE.md                             ← user-global

`<encoded>` is the project absolute path with every non-alphanumeric
character replaced by `-` (matches Claude Code's on-disk convention,
e.g. `C:\\Users\\me\\proj` -> `C--Users-me-proj`).

Pure stdlib. No I/O failures escape this module — every helper degrades
to "no MEMORY.md found" rather than raising.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# Cap how much MEMORY.md we read into hook output — keeps token cost
# bounded even on 250 KB MEMORY.md files (the user has one this size).
MAX_MEMORY_MD_CHARS = 6000  # ~1500 tokens
MAX_TLDR_LINES = 80
MAX_HEADINGS = 12

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class MemoryFile:
    """A located memory document."""
    path: Path
    kind: str          # "project_memory_md" | "claude_dir_memory_md" |
                       # "project_claude_md" | "auto_memory_md" | "global_claude_md"
    size_bytes: int
    mtime: int

    @property
    def age_seconds(self) -> int:
        return int(time.time()) - self.mtime


@dataclass
class ParsedMemory:
    """A snapshot of a memory file rendered for inclusion in hook output."""
    file: MemoryFile
    tldr: str = ""                              # truncated content for hook output
    headings: list[str] = field(default_factory=list)
    raw_size_chars: int = 0
    truncated: bool = False


def encode_project_path(project_root: str | Path) -> str:
    """Encode an absolute project path the way Claude Code does on disk."""
    s = str(Path(project_root).resolve())
    return _NON_ALNUM.sub("-", s)


def find_memory_files(project_root: str | Path | None = None) -> list[MemoryFile]:
    """Return all candidate memory files in priority order.

    Reads filesystem only — does not parse content. Safe to call from a
    hot hook path."""
    out: list[MemoryFile] = []

    if project_root is None:
        project_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    proj = Path(project_root).resolve()

    candidates: list[tuple[Path, str]] = [
        (proj / "MEMORY.md", "project_memory_md"),
        (proj / ".claude" / "MEMORY.md", "claude_dir_memory_md"),
        (proj / "CLAUDE.md", "project_claude_md"),
    ]

    # Claude Code auto-memory location (encoded project path)
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or
                str(Path.home()))
    encoded = encode_project_path(proj)
    auto = home / ".claude" / "projects" / encoded / "memory" / "MEMORY.md"
    candidates.append((auto, "auto_memory_md"))

    # User-global instructions
    candidates.append((home / ".claude" / "CLAUDE.md", "global_claude_md"))

    for path, kind in candidates:
        try:
            if path.exists() and path.is_file():
                st = path.stat()
                out.append(MemoryFile(
                    path=path, kind=kind,
                    size_bytes=st.st_size, mtime=int(st.st_mtime),
                ))
        except Exception:
            # Path may be unreadable on Windows due to permissions — skip
            continue
    return out


def parse_memory_file(mf: MemoryFile, max_chars: int = MAX_MEMORY_MD_CHARS) -> ParsedMemory:
    """Read a memory file and produce a bounded TL;DR + heading list."""
    pm = ParsedMemory(file=mf)
    try:
        text = mf.path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return pm

    pm.raw_size_chars = len(text)

    # Extract heading anchors (top-level + secondary)
    pm.headings = [
        f"{'#' * len(h)} {title}".strip()
        for h, title in _HEADING_RE.findall(text)[:MAX_HEADINGS]
    ]

    # Build TL;DR — first MAX_TLDR_LINES non-blank lines, capped at max_chars.
    # If a single line itself exceeds the remaining char budget, truncate the
    # line — single huge lines (e.g. a 200 KB MEMORY.md with one big paragraph)
    # would otherwise blow past max_chars on first append.
    lines: list[str] = []
    chars = 0
    for line in text.splitlines():
        if not line.strip() and (not lines or not lines[-1].strip()):
            continue  # collapse runs of blank lines
        remaining = max_chars - chars
        if len(line) >= remaining:
            # Cap this line so total stays under max_chars
            lines.append(line[: max(0, remaining)])
            chars = max_chars
            pm.truncated = True
            break
        lines.append(line)
        chars += len(line) + 1
        if len(lines) >= MAX_TLDR_LINES or chars >= max_chars:
            pm.truncated = True
            break

    if chars < max_chars and len(lines) < MAX_TLDR_LINES:
        # We exited the loop because we ran out of input, not because we
        # hit a cap — so we are NOT truncated.
        pm.truncated = pm.truncated  # leave as-is

    if len(lines) >= MAX_TLDR_LINES and chars < max_chars:
        # Hit line cap but not char cap → still truncated
        pm.truncated = True

    pm.tldr = "\n".join(lines).rstrip()
    if pm.truncated:
        pm.tldr += f"\n\n... [truncated, {pm.raw_size_chars} chars total]"

    return pm


def render_memory_block(parsed: list[ParsedMemory], heading_only: bool = False) -> str:
    """Format a list of parsed memory files for inclusion in hook output.

    `heading_only=True` produces just the heading skeleton (cheap — used
    for SessionStart so the catalog stays the primary signal). Setting it
    to False emits the full TL;DR (used by `/recall` synthesis prompts)."""
    if not parsed:
        return ""

    out: list[str] = []
    for pm in parsed:
        if not pm.tldr and not pm.headings:
            continue
        # Friendly age string
        age_s = pm.file.age_seconds
        if age_s < 3600:
            age = f"{age_s // 60}m ago"
        elif age_s < 86400:
            age = f"{age_s // 3600}h ago"
        else:
            age = f"{age_s // 86400}d ago"

        out.append(f"### {pm.file.kind} ({pm.file.path.name}, {age}, "
                   f"{pm.file.size_bytes} bytes)")

        if heading_only and pm.headings:
            out.append("\n".join(f"  {h}" for h in pm.headings[:MAX_HEADINGS]))
        elif pm.tldr:
            out.append(pm.tldr)
        out.append("")

    return "\n".join(out).rstrip()


def detect_drift(
    parsed: list[ParsedMemory],
    latest_chunk_ts: int | None,
    chunks_since_memory_update: int,
    bootstrap_chunks_since_memory_update: int = 0,
) -> list[str]:
    """Return a list of drift warnings (each a one-line string).

    v6.0.1: bootstrap_chunks_since_memory_update is the count of chunks
    tagged kind=bootstrap_doc or kind=bootstrap_transcript. When 95%+ of
    "newer than MEMORY.md" chunks are bootstrap, drift is suppressed —
    bootstrap is seeding history, not signaling that MEMORY.md is stale.

    Drift signals:
      - MEMORY.md older than the most recent ingested chunk by 12+ hours
        AND ≥ 5 chunks ingested since
      - MEMORY.md missing entirely while chunks.db has data (operator
        should consider creating MEMORY.md)
    """
    warnings: list[str] = []

    if not parsed:
        if latest_chunk_ts and latest_chunk_ts > 0:
            warnings.append(
                "no MEMORY.md found for this project — operator-curated truth "
                "anchor missing. Consider creating <project>/MEMORY.md to "
                "stabilize recall."
            )
        return warnings

    # Find the youngest memory file
    newest_memory_ts = max(pm.file.mtime for pm in parsed)
    if not latest_chunk_ts:
        return warnings

    delta = latest_chunk_ts - newest_memory_ts
    if delta > 12 * 3600 and chunks_since_memory_update >= 5:
        # v6.0.1: suppress drift when ≥95% of "newer" chunks are bootstrap.
        # Bootstrap chunks are seeded historical content (README/transcripts),
        # not real signal that MEMORY.md is stale.
        non_bootstrap = (
            chunks_since_memory_update - bootstrap_chunks_since_memory_update
        )
        bootstrap_ratio = (
            bootstrap_chunks_since_memory_update / chunks_since_memory_update
            if chunks_since_memory_update else 0.0
        )
        if bootstrap_ratio >= 0.95:
            return warnings
        hrs = delta // 3600
        warnings.append(
            f"MEMORY.md is {hrs}h older than latest ingested chunk; "
            f"{non_bootstrap} non-bootstrap chunks ingested since last "
            f"MEMORY.md update — anchor may be stale."
        )
    return warnings


def load_for_hook(
    project_root: str | Path | None = None,
    heading_only: bool = True,
) -> tuple[str, list[MemoryFile]]:
    """Convenience: locate + parse + render in one call. Returns
    (rendered_block, list_of_files_found)."""
    files = find_memory_files(project_root)
    if not files:
        return "", []
    parsed = [parse_memory_file(mf) for mf in files]
    block = render_memory_block(parsed, heading_only=heading_only)
    return block, files
