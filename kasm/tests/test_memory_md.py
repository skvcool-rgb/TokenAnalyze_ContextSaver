"""Unit tests for lib.memory_md — MEMORY.md / CLAUDE.md integration."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.memory_md import (
    MAX_MEMORY_MD_CHARS,
    MAX_TLDR_LINES,
    MemoryFile,
    detect_drift,
    encode_project_path,
    find_memory_files,
    load_for_hook,
    parse_memory_file,
    render_memory_block,
)


class EncodeProjectPathTests(unittest.TestCase):
    def test_windows_path_encoded(self):
        # Matches Claude Code's on-disk convention
        encoded = encode_project_path("C:/Users/me/proj")
        self.assertNotIn(":", encoded)
        self.assertNotIn("/", encoded)
        self.assertIn("C-", encoded)

    def test_path_with_spaces(self):
        encoded = encode_project_path("/home/user/My Project")
        self.assertNotIn(" ", encoded)
        self.assertIn("My-Project", encoded)

    def test_alphanumeric_preserved(self):
        encoded = encode_project_path("/abc123/def456")
        self.assertIn("abc123", encoded)
        self.assertIn("def456", encoded)


class FindMemoryFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        # Pin HOME/USERPROFILE so user-global lookups don't leak
        self.fake_home = Path(self.tmp.name) / "home"
        self.fake_home.mkdir()
        self._old_env = {
            k: os.environ.get(k)
            for k in ("HOME", "USERPROFILE", "CLAUDE_PROJECT_DIR")
        }
        os.environ["HOME"] = str(self.fake_home)
        os.environ["USERPROFILE"] = str(self.fake_home)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_no_files_returns_empty(self):
        self.assertEqual(find_memory_files(self.proj), [])

    def test_finds_project_memory_md(self):
        (self.proj / "MEMORY.md").write_text("# notes", encoding="utf-8")
        files = find_memory_files(self.proj)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].kind, "project_memory_md")

    def test_finds_claude_dir_memory_md(self):
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / "MEMORY.md").write_text("# x", encoding="utf-8")
        files = find_memory_files(self.proj)
        kinds = {f.kind for f in files}
        self.assertIn("claude_dir_memory_md", kinds)

    def test_finds_claude_md(self):
        (self.proj / "CLAUDE.md").write_text("# x", encoding="utf-8")
        files = find_memory_files(self.proj)
        kinds = {f.kind for f in files}
        self.assertIn("project_claude_md", kinds)

    def test_finds_auto_memory_md(self):
        encoded = encode_project_path(self.proj)
        auto_dir = (self.fake_home / ".claude" / "projects" / encoded /
                    "memory")
        auto_dir.mkdir(parents=True)
        (auto_dir / "MEMORY.md").write_text("# auto", encoding="utf-8")
        files = find_memory_files(self.proj)
        kinds = {f.kind for f in files}
        self.assertIn("auto_memory_md", kinds)

    def test_finds_global_claude_md(self):
        (self.fake_home / ".claude").mkdir(parents=True, exist_ok=True)
        (self.fake_home / ".claude" / "CLAUDE.md").write_text("# global",
                                                              encoding="utf-8")
        files = find_memory_files(self.proj)
        kinds = {f.kind for f in files}
        self.assertIn("global_claude_md", kinds)

    def test_returns_files_in_priority_order(self):
        # Create all 5 candidate locations
        (self.proj / "MEMORY.md").write_text("a", encoding="utf-8")
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / "MEMORY.md").write_text("b", encoding="utf-8")
        (self.proj / "CLAUDE.md").write_text("c", encoding="utf-8")
        (self.fake_home / ".claude").mkdir(parents=True, exist_ok=True)
        (self.fake_home / ".claude" / "CLAUDE.md").write_text("e",
                                                              encoding="utf-8")
        files = find_memory_files(self.proj)
        kinds = [f.kind for f in files]
        # Project files should come before user-global
        self.assertEqual(kinds[0], "project_memory_md")
        self.assertIn("global_claude_md", kinds)
        self.assertEqual(kinds[-1], "global_claude_md")


class ParseMemoryFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _mkfile(self, content: str, name: str = "MEMORY.md") -> MemoryFile:
        p = Path(self.tmp.name) / name
        p.write_text(content, encoding="utf-8")
        st = p.stat()
        return MemoryFile(path=p, kind="project_memory_md",
                          size_bytes=st.st_size, mtime=int(st.st_mtime))

    def test_extracts_headings(self):
        mf = self._mkfile(
            "# Top\n\nintro\n\n## Subhead\n\ntext\n\n### Sub-sub\n\ndone"
        )
        pm = parse_memory_file(mf)
        joined = "\n".join(pm.headings)
        self.assertIn("# Top", joined)
        self.assertIn("## Subhead", joined)
        self.assertIn("### Sub-sub", joined)

    def test_short_file_not_truncated(self):
        mf = self._mkfile("# small\n\njust a few lines\n")
        pm = parse_memory_file(mf)
        self.assertFalse(pm.truncated)
        self.assertIn("just a few lines", pm.tldr)

    def test_long_file_truncated_at_max_chars(self):
        # Generate >MAX_MEMORY_MD_CHARS content
        big = "# x\n\n" + "y" * (MAX_MEMORY_MD_CHARS * 3)
        mf = self._mkfile(big)
        pm = parse_memory_file(mf)
        self.assertTrue(pm.truncated)
        self.assertIn("truncated", pm.tldr)
        self.assertLessEqual(
            len(pm.tldr), MAX_MEMORY_MD_CHARS + 200,  # +marker text
        )

    def test_long_file_truncated_at_max_lines(self):
        many_lines = "# x\n\n" + "\n".join(f"line_{i}" for i in range(500))
        mf = self._mkfile(many_lines)
        pm = parse_memory_file(mf)
        self.assertTrue(pm.truncated)
        # MAX_TLDR_LINES + truncation marker
        self.assertLessEqual(pm.tldr.count("\n"), MAX_TLDR_LINES + 5)

    def test_collapses_blank_runs(self):
        mf = self._mkfile("# x\n\n\n\n\nA\n\n\n\nB\n")
        pm = parse_memory_file(mf)
        # No 3+ blank lines in TL;DR
        self.assertNotIn("\n\n\n\n", pm.tldr)


class RenderMemoryBlockTests(unittest.TestCase):
    def _mkparsed(self, content: str, kind: str = "project_memory_md"):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "MEMORY.md"
            p.write_text(content, encoding="utf-8")
            mf = MemoryFile(path=p, kind=kind,
                            size_bytes=p.stat().st_size,
                            mtime=int(p.stat().st_mtime))
            return parse_memory_file(mf)

    def test_empty_input_returns_empty(self):
        self.assertEqual(render_memory_block([]), "")

    def test_heading_only_omits_body(self):
        pm = self._mkparsed("# Top\n\nbody text here\n## Sub")
        out = render_memory_block([pm], heading_only=True)
        self.assertIn("# Top", out)
        self.assertIn("## Sub", out)
        self.assertNotIn("body text here", out)

    def test_full_includes_body(self):
        pm = self._mkparsed("# Top\n\nbody text here")
        out = render_memory_block([pm], heading_only=False)
        self.assertIn("body text here", out)

    def test_includes_age_label(self):
        pm = self._mkparsed("# x")
        out = render_memory_block([pm])
        # m/h/d ago format
        import re
        self.assertTrue(re.search(r"\d+(m|h|d) ago", out),
                        msg=f"no age in output: {out!r}")


class DetectDriftTests(unittest.TestCase):
    def _mkparsed(self, mtime_ts: int):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "MEMORY.md"
            p.write_text("# x", encoding="utf-8")
            os.utime(p, (mtime_ts, mtime_ts))
            mf = MemoryFile(path=p, kind="project_memory_md",
                            size_bytes=p.stat().st_size,
                            mtime=mtime_ts)
            return parse_memory_file(mf)

    def test_no_memory_md_with_chunks_warns(self):
        warns = detect_drift([], latest_chunk_ts=int(time.time()),
                             chunks_since_memory_update=10)
        self.assertEqual(len(warns), 1)
        self.assertIn("MEMORY.md", warns[0])

    def test_no_memory_md_no_chunks_silent(self):
        warns = detect_drift([], latest_chunk_ts=None,
                             chunks_since_memory_update=0)
        self.assertEqual(warns, [])

    def test_recent_memory_md_silent(self):
        now = int(time.time())
        pm = self._mkparsed(now - 60)  # 1 min old
        warns = detect_drift([pm], latest_chunk_ts=now,
                             chunks_since_memory_update=2)
        self.assertEqual(warns, [])

    def test_stale_memory_md_with_many_chunks_warns(self):
        now = int(time.time())
        pm = self._mkparsed(now - 25 * 3600)  # 25h old
        warns = detect_drift([pm], latest_chunk_ts=now,
                             chunks_since_memory_update=10)
        self.assertEqual(len(warns), 1)
        self.assertIn("stale", warns[0])

    def test_drift_suppressed_when_all_chunks_are_bootstrap(self):
        """v6.0.1: bootstrap chunks shouldn't trigger drift."""
        now = int(time.time())
        pm = self._mkparsed(now - 25 * 3600)  # MEMORY.md 25h old
        # 100% of newer chunks are bootstrap → drift suppressed
        warns = detect_drift(
            [pm], latest_chunk_ts=now,
            chunks_since_memory_update=210,
            bootstrap_chunks_since_memory_update=210,
        )
        self.assertEqual(warns, [])

    def test_drift_suppressed_when_95pct_chunks_are_bootstrap(self):
        now = int(time.time())
        pm = self._mkparsed(now - 25 * 3600)
        # 95% bootstrap (210 of 220) → suppressed
        warns = detect_drift(
            [pm], latest_chunk_ts=now,
            chunks_since_memory_update=220,
            bootstrap_chunks_since_memory_update=210,
        )
        self.assertEqual(warns, [])

    def test_drift_fires_when_under_95pct_bootstrap(self):
        now = int(time.time())
        pm = self._mkparsed(now - 25 * 3600)
        # 50% bootstrap (50 of 100) → drift still fires for the 50 real chunks
        warns = detect_drift(
            [pm], latest_chunk_ts=now,
            chunks_since_memory_update=100,
            bootstrap_chunks_since_memory_update=50,
        )
        self.assertEqual(len(warns), 1)
        # The warning should report the non-bootstrap count, not total
        self.assertIn("50 non-bootstrap chunks", warns[0])

    def test_stale_memory_md_with_few_chunks_silent(self):
        # Threshold: 5+ chunks AND 12+ hours
        now = int(time.time())
        pm = self._mkparsed(now - 25 * 3600)
        warns = detect_drift([pm], latest_chunk_ts=now,
                             chunks_since_memory_update=2)
        self.assertEqual(warns, [])  # below chunk threshold


class LoadForHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        self.fake_home = self.proj / "home"
        self.fake_home.mkdir()
        self._old = {k: os.environ.get(k)
                     for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(self.fake_home)
        os.environ["USERPROFILE"] = str(self.fake_home)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_returns_empty_when_nothing_found(self):
        block, files = load_for_hook(self.proj)
        self.assertEqual(block, "")
        self.assertEqual(files, [])

    def test_returns_block_when_files_present(self):
        (self.proj / "MEMORY.md").write_text(
            "# Top\n\nimportant note\n", encoding="utf-8",
        )
        block, files = load_for_hook(self.proj)
        self.assertGreater(len(block), 0)
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
