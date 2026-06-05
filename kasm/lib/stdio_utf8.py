"""Force stdout/stderr to UTF-8.

Without this, Windows + non-UTF8 console (cp1252 default) crashes when
the catalog output contains characters like → ✓ • or any non-ASCII text
from past sessions. Real bug found in v4 build.

Idempotent: safe to call from any entry point.
"""
from __future__ import annotations

import sys


def force_utf8_io() -> None:
    """Reconfigure stdout/stderr to UTF-8 with replace fallback."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
