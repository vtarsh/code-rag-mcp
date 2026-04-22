"""Tests for scripts/churn_reranker_judge.py (local MiniLM judge)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def judge_module():
    """Import the script as a module for direct unit testing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "churn_reranker_judge", REPO_ROOT / "scripts" / "churn_reranker_judge.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Ephemeral SQLite DB mirroring the `chunks` FTS5 projection we query."""
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE chunks (content TEXT, repo_name TEXT, file_path TEXT, "
        "file_type TEXT, chunk_type TEXT, language TEXT)"
    )
    rows = [
        ("payout handler code", "grpc-apm-payper", "methods/payout.js", "grpc_method", "function", "js"),
        ("stripe client code", "grpc-mpi-stripe", "libs/stripe-client.js", "library", "function", "js"),
        ("ach payment doc", "grpc-apm-ach", "docs/docs/architecture.md", "doc", "section", "md"),
    ]
    cur.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db_path


def test_fetch_snippet_returns_content(judge_module, tmp_db: Path):
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    snip = judge_module.fetch_snippet(cur, "grpc-apm-payper", "methods/payout.js", "function", max_chars=1000)
    assert snip == "payout handler code"
    conn.close()


def test_fetch_snippet_falls_back_when_chunk_type_missing(judge_module, tmp_db: Path):
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    snip = judge_module.fetch_snippet(cur, "grpc-apm-payper", "methods/payout.js", "NONEXISTENT", max_chars=1000)
    assert snip == "payout handler code"
    conn.close()


def test_fetch_snippet_returns_empty_when_not_found(judge_module, tmp_db: Path):
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    snip = judge_module.fetch_snippet(cur, "no-repo", "no-file", "no-type", max_chars=1000)
    assert snip == ""
    conn.close()


def test_fetch_snippet_respects_char_cap(judge_module, tmp_path: Path):
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE chunks (content TEXT, repo_name TEXT, file_path TEXT, "
        "file_type TEXT, chunk_type TEXT, language TEXT)"
    )
    cur.execute(
        "INSERT INTO chunks VALUES (?,?,?,?,?,?)",
        ("x" * 5000, "r", "f", "library", "function", "js"),
    )
    conn.commit()
    snip = judge_module.fetch_snippet(cur, "r", "f", "function", max_chars=100)
    assert len(snip) == 100
    conn.close()


def test_score_list_aggregates_mean(judge_module, tmp_db: Path):
    judge = MagicMock()
    judge.predict.return_value = [5.0, 3.0]
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    items = [
        {"repo_name": "grpc-apm-payper", "file_path": "methods/payout.js", "chunk_type": "function"},
        {"repo_name": "grpc-apm-ach", "file_path": "docs/docs/architecture.md", "chunk_type": "section"},
    ]
    mean, scores = judge_module.score_list(judge, "payout query", items, cur, max_chars=500, batch_size=2)
    assert mean == pytest.approx(4.0)
    assert scores == [5.0, 3.0]
    # Prompt format: "repo file snippet"
    pairs = judge.predict.call_args.args[0]
    assert pairs[0][0] == "payout query"
    assert "grpc-apm-payper" in pairs[0][1]
    assert "methods/payout.js" in pairs[0][1]
    assert "payout handler code" in pairs[0][1]
    conn.close()


def test_score_list_returns_empty_on_no_items(judge_module, tmp_db: Path):
    judge = MagicMock()
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    mean, scores = judge_module.score_list(judge, "q", [], cur, max_chars=500, batch_size=2)
    assert mean == 0.0
    assert scores == []
    judge.predict.assert_not_called()
    conn.close()


def test_deprecated_haiku_script_is_deleted():
    """Audit 2026-04-22: the Haiku stub was deleted after the migration to
    the local MiniLM judge. Git history preserves the original scaffold.
    """
    assert not (REPO_ROOT / "scripts" / "churn_llm_judge.py").exists()
