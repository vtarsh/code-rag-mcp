"""Tests for search/env_vars.py — UPPERCASE identifier retrieval."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src.search.env_vars import env_var_search, extract_upper_idents


def _mock_db_connection(mock_conn):
    @contextmanager
    def _cm():
        yield mock_conn

    return _cm


class TestExtractUpperIdents:
    def test_extracts_snake_case_upper(self):
        idents = extract_upper_idents("Fix FORCE_REDIRECTS_PROVIDERS for trustly")
        assert idents == ["FORCE_REDIRECTS_PROVIDERS"]

    def test_multiple_idents_deduped(self):
        idents = extract_upper_idents("set PORT and HOST and PORT again")
        assert idents == ["PORT", "HOST"]

    def test_short_acronyms_matched(self):
        # 3-char minimum still allows URL / API / TLS
        idents = extract_upper_idents("call API with URL")
        assert "API" in idents
        assert "URL" in idents

    def test_lower_and_mixed_ignored(self):
        assert extract_upper_idents("call apiSomething + url + Post") == []

    def test_empty_query(self):
        assert extract_upper_idents("") == []

    def test_two_char_ident_ignored(self):
        # {2,} quantifier → needs ≥ 3 chars
        assert extract_upper_idents("use AB and XY") == []


class TestEnvVarSearch:
    @patch("src.search.env_vars.db_connection")
    def test_no_upper_idents_skips_db(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)

        assert env_var_search("lowercase query") == []
        mock_conn.execute.assert_not_called()

    @patch("src.search.env_vars.db_connection")
    def test_returns_matching_rows(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "repo": "express-api-callbacks",
                "var_name": "FORCE_REDIRECTS_PROVIDERS",
                "raw_value": "skrill",
                "source": "consts_js_raw",
            }
        ]
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)

        hits = env_var_search("set FORCE_REDIRECTS_PROVIDERS to true")
        assert len(hits) == 1
        assert hits[0]["var_name"] == "FORCE_REDIRECTS_PROVIDERS"

        # Verify LIKE pattern was passed
        call_args = mock_conn.execute.call_args
        _sql, params = call_args[0]
        assert "%FORCE_REDIRECTS_PROVIDERS%" in params

    @patch("src.search.env_vars.db_connection")
    def test_multiple_idents_in_query(self, mock_db_conn):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)

        env_var_search("PORT and HOST collision")
        sql, params = mock_conn.execute.call_args[0]
        # Two OR clauses + limit
        assert sql.count("var_name LIKE ?") == 2
        assert "%PORT%" in params
        assert "%HOST%" in params

    @patch("src.search.env_vars.db_connection")
    def test_handles_operational_error(self, mock_db_conn):
        import sqlite3

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such table")
        mock_db_conn.side_effect = _mock_db_connection(mock_conn)

        assert env_var_search("set PORT") == []
