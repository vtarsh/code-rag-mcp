"""Regression tests for the indexing pipeline at src/index/builders/.

Covers two bugs discovered 2026-04-22:

Bug A (P0) — code_facts_fts rowid drift in repo_indexer.py
    The prior implementation inserted into code_facts, then did
    `INSERT INTO code_facts_fts(rowid, ...) VALUES (last_insert_rowid(), ...)`.
    Because the same connection also wrote into the `chunks` FTS5 virtual table
    in the enclosing loop, the connection-level `last_insert_rowid()` could
    resolve to a rowid in a different table (FTS5 triggers mutate it too).
    Fix: capture `cursor.lastrowid` from the direct code_facts INSERT and bind
    that captured value explicitly as the code_facts_fts rowid.

Bug B (P1) — FTS5 optimize misuse in orchestrator.py
    Old form:
        INSERT INTO chunks(chunks, rank, content, repo_name, file_path, file_type,
                           chunk_type, language)
        VALUES('optimize', '', '', '', '', '', '', '')
    FTS5 only recognizes the 2-column form
        INSERT INTO chunks(chunks) VALUES('optimize')
    as an optimize command; anything else becomes a plain row insert (content=''
    garbage row every build). Fix: use the 2-column form.

Tests use ephemeral SQLite DBs in tmp_path, never the production knowledge.db.
"""

from __future__ import annotations

import sqlite3

from src.index.builders.db import create_db
from src.index.builders.orchestrator import build_index
from src.index.builders.repo_indexer import index_repo


def _fresh_db(tmp_path) -> sqlite3.Connection:
    """Create an empty knowledge.db-style schema in tmp_path."""
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    create_db(conn)
    return conn


class TestFtsOptimize:
    """Bug B: the FTS5 'optimize' pseudo-insert must not leave a garbage row."""

    def test_fts_optimize_does_not_create_garbage_row(self, tmp_path, monkeypatch):
        """Run the builder's optimize step on an ephemeral DB and confirm no
        row with empty content is created.
        """
        code_rag_home = tmp_path / "code-rag"
        (code_rag_home / "db").mkdir(parents=True)
        (code_rag_home / "extracted").mkdir(parents=True)
        (code_rag_home / "profiles" / "example").mkdir(parents=True)

        (code_rag_home / "extracted" / "_index.json").write_text(
            '{"empty-repo": {"type": "service", "sha": "abc", "org_deps": [], "artifacts": {}}}'
        )

        import src.index.builders._common as common_mod
        import src.index.builders.db as db_mod
        import src.index.builders.docs_indexer as docs_mod
        import src.index.builders.incremental as incr_mod
        import src.index.builders.orchestrator as orch_mod
        import src.index.builders.raw_indexer as raw_mod
        import src.index.builders.repo_indexer as repo_mod

        db_dir = code_rag_home / "db"
        db_path = db_dir / "knowledge.db"
        extracted_dir = code_rag_home / "extracted"
        index_file = extracted_dir / "_index.json"

        for mod in (common_mod, db_mod, docs_mod, incr_mod, orch_mod, raw_mod, repo_mod):
            if hasattr(mod, "_BASE_DIR"):
                monkeypatch.setattr(mod, "_BASE_DIR", code_rag_home, raising=False)
            if hasattr(mod, "DB_DIR"):
                monkeypatch.setattr(mod, "DB_DIR", db_dir, raising=False)
            if hasattr(mod, "DB_PATH"):
                monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
            if hasattr(mod, "EXTRACTED_DIR"):
                monkeypatch.setattr(mod, "EXTRACTED_DIR", extracted_dir, raising=False)
            if hasattr(mod, "INDEX_FILE"):
                monkeypatch.setattr(mod, "INDEX_FILE", index_file, raising=False)

        for attr in (
            "index_gotchas", "index_domain_registry", "index_flows",
            "index_tasks", "index_references", "index_dictionary", "index_providers",
        ):
            monkeypatch.setattr(orch_mod, attr, lambda *_a, **_kw: (0, 0), raising=True)
        for attr in ("index_seeds", "index_test_scripts"):
            monkeypatch.setattr(orch_mod, attr, lambda *_a, **_kw: (0, 0), raising=True)

        monkeypatch.setattr("sys.argv", ["build_index"])

        build_index()

        conn = sqlite3.connect(str(db_path))
        try:
            (garbage_count,) = conn.execute("SELECT count(*) FROM chunks WHERE content=''").fetchone()
            assert garbage_count == 0, f"optimize inserted {garbage_count} garbage row(s) with content=''"

            (optimize_as_content,) = conn.execute("SELECT count(*) FROM chunks WHERE content='optimize'").fetchone()
            assert optimize_as_content == 0, "literal 'optimize' string ended up as a chunk row"
        finally:
            conn.close()

    def test_fts_optimize_command_form_is_safe(self, tmp_path):
        """Unit-level check: the 2-column form runs cleanly on an empty FTS5 table."""
        conn = _fresh_db(tmp_path)
        try:
            (before,) = conn.execute("SELECT count(*) FROM chunks").fetchone()
            conn.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
            conn.commit()
            (after,) = conn.execute("SELECT count(*) FROM chunks").fetchone()
            assert after == before, "optimize pseudo-insert changed row count"

            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("real body", "repo-a", "src/x.js", "service", "code_file", "javascript"),
            )
            conn.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
            conn.commit()
            (final,) = conn.execute("SELECT count(*) FROM chunks").fetchone()
            assert final == 1, f"expected 1 row after optimize, got {final}"
            (empty_content,) = conn.execute("SELECT count(*) FROM chunks WHERE content=''").fetchone()
            assert empty_content == 0
        finally:
            conn.close()


