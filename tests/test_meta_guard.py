"""Tests for meta_guard — memoization warning for analyze_task."""

import sqlite3

import pytest

from src.tools.analyze.base import AnalysisContext
from src.tools.analyze.meta_guard import _extract_jira_ids, section_meta_guard


@pytest.fixture
def db():
    """In-memory SQLite with a minimal task_history fixture."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_history (ticket_id TEXT, summary TEXT, description TEXT)")
    rows = [
        ("PI-60", "Payper - Interac e-Transfer APM integration", "Standard APM flow"),
        ("PI-40", "trustly integration", "trustly portal"),
        ("PI-5", "Okto Cash APM integrations", "Integrate Okto Wallet as an APM"),
        ("CORE-2408", "payout method options", "add payout handling"),
        # Add more generic noise so "provider"/"payment" have high DF
        *[(f"GEN-{i}", "payment provider integration", "generic task") for i in range(30)],
    ]
    conn.executemany("INSERT INTO task_history VALUES (?,?,?)", rows)
    conn.commit()
    yield conn
    conn.close()


def _ctx(db, description: str, exclude_task_id: str = "") -> AnalysisContext:
    return AnalysisContext(
        conn=db,
        description=description,
        words=set(),
        provider="",
        exclude_task_id=exclude_task_id,
    )


# ---------------------------------------------------------------------------
# Jira ID detection
# ---------------------------------------------------------------------------


def test_extract_jira_ids_single():
    assert _extract_jira_ids("PI-60") == ["PI-60"]


def test_extract_jira_ids_in_sentence():
    assert _extract_jira_ids("implement PI-60 Payper integration") == ["PI-60"]


def test_extract_jira_ids_multiple():
    assert _extract_jira_ids("Related to PI-60 and CORE-2408") == ["PI-60", "CORE-2408"]


def test_extract_jira_ids_lowercase_rejected():
    # Jira IDs are always uppercase.
    assert _extract_jira_ids("pi-60 was fun") == []


def test_extract_jira_ids_none():
    assert _extract_jira_ids("integrate a new provider") == []


# ---------------------------------------------------------------------------
# Jira ID short-circuit behavior
# ---------------------------------------------------------------------------


def test_warns_on_known_jira_id(db):
    out = section_meta_guard(_ctx(db, "PI-60"))
    assert "Memoization Warning" in out
    assert "directly references" in out
    assert "PI-60" in out


def test_warns_on_embedded_jira_id(db):
    out = section_meta_guard(_ctx(db, "Let's revisit PI-60 implementation"))
    assert "directly references" in out
    assert "PI-60" in out


def test_warns_on_multiple_ids(db):
    out = section_meta_guard(_ctx(db, "Compare PI-60 vs CORE-2408"))
    assert "PI-60" in out
    assert "CORE-2408" in out


def test_ignores_unknown_jira_id(db):
    # PI-999999 has valid format but is NOT in task_history.
    out = section_meta_guard(_ctx(db, "Related to PI-999999"))
    assert out == ""


def test_exclude_task_id_suppresses_jira_warn(db):
    out = section_meta_guard(_ctx(db, "PI-60 Payper", exclude_task_id="PI-60"))
    # PI-60 excluded and no other rare tokens match → no warning.
    assert out == ""


# ---------------------------------------------------------------------------
# Rare-token scoring
# ---------------------------------------------------------------------------


def test_warns_on_rare_token_match(db):
    # "payper" + "interac" + "etransfer" all appear only in PI-60.
    out = section_meta_guard(_ctx(db, "Integrate Payper Interac eTransfer"))
    assert "Memoization Warning" in out
    assert "PI-60" in out
    assert "similarity" in out


def test_quiet_on_generic_query(db):
    # "new provider integration" — all terms appear in many rows → no warn.
    out = section_meta_guard(_ctx(db, "new provider integration"))
    assert out == ""


def test_quiet_on_unknown_terms(db):
    # Iugu/Pix/Brazilian have 0 occurrences → not matched, no warn.
    out = section_meta_guard(_ctx(db, "Add Pix via new Brazilian provider Iugu"))
    assert out == ""


def test_quiet_on_single_rare_token(db):
    # Only "trustly" is rare; MIN_RARE_TOKENS=2 → no warn.
    out = section_meta_guard(_ctx(db, "trustly integration"))
    assert out == ""


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_empty_description_returns_empty(db):
    assert section_meta_guard(_ctx(db, "")) == ""


def test_missing_task_history_returns_empty():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # No task_history table — query will raise, section returns "".
    assert section_meta_guard(_ctx(conn, "PI-60")) == ""
    conn.close()
