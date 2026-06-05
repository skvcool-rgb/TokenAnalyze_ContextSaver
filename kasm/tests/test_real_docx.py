"""Real-world end-to-end test using ~/Downloads/self_improving_ai_fields.docx.

We extract text via stdlib zipfile + minidom (.docx is just a zip of XML),
chunk it, ingest, build a catalog, and run a recall query — verifying the
full pipeline works on a non-trivial real document.

Skipped if the document isn't present (so CI / fresh machines don't fail)."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from xml.dom import minidom

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.chunker import chunk_text
from lib.paths import FILE_CHUNKS_DB, ensure_kos_dir
from lib.recall import execute_recall_local_only
from lib.store import Store

DOCX_PATH = Path.home() / "Downloads" / "self_improving_ai_fields.docx"


def _extract_docx_text(path: Path) -> str:
    """Pull plain text from a .docx (Word XML). Pure stdlib."""
    with zipfile.ZipFile(path) as z:
        with z.open("word/document.xml") as f:
            xml = f.read()
    doc = minidom.parseString(xml)
    paragraphs = []
    for p in doc.getElementsByTagName("w:p"):
        runs = []
        for t in p.getElementsByTagName("w:t"):
            if t.firstChild and t.firstChild.nodeValue:
                runs.append(t.firstChild.nodeValue)
        if runs:
            paragraphs.append("".join(runs))
    return "\n\n".join(paragraphs)


@unittest.skipUnless(
    DOCX_PATH.exists(),
    f"sample doc not present at {DOCX_PATH}; skipping real-docx test",
)
class RealDocxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _extract_docx_text(DOCX_PATH)
        # Sanity: extracted something non-trivial
        assert len(cls.text) > 1000, (
            f"docx extraction too small ({len(cls.text)} chars)"
        )

    def test_extraction_returns_substantial_text(self):
        self.assertGreater(len(self.text), 1000)

    def test_chunker_handles_real_docx(self):
        chunks = chunk_text(self.text, max_chars=400, overlap=50)
        self.assertGreater(len(chunks), 5)
        for c in chunks:
            self.assertLessEqual(len(c.text), 600)  # some slack for unicode
            self.assertIn(c.kind, {"prose", "code"})

    def test_full_ingest_and_recall_on_real_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            kos = ensure_kos_dir(tmp, user_level=False)
            store = Store(kos / FILE_CHUNKS_DB)
            now = int(time.time())
            try:
                chunks = chunk_text(self.text, max_chars=400, overlap=50)
                bulk = [{
                    "session_id": "real-doc-session", "project": tmp,
                    "ts": now, "text": c.text, "kind": c.kind,
                    "language": c.language, "file_refs": [],
                    "asserted_by_user": False,
                } for c in chunks]
                n = store.add_chunks_bulk(bulk)
                store.upsert_session(
                    "real-doc-session", started_at=now - 60,
                    ended_at=now, project=tmp, chunk_count=n,
                    tags=["ai", "research"], summary="ingested ai-fields doc",
                )
                self.assertEqual(store.count(), len(chunks))
            finally:
                store.close()

            # Pick the most-frequent meaningful word from the doc as the
            # query — this gives us a high-confidence target without
            # hardcoding doc-specific text.
            words = re.findall(r"[A-Za-z]{6,}", self.text.lower())
            from collections import Counter
            common = Counter(words).most_common(20)
            # Skip obvious stopwords-of-the-domain
            stop = {"system", "systems", "models", "model", "should",
                    "rather", "however", "across", "without", "becomes"}
            query_word = next((w for w, _ in common if w not in stop), None)
            self.assertIsNotNone(
                query_word, msg="failed to derive a query word from the doc"
            )

            t0 = time.perf_counter()
            rc = execute_recall_local_only(
                query=query_word, window_days=7, project_root=tmp,
            )
            elapsed = time.perf_counter() - t0

            self.assertLess(elapsed, 2.0,
                            msg=f"recall took {elapsed:.2f}s for "
                                f"{len(chunks)} chunks")
            self.assertGreater(
                len(rc.passages), 0,
                msg=f"recall for '{query_word}' returned 0 passages — "
                    f"chunker or grep broken on real docx",
            )
            joined = " ".join(p["text"].lower() for p in rc.passages)
            self.assertIn(query_word, joined)
            print(
                f"\n  [real-docx] {len(chunks)} chunks ingested, "
                f"queried '{query_word}', got {len(rc.passages)} passages "
                f"in {elapsed * 1000:.0f}ms"
            )


if __name__ == "__main__":
    unittest.main()
