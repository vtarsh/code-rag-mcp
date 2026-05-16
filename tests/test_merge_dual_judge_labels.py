"""Tests for scripts/merge_dual_judge_labels.py (dual-judge consensus merger)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def merger_module():
    """Import the script as a module for direct unit testing."""
    spec = importlib.util.spec_from_file_location(
        "merge_dual_judge_labels",
        REPO_ROOT / "scripts" / "data" / "merge_dual_judge_labels.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- Truth table: every cell ------------------------------------------------


@pytest.mark.parametrize(
    "opus,minilm,expected_label,expected_reason",
    [
        ("+", "+", "+", "both_positive"),
        ("-", "-", "-", "both_negative"),
        ("+", "-", "?_CONFLICT", "conflict_opus_plus_minilm_minus"),
        ("-", "+", "?_CONFLICT", "conflict_opus_minus_minilm_plus"),
        ("+", "?", "+", "opus_trumps_minilm_ambiguous"),
        ("-", "?", "-", "opus_trumps_minilm_ambiguous"),
        ("?", "+", "+", "minilm_trumps_opus_ambiguous"),
        ("?", "-", "-", "minilm_trumps_opus_ambiguous"),
        ("?", "?", "?", "both_ambiguous"),
    ],
)
def test_merge_labels_truth_table(
    merger_module, opus: str, minilm: str, expected_label: str, expected_reason: str
) -> None:
    label, reason = merger_module.merge_labels(opus, minilm)
    assert label == expected_label
    assert reason == expected_reason


def test_merge_labels_rejects_unexpected_pair(merger_module) -> None:
    with pytest.raises(ValueError):
        merger_module.merge_labels("x", "+")


# ---- Schema preservation ----------------------------------------------------


def _input_row(
    query: str,
    file_path: str,
    rank: int,
    label: str,
    *,
    note: str = "",
    minilm_score: float | None = None,
    judge: str = "opus-bias-aware",
    category: str = "doc",
    query_tag: str = "doc-intent",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "query": query,
        "query_tag": query_tag,
        "rank": rank,
        "repo_name": "my-repo",
        "file_path": file_path,
        "file_type": "docs",
        "chunk_type": "doc_section",
        "combined_score": 0.9,
        "rerank_score": 0.8,
        "penalty": 0.0,
        "category": category,
        "label": label,
        "note": note,
        "regen_source": "test",
        "judge": judge,
    }
    if minilm_score is not None:
        row["minilm_score"] = minilm_score
    return row


def test_merge_rows_preserves_all_input_fields(merger_module) -> None:
    """All original Opus fields survive; new consensus fields are added."""
    opus = [_input_row("q1", "a.md", 1, "+", note="relevant doc")]
    minilm = [_input_row("q1", "a.md", 1, "+", minilm_score=0.8, judge="minilm-L6")]
    merged = merger_module.merge_rows(opus, minilm)
    assert len(merged) == 1
    row = merged[0]

    # Every original field from the Opus row is present.
    for k in (
        "query",
        "query_tag",
        "rank",
        "repo_name",
        "file_path",
        "file_type",
        "chunk_type",
        "combined_score",
        "rerank_score",
        "penalty",
        "category",
        "note",
        "regen_source",
    ):
        assert k in row, f"missing {k!r} in merged row"

    # New consensus fields.
    assert row["label_consensus"] == "+"
    assert row["consensus_reason"] == "both_positive"
    assert row["label_opus"] == "+"
    assert row["label_minilm"] == "+"
    assert row["note_opus"] == "relevant doc"
    assert row["minilm_score"] == 0.8

    # Ambiguous single-judge columns are stripped.
    assert "label" not in row
    assert "judge" not in row


def test_merge_rows_matches_by_query_filepath_rank(merger_module) -> None:
    """Same file at different ranks is two distinct rows (chunks of same file)."""
    opus = [
        _input_row("q1", "f.md", 1, "+", note="chunk A"),
        _input_row("q1", "f.md", 2, "-", note="chunk B"),
    ]
    minilm = [
        _input_row("q1", "f.md", 1, "+", minilm_score=0.7),
        _input_row("q1", "f.md", 2, "+", minilm_score=0.6),  # disagrees!
    ]
    merged = merger_module.merge_rows(opus, minilm)
    assert len(merged) == 2
    assert merged[0]["label_consensus"] == "+"
    assert merged[0]["note_opus"] == "chunk A"
    # Second row: Opus "-", MiniLM "+" -> ?_CONFLICT
    assert merged[1]["label_consensus"] == "?_CONFLICT"
    assert merged[1]["consensus_reason"] == "conflict_opus_minus_minilm_plus"
    assert merged[1]["note_opus"] == "chunk B"


def test_merge_rows_rejects_duplicate_keys(merger_module) -> None:
    """Duplicate (query, file_path, rank) in either input is a hard error."""
    opus = [
        _input_row("q1", "a.md", 1, "+"),
        _input_row("q1", "a.md", 1, "-"),  # dup
    ]
    minilm = [
        _input_row("q1", "a.md", 1, "+", minilm_score=0.5),
        _input_row("q1", "a.md", 1, "+", minilm_score=0.5),
    ]
    with pytest.raises(ValueError, match="duplicate key"):
        merger_module.merge_rows(opus, minilm)


def test_merge_rows_rejects_unmatched_rows(merger_module) -> None:
    """If one judge has a row the other doesn't, error loudly."""
    opus = [_input_row("q1", "a.md", 1, "+")]
    minilm = [_input_row("q2", "b.md", 1, "+", minilm_score=0.5)]
    with pytest.raises(ValueError, match="out of sync"):
        merger_module.merge_rows(opus, minilm)


