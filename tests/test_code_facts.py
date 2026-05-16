"""Tests for search/code_facts.py — FTS5 search over the code_facts table."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src.search.code_facts import code_facts_search, fetch_chunks_for_files


def _mock_db_connection(mock_conn):
    @contextmanager
    def _cm():
        yield mock_conn

    return _cm


class TestCodeFactsSearch:
    @patch("src.search.code_facts.db_connection")
    def test_returns_matching_rows(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "repo_name": "grpc-apm-trustly",
                "file_path": "libs/validate.js",
                "fact_type": "validation_guard",
                "condition": "status !== 'SUCCESS'",
                "message": "invalid status",
                "line_number": 42,
            }
        ]
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)

        hits = code_facts_search("trustly status")
        assert len(hits) == 1
        assert hits[0]["repo_name"] == "grpc-apm-trustly"
        assert hits[0]["fact_type"] == "validation_guard"

    @patch("src.search.code_facts.db_connection")
    def test_empty_query_returns_empty(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)
        # Empty string sanitizes to "" → early return, no DB call
        assert code_facts_search("") == []
        mock_conn.execute.assert_not_called()

    @patch("src.search.code_facts.db_connection")
    def test_repo_filter_passes_param(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)

        code_facts_search("signature", repo="apm-trustly")
        call_args = mock_conn.execute.call_args
        sql, params = call_args[0]
        assert "cf.repo_name LIKE ?" in sql
        assert "%apm-trustly%" in params

    @patch("src.search.code_facts.db_connection")
    def test_handles_operational_error(self, mock_db_conn):
        import sqlite3

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such table")
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)

        assert code_facts_search("anything") == []


class TestFetchChunksForFiles:
    @patch("src.search.code_facts.db_connection")
    def test_fetches_first_chunk_per_pair(self, mock_db_conn):
        mock_conn = MagicMock()

        def execute_side_effect(sql, params):
            result = MagicMock()
            result.fetchall.return_value = [
                {
                    "rowid": 100,
                    "repo_name": "repo-a",
                    "file_path": "src/foo.js",
                    "file_type": "library",
                    "chunk_type": "code_file",
                    "snippet": "first 400 chars of foo.js content",
                }
            ]
            return result

        mock_conn.execute.side_effect = execute_side_effect
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)

        pairs = [("repo-a", "src/foo.js"), ("repo-b", "missing.js")]
        chunks = fetch_chunks_for_files(pairs)
        assert len(chunks) == 1
        assert chunks[0]["rowid"] == 100
        assert chunks[0]["repo_name"] == "repo-a"

        # Verify batched query: single call with flat param list
        assert mock_conn.execute.call_count == 1
        sql, params = mock_conn.execute.call_args[0]
        assert "VALUES" in sql
        assert "IN" in sql
        assert params == ["repo-a", "src/foo.js", "repo-b", "missing.js"]

    def test_empty_pairs_returns_empty(self):
        assert fetch_chunks_for_files([]) == []
