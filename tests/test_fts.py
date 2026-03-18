"""Tests for search/fts.py — query expansion and FTS5 sanitization."""

from src.search.fts import expand_query, sanitize_fts_query


class TestExpandQuery:
    def test_single_abbreviation(self):
        result = expand_query("NT provider")
        assert "network token" in result
        assert result.startswith("NT provider")

    def test_multiple_abbreviations(self):
        result = expand_query("3DS and APM")
        assert "3d secure" in result
        assert "alternative payment method" in result

    def test_no_expansion(self):
        result = expand_query("payment gateway")
        assert result == "payment gateway"

    def test_case_insensitive(self):
        result = expand_query("nt")
        assert "network token" in result

    def test_strips_punctuation(self):
        result = expand_query("What about NT?")
        assert "network token" in result

    def test_empty_query(self):
        assert expand_query("") == ""

    def test_ddm_expansion(self):
        result = expand_query("DDM flow")
        assert "direct debit mandate" in result


class TestSanitizeFtsQuery:
    def test_simple_words(self):
        result = sanitize_fts_query("payment gateway")
        assert "payment" in result
        assert "gateway" in result
        assert "OR" in result

    def test_short_tokens_skipped(self):
        result = sanitize_fts_query("a to payment")
        # "a" and "to" are < 3 chars, should be skipped
        assert result == "payment"

    def test_hyphenated_term_quoted(self):
        result = sanitize_fts_query("grpc-apm-trustly")
        assert '"grpc-apm-trustly"' in result

    def test_dotted_token_split_and_quoted(self):
        result = sanitize_fts_query("payment.completed")
        assert "payment" in result
        assert "completed" in result
        assert '"payment.completed"' in result

    def test_dotted_short_parts_filtered(self):
        # "a.b" — both parts < 3 chars, only quoted form remains
        result = sanitize_fts_query("a.b")
        assert '"a.b"' in result

    def test_empty_query_returns_original(self):
        result = sanitize_fts_query("")
        assert result == ""

    def test_all_short_tokens_returns_original(self):
        result = sanitize_fts_query("a b")
        assert result == "a b"

    def test_or_joining(self):
        result = sanitize_fts_query("trustly verification webhook")
        parts = result.split(" OR ")
        assert len(parts) == 3
