"""Tests for scripts/runpod/prepare_train_data.py.

Covers:
- 0-byte db path rejection (profiles/pay-com/knowledge.db trap)
- filter drops non-doc-intent / label_final != '+' rows
- (query, file_path) dedup keeps one pair
- secret scrub drops row + records count
- resolved_count < subset → clear error
- seed determinism over sample() output
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.runpod import prepare_train_data as ptd

# ----- helpers ---------------------------------------------------------------


def _write_labeled(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _build_db(path: Path, chunks: list[tuple[str, str, str]]) -> None:
    """chunks = [(repo_name, file_path, content), ...]"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE VIRTUAL TABLE chunks USING fts5("
        "content, repo_name, file_path, file_type, chunk_type, language, "
        "tokenize='porter unicode61')"
    )
    for repo, fp, content in chunks:
        conn.execute(
            "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, repo, fp, "docs", "doc_section", "markdown"),
        )
    conn.commit()
    conn.close()


def _row(query: str, repo: str, fp: str, *, tag="doc-intent", label="+") -> dict:
    return {
        "query": query,
        "query_tag": tag,
        "repo_name": repo,
        "file_path": fp,
        "label_final": label,
    }


# ----- tests -----------------------------------------------------------------


def test_rejects_empty_db_path(tmp_path):
    """profiles/pay-com/knowledge.db is 0 bytes on disk — must abort loudly."""
    empty = tmp_path / "knowledge.db"
    empty.touch()
    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(labeled, [_row("q", "r", "f")])

    with pytest.raises(ValueError, match="empty"):
        ptd.build_pairs(labeled, empty)


def test_rejects_missing_db_path(tmp_path):
    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(labeled, [_row("q", "r", "f")])
    with pytest.raises(FileNotFoundError, match="knowledge db"):
        ptd.build_pairs(labeled, tmp_path / "nope.db")


def test_rejects_missing_labeled(tmp_path):
    db = tmp_path / "kb.db"
    _build_db(db, [("r", "f.md", "x")])
    with pytest.raises(FileNotFoundError, match="labeled"):
        ptd.build_pairs(tmp_path / "missing.jsonl", db)


def test_filters_doc_intent_positive_only(tmp_path):
    labeled = tmp_path / "labeled.jsonl"
    rows = [
        _row("q1", "r1", "a.md"),  # keep
        _row("q2", "r1", "b.md", tag="code-intent"),  # drop: wrong tag
        _row("q3", "r1", "c.md", label="-"),  # drop: negative
        _row("q4", "r1", "d.md", label="?"),  # drop: ambiguous
    ]
    _write_labeled(labeled, rows)
    db = tmp_path / "kb.db"
    _build_db(
        db,
        [
            ("r1", "a.md", "Alpha content"),
            ("r1", "b.md", "Beta content"),
            ("r1", "c.md", "Gamma content"),
            ("r1", "d.md", "Delta content"),
        ],
    )

    pairs, missing, scrubbed = ptd.build_pairs(labeled, db)
    assert len(pairs) == 1
    assert pairs[0]["query"] == "q1"
    assert pairs[0]["positive"] == "Alpha content"
    assert missing == 0
    assert scrubbed == 0


def test_dedups_by_query_file_path(tmp_path):
    labeled = tmp_path / "labeled.jsonl"
    # Same (query, file_path) appearing twice — only one pair should survive.
    rows = [
        _row("same-q", "r1", "same.md"),
        _row("same-q", "r1", "same.md"),
        _row("same-q", "r2", "other.md"),  # different file_path → kept
    ]
    _write_labeled(labeled, rows)
    db = tmp_path / "kb.db"
    _build_db(
        db,
        [
            ("r1", "same.md", "one"),
            ("r2", "other.md", "two"),
        ],
    )

    pairs, _, _ = ptd.build_pairs(labeled, db)
    assert len(pairs) == 2
    seen_files = sorted(p["_file_path"] for p in pairs)
    assert seen_files == ["other.md", "same.md"]


def test_secrets_scrub_drops_matching_row(tmp_path):
    labeled = tmp_path / "labeled.jsonl"
    rows = [
        _row("q1", "r1", "safe.md"),
        _row("q2", "r1", "leaky.md"),
    ]
    _write_labeled(labeled, rows)
    db = tmp_path / "kb.db"
    _build_db(
        db,
        [
            ("r1", "safe.md", "Clean docs content"),
            ("r1", "leaky.md", "config: X-Api-Key = abc123"),
        ],
    )

    pairs, missing, scrubbed = ptd.build_pairs(labeled, db)
    assert [p["query"] for p in pairs] == ["q1"]
    assert scrubbed == 1
    assert missing == 0


