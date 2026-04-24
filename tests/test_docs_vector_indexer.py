"""Tests for the docs-tower vector indexer.

SentenceTransformer + LanceDB are mocked so the suite stays fast and
deterministic (no model downloads, no real vector writes).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.index.builders.docs_vector_indexer import (
    DOC_FILE_TYPES,
    build_docs_vectors,
    fetch_doc_chunks,
)

def _mixed_db(tmp_path: Path) -> Path:
    """Create a knowledge.db with mixed file_types so we can exercise the filter."""
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE chunks (
            content TEXT NOT NULL,
            repo_name TEXT,
            file_path TEXT,
            file_type TEXT,
            chunk_type TEXT,
            language TEXT
        )
        """
    )
    doc_rows = [
        ("nuvei refund handling notes", "nuvei", "docs/refunds.md", "docs", "markdown"),
        ("gotcha: expiration behaviour", "trustly", "gotcha.md", "gotchas", "note"),
        ("flow annotation for sdk", "payper", "flow.md", "flow_annotation", "step"),
        ("provider_doc body text", "nuvei", "docs/provider.md", "provider_doc", "section"),
        ("reference table row", "core", "ref.md", "reference", "table"),
        ("task file summary", "core", "tasks/t1.md", "task", "summary"),
        ("dictionary entry", "core", "dict.md", "dictionary", "entry"),
        ("domain registry content", "core", "registry.md", "domain_registry", "entry"),
        ("doc legacy key", "core", "legacy.md", "doc", "markdown"),
    ]
    code_rows = [
        ("def refund(): pass", "nuvei", "src/refund.py", "service", "code_file"),
        ("frontend button", "web", "app/button.tsx", "frontend", "code_file"),
        ("workflow yaml", "core", "wf.yaml", "workflow", "yaml"),
    ]
    for content, repo, fpath, ftype, ctype in doc_rows + code_rows:
        conn.execute(
            "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, repo, fpath, ftype, ctype, None),
        )
    conn.commit()
    conn.close()
    return db_path

class _FakeModel:
    """Replacement for SentenceTransformer that records encode calls."""

    def __init__(self, dim: int = 768, fail_after: int | None = None):
        self.dim = dim
        self.calls: list[list[str]] = []
        self._encoded = 0
        self._fail_after = fail_after
        self.encode = MagicMock(side_effect=self._encode_impl)

    def _encode_impl(self, texts, batch_size=None, show_progress_bar=False):
        self.calls.append(list(texts))
        self._encoded += len(texts)
        if self._fail_after is not None and self._encoded > self._fail_after:
            raise RuntimeError("simulated encoder crash")
        return [[0.01 * (i + 1)] * self.dim for i in range(len(texts))]

class _FakeTable:
    def __init__(self, initial: list[dict] | None = None):
        self.rows: list[dict] = list(initial or [])
        self.create_index = MagicMock()
        self.delete = MagicMock(side_effect=self._delete)
        self.add = MagicMock(side_effect=self.rows.extend)
        self.optimize = MagicMock()

    def count_rows(self) -> int:
        return len(self.rows)

    def _delete(self, _filter: str) -> None:
        self.rows.clear()

class _FakeLanceDB:
    def __init__(self):
        self.table: _FakeTable | None = None
        self.drop_table = MagicMock(side_effect=self._drop)
        self.create_table = MagicMock(side_effect=self._create_table)
        self.open_table = MagicMock(side_effect=self._open_table)

    def _create_table(self, name: str, data: list[dict]):
        assert name == "chunks"
        self.table = _FakeTable(data)
        return self.table

    def _open_table(self, name: str):
        assert name == "chunks"
        if self.table is None:
            raise RuntimeError("no table yet")
        return self.table

    def _drop(self, name: str):
        if name == "chunks":
            self.table = None

