"""Unit tests for lib.chunker — prose+code splitter and file-ref extractor."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.chunker import Chunk, chunk_text, extract_file_refs


class ProseChunkerTests(unittest.TestCase):
    def test_empty_text_returns_empty_list(self):
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("   \n   "), [])

    def test_whitespace_only_returns_empty(self):
        self.assertEqual(chunk_text("\n\n\n"), [])

    def test_short_text_single_chunk(self):
        chunks = chunk_text("Hello world.")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].kind, "prose")
        self.assertIn("Hello world.", chunks[0].text)

    def test_long_prose_splits_at_sentence(self):
        text = ". ".join(f"Sentence {i} is here" for i in range(40)) + "."
        chunks = chunk_text(text, max_chars=200, overlap=20)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.kind, "prose")
            self.assertLessEqual(len(c.text), 200)

    def test_huge_single_sentence_hard_wraps(self):
        # No sentence boundary — must hard-wrap
        text = "x" * 1500
        chunks = chunk_text(text, max_chars=400, overlap=20)
        self.assertGreaterEqual(len(chunks), 4)


class CodeChunkerTests(unittest.TestCase):
    def test_code_block_preserves_fences(self):
        text = "before\n```python\nprint('hi')\n```\nafter"
        chunks = chunk_text(text)
        # 3 chunks: prose-before, code, prose-after
        # (or 1 + 1 + 1)
        kinds = [c.kind for c in chunks]
        self.assertIn("code", kinds)
        code_chunk = next(c for c in chunks if c.kind == "code")
        self.assertTrue(code_chunk.text.startswith("```python"))
        self.assertTrue(code_chunk.text.rstrip().endswith("```"))
        self.assertEqual(code_chunk.language, "python")

    def test_long_code_block_splits_with_fences_per_chunk(self):
        body = "\n".join(f"line_{i} = {i}" for i in range(50))
        text = f"```python\n{body}\n```"
        chunks = chunk_text(text, max_chars=200)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.kind, "code")
            self.assertEqual(c.language, "python")
            # Each split chunk must have its own opening + closing fence
            self.assertTrue(c.text.lstrip().startswith("```python"))
            self.assertTrue(c.text.rstrip().endswith("```"))

    def test_unfenced_language_is_none(self):
        text = "```\nplain text in fence\n```"
        chunks = chunk_text(text)
        code = next(c for c in chunks if c.kind == "code")
        self.assertIsNone(code.language)

    def test_prose_then_code_then_prose_order_preserved(self):
        text = "First prose.\n```js\nlet x = 1;\n```\nLast prose."
        chunks = chunk_text(text)
        kinds = [c.kind for c in chunks]
        # prose, code, prose
        self.assertEqual(kinds[0], "prose")
        self.assertEqual(kinds[1], "code")
        self.assertEqual(kinds[2], "prose")


class FileRefExtractorTests(unittest.TestCase):
    def test_extracts_unix_paths(self):
        text = "see /usr/local/bin/script.sh and /etc/config.yaml"
        refs = extract_file_refs(text)
        self.assertIn("/usr/local/bin/script.sh", refs)

    def test_extracts_windows_paths(self):
        text = r"check C:\Users\me\project\src\main.py for the bug"
        refs = extract_file_refs(text)
        # The regex matches forward slashes, but Windows path may match too
        self.assertTrue(any("main.py" in r for r in refs))

    def test_extracts_relative_paths_with_extension(self):
        text = "Edit src/auth.py and update tests/test_auth.py"
        refs = extract_file_refs(text)
        self.assertIn("src/auth.py", refs)
        self.assertIn("tests/test_auth.py", refs)

    def test_dedupes_repeated_refs(self):
        text = "src/x.py is here. src/x.py is also there. src/x.py everywhere."
        refs = extract_file_refs(text)
        self.assertEqual(refs.count("src/x.py"), 1)

    def test_caps_at_20(self):
        text = " ".join(f"src/file_{i}.py" for i in range(50))
        refs = extract_file_refs(text)
        self.assertLessEqual(len(refs), 20)

    def test_strips_trailing_punctuation(self):
        text = "Look at src/foo.py, then src/bar.py."
        refs = extract_file_refs(text)
        # Periods get stripped (file extension still preserved by the regex)
        self.assertIn("src/foo.py", refs)
        self.assertIn("src/bar.py", refs)

    def test_no_matches_returns_empty(self):
        self.assertEqual(extract_file_refs("just plain prose with no paths"), [])


if __name__ == "__main__":
    unittest.main()