def test_missing_content_counted_as_missing_not_scrubbed(tmp_path):
    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(
        labeled,
        [
            _row("q1", "r1", "present.md"),
            _row("q2", "r1", "absent.md"),
        ],
    )
    db = tmp_path / "kb.db"
    _build_db(db, [("r1", "present.md", "here")])

    pairs, missing, scrubbed = ptd.build_pairs(labeled, db)
    assert len(pairs) == 1
    assert missing == 1
    assert scrubbed == 0


def test_assert_row_count_fails_if_below_subset(tmp_path, capsys):
    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(labeled, [_row("q1", "r1", "a.md")])
    db = tmp_path / "kb.db"
    _build_db(db, [("r1", "a.md", "one")])
    out = tmp_path / "train.jsonl"

    rc = ptd.run(
        subset=5,
        seed=42,
        out=out,
        db_path=db,
        labeled_path=labeled,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "resolved 1 rows, requested 5" in err
    assert not out.exists()


def test_seed_determines_sample(tmp_path):
    pairs = [{"query": f"q{i}", "positive": f"p{i}", "_repo_name": "r", "_file_path": f"{i}"} for i in range(20)]
    a = ptd.sample_pairs(pairs, subset=5, seed=42)
    b = ptd.sample_pairs(pairs, subset=5, seed=42)
    c = ptd.sample_pairs(pairs, subset=5, seed=99)
    assert [x["query"] for x in a] == [x["query"] for x in b]
    assert [x["query"] for x in a] != [x["query"] for x in c]
    assert len(a) == 5


def test_full_mode_emits_every_resolved_pair(tmp_path):
    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(
        labeled,
        [
            _row("q1", "r1", "a.md"),
            _row("q2", "r1", "b.md"),
            _row("q3", "r1", "c.md"),
        ],
    )
    db = tmp_path / "kb.db"
    _build_db(
        db,
        [
            ("r1", "a.md", "alpha"),
            ("r1", "b.md", "beta"),
            ("r1", "c.md", "gamma"),
        ],
    )
    out = tmp_path / "train.jsonl"

    rc = ptd.run(subset=None, seed=42, out=out, db_path=db, labeled_path=labeled)
    assert rc == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for ln in lines:
        obj = json.loads(ln)
        assert set(obj.keys()) == {"query", "positive"}


def test_output_jsonl_is_prefix_free(tmp_path):
    """Stage C training expects raw text in 'query'/'positive'; the nomic
    search_query:/search_document: prefix is added downstream in train_docs_embedder.
    """
    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(labeled, [_row("vyne API url", "grpc-oauth-vyne", "docs/data-layer.md")])
    db = tmp_path / "kb.db"
    _build_db(db, [("grpc-oauth-vyne", "docs/data-layer.md", "VYNE_API_URL points at uat.")])
    out = tmp_path / "train.jsonl"

    rc = ptd.run(subset=1, seed=42, out=out, db_path=db, labeled_path=labeled)
    assert rc == 0
    obj = json.loads(out.read_text().splitlines()[0])
    assert not obj["query"].startswith("search_query:")
    assert not obj["positive"].startswith("search_document:")


# ----- eval-disjoint guards (P7 Phase 1.2 / CM4) -----------------------------


def _write_eval(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _eval_row(query: str, expected: list[tuple[str, str]]) -> dict:
    return {
        "query": query,
        "expected_paths": [{"repo_name": repo, "file_path": fp} for repo, fp in expected],
    }


def test_query_disjoint_passes_clean_pairs(tmp_path):
    """Train pairs whose queries are NOT in the eval set must write cleanly."""
    eval_file = tmp_path / "eval.jsonl"
    _write_eval(
        eval_file,
        [
            _eval_row("eval query alpha", [("repo-x", "docs/x.md")]),
            _eval_row("eval query beta", [("repo-y", "docs/y.md")]),
        ],
    )

    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(
        labeled,
        [
            _row("totally fresh train query", "repo-z", "docs/z.md"),
        ],
    )
    db = tmp_path / "kb.db"
    _build_db(db, [("repo-z", "docs/z.md", "Zeta content")])
    out = tmp_path / "train.jsonl"

    rc = ptd.run(
        subset=None,
        seed=42,
        out=out,
        db_path=db,
        labeled_path=labeled,
        eval_files=(eval_file,),
    )
    assert rc == 0
    assert out.exists()
    line = json.loads(out.read_text().splitlines()[0])
    assert line["query"] == "totally fresh train query"


def test_query_disjoint_rejects_leaking_pair(tmp_path):
    """A train pair whose query is in eval-v3 must raise ValueError."""
    eval_file = tmp_path / "eval.jsonl"
    _write_eval(
        eval_file,
        [
            _eval_row("Nuvei payout checksum formula", [("repo-x", "docs/x.md")]),
        ],
    )

    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(
        labeled,
        [
            # Same query as eval, different positive doc → silver-positive leak.
            _row("Nuvei payout checksum formula", "repo-z", "docs/z.md"),
        ],
    )
    db = tmp_path / "kb.db"
    _build_db(db, [("repo-z", "docs/z.md", "Zeta content")])
    out = tmp_path / "train.jsonl"

    with pytest.raises(ValueError, match=r"REFUSE.*train pairs collide with eval queries"):
        ptd.run(
            subset=None,
            seed=42,
            out=out,
            db_path=db,
            labeled_path=labeled,
            eval_files=(eval_file,),
        )
    assert not out.exists()


def test_query_disjoint_normalizes_case_and_whitespace(tmp_path):
    """Case-insensitive + whitespace-stripped match — '  NUVEI ... ' collides."""
    eval_file = tmp_path / "eval.jsonl"
    _write_eval(
        eval_file,
        [
            _eval_row("nuvei payout checksum", [("repo-x", "docs/x.md")]),
        ],
    )

    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(
        labeled,
        [
            _row("  NUVEI Payout Checksum  ", "repo-z", "docs/z.md"),
        ],
    )
    db = tmp_path / "kb.db"
    _build_db(db, [("repo-z", "docs/z.md", "Zeta content")])
    out = tmp_path / "train.jsonl"

    with pytest.raises(ValueError, match="REFUSE"):
        ptd.run(
            subset=None,
            seed=42,
            out=out,
            db_path=db,
            labeled_path=labeled,
            eval_files=(eval_file,),
        )


def test_path_disjoint_rejects_leaking_path(tmp_path):
    """A train pair whose positive path is in eval expected_paths must raise."""
    eval_file = tmp_path / "eval.jsonl"
    # Eval references docs/leaky.md as an expected_path.
    _write_eval(
        eval_file,
        [
            _eval_row("eval query alpha", [("repo-shared", "docs/leaky.md")]),
        ],
    )

    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(
        labeled,
        [
            # Different query, but same path → still leaks via path side.
            _row("brand new training query", "repo-shared", "docs/leaky.md"),
        ],
    )
    db = tmp_path / "kb.db"
    _build_db(db, [("repo-shared", "docs/leaky.md", "Leaky content")])
    out = tmp_path / "train.jsonl"

    with pytest.raises(ValueError, match=r"REFUSE.*train pairs collide with eval expected_paths"):
        ptd.run(
            subset=None,
            seed=42,
            out=out,
            db_path=db,
            labeled_path=labeled,
            eval_files=(eval_file,),
        )
    assert not out.exists()


def test_eval_files_union_across_v3_and_v3_n150(tmp_path):
    """Disjoint check must union queries across BOTH eval files."""
    eval_v3 = tmp_path / "eval_v3.jsonl"
    eval_v3_n150 = tmp_path / "eval_v3_n150.jsonl"
    _write_eval(eval_v3, [_eval_row("query in v3 only", [("repo-x", "x.md")])])
    _write_eval(
        eval_v3_n150,
        [_eval_row("query in n150 only", [("repo-y", "y.md")])],
    )

    labeled = tmp_path / "labeled.jsonl"
    _write_labeled(
        labeled,
        [
            # Collides with the n150-only entry — proves the union worked.
            _row("query in n150 only", "repo-z", "z.md"),
        ],
    )
    db = tmp_path / "kb.db"
    _build_db(db, [("repo-z", "z.md", "content")])
    out = tmp_path / "train.jsonl"

    with pytest.raises(ValueError, match=r"REFUSE.*eval queries"):
        ptd.run(
            subset=None,
            seed=42,
            out=out,
            db_path=db,
            labeled_path=labeled,
            eval_files=(eval_v3, eval_v3_n150),
        )


def test_load_eval_queries_lowercases_and_strips(tmp_path):
    """Direct unit test of normalization."""
    eval_file = tmp_path / "eval.jsonl"
    _write_eval(
        eval_file,
        [
            _eval_row("  MixedCase Query  ", []),
            _eval_row("\tTabbed Query\n", []),
        ],
    )
    qs = ptd.load_eval_queries((eval_file,))
    assert "mixedcase query" in qs
    assert "tabbed query" in qs


def test_load_eval_paths_collects_all_expected_paths(tmp_path):
    """Direct unit test of path loader — every expected_path entry counts."""
    eval_file = tmp_path / "eval.jsonl"
    _write_eval(
        eval_file,
        [
            _eval_row("q1", [("r1", "a.md"), ("r1", "b.md")]),
            _eval_row("q2", [("r2", "c.md")]),
        ],
    )
    ps = ptd.load_eval_paths((eval_file,))
    assert ps == {("r1", "a.md"), ("r1", "b.md"), ("r2", "c.md")}


def test_assert_helpers_pass_with_empty_pairs():
    """Empty pair list must never trip either assert."""
    ptd._assert_query_disjoint_from_eval([], {"any"})
    ptd._assert_path_disjoint_from_eval([], {("r", "f")})
