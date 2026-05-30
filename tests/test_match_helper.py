"""
Unit tests for the _frag_matches / _any_frag_match helpers in test_benchmark.py.

These tests run without a live VexIndex daemon — they only exercise the
pure-Python fragment matching logic.

Run with:
    uv run pytest tests/test_match_helper.py -v -s
"""

import pytest

# We import the helpers directly from the benchmark module.
# settings must be importable (it reads config from env / defaults).
from tests.test_benchmark import _frag_matches, _any_frag_match


# ---------------------------------------------------------------------------
# _frag_matches — basic cases
# ---------------------------------------------------------------------------

class TestFragMatches:

    def test_exact_substring(self):
        """Plain substring match should always succeed."""
        assert _frag_matches("db.py", "/home/user/vexindex/db.py") is True

    def test_case_insensitive(self):
        """Fragment and target should be compared case-insensitively."""
        assert _frag_matches("DB.PY", "/home/user/db.py") is True

    def test_whitespace_trim_fragment(self):
        """Leading/trailing whitespace in the fragment should be stripped."""
        assert _frag_matches("  db.py  ", "db.py") is True

    def test_whitespace_trim_target(self):
        """Leading/trailing whitespace in the target should be stripped."""
        assert _frag_matches("db.py", "  /home/user/db.py  ") is True

    def test_path_normalisation(self):
        """Backslashes in either string should be treated as forward slashes."""
        assert _frag_matches("vexindex\\db.py", "vexindex/db.py") is True

    # ---------------------------------------------------------------------------
    # Levenshtein ≤ 1 — the three single-edit operations
    # ---------------------------------------------------------------------------

    def test_levenshtein_substitution(self):
        """One character substituted → edit distance 1 → match."""
        # 'dv.py' vs 'db.py' — 'v' substituted for 'b'
        assert _frag_matches("dv.py", "/home/user/db.py") is True

    def test_levenshtein_insertion(self):
        """Fragment has one extra char → edit distance 1 → match."""
        # 'db.pyy' vs 'db.py' — extra trailing 'y'
        assert _frag_matches("db.pyy", "/home/user/db.py") is True

    def test_levenshtein_deletion(self):
        """Fragment is missing one char → edit distance 1 → match."""
        # 'dbpy' vs 'db.py' — missing the '.'
        assert _frag_matches("dbpy", "/home/user/db.py") is True

    def test_levenshtein_distance_2_no_match(self):
        """Two edits needed → should NOT match (edit distance > 1)."""
        # 'dbb.pyy' vs 'db.py' — two edits required
        assert _frag_matches("dbb.pyy", "/home/user/db.py") is False

    # ---------------------------------------------------------------------------
    # Min-length guard
    # ---------------------------------------------------------------------------

    def test_min_length_skip(self):
        """Fragment shorter than VEXINDEX_MIN_MATCH_LENGTH (default 2) → False."""
        # Single-char fragment 'd' should be ignored
        assert _frag_matches("d", "db.py") is False

    def test_empty_fragment(self):
        """Empty fragment should always return False (len 0 < min 2)."""
        assert _frag_matches("", "db.py") is False

    def test_min_length_boundary_exactly_2(self):
        """Fragment of exactly min-length characters should be checked normally."""
        # 'py' is 2 chars and appears in 'db.py'
        assert _frag_matches("py", "db.py") is True

    # ---------------------------------------------------------------------------
    # Edge cases
    # ---------------------------------------------------------------------------

    def test_empty_target(self):
        """Any non-trivial fragment against an empty target should return False."""
        assert _frag_matches("db.py", "") is False

    def test_fragment_longer_than_target(self):
        """Fragment longer than target cannot be a substring; fuzzy should still
        work if edit distance ≤ 1."""
        # 'db.py' (5) vs 'db.p' (4) — one deletion → distance 1
        assert _frag_matches("db.py", "db.p") is True

    def test_no_match_completely_different(self):
        """Totally different strings should not match."""
        assert _frag_matches("vector.py", "indexer.py") is False


# ---------------------------------------------------------------------------
# _any_frag_match — OR-across-fragments semantics
# ---------------------------------------------------------------------------

class TestAnyFragMatch:

    def test_first_frag_matches(self):
        assert _any_frag_match(["db.py", "vector.py"], "/home/user/db.py") is True

    def test_second_frag_matches(self):
        assert _any_frag_match(["vector.py", "db.py"], "/home/user/db.py") is True

    def test_none_match(self):
        assert _any_frag_match(["watcher.py", "config.py"], "/home/user/db.py") is False

    def test_empty_frag_list(self):
        """Empty fragment list → no frags to match → False."""
        assert _any_frag_match([], "/home/user/db.py") is False

    def test_fuzzy_in_list(self):
        """List containing a fuzzy-matchable fragment should succeed."""
        # 'dv.py' is one substitution away from 'db.py'
        assert _any_frag_match(["dv.py"], "/home/user/db.py") is True