def _patch_model_and_lance(fake_model: _FakeModel, fake_lance: _FakeLanceDB):
    """Context-manager style helper to patch both deps at once."""
    fake_st = MagicMock(return_value=fake_model)
    fake_torch = MagicMock()
    fake_torch.backends.mps.is_available.return_value = False
    fake_torch.cuda.is_available.return_value = False
    fake_lancedb = MagicMock()
    fake_lancedb.connect = MagicMock(return_value=fake_lance)

    patches = [
        patch.dict(
            "sys.modules",
            {
                "sentence_transformers": MagicMock(SentenceTransformer=fake_st),
                "torch": fake_torch,
                "lancedb": fake_lancedb,
            },
        )
    ]
    return patches, fake_st, fake_lancedb

# ------------------------------- DOC_FILE_TYPES --------------------------------

class TestDocFileTypesContract:
    def test_every_expected_type_is_present(self):
        expected = {
            "doc",
            "docs",
            "gotchas",
            "reference",
            "provider_doc",
            "task",
            "flow_annotation",
            "dictionary",
            "domain_registry",
        }
        assert expected <= set(DOC_FILE_TYPES)

    def test_code_types_are_excluded(self):
        forbidden = {"service", "frontend", "workflow", "provider_config", "test_script", "code_file"}
        assert forbidden & set(DOC_FILE_TYPES) == set()

# ----------------------------- SQL filter -------------------------------------

class TestFetchDocChunks:
    def test_returns_only_doc_rows(self, tmp_path):
        db_path = _mixed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            rows = fetch_doc_chunks(conn)
        finally:
            conn.close()
        assert len(rows) == 9  # matches the 9 doc-flavoured rows in _mixed_db
        file_types = {r[4] for r in rows}
        assert file_types <= set(DOC_FILE_TYPES)
        assert "service" not in file_types and "frontend" not in file_types

    def test_only_repos_filter(self, tmp_path):
        db_path = _mixed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            rows = fetch_doc_chunks(conn, only_repos={"nuvei"})
        finally:
            conn.close()
        assert len(rows) == 2
        assert {r[2] for r in rows} == {"nuvei"}

    def test_missing_chunks_table_raises_runtimeerror(self, tmp_path):
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        try:
            with pytest.raises(RuntimeError, match="no 'chunks' table"):
                fetch_doc_chunks(conn)
        finally:
            conn.close()

# ----------------------------- Full build flow --------------------------------

class TestBuildDocsVectors:
    def test_force_creates_lancedb_table_with_expected_schema(self, tmp_path, monkeypatch):
        db_path = _mixed_db(tmp_path)
        lance_dir = tmp_path / "db" / "vectors.lance.docs"

        fake_model = _FakeModel(dim=768)
        fake_lance = _FakeLanceDB()
        patches, _, fake_lancedb = _patch_model_and_lance(fake_model, fake_lance)

        with patches[0]:
            result = build_docs_vectors(db_path, lance_dir, force=True, log_every=10)

        assert result["chunks_embedded"] == 9
        assert result["vectors_stored"] == 9
        assert result["lance_path"] == str(lance_dir)
        fake_lancedb.connect.assert_called_with(str(lance_dir))
        assert fake_lance.table is not None
        expected_keys = {
            "rowid",
            "vector",
            "repo_name",
            "file_path",
            "file_type",
            "chunk_type",
            "content_preview",
        }
        assert expected_keys <= set(fake_lance.table.rows[0].keys())
        assert len(fake_lance.table.rows[0]["vector"]) == 768

    def test_document_prefix_applied_to_every_text(self, tmp_path):
        db_path = _mixed_db(tmp_path)
        lance_dir = tmp_path / "db" / "vectors.lance.docs"

        fake_model = _FakeModel(dim=768)
        fake_lance = _FakeLanceDB()
        patches, _, _ = _patch_model_and_lance(fake_model, fake_lance)

        with patches[0]:
            build_docs_vectors(db_path, lance_dir, force=True, log_every=100)

        # Flatten every text that was sent to encode() across every batch.
        sent_texts = [t for batch in fake_model.calls for t in batch]
        assert len(sent_texts) == 9
        for t in sent_texts:
            assert t.startswith("search_document: "), f"missing doc prefix: {t!r}"
        # Spot-check a known row content is embedded in the passed text.
        assert any("nuvei refund handling notes" in t for t in sent_texts)
        # Positional sanity — recorded via MagicMock.call_args_list.
        assert fake_model.encode.call_count >= 1
        first_call_args, first_call_kwargs = fake_model.encode.call_args_list[0]
        batch_texts = first_call_args[0]
        assert all(s.startswith("search_document: ") for s in batch_texts)
        assert first_call_kwargs.get("show_progress_bar") is False

