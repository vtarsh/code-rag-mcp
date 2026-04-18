"""High-level build orchestration — equivalent to the original ``main()``.

Behavior is preserved bit-for-bit: same print messages, same SQL, same CLI flags
(``--reset-repo=X``, ``--repos=X``, ``--incremental``).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime

from ._common import DB_DIR, DB_PATH, EXTRACTED_DIR, INDEX_FILE
from .db import create_db, delete_repo_data, reset_repo_all_layers
from .docs_indexer import (
    index_dictionary,
    index_domain_registry,
    index_flows,
    index_gotchas,
    index_providers,
    index_references,
    index_tasks,
)
from .incremental import (
    compute_profile_docs_fingerprint,
    detect_changed_repos,
    load_existing_shas,
)
from .raw_indexer import index_seeds, index_test_scripts
from .repo_indexer import index_repo


def build_index():
    """Run the FTS5 index builder.

    CLI surface (read from ``sys.argv``):
      --reset-repo=NAME    Drop one repo across SQLite + LanceDB and exit.
      --repos=A,B          Re-index only the listed repos (implies incremental).
      --incremental        Re-index only repos whose HEAD SHA changed.
    """
    # Early-exit flags (don't require extracted artifacts)
    reset_target = None
    for arg in sys.argv[1:]:
        if arg.startswith("--reset-repo="):
            reset_target = arg.split("=", 1)[1]

    if reset_target:
        if not DB_PATH.exists():
            print(f"Error: {DB_PATH} does not exist.")
            sys.exit(1)
        conn = sqlite3.connect(str(DB_PATH))
        try:
            stats = reset_repo_all_layers(conn, reset_target)
            conn.commit()
        finally:
            conn.close()
        print(f"Reset '{reset_target}':")
        for layer, n in stats.items():
            print(f"  {layer}: {n} rows deleted")
        return

    if not EXTRACTED_DIR.exists() or not INDEX_FILE.exists():
        print("Error: Run extract_artifacts.py first.")
        sys.exit(1)

    # Parse CLI flags
    only_repos = None
    incremental = False
    for arg in sys.argv[1:]:
        if arg.startswith("--repos="):
            only_repos = set(arg.split("=", 1)[1].split(","))
        elif arg == "--incremental":
            incremental = True

    # --repos implies incremental behavior
    if only_repos:
        incremental = True

    # Load repo metadata
    repo_meta = json.loads(INDEX_FILE.read_text())
    print(f"Found {len(repo_meta)} repos in index")

    DB_DIR.mkdir(parents=True, exist_ok=True)

    if incremental and not DB_PATH.exists():
        print("No existing database found. Running full build instead.")
        incremental = False
        only_repos = None

    # For --incremental without --repos: auto-detect changed repos by SHA comparison
    if incremental and only_repos is None:
        conn_detect = sqlite3.connect(str(DB_PATH))
        existing_shas = load_existing_shas(conn_detect)
        conn_detect.close()

        changed, removed = detect_changed_repos(repo_meta, existing_shas)

        if not changed and not removed:
            # Even when no repos changed, profile docs (providers/tasks/references)
            # may have updated — re-index those cheaply IF fingerprint changed.
            # Without this gate, every quiet night re-indexes ~18k provider_doc
            # chunks → 18k LanceDB orphan/missing cycle → 10+ h embed storm.
            docs_fp = compute_profile_docs_fingerprint()
            conn_docs = sqlite3.connect(str(DB_PATH))
            stored_row = conn_docs.execute("SELECT value FROM build_info WHERE key = 'profile_docs_fp'").fetchone()

            if stored_row and stored_row[0] == docs_fp:
                conn_docs.close()
                print("Profile docs unchanged — skipping re-index (saves ~18k embed churn)")
                print("All repos up to date. Nothing else to re-index.")
                return

            conn_docs.execute("BEGIN")
            pd_files, pd_chunks = index_providers(conn_docs)
            conn_docs.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)",
                ("profile_docs_fp", docs_fp),
            )
            conn_docs.commit()
            conn_docs.close()
            if pd_chunks:
                print(f"Provider docs: {pd_files} files, {pd_chunks} chunks re-indexed")
            print("All repos up to date. Nothing else to re-index.")
            return

        only_repos = changed
        print(
            f"SHA comparison: {len(changed)} changed, {len(removed)} removed, {len(repo_meta) - len(changed)} unchanged"
        )

        # Clean removed repos
        if removed:
            conn_clean = sqlite3.connect(str(DB_PATH))
            conn_clean.execute("BEGIN")
            for repo_name in sorted(removed):
                deleted = delete_repo_data(conn_clean, repo_name)
                if deleted:
                    print(f"  Removed {deleted} chunks for deleted repo {repo_name}")
            conn_clean.commit()
            conn_clean.close()

    # Track whether we're using a temp file (full build) for atomic rename later
    tmp_path = None

    if not incremental:
        # Full build: write to temp file, then atomic rename on success
        tmp_path = DB_PATH.with_suffix(".db.tmp")
        if tmp_path.exists():
            tmp_path.unlink()

        conn = sqlite3.connect(str(tmp_path))
        create_db(conn)

        repos_to_index = sorted(repo_meta.items())
        print(f"Full build: indexing {len(repos_to_index)} repos")
    else:
        conn = sqlite3.connect(str(DB_PATH))
        create_db(conn)  # ensures tables exist

        repos_to_index = [(name, meta) for name, meta in sorted(repo_meta.items()) if name in only_repos]
        print(f"Incremental build: re-indexing {len(repos_to_index)} repos")

    total_chunks = 0
    total_files = 0

    if incremental:
        # Wrap incremental delete+insert in a single transaction
        conn.execute("BEGIN")
        try:
            # Delete old data for changed repos (chunks, chunk_meta, code_facts)
            for repo_name in sorted(only_repos):
                deleted = delete_repo_data(conn, repo_name)
                if deleted:
                    print(f"  Removed {deleted} old chunks for {repo_name}")

            for i, (repo_name, meta) in enumerate(repos_to_index, 1):
                files, chunks = index_repo(conn, repo_name, meta)
                total_files += files
                total_chunks += chunks

                if i % 50 == 0:
                    print(f"  [{i}/{len(repos_to_index)}] {total_chunks} chunks indexed...")

            # --- Profile doc re-index — gated by mtime fingerprint ---
            # Without gate, every incremental run delete+re-inserts ~22k chunks
            # (gotchas/references/dictionary/providers/...). That churns SQLite
            # rowids which are used as LanceDB primary keys → every chunk becomes
            # orphan + missing → embed_missing_vectors re-embeds 22k chunks every
            # night. With coderank on MPS that's 30-90 min; on CPU-only 16 GB
            # Mac it's 10-50 h → hang + OOM.
            docs_fp = compute_profile_docs_fingerprint()
            stored_fp_row = conn.execute("SELECT value FROM build_info WHERE key = 'profile_docs_fp'").fetchone()
            if stored_fp_row and stored_fp_row[0] == docs_fp:
                print("  Profile docs unchanged — skipping re-index (saves ~22k embed churn)")
            else:
                # Re-index gotchas (delete old, insert fresh)
                deleted_dk = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'gotchas'").fetchall()
                for (rowid,) in deleted_dk:
                    conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
                if deleted_dk:
                    print(f"  Removed {len(deleted_dk)} old gotchas chunks")
                dk_files, dk_chunks = index_gotchas(conn)
                total_files += dk_files
                total_chunks += dk_chunks

                # Re-index domain registry (delete old, insert fresh)
                deleted_dr = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'domain_registry'").fetchall()
                for (rowid,) in deleted_dr:
                    conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
                if deleted_dr:
                    print(f"  Removed {len(deleted_dr)} old domain registry chunks")
                dr_files, dr_chunks = index_domain_registry(conn)
                total_files += dr_files
                total_chunks += dr_chunks

                # Re-index flow annotations (delete old, insert fresh)
                deleted_fl = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'flow_annotation'").fetchall()
                for (rowid,) in deleted_fl:
                    conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
                if deleted_fl:
                    print(f"  Removed {len(deleted_fl)} old flow annotation chunks")
                fl_files, fl_chunks = index_flows(conn)
                total_files += fl_files
                total_chunks += fl_chunks

                # Re-index seeds.cql (delete old, insert fresh)
                deleted_sc = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'provider_config'").fetchall()
                for (rowid,) in deleted_sc:
                    conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
                if deleted_sc:
                    print(f"  Removed {len(deleted_sc)} old provider config chunks")
                sc_files, sc_chunks = index_seeds(conn)
                total_files += sc_files
                total_chunks += sc_chunks

                # Re-index test scripts (delete old, insert fresh)
                deleted_ts = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'test_script'").fetchall()
                for (rowid,) in deleted_ts:
                    conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
                if deleted_ts:
                    print(f"  Removed {len(deleted_ts)} old test script chunks")
                ts_files, ts_chunks = index_test_scripts(conn)
                total_files += ts_files
                total_chunks += ts_chunks

                # Re-index tasks (delete old, insert fresh)
                deleted_tk = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'task'").fetchall()
                for (rowid,) in deleted_tk:
                    conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
                if deleted_tk:
                    tk_rowids = [r[0] for r in deleted_tk]
                    placeholders = ",".join("?" * len(tk_rowids))
                    conn.execute(f"DELETE FROM chunk_meta WHERE chunk_rowid IN ({placeholders})", tk_rowids)
                    print(f"  Removed {len(deleted_tk)} old task chunks")
                tk_files, tk_chunks = index_tasks(conn)
                total_files += tk_files
                total_chunks += tk_chunks

                # Re-index references (delete old, insert fresh)
                deleted_rf = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'reference'").fetchall()
                for (rowid,) in deleted_rf:
                    conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
                if deleted_rf:
                    print(f"  Removed {len(deleted_rf)} old reference chunks")
                rf_files, rf_chunks = index_references(conn)
                total_files += rf_files
                total_chunks += rf_chunks

                # Re-index dictionary (delete old, insert fresh)
                deleted_dc = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'dictionary'").fetchall()
                for (rowid,) in deleted_dc:
                    conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
                if deleted_dc:
                    print(f"  Removed {len(deleted_dc)} old dictionary chunks")
                dc_files, dc_chunks = index_dictionary(conn)
                total_files += dc_files
                total_chunks += dc_chunks

                # Re-index provider docs (function is idempotent — deletes per-provider first)
                pd_files, pd_chunks = index_providers(conn)
                total_files += pd_files
                total_chunks += pd_chunks

                # Store fingerprint so tomorrow's run can skip when nothing changed
                conn.execute(
                    "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)",
                    ("profile_docs_fp", docs_fp),
                )

            # Clean old package_usage chunks (rebuilt by build_graph.py)
            deleted_pu = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'package_usage'").fetchall()
            for (rowid,) in deleted_pu:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_pu:
                print(f"  Removed {len(deleted_pu)} old package usage chunks")

            # Update build info with global counts
            total_chunks_global = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            total_repos_global = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
            total_files_global = conn.execute(
                "SELECT COUNT(DISTINCT file_path || '|' || repo_name) FROM chunks"
            ).fetchone()[0]

            conn.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)",
                ("last_build", datetime.now(UTC).isoformat()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)",
                ("total_chunks", str(total_chunks_global)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)",
                ("total_files", str(total_files_global)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_repos", str(total_repos_global))
            )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        # Full build: no explicit transaction needed (writing to temp file)
        for i, (repo_name, meta) in enumerate(repos_to_index, 1):
            files, chunks = index_repo(conn, repo_name, meta)
            total_files += files
            total_chunks += chunks

            if i % 50 == 0:
                print(f"  [{i}/{len(repos_to_index)}] {total_chunks} chunks indexed...")
                conn.commit()

        # Index gotchas files (separate from repo clones)
        dk_files, dk_chunks = index_gotchas(conn)
        total_files += dk_files
        total_chunks += dk_chunks

        # Index domain registry
        dr_files, dr_chunks = index_domain_registry(conn)
        total_files += dr_files
        total_chunks += dr_chunks

        # Index flow annotations
        fl_files, fl_chunks = index_flows(conn)
        total_files += fl_files
        total_chunks += fl_chunks

        # Index seeds.cql provider configs
        sc_files, sc_chunks = index_seeds(conn)
        total_files += sc_files
        total_chunks += sc_chunks

        # Index test scripts from repo scripts/ directories
        ts_files, ts_chunks = index_test_scripts(conn)
        total_files += ts_files
        total_chunks += ts_chunks

        # Index task files
        tk_files, tk_chunks = index_tasks(conn)
        total_files += tk_files
        total_chunks += tk_chunks

        # Index reference files
        rf_files, rf_chunks = index_references(conn)
        total_files += rf_files
        total_chunks += rf_chunks

        # Index dictionary YAMLs
        dc_files, dc_chunks = index_dictionary(conn)
        total_files += dc_files
        total_chunks += dc_chunks

        # Index provider docs
        pd_files, pd_chunks = index_providers(conn)
        total_files += pd_files
        total_chunks += pd_chunks

        total_chunks_global = total_chunks
        total_repos_global = len(repo_meta)

        conn.execute(
            "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("last_build", datetime.now(UTC).isoformat())
        )
        conn.execute(
            "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_chunks", str(total_chunks_global))
        )
        conn.execute("INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_files", str(total_files)))
        conn.execute(
            "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_repos", str(total_repos_global))
        )

        conn.commit()

    # Optimize FTS
    print("Optimizing FTS index...")
    conn.execute(
        "INSERT INTO chunks(chunks, rank, content, repo_name, file_path, file_type, chunk_type, language) VALUES('optimize', '', '', '', '', '', '', '')"
    )
    conn.commit()
    conn.close()

    # Full build: atomic rename from temp file to final path
    if tmp_path is not None:
        tmp_path.rename(DB_PATH)

    db_size = DB_PATH.stat().st_size / (1024 * 1024)
    print("\n=== Index Summary ===")
    if incremental:
        print(f"Mode:          incremental ({len(repos_to_index)} repos)")
        print(f"Re-indexed:    {total_chunks} chunks from {total_files} files")
        print(f"Total chunks:  {total_chunks_global}")
    else:
        print("Mode:          full")
        print(f"Total files:   {total_files}")
        print(f"Total chunks:  {total_chunks}")
    print(f"Total repos:   {total_repos_global}")
    print(f"Database size: {db_size:.1f} MB")
    print(f"Database:      {DB_PATH}")
    print("====================")
