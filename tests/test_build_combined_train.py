"""Tests for scripts/build_combined_train.py.

Covers:
- code-source loading (per-query candidate lists with binary labels) and
  expansion into one row per (query, doc).
- docs-source loading (anchor/positive/negative triplets) and expansion to
  two rows per triplet.
- balance_labels: pos:neg ratio stays within ±tolerance after combination.
- build_combined: full happy path on synthetic fixtures, and the contract
  asserted by the spec (≥10000 rows, ±10% balance, both sources present)
  on the *real* sources (when present) so production data drift is caught.
- bad-row error paths (missing keys, length mismatch, non-binary labels).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build import build_combined_train as bct

# ----- fixtures --------------------------------------------------------------


@pytest.fixture
def code_src(tmp_path: Path) -> Path:
    """Synthetic v8-shaped code-source file: 5 queries x 4 docs each."""
    rows = []
    for q in range(5):
        rows.append(
            {
                "query": f"code query {q}",
                "docs": [f"doc {q}-{i}" for i in range(4)],
                # 1 positive + 3 negatives per row
                "labels": [1.0, 0.0, 0.0, 0.0],
            }
        )
    path = tmp_path / "code.jsonl"
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


@pytest.fixture
def docs_src(tmp_path: Path) -> Path:
    """Synthetic r1-shaped docs-source file: 7 triplets."""
    path = tmp_path / "docs.jsonl"
    with path.open("w") as f:
        for i in range(7):
            f.write(
                json.dumps(
                    {
                        "anchor": f"docs query {i}",
                        "positive": f"pos doc {i}",
                        "negative": f"neg doc {i}",
                    }
                )
                + "\n"
            )
    return path


# ----- per-loader tests ------------------------------------------------------


def test_load_code_pairs_expands_one_row_per_doc(code_src: Path):
    rows = bct.load_code_pairs(code_src)
    # 5 queries x 4 docs = 20 expanded rows
    assert len(rows) == 20
    assert all(r["source"] == "code" for r in rows)
    assert all(r["label"] in (0, 1) for r in rows)
    # 5 positives (one per query) + 15 negatives
    assert sum(r["label"] for r in rows) == 5


def test_load_code_pairs_rejects_missing_keys(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"query": "q", "docs": ["d"]}) + "\n")
    with pytest.raises(ValueError, match="missing keys"):
        bct.load_code_pairs(bad)


def test_load_code_pairs_rejects_length_mismatch(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"query": "q", "docs": ["d1", "d2"], "labels": [1.0]}) + "\n")
    with pytest.raises(ValueError, match="length mismatch"):
        bct.load_code_pairs(bad)


def test_load_code_pairs_rejects_non_binary_label(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"query": "q", "docs": ["d"], "labels": [2.0]}) + "\n")
    with pytest.raises(ValueError, match="not 0/1"):
        bct.load_code_pairs(bad)


def test_load_docs_triplets_expands_two_rows_per_triplet(docs_src: Path):
    rows = bct.load_docs_triplets(docs_src)
    # 7 triplets x 2 rows each = 14 rows
    assert len(rows) == 14
    assert all(r["source"] == "docs" for r in rows)
    # Exactly 7 positives + 7 negatives
    n_pos = sum(1 for r in rows if r["label"] == 1)
    n_neg = sum(1 for r in rows if r["label"] == 0)
    assert n_pos == 7
    assert n_neg == 7


def test_load_docs_triplets_rejects_missing_keys(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    # Missing both `anchor` and `query`, only positive provided.
    bad.write_text(json.dumps({"positive": "p", "negative": "n"}) + "\n")
    with pytest.raises(ValueError, match="missing keys"):
        bct.load_docs_triplets(bad)


def test_load_docs_triplets_accepts_query_alias(tmp_path: Path):
    """The R1 v3 triplet builder writes `query` instead of `anchor`; the
    loader must accept both so a real /tmp/r1_cosent_triplets_v3.jsonl
    parses cleanly without manual key-renaming."""
    path = tmp_path / "queryform.jsonl"
    with path.open("w") as f:
        f.write(
            json.dumps(
                {
                    "query": "my anchor question",
                    "positive": "good doc",
                    "negative": "bad doc",
                }
            )
            + "\n"
        )
    rows = bct.load_docs_triplets(path)
    assert len(rows) == 2
    assert rows[0]["query"] == "my anchor question"
    assert rows[0]["label"] == 1
    assert rows[1]["label"] == 0


# ----- balance_labels -------------------------------------------------------


def test_balance_labels_no_op_when_already_balanced():
    rows = [
        {"query": "q", "doc": "d", "label": 1, "source": "x"},
        {"query": "q", "doc": "d", "label": 0, "source": "x"},
    ]
    out = bct.balance_labels(rows)
    # Already 1:1 — nothing to drop.
    assert len(out) == 2


def test_balance_labels_downsamples_majority_negatives():
    # 10 positives, 100 negatives → 10:1 imbalance, way outside ±10%.
    rows = [{"query": "q", "doc": "d", "label": 1, "source": "x"} for _ in range(10)]
    rows += [{"query": "q", "doc": "d", "label": 0, "source": "x"} for _ in range(100)]
    out = bct.balance_labels(rows, seed=1, tolerance=0.10)
    n_pos = sum(1 for r in out if r["label"] == 1)
    n_neg = sum(1 for r in out if r["label"] == 0)
    # We never up-sample, so positives stay at 10. Negatives capped at
    # 10 * (1 + 0.10) = 11.
    assert n_pos == 10
    assert n_neg <= 11
    # Within ±10% means larger/smaller - 1 <= 0.10
    assert max(n_pos, n_neg) / min(n_pos, n_neg) - 1 <= 0.10 + 1e-9


def test_balance_labels_downsamples_majority_positives():
    rows = [{"query": "q", "doc": "d", "label": 1, "source": "x"} for _ in range(50)]
    rows += [{"query": "q", "doc": "d", "label": 0, "source": "x"} for _ in range(10)]
    out = bct.balance_labels(rows, seed=42, tolerance=0.10)
    n_pos = sum(1 for r in out if r["label"] == 1)
    n_neg = sum(1 for r in out if r["label"] == 0)
    assert n_neg == 10
    assert n_pos <= 11
    assert max(n_pos, n_neg) / min(n_pos, n_neg) - 1 <= 0.10 + 1e-9


# ----- build_combined: synthetic happy path ---------------------------------


def test_build_combined_happy_path(code_src: Path, docs_src: Path, tmp_path: Path):
    out = tmp_path / "combined" / "train.jsonl"
    stats = bct.build_combined(
        code_src=code_src,
        docs_src=docs_src,
        out_path=out,
        seed=42,
    )
    assert out.exists()
    # Read back the file, decode each row, sanity-check schema.
    written = []
    with out.open() as f:
        for line in f:
            line = line.strip()
            if line:
                written.append(json.loads(line))
    assert len(written) == stats["total_rows"]
    for r in written:
        assert set(r.keys()) == {"query", "doc", "label", "source"}
        assert r["label"] in (0, 1)
        assert r["source"] in ("code", "docs")

    # Both sources must be represented in the final file.
    assert stats["code_rows"] > 0
    assert stats["docs_rows"] > 0

    # Pos:neg ratio within ±10%.
    n_pos = stats["positives"]
    n_neg = stats["negatives"]
    assert n_pos > 0 and n_neg > 0
    assert max(n_pos, n_neg) / min(n_pos, n_neg) - 1 <= 0.10 + 1e-9


def test_build_combined_missing_code_src(tmp_path: Path, docs_src: Path):
    with pytest.raises(FileNotFoundError, match="code-src not found"):
        bct.build_combined(
            code_src=tmp_path / "missing.jsonl",
            docs_src=docs_src,
            out_path=tmp_path / "out.jsonl",
        )


def test_build_combined_missing_docs_src(tmp_path: Path, code_src: Path):
    with pytest.raises(FileNotFoundError, match="docs-src not found"):
        bct.build_combined(
            code_src=code_src,
            docs_src=tmp_path / "missing.jsonl",
            out_path=tmp_path / "out.jsonl",
        )


# ----- production-data contract (skipped if real sources absent) ------------


def _real_sources_available() -> bool:
    return bct.DEFAULT_CODE_SRC.is_file() and bct.DEFAULT_DOCS_SRC.is_file()


@pytest.mark.skipif(
    not _real_sources_available(),
    reason="real combined-train sources not present in this environment",
)
def test_build_combined_real_sources_meet_spec(tmp_path: Path):
    """When both real sources exist, the produced file must satisfy the spec.

    This is the contract documented in the task brief:
      - file exists
      - valid JSONL
      - >= 10000 rows
      - balanced labels within +-10%
      - both 'code' and 'docs' sources present
    """
    out = tmp_path / "combined.jsonl"
    stats = bct.build_combined(
        code_src=bct.DEFAULT_CODE_SRC,
        docs_src=bct.DEFAULT_DOCS_SRC,
        out_path=out,
        seed=42,
    )
    assert out.exists()
    n_lines = 0
    with out.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            row = json.loads(line)
            assert set(row.keys()) >= {"query", "doc", "label", "source"}
    assert n_lines == stats["total_rows"]
    assert stats["total_rows"] >= 10000, f"combined train must have >=10000 rows, got {stats['total_rows']}"
    n_pos = stats["positives"]
    n_neg = stats["negatives"]
    ratio_drift = max(n_pos, n_neg) / min(n_pos, n_neg) - 1
    assert ratio_drift <= 0.10 + 1e-9, f"pos:neg ratio drift {ratio_drift:.3f} exceeds +-10%"
    assert stats["code_rows"] > 0
    assert stats["docs_rows"] > 0


# ----- CLI smoke -------------------------------------------------------------


def test_cli_main_writes_to_out(code_src: Path, docs_src: Path, tmp_path: Path, capsys):
    out = tmp_path / "out.jsonl"
    rc = bct.main(
        [
            f"--code-src={code_src}",
            f"--docs-src={docs_src}",
            f"--out={out}",
            "--seed=7",
        ]
    )
    assert rc == 0
    assert out.exists()
    captured = capsys.readouterr()
    # Stats JSON dumped to stdout — use it as the contract.
    stats = json.loads(captured.out)
    assert stats["total_rows"] > 0
    assert stats["code_rows"] > 0
    assert stats["docs_rows"] > 0