class TestCodeFactsFtsRowidCoherent:
    """Bug A: every code_facts_fts rowid must map to a code_facts.id."""

    def test_code_facts_fts_rowid_coherent(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path)

        extracted = tmp_path / "extracted"
        repo_dir = extracted / "trustly-service" / "libs"
        repo_dir.mkdir(parents=True)

        js_source = (
            "const MAX_RETRIES = 5;\n"
            "const API_URL = process.env.API_URL || 'https://api.example.com';\n"
            "function validate(req) {\n"
            "  if (req.status !== 'SUCCESS') {\n"
            "    throw new Error('invalid status');\n"
            "  }\n"
            "  return true;\n"
            "}\n"
        )
        (repo_dir / "validate.js").write_text(js_source)

        import src.index.builders.repo_indexer as repo_mod

        monkeypatch.setattr(repo_mod, "EXTRACTED_DIR", extracted, raising=False)

        meta = {"type": "service", "sha": "deadbeef", "org_deps": [], "artifacts": {}}
        index_repo(conn, "trustly-service", meta)

        (facts_count,) = conn.execute("SELECT count(*) FROM code_facts").fetchone()
        assert facts_count >= 3, f"expected >=3 facts extracted, got {facts_count}"

        (fts_count,) = conn.execute("SELECT count(*) FROM code_facts_fts").fetchone()
        assert fts_count == facts_count, (
            f"code_facts_fts row count {fts_count} != code_facts row count {facts_count}"
        )

        orphan_rows = conn.execute(
            "SELECT fts.rowid FROM code_facts_fts fts LEFT JOIN code_facts cf ON cf.id = fts.rowid WHERE cf.id IS NULL"
        ).fetchall()
        assert orphan_rows == [], f"orphan FTS rowids: {orphan_rows}"

        mismatches = conn.execute(
            "SELECT cf.id, cf.condition, fts.condition FROM code_facts cf "
            "JOIN code_facts_fts fts ON fts.rowid = cf.id "
            "WHERE cf.condition IS NOT fts.condition"
        ).fetchall()
        assert mismatches == [], f"binding mismatches: {mismatches}"

        conn.close()

    def test_code_facts_fts_rowid_coherent_direct_inserts(self, tmp_path):
        """Lower-level contract: N code_facts inserts via the fixed pattern."""
        conn = _fresh_db(tmp_path)
        fact_rows = [
            ("repo-a", "src/a.js", "fn_a", "validation_guard", "x !== y", "err a", 10, "snippet a"),
            ("repo-a", "src/b.js", "fn_b", "const_value", "MAX", "5", 1, "snippet b"),
            ("repo-a", "src/c.js", "fn_c", "env_var", "API_URL", "https://x", 2, "snippet c"),
        ]
        captured_ids: list[int] = []
        try:
            for (repo_name, file_path, function_name, fact_type, condition, message, line_number, raw_snippet) in fact_rows:
                cur = conn.execute(
                    "INSERT INTO code_facts(repo_name, file_path, function_name, fact_type, "
                    "condition, message, line_number, raw_snippet) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo_name, file_path, function_name, fact_type, condition, message, line_number, raw_snippet),
                )
                code_fact_id = cur.lastrowid
                captured_ids.append(code_fact_id)

                conn.execute(
                    "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("noise content", repo_name, file_path, "service", "code_file", "javascript"),
                )

                conn.execute(
                    "INSERT INTO code_facts_fts(rowid, repo_name, file_path, function_name, "
                    "fact_type, condition, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (code_fact_id, repo_name, file_path, function_name, fact_type, condition, message),
                )

            fts_rowids = [r[0] for r in conn.execute("SELECT rowid FROM code_facts_fts").fetchall()]
            assert sorted(fts_rowids) == sorted(captured_ids)

            joined = conn.execute(
                "SELECT cf.id, cf.condition, fts.condition FROM code_facts cf "
                "JOIN code_facts_fts fts ON fts.rowid = cf.id ORDER BY cf.id"
            ).fetchall()
            assert len(joined) == len(fact_rows)
            for cf_id, cf_cond, fts_cond in joined:
                assert cf_cond == fts_cond, f"id={cf_id}: {cf_cond!r} vs {fts_cond!r}"
        finally:
            conn.close()


def test_fts_optimize_canonical_form_only_touches_command_column(tmp_path):
    """Pins the canonical 2-column FTS5 optimize form as the invariant."""
    from pathlib import Path as _Path

    src = _Path(__file__).resolve().parent.parent / "src" / "index" / "builders" / "orchestrator.py"
    text = src.read_text()
    assert "INSERT INTO chunks(chunks) VALUES('optimize')" in text, (
        "orchestrator.py must use the canonical 2-column FTS5 optimize form"
    )
    assert "VALUES('optimize', ''" not in text, (
        "orchestrator.py still contains the extra-column 'optimize' insert"
    )
