"""Tests for src/search/vector.py — vector_search error propagation."""

from unittest.mock import patch


class TestVectorSearchErrorPropagation:
    def test_vector_search_propagates_warning(self):
        """If get_vector_search returns (None, None, warning), vector_search
        must surface that warning verbatim rather than the generic fallback."""
        from src.search import vector as vector_mod

        warning = "lance dir missing at /tmp/foo"
        with patch.object(
            vector_mod,
            "get_vector_search",
            return_value=(None, None, warning),
        ):
            results, err = vector_mod.vector_search("hello")

        assert results == []
        assert err == warning

    def test_vector_search_generic_when_no_error_and_no_warning(self):
        """If get_vector_search returns (None, None, None), vector_search
        falls back to the generic 'unavailable' string."""
        from src.search import vector as vector_mod

        with patch.object(
            vector_mod,
            "get_vector_search",
            return_value=(None, None, None),
        ):
            results, err = vector_mod.vector_search("hello")

        assert results == []
        assert err is not None
        assert "unavailable" in err.lower()

    def test_vector_search_propagates_err_when_table_none(self):
        """Hard error path: provider present but table missing, err set."""
        from src.search import vector as vector_mod

        hard_err = "No vector table at /tmp/bar. Run: python3 scripts/build_vectors.py"
        with patch.object(
            vector_mod,
            "get_vector_search",
            return_value=(object(), None, hard_err),
        ):
            results, err = vector_mod.vector_search("hello")

        assert results == []
        assert err == hard_err