def test_merge_rows_preserves_order_of_opus_input(merger_module) -> None:
    """Output order follows Opus input order for deterministic downstream diffs."""
    opus = [
        _input_row("q2", "b.md", 1, "-"),
        _input_row("q1", "a.md", 1, "+"),
    ]
    minilm = [
        _input_row("q1", "a.md", 1, "+", minilm_score=0.5),
        _input_row("q2", "b.md", 1, "-", minilm_score=0.05),
    ]
    merged = merger_module.merge_rows(opus, minilm)
    assert [(r["query"], r["file_path"]) for r in merged] == [
        ("q2", "b.md"),
        ("q1", "a.md"),
    ]


# ---- End-to-end via main() --------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_main_end_to_end(merger_module, tmp_path: Path, monkeypatch) -> None:
    """Full CLI: read two jsonl, write consensus jsonl + disagreement md."""
    opus_rows = [
        _input_row("agree", "a.md", 1, "+", note="doc matches"),
        _input_row("agree-neg", "b.md", 1, "-", note="not relevant"),
        _input_row("conflict", "c.md", 1, "+", note="Opus says yes", query_tag="repo-intent", category="code"),
        _input_row("ambig", "d.md", 1, "+", note="Opus confident"),
    ]
    minilm_rows = [
        _input_row("agree", "a.md", 1, "+", minilm_score=0.8, judge="minilm-L6"),
        _input_row("agree-neg", "b.md", 1, "-", minilm_score=0.05, judge="minilm-L6"),
        _input_row(
            "conflict", "c.md", 1, "-", minilm_score=0.08, judge="minilm-L6", query_tag="repo-intent", category="code"
        ),
        _input_row("ambig", "d.md", 1, "?", minilm_score=0.3, judge="minilm-L6"),
    ]
    opus_path = tmp_path / "opus.jsonl"
    minilm_path = tmp_path / "minilm.jsonl"
    out_path = tmp_path / "consensus.jsonl"
    md_path = tmp_path / "DISAGREEMENTS.md"
    _write_jsonl(opus_path, opus_rows)
    _write_jsonl(minilm_path, minilm_rows)

    argv = [
        "merge_dual_judge_labels",
        "--opus",
        str(opus_path),
        "--minilm",
        str(minilm_path),
        "--out",
        str(out_path),
        "--disagreements",
        str(md_path),
    ]
    monkeypatch.setattr("sys.argv", argv)
    rc = merger_module.main()
    assert rc == 0

    # Consensus jsonl: all 4 rows, correct labels, every input field preserved.
    out_rows = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    assert len(out_rows) == 4
    labels = [r["label_consensus"] for r in out_rows]
    assert labels == ["+", "-", "?_CONFLICT", "+"]
    reasons = [r["consensus_reason"] for r in out_rows]
    assert reasons == [
        "both_positive",
        "both_negative",
        "conflict_opus_plus_minilm_minus",
        "opus_trumps_minilm_ambiguous",
    ]
    # Opus note carried through under note_opus; MiniLM score present.
    assert out_rows[2]["note_opus"] == "Opus says yes"
    assert out_rows[2]["minilm_score"] == 0.08

    # Disagreement markdown exists, is self-contained, and lists the conflict.
    md = md_path.read_text(encoding="utf-8")
    assert "# v12 Candidate Labels — Disagreement Report" in md
    assert "Total conflicts:** 1 / 4" in md
    assert "conflict" in md  # the query text
    assert "Opus says yes" in md  # Opus note rendered
    # Non-conflict queries should NOT appear in the disagreement report body.
    assert "agree-neg" not in md


