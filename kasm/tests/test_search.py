"""Unit tests for lib.search — BM25, synonyms, grep_passages."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.search import BM25, SynonymCache, grep_passages, tokenize


class TokenizeTests(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(tokenize("Hello WORLD"), ["hello", "world"])

    def test_strips_punctuation(self):
        self.assertEqual(tokenize("hello, world!"), ["hello", "world"])

    def test_keeps_underscores_and_digits(self):
        self.assertEqual(tokenize("var_1 and api_v2"), ["var_1", "and", "api_v2"])

    def test_empty(self):
        self.assertEqual(tokenize(""), [])


class BM25Tests(unittest.TestCase):
    def test_simple_corpus_ranks_matching_doc_first(self):
        corpus = [
            tokenize("the auth module uses oauth"),
            tokenize("file system code"),
            tokenize("oauth implementation details for auth"),
        ]
        bm = BM25(corpus)
        ranked = bm.top_n(tokenize("oauth auth"), n=3)
        # The two oauth+auth docs should rank above the unrelated one
        ranked_ids = [i for i, _ in ranked]
        self.assertIn(0, ranked_ids[:2])
        self.assertIn(2, ranked_ids[:2])
        self.assertNotIn(1, ranked_ids[:2])

    def test_tiny_corpus_doesnt_zero_idf(self):
        # 2 docs, term in both: classic BM25 floors to negative — we floor to epsilon
        corpus = [tokenize("auth oauth"), tokenize("auth oauth")]
        bm = BM25(corpus)
        scores = bm.scores(tokenize("auth"))
        # Both should be > 0 thanks to epsilon floor
        self.assertTrue(all(s > 0 for s in scores))

    def test_empty_corpus_no_crash(self):
        bm = BM25([])
        self.assertEqual(bm.scores(tokenize("anything")), [])

    def test_top_n_limits_results(self):
        corpus = [tokenize(f"doc {i}") for i in range(10)]
        bm = BM25(corpus)
        ranked = bm.top_n(tokenize("doc"), n=3)
        self.assertLessEqual(len(ranked), 3)


class SynonymCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.path = Path(self.tmp.name)
        self.tmp.close()
        self.path.unlink()  # so cache starts empty

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_seed_expands_known_term(self):
        cache = SynonymCache(self.path)
        terms = cache.expand("auth issue")
        self.assertIn("auth", terms)
        self.assertIn("authentication", terms)
        self.assertIn("token", terms)
        self.assertIn("issue", terms)

    def test_unknown_term_returns_just_tokens(self):
        cache = SynonymCache(self.path)
        terms = cache.expand("foozbar")
        self.assertEqual(terms, ["foozbar"])

    def test_remember_persists_to_file(self):
        c1 = SynonymCache(self.path)
        c1.remember("custom query", ["term1", "term2", "term3"])
        # Reload from disk
        c2 = SynonymCache(self.path)
        terms = c2.expand("custom query")
        self.assertIn("term1", terms)
        self.assertIn("term2", terms)

    def test_remember_dedupes(self):
        cache = SynonymCache(self.path)
        cache.remember("q", ["a", "a", "b", "a"])
        terms = cache.expand("q")
        # Original tokens + unique extras
        a_count = terms.count("a")
        self.assertEqual(a_count, 1)

    def test_lru_eviction_at_max_entries(self):
        cache = SynonymCache(self.path)
        # Cap at MAX_ENTRIES = 500
        for i in range(SynonymCache.MAX_ENTRIES + 5):
            cache.remember(f"q{i}", [f"t{i}"])
        # Earliest should have been evicted
        self.assertLessEqual(len(cache._cache), SynonymCache.MAX_ENTRIES + 1)

    def test_no_path_works_in_memory(self):
        cache = SynonymCache(None)
        cache.remember("q", ["t1"])
        self.assertIn("t1", cache.expand("q"))


class GrepPassagesTests(unittest.TestCase):
    def test_match_with_context(self):
        text = "line 0\nline 1\nMATCH here\nline 3\nline 4"
        out = grep_passages(text, ["MATCH"], context_lines=1)
        self.assertEqual(len(out), 1)
        self.assertIn("line 1", out[0])
        self.assertIn("MATCH", out[0])
        self.assertIn("line 3", out[0])

    def test_multiple_matches_merged_when_overlapping(self):
        text = "a\nMATCH\nb\nMATCH\nc"
        out = grep_passages(text, ["MATCH"], context_lines=1)
        # Two windows overlap and should merge into one
        self.assertEqual(len(out), 1)

    def test_separated_matches_yield_separate_windows(self):
        text = "a\nb\nMATCH\nc\nd\ne\nf\ng\nMATCH\nh"
        out = grep_passages(text, ["MATCH"], context_lines=0)
        self.assertEqual(len(out), 2)

    def test_case_insensitive(self):
        out = grep_passages("Hello WORLD", ["world"], context_lines=0)
        self.assertEqual(out, ["Hello WORLD"])

    def test_no_terms_returns_empty(self):
        self.assertEqual(grep_passages("text", []), [])

    def test_no_text_returns_empty(self):
        self.assertEqual(grep_passages("", ["x"]), [])

    def test_special_regex_chars_escaped(self):
        # `.*` escaped to `\.\*` — looks for literal ".*" in text, not regex
        # wildcard. The text doesn't contain literal ".*", so no match.
        out = grep_passages("foo.bar baz", [".*"], context_lines=0)
        self.assertEqual(out, [])
        # But the literal `.*` substring would match
        out2 = grep_passages("contains .* here", [".*"], context_lines=0)
        self.assertEqual(out2, ["contains .* here"])


if __name__ == "__main__":
    unittest.main()
