"""FTS5 preclean regression: enriched queries containing Jira-style
punctuation (colons, exclamation, slashes, quotes, brackets) must not crash
sqlite3 MATCH queries.

Background: v9 attempt (2026-04-21) to use build_query_text for eval revealed
that `Alias: payment_x` descriptions cause `fts5: no such column: Alias`.
Fix: broadened _FTS_PRECLEAN regex in prepare_finetune_data.py. These tests
lock the invariant so a future simplification doesn't reintroduce the crash.
"""

from __future__ import annotations

import sqlite3

import pytest

from scripts.prepare_finetune_data import preclean_for_fts
from src.search.fts import sanitize_fts_query


def _match_via_sqlite(query: str) -> int | str:
    """Return match count or error string."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    conn.execute("INSERT INTO t(body) VALUES ('trustly payout verification')")
    try:
        row = conn.execute("SELECT count(*) FROM t WHERE body MATCH ?", (query,)).fetchone()
        return row[0]
    except sqlite3.OperationalError as e:
        return f"ERROR: {e}"


@pytest.mark.parametrize(
    "raw",
    [
        "Alias: payment_123",
        "Secret key: abc!xyz",
        "URL /path/to/file",
        'Quote "word" in summary',
        "[FE] bump from 0.21.1 to 0.21.2",
        "payment provider mandatory with 3ds data — we will need to force 3ds",
        "MID: 25130 Login : adir Password : b7",
    ],
)
def test_preclean_query_passes_fts(raw: str) -> None:
    """Enriched queries after preclean+sanitize must MATCH without crash."""
    cleaned = preclean_for_fts(raw)
    sanitized = sanitize_fts_query(cleaned)
    result = _match_via_sqlite(sanitized)
    assert isinstance(result, int), f"FTS crashed on preclean+sanitize of {raw!r} → {sanitized!r}: {result}"


def test_preclean_preserves_alphanumeric() -> None:
    """preclean must not strip word chars / underscores / hyphens / dots."""
    assert preclean_for_fts("trustly_refund.v1-alpha") == "trustly_refund.v1-alpha"


def test_preclean_strips_fts_reserved() -> None:
    """Every FTS-hazardous char must be replaced with space."""
    raw = "a:b{c}d[e]f(g)h\"i'j,k;l!m?n@o#p$q%r^s&t*u+v=w<x>y|z~"
    cleaned = preclean_for_fts(raw)
    # No reserved punct should remain
    for ch in ":{}[]()\"',;!?@#$%^&*+=<>|~":
        assert ch not in cleaned, f"preclean left {ch!r} in output: {cleaned!r}"