def test_main_errors_when_row_counts_differ(merger_module, tmp_path: Path, monkeypatch) -> None:
    opus = [_input_row("q", "a.md", 1, "+")]
    minilm = [
        _input_row("q", "a.md", 1, "+", minilm_score=0.5),
        _input_row("q", "b.md", 1, "+", minilm_score=0.5),
    ]
    opus_path = tmp_path / "opus.jsonl"
    minilm_path = tmp_path / "minilm.jsonl"
    _write_jsonl(opus_path, opus)
    _write_jsonl(minilm_path, minilm)

    argv = [
        "merge_dual_judge_labels",
        "--opus",
        str(opus_path),
        "--minilm",
        str(minilm_path),
        "--out",
        str(tmp_path / "c.jsonl"),
        "--disagreements",
        str(tmp_path / "d.md"),
    ]
    monkeypatch.setattr("sys.argv", argv)
    rc = merger_module.main()
    assert rc == 1


def test_main_errors_when_input_missing(merger_module, tmp_path: Path, monkeypatch) -> None:
    argv = [
        "merge_dual_judge_labels",
        "--opus",
        str(tmp_path / "nope.jsonl"),
        "--minilm",
        str(tmp_path / "nope2.jsonl"),
        "--out",
        str(tmp_path / "c.jsonl"),
        "--disagreements",
        str(tmp_path / "d.md"),
    ]
    monkeypatch.setattr("sys.argv", argv)
    rc = merger_module.main()
    assert rc == 1


# ---- Disagreement markdown --------------------------------------------------


def test_disagreement_markdown_has_summary_and_hints(merger_module) -> None:
    """Report must be self-contained: stats block + per-row hints."""
    merged = [
        {
            "query": "doc-intent hit on code file",
            "query_tag": "doc-intent",
            "rank": 1,
            "repo_name": "r",
            "file_path": "src/x.py",
            "chunk_type": "function",
            "category": "code",
            "label_consensus": "?_CONFLICT",
            "consensus_reason": "conflict_opus_plus_minilm_minus",
            "label_opus": "+",
            "label_minilm": "-",
            "note_opus": "handler matches query",
            "minilm_score": 0.05,
        },
        {
            "query": "both_agree_positive_row_xyz",
            "query_tag": "doc-intent",
            "rank": 1,
            "label_consensus": "+",
            "consensus_reason": "both_positive",
            "category": "doc",
        },
    ]
    md = merger_module.build_disagreement_markdown(merged)
    # Summary mentions 1 conflict / 2 total.
    assert "Total conflicts:** 1 / 2" in md
    # Per-row arbitration hint rendered.
    assert "handler matches query" in md
    assert "doc-intent query hit a code file" in md
    # Non-conflict row is NOT in the row listing.
    assert "both_agree_positive_row_xyz" not in md
