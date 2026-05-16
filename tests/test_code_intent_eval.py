"""Tests for the v1 code-intent eval (NEW).

Two surfaces:
1. The output file (`profiles/pay-com/eval/code_intent_eval_v1.jsonl`) — schema +
   size acceptance bar (>=30 unique queries; median >=3 positives per query;
   stratification recorded per row).
2. The builder helpers (`scripts/build_code_eval.py`) — `_split_tokens`,
   `_stratum_for`, and `relevance_score` produce the expected deterministic
   outputs for synthetic inputs.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

EVAL_PATH = REPO_ROOT / "profiles" / "pay-com" / "eval" / "code_intent_eval_v1.jsonl"

REQUIRED_FIELDS = ("query", "query_id", "expected_paths", "stratum")


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_code_eval",
        REPO_ROOT / "scripts" / "build" / "build_code_eval.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Live JSONL contract ----------------------------------------------------


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    if not EVAL_PATH.exists():
        pytest.skip(f"{EVAL_PATH} not built yet")
    out: list[dict] = []
    with EVAL_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def test_eval_file_exists():
    assert EVAL_PATH.exists(), f"{EVAL_PATH} must be built before running this suite"


def test_jsonl_valid(rows):
    assert rows, "eval is empty"
    for r in rows:
        assert isinstance(r, dict)


def test_schema_present(rows):
    """Required fields with correct primitive types — mirrors docs eval shape."""
    for r in rows:
        for key in REQUIRED_FIELDS:
            assert key in r, f"missing {key} in {r}"
        assert isinstance(r["query"], str) and r["query"].strip()
        assert isinstance(r["query_id"], str) and r["query_id"].strip()
        assert isinstance(r["expected_paths"], list)
        assert isinstance(r["stratum"], str)
        # Each expected_path is {repo_name, file_path}
        for ep in r["expected_paths"]:
            assert isinstance(ep, dict)
            assert ep.get("repo_name")
            assert ep.get("file_path")


def test_min_queries(rows):
    """Acceptance: >= 30 unique queries."""
    assert len(rows) >= 30, f"only {len(rows)} queries, need >= 30"


def test_unique_query_ids(rows):
    """query_id must be unique — downstream eval keys on it."""
    qids = [r["query_id"] for r in rows]
    assert len(qids) == len(set(qids)), "duplicate query_id detected"


def test_median_positives(rows):
    """Acceptance: median positives per query >= 3."""
    pos = [len(r["expected_paths"]) for r in rows]
    median_pos = statistics.median(pos)
    assert median_pos >= 3, f"median positives={median_pos}, need >= 3"


def test_min_positives(rows):
    """Bug-2 contract: every row has >= 3 positives (the build skips below that)."""
    for r in rows:
        n = len(r["expected_paths"])
        assert n >= 3, f"query_id={r['query_id']} has only {n} positives"


def test_stratum_distribution(rows):
    """At least 2 distinct strata represented — proves stratification is wired."""
    strata = {r["stratum"] for r in rows}
    assert len(strata) >= 2, f"only one stratum: {strata}"


# --- builder unit tests -----------------------------------------------------


def test_split_tokens_camelcase():
    """_split_tokens must explode camelCase + word boundaries."""
    builder = _load_builder()
    out = builder._split_tokens("doNotExpire APM_TYPES helloWorld")
    # "do" filtered (len < 3); rest kept.
    assert "donotexpire" in out
    assert "apm" in out  # snake-split from APM_TYPES
    assert "types" in out
    assert "helloworld" in out
    assert "hello" in out
    assert "world" in out


def test_split_tokens_empty():
    builder = _load_builder()
    assert builder._split_tokens("") == set()
    assert builder._split_tokens(None) == set()


def test_stratum_for_keyword_matches():
    """Each keyword stratum is selected when its sentinel word appears."""
    builder = _load_builder()
    assert builder._stratum_for("payment error stack") == "debug"
    assert builder._stratum_for("audit clientIp") == "audit"
    assert builder._stratum_for("trustly webhook flow") == "trace"
    assert builder._stratum_for("grpc-apm-trustly initialize") == "integration"
    # Falls back to lookup for short identifier-style queries.
    assert builder._stratum_for("APM_TYPES list") == "lookup"
    # Long no-keyword query falls through to "other".
    long_q = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    assert builder._stratum_for(long_q) == "other"


def test_relevance_score_hits_path_and_repo():
    """relevance_score adds path + repo + content signals; identical tokens -> ~max."""
    builder = _load_builder()
    qtoks = builder._split_tokens("doNotExpire APM provider")
    cand = {
        "repo_name": "grpc-apm-trustly",
        "file_path": "libs/doNotExpire-handler.js",
        "snippet": "doNotExpire provider config",
    }
    chunk = "doNotExpire APM provider workflow"
    score = builder.relevance_score(qtoks, cand, chunk)
    # All three signals fire; cap at 1.0.
    assert score >= 0.5
    assert score <= 1.0


def test_relevance_score_zero_on_disjoint():
    """No overlap -> score 0.0."""
    builder = _load_builder()
    qtoks = builder._split_tokens("alpha beta gamma")
    cand = {"repo_name": "zzz", "file_path": "yyy/xxx.js", "snippet": "lorem ipsum"}
    score = builder.relevance_score(qtoks, cand, "dolor sit amet")
    assert score == 0.0


def test_relevance_score_handles_empty_query():
    """Empty query -> 0.0, never divide-by-zero."""
    builder = _load_builder()
    cand = {"repo_name": "r", "file_path": "p", "snippet": "anything"}
    score = builder.relevance_score(set(), cand, "anything")
    assert score == 0.0
