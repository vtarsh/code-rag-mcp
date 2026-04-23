"""Tests for widened `_query_wants_docs()` doc-intent detection.

2026-04-23 reformulation agent found `+file_path` transitions 0/9 improved
because doc-intent reformulations (e.g. "Nuvei error code table") failed the
narrow `_DOC_QUERY_RE` trigger. This test locks in the ABSENCE-based widening:
queries with no code signatures and no repo token are doc-intent when 2-15
tokens long.
"""

import pytest

from src.search.hybrid import _query_wants_docs


@pytest.mark.parametrize(
    "query,expected",
    [
        # Absence-based: no code sig, mid-length → doc-intent
        ("Nuvei error code table reason strings", True),
        ("webhook signature", True),
        ("how to configure idempotency", True),
        # Explicit trigger still works
        ("docs/guide/README.md", True),
        # Code signatures → code-intent
        ("handleCallback(req)", False),  # fn() call
        ("SIGTERM_HANDLER", False),  # SCREAMING_SNAKE
        # Repo token → code-intent
        ("grpc-apm-payper payout", False),
    ],
)
def test_query_wants_docs(query: str, expected: bool) -> None:
    assert _query_wants_docs(query) is expected


def test_query_wants_docs_empty() -> None:
    assert _query_wants_docs("") is False
    assert _query_wants_docs(None) is False  # type: ignore[arg-type]


def test_query_wants_docs_snake_case_rejected() -> None:
    """snake_case identifier with 2+ parts is a code signal, not doc-intent."""
    assert _query_wants_docs("async_flow_processor") is False


def test_query_wants_docs_file_ext_rejected() -> None:
    """File extensions .js/.ts/.py/.go/.proto signal code-intent."""
    assert _query_wants_docs("payment.proto schema") is False
    assert _query_wants_docs("handler.ts callback") is False


def test_query_wants_docs_long_query_rejected() -> None:
    """Queries >15 tokens are ambiguous; we fall through to False."""
    q = " ".join(["word"] * 20)
    assert _query_wants_docs(q) is False


def test_query_wants_docs_single_token_rejected() -> None:
    """1-token queries are usually bare identifiers — code-intent."""
    assert _query_wants_docs("reconciliation") is False
