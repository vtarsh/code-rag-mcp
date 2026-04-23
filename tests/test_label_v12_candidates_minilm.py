"""Tests for scripts/label_v12_candidates_minilm.py (local MiniLM v12 labeler)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

@pytest.fixture
def labeler_module():
    """Import the script as a module for direct unit testing."""
    spec = importlib.util.spec_from_file_location(
        "label_v12_candidates_minilm",
        REPO_ROOT / "scripts" / "label_v12_candidates_minilm.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_label_from_score_positive(labeler_module):
    assert labeler_module.label_from_score(0.5) == "+"

def test_label_from_score_boundary_pos(labeler_module):
    assert labeler_module.label_from_score(0.45) == "+"

def test_label_from_score_negative(labeler_module):
    assert labeler_module.label_from_score(0.1) == "-"

def test_label_from_score_boundary_neg(labeler_module):
    assert labeler_module.label_from_score(0.149) == "-"
    assert labeler_module.label_from_score(0.15) == "?"

def test_label_from_score_ambiguous(labeler_module):
    assert labeler_module.label_from_score(0.3) == "?"

def test_load_chunk_text_from_extracted(labeler_module, tmp_path: Path):
    repo_root = tmp_path
    p = repo_root / "extracted" / "my-repo" / "a/b.md"
    p.parent.mkdir(parents=True)
    p.write_text("hello from extracted")
    text, note = labeler_module.load_chunk_text(
        "my-repo", "a/b.md", repo_root=repo_root, db_cur=None
    )
    assert text == "hello from extracted"
    assert note is None

def test_load_chunk_text_from_raw_fallback(labeler_module, tmp_path: Path):
    repo_root = tmp_path
    p = repo_root / "raw" / "my-repo" / "a/b.md"
    p.parent.mkdir(parents=True)
    p.write_text("hello from raw")
    text, note = labeler_module.load_chunk_text(
        "my-repo", "a/b.md", repo_root=repo_root, db_cur=None
    )
    assert text == "hello from raw"
    assert note is None

def test_load_chunk_text_db_fallback(labeler_module, tmp_path: Path):
    repo_root = tmp_path
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute(
        "CREATE TABLE chunks (content TEXT, repo_name TEXT, file_path TEXT, "
        "file_type TEXT, chunk_type TEXT, language TEXT)"
    )
    cur.execute(
        "INSERT INTO chunks VALUES (?,?,?,?,?,?)",
        ("hello from db", "scraped-docs", "p/x.md", "doc", "doc_section", "md"),
    )
    text, note = labeler_module.load_chunk_text(
        "scraped-docs", "p/x.md", repo_root=repo_root, db_cur=cur
    )
    assert text == "hello from db"
    assert note == "db fallback"
    db.close()

def test_load_chunk_text_file_not_found(labeler_module, tmp_path: Path):
    """No filesystem file + no DB row -> `file not found` note + empty text."""
    repo_root = tmp_path
    text, note = labeler_module.load_chunk_text(
        "missing-repo", "nope/x.md", repo_root=repo_root, db_cur=None
    )
    assert text == ""
    assert note == "file not found"

def test_load_chunk_text_respects_max_chars(labeler_module, tmp_path: Path):
    repo_root = tmp_path
    p = repo_root / "extracted" / "r" / "big.md"
    p.parent.mkdir(parents=True)
    p.write_text("x" * 10_000)
    text, _ = labeler_module.load_chunk_text(
        "r", "big.md", repo_root=repo_root, db_cur=None, max_chars=500
    )
    assert len(text) == 500

# ---- End-to-end: output schema ----------------------------------------------

def _make_input(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

def test_output_schema_preserves_inputs_and_adds_new_fields(
    labeler_module, tmp_path: Path, monkeypatch
):
    """main() should preserve every input field and add label, minilm_score,
    extracted = tmp_path / "extracted" / "r1" / "a.md"
    extracted.parent.mkdir(parents=True)
    extracted.write_text("file-based content for query")

    input_path = tmp_path / "v12_input.jsonl"
    output_path = tmp_path / "v12_labeled.jsonl"
    input_rows = [
        {
            "query": "some query",
            "query_tag": "doc-intent",
            "rank": 1,
            "repo_name": "r1",
            "file_path": "a.md",
            "file_type": "docs",
            "chunk_type": "doc_section",
            "combined_score": 0.9,
            "rerank_score": 0.8,
            "penalty": 0.0,
            "category": "doc",
            "label": "",
            "note": "",
            "regen_source": "test",
        },
        {
            "query": "missing file query",
            "query_tag": "general",
            "rank": 2,
            "repo_name": "nope",
            "file_path": "missing.md",
            "file_type": "docs",
            "chunk_type": "doc_section",
            "combined_score": 0.5,
            "rerank_score": 0.4,
            "penalty": 0.0,
            "category": "doc",
            "label": "",
            "note": "",
            "regen_source": "test",
        },
    ]
    _make_input(input_path, input_rows)

    fake_judge = MagicMock()
    fake_judge.predict.return_value = [0.9, 0.9]

    class FakeCrossEncoder:
        def __init__(self, *a, **kw):
            pass

        def predict(self, *a, **kw):
            return fake_judge.predict(*a, **kw)

    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "CrossEncoder", FakeCrossEncoder)

    argv = [
        "label_v12_candidates_minilm",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--db",
        str(tmp_path / "nonexistent.db"),  # forces no-DB path
        "--repo-root",
        str(tmp_path),
        "--batch-size",
        "2",
    ]
    monkeypatch.setattr("sys.argv", argv)
    rc = labeler_module.main()
    assert rc == 0

    out_rows = [json.loads(line) for line in output_path.read_text().splitlines() if line.strip()]
    assert len(out_rows) == 2

    r0 = out_rows[0]
    assert r0["label"] == "+"
    assert r0["judge"] == "minilm-L6"
    assert "minilm_score" in r0
    for k in input_rows[0]:
        assert k in r0, f"input field {k} missing from output"
    assert r0["rank"] == 1
    assert r0["repo_name"] == "r1"
    assert r0["regen_source"] == "test"

    r1 = out_rows[1]
    assert r1["label"] == "?"
    assert r1["note"] == "file not found"
    assert r1["judge"] == "minilm-L6"
    assert "minilm_score" in r1

def test_main_exits_with_error_when_input_missing(labeler_module, tmp_path: Path, monkeypatch):
    argv = [
        "label_v12_candidates_minilm",
        "--input",
        str(tmp_path / "nope.jsonl"),
    ]
    monkeypatch.setattr("sys.argv", argv)
    rc = labeler_module.main()
    assert rc == 1
