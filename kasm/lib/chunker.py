"""Smart chunker — prose with overlap, code with fence preservation.

Pure stdlib, zero dependencies.

Bugs intentionally fixed (carry-over from v3):
  - Code-block split preserves opening AND closing fence per chunk
  - Language hint preserved across continuations
  - No leading-space off-by-one
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 400
DEFAULT_OVERLAP = 50

_FENCE_RE = re.compile(r"(```[^\n]*\n.*?```)", re.DOTALL)
_FILE_REF_RE = re.compile(
    r"[A-Za-z]:[\\/](?:[A-Za-z0-9_.\\/\-]+/)*[A-Za-z0-9_.\\/\-]+"
    r"|/(?:[A-Za-z0-9_.\\/\-]+/)+[A-Za-z0-9_.\\/\-]+"
    r"|(?:[A-Za-z0-9_\-./]+/)+[A-Za-z0-9_.\-]+\.(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml|sh|rs|go|c|cpp|h|hpp|java|rb|php|sql)"
)


@dataclass(frozen=True)
class Chunk:
    text: str
    kind: str          # "prose" | "code"
    language: str | None = None


def extract_file_refs(text: str) -> list[str]:
    """Pull plausible file paths out of text. Used to seed file_refs index."""
    seen = set()
    out = []
    for m in _FILE_REF_RE.finditer(text):
        s = m.group(0).strip(".,;:()[]{}'\"")
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:20]  # cap


def _parse_fence(block: str) -> tuple[str, str, str]:
    """Returns (header_with_newline, body_no_trailing_fence, footer)."""
    lines = block.split("\n", 1)
    header = (lines[0] if lines else "```") + "\n"
    rest = lines[1] if len(lines) > 1 else ""
    if rest.rstrip("\n").endswith("```"):
        body = rest.rstrip("\n")[:-3].rstrip("\n")
    else:
        body = rest.rstrip("\n")
    return header, body, "```"


def _split_code(block: str, max_chars: int) -> list[Chunk]:
    header, body, footer = _parse_fence(block)
    m = re.match(r"```([A-Za-z0-9_+\-]*)\s*\n", header)
    language = m.group(1) if m and m.group(1) else None

    full = header + body + "\n" + footer
    if len(full) <= max_chars:
        return [Chunk(text=full, kind="code", language=language)]

    budget = max_chars - len(header) - len(footer) - 1
    if budget < 40:
        return [Chunk(text=full, kind="code", language=language)]

    out: list[Chunk] = []
    cur: list[str] = []
    cur_len = 0
    for line in body.split("\n"):
        if cur_len + len(line) + 1 > budget and cur:
            out.append(Chunk(
                text=header + "\n".join(cur) + "\n" + footer,
                kind="code", language=language,
            ))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        out.append(Chunk(
            text=header + "\n".join(cur) + "\n" + footer,
            kind="code", language=language,
        ))
    return out


def _hard_wrap(s: str, max_chars: int, overlap: int) -> list[Chunk]:
    """Split a single string into max_chars-sized chunks with overlap."""
    step = max(1, max_chars - overlap)
    out: list[Chunk] = []
    for i in range(0, len(s), step):
        piece = s[i : i + max_chars]
        if piece.strip():
            out.append(Chunk(text=piece, kind="prose"))
    return out


def _split_prose(text: str, max_chars: int, overlap: int) -> list[Chunk]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [Chunk(text=text, kind="prose")]

    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[Chunk] = []
    cur = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if not cur:
            if len(s) <= max_chars:
                cur = s
            else:
                # hard-wrap a single huge sentence
                out.extend(_hard_wrap(s, max_chars, overlap))
                cur = ""
            continue
        if len(cur) + 1 + len(s) <= max_chars:
            cur += " " + s
        else:
            out.append(Chunk(text=cur, kind="prose"))
            cur = ""
            # If `s` itself exceeds max_chars, hard-wrap it. (Without this,
            # the next iteration would assign `s` to `cur` and emit an
            # oversize chunk at the end.)
            if len(s) > max_chars:
                out.extend(_hard_wrap(s, max_chars, overlap))
                continue
            # Otherwise carry overlap forward
            tail = ""
            if overlap > 0 and out:
                last = out[-1].text
                tail = last[-overlap:] if len(last) > overlap else last
                sp = tail.find(" ")
                if sp > 0:
                    tail = tail[sp + 1 :]
            if tail and len(tail) + 1 + len(s) <= max_chars:
                cur = tail + " " + s
            else:
                cur = s
    if cur:
        out.append(Chunk(text=cur, kind="prose"))
    return out


def chunk_text(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Split mixed prose+code text into chunks preserving order."""
    if not text or not text.strip():
        return []
    out: list[Chunk] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        prose = text[pos : m.start()]
        if prose.strip():
            out.extend(_split_prose(prose, max_chars, overlap))
        out.extend(_split_code(m.group(0), max_chars))
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        out.extend(_split_prose(tail, max_chars, overlap))
    return out
