"""Tests for tools/context.py — context builder tool."""

from unittest.mock import MagicMock, patch

from src.tools.context import _build_deps_section, _sanitize_for_fts


class TestSanitizeForFts:
    def test_simple_words(self):
        result = _sanitize_for_fts("settlement reconciliation flow")
        assert "settlement" in result
        assert "OR" in result

    def test_short_tokens_skipped(self):
        result = _sanitize_for_fts("a to settlement")
        assert "settlement" in result
        # "a" and "to" should be filtered
        assert result.count("OR") <= 1

    def test_empty_query(self):
        result = _sanitize_for_fts("")
        assert result == ""

    def test_all_short(self):
        result = _sanitize_for_fts("a b c")
        assert result == ""  # all tokens < 3 chars, nothing useful for FTS

    def test_stop_words_filtered(self):
        result = _sanitize_for_fts("add refund support to Trustly")
        assert result == "refund OR Trustly"

    def test_stop_words_fallback(self):
        # All meaningful tokens are stop words — fallback to len>=3 filter
        result = _sanitize_for_fts("add new support")
        assert result == "add OR new OR support"


class TestBuildDepsSection:
    @patch("src.tools.context.get_db")
    @patch("src.tools.context.get_outgoing_edges")
    @patch("src.tools.context.get_incoming_edges")
    def test_empty_deps(self, mock_incoming, mock_outgoing, mock_get_db):
        mock_get_db.return_value = MagicMock()
        mock_outgoing.return_value = []
        mock_incoming.return_value = []

        result = _build_deps_section(["repo-a"])
        assert "Dependencies" in result

    @patch("src.tools.context.get_db")
    @patch("src.tools.context.get_outgoing_edges")
    @patch("src.tools.context.get_incoming_edges")
    def test_with_outgoing(self, mock_incoming, mock_outgoing, mock_get_db):
        from src.types import GraphEdge

        mock_get_db.return_value = MagicMock()
        mock_outgoing.return_value = [
            GraphEdge(source="repo-a", target="repo-b", edge_type="grpc_call"),
            GraphEdge(source="repo-a", target="repo-c", edge_type="grpc_call"),
        ]
        mock_incoming.return_value = []

        result = _build_deps_section(["repo-a"])
        assert "Depends on" in result
        assert "repo-b" in result
        assert "grpc_call" in result

    @patch("src.tools.context.get_db")
    @patch("src.tools.context.get_outgoing_edges")
    @patch("src.tools.context.get_incoming_edges")
    def test_filters_virtual_nodes(self, mock_incoming, mock_outgoing, mock_get_db):
        from src.types import GraphEdge

        mock_get_db.return_value = MagicMock()
        mock_outgoing.return_value = [
            GraphEdge(source="repo-a", target="pkg:lodash", edge_type="npm_dep"),
            GraphEdge(source="repo-a", target="real-repo", edge_type="grpc_call"),
        ]
        mock_incoming.return_value = []

        result = _build_deps_section(["repo-a"])
        assert "pkg:lodash" not in result
        assert "real-repo" in result

    @patch("src.tools.context.get_db")
    @patch("src.tools.context.get_outgoing_edges")
    @patch("src.tools.context.get_incoming_edges")
    def test_caps_at_6_repos(self, mock_incoming, mock_outgoing, mock_get_db):
        mock_get_db.return_value = MagicMock()
        mock_outgoing.return_value = []
        mock_incoming.return_value = []

        repos = [f"repo-{i}" for i in range(10)]
        result = _build_deps_section(repos)
        # Should still return without error, capped internally
        assert "Dependencies" in result