# ------------------------------ Checkpoint resume -----------------------------

class TestCheckpointResume:
    def test_resumes_from_saved_state_after_crash(self, tmp_path):
        """Streaming-mode resume: checkpoint records done rowids, LanceDB holds
        the actual vectors. New run with force=False skips done rowids and
        appends the rest to the existing LanceDB table.
        """
        db_path = _mixed_db(tmp_path)
        lance_dir = tmp_path / "db" / "vectors.lance.docs"
        checkpoint = tmp_path / "docs_checkpoint.json"

        # Simulate prior crashed run state:
        #   - checkpoint says rowids 1-4 are done (streaming format — rowids only)
        #   - LanceDB already holds stub vectors for those rowids (the streaming
        #     writer landed them before the crash)
        checkpoint.write_text(json.dumps({"done_rowids": [1, 2, 3, 4]}))
        pre_populated_rows = [
            {
                "rowid": rid,
                "vector": [0.0] * 768,
                "repo_name": "stub",
                "file_path": "stub.md",
                "file_type": "docs",
                "chunk_type": "markdown",
                "content_preview": "stub",
            }
            for rid in (1, 2, 3, 4)
        ]

        resume_model = _FakeModel(dim=768)
        fake_lance = _FakeLanceDB()
        fake_lance.table = _FakeTable(pre_populated_rows)
        patches, _, _ = _patch_model_and_lance(resume_model, fake_lance)

        with patches[0]:
            result = build_docs_vectors(
                db_path,
                lance_dir,
                force=False,  # keep checkpoint + LanceDB; resume from delta
                checkpoint_path=checkpoint,
                log_every=100,
            )

        sent_texts = "\n".join(t for batch in resume_model.calls for t in batch)
        # First 4 rows must NOT be re-embedded (checkpoint marked them done).
        assert "nuvei refund handling notes" not in sent_texts  # rowid 1
        assert "gotcha: expiration behaviour" not in sent_texts  # rowid 2
        # Remaining rows WERE embedded on the resume run.
        assert "dictionary entry" in sent_texts  # rowid 7
        # Streaming semantics: chunks_embedded counts THIS run only.
        assert result["chunks_embedded"] == 5
        # LanceDB total = 4 prepopulated + 5 newly streamed.
        assert result["vectors_stored"] == 9
        # Checkpoint stays (force=False). The next run would skip everything
        # because all 9 rowids are recorded as done.
        assert checkpoint.exists()
        assert set(json.loads(checkpoint.read_text())["done_rowids"]) == {1, 2, 3, 4, 5, 6, 7, 8, 9}

    def test_force_clears_stale_checkpoint(self, tmp_path):
        """`--force` must wipe the checkpoint at start so the loop doesn't skip
        rowids whose embeddings only existed in the about-to-be-dropped table.
        """
        db_path = _mixed_db(tmp_path)
        lance_dir = tmp_path / "db" / "vectors.lance.docs"
        checkpoint = tmp_path / "docs_checkpoint.json"
        checkpoint.write_text(json.dumps({"done_rowids": [1, 2, 3, 4]}))

        fake_model = _FakeModel(dim=768)
        fake_lance = _FakeLanceDB()
        patches, _, _ = _patch_model_and_lance(fake_model, fake_lance)

        with patches[0]:
            result = build_docs_vectors(
                db_path,
                lance_dir,
                force=True,
                checkpoint_path=checkpoint,
                log_every=100,
            )

        # All 9 rows re-embedded — the stale checkpoint did not cause skips.
        assert result["chunks_embedded"] == 9
        assert result["vectors_stored"] == 9
        # Force build cleared checkpoint at the end.
        assert not checkpoint.exists()