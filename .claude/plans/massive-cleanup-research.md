# Massive Cleanup Research — Consolidated Findings

> Autonomous multi-agent investigation completed 2026-05-16
> 5 scopes × 5 agents = 50K+ words of findings across ~4,500 files

---

## Executive Summary

| Category | Count | Top Impact |
|----------|-------|------------|
| **Duplicates** | 35+ clusters | 16 byte-identical file pairs in `providers/` (~15MB), `classify_file()` 100% copy-paste, `_mock_wiring` fixture ×4 |
| **Stale refs/code** | 22 items | `eval_parallel.sh` completely broken, `churn_replay/` refs in 4 files, REMOVED provider dirs still exist, `.pre-commit-config.yaml` paths stale |
| **Compression** | 42 files/dirs | `search/hybrid.py` 1030 lines / 7 responsibilities, `test_runpod_lifecycle.py` 1898 lines, `plaid/legal.md` 1.0M, `db/` 56GB |
| **Orphans** | 16 root scripts + 6 provider stubs | Zero references anywhere in repo |

---

## 1. Docs (`profiles/pay-com/docs/`)

### Duplicates (~15MB recoverable)
- **16 byte-identical pairs** in `providers/`: `plaid/llms-full.txt.md` ↔ `docs_llms-full.txt.md` (5.3M each), `bankingcircle/_openapi/` ↔ `swagger/`, `checkout/*.md` ↔ `docs_*.md` (9 pairs), etc.
- **Near-duplicates**: `gotchas/global-conventions.md` ↔ `errors-and-throw-policy.md` ↔ `error-code-mapping.md` — ~78KB overlapping prose.
- `notes/_moc/*.md` (11 files) — auto-generated reverse indexes duplicating frontmatter `related:` links.

### Stale
- `providers/aircash/`, `neosurf/` — explicitly **REMOVED** in `do-not-expire-matrix.md`, yet dirs exist.
- `providers/stripe-cashapp/` (30 files) — dead integration, no gotchas/flows.
- `providers/iris/`, `ilixium/`, `neteller/`, `rtp/`, `libra/` — orphaned stubs.
- `express-webhooks-paypal` deprecated in `gotchas/grpc-apm-paypal.md`.

### Compression
- `providers/plaid/legal.md` — **1.0M** raw legal text.
- `providers/evo/*.pdf` (6 files, ~5.5M) — PDFs coexist with MD extractions.
- `providers/ach/` — 274 files (199 under 3KB) — extreme fragmentation.
- `providers/bankingcircle/` — 32 changelog files + duplicate `_openapi/` vs `swagger/`.

**Quick wins**: Delete 16 identical pairs, remove PDFs where MDs exist, merge ACH stubs, delete REMOVED provider dirs, kill `notes/_moc/`.

---

## 2. Scripts (`scripts/`)

### Duplicated Logic (8 clusters)
1. `classify_file()` — 100% copy-paste in `v12_candidates.py` ↔ `v12_candidates_regen_doc.py`
2. `pause_daemon()` — 3× identical urllib POST (`embed_missing_vectors.py`, `finetune_reranker.py`, `eval_finetune.py`)
3. `preclean_for_fts()` — divergent regex twins (`benchmark_rerank_ab.py` vs `prepare_finetune_data.py`)
4. `run_hybrid_search()` — re-implemented in `detect_blind_spots.py` instead of importing from `bench_utils.py`
5. `_parse_out()` — explicitly duplicated across `train_docs_embedder.py` ↔ `train_reranker_ce.py`
6. `resolve_profile_dir()` / `get_db()` — duplicated in `bench_utils.py` vs `build_audit_context.py`
7. `sys.path.insert()` bootstrap — **~40 scripts** roll their own; only 2 use `_common.setup_paths()`
8. `percentile()` — 2× (`benchmark_rerank_ab.py` vs `merge_eval_shards.py`)

### Stale
- `eval_parallel.sh` — **completely broken**: references deleted `finetune_history/`, moved scripts. Zero external references.
- `eval_jidm.py` docstring — points to deleted `churn_replay/v8_vs_base.json`.
- `scripts/AGENTS.md` — duplicate `eval_verdict.py` entry, wrong `gen_repo_facts.py` path, false claim about `_common.py` usage.
- `embed_missing_vectors.py` import comment — references non-existent `build/build_vectors.py`.

### Compression
- **11 mega-scripts >500 lines**: `prepare_finetune_data.py` (1,578), `eval_finetune.py` (1,159), `full_pipeline.py` (1,065), `finetune_reranker.py` (963), `benchmark_doc_intent.py` (972).
- **16 root-level orphan scripts** with zero Makefile/test/src references: `cross_validate_task.py`, `collect_task.py`, `manage_ground_truth.py`, `auto_collect.py`, `analyze_gaps.py`, `ci-pattern-checker.py`, `task_type_checklist.py`, `build_method_matrix.py`, `parse_jaeger_trace.py`, `method_level_gaps.py`, `build_test_map.py`, `collect_ci_runs.py`, `gen_repo_facts.py`, `check_pr_patterns.py`, `export_patterns.py`.
- `__pycache__` — 70+ stale `.pyc` files from deleted modules.

**Quick wins**: Delete `eval_parallel.sh`, extract `pause_daemon()` to `_common.py`, migrate ~40 scripts to `_common.setup_paths()`, move orphans to `attic/` or delete.

---

## 3. Source (`src/`)

### Duplicated Logic (6 clusters)
1. `scripts/build_vectors.py` ↔ `index/builders/docs_vector_indexer.py` — **~200+ lines** copy-paste (`prepare_text`, `make_record`, `_encode`, `_open_or_create_writer`, `_build_ivfpq_index`). Historical drift already happened (COMPACT_EVERY_BATCHES=20 vs 25).
2. `graph/builders/_common.py` ↔ `index/builders/_common.py` ↔ `config.py` — profile/config loading triplicated.
3. `index/builders/orchestrator.py` — 9× identical row-by-row DELETE loop (70+ lines → 3-line helper).
4. `search/hybrid.py` `_expand_siblings` & `_annotate_similar_repos` — duplicate table-check + fallback (also in `tools/service.py` and `tools/analyze/shared_sections.py`).
5. `graph/builders/npm_edges.py` ↔ `pkg_resolution.py` — both parse `org_deps` JSON independently.
6. `index/builders/docs_vector_indexer.py:466-503` — `_fix_gte_persistent_false_buffers` duplicated in `scripts/runpod/train_docs_embedder.py`.

### Stale Comments/Code (8 items)
- `models.py:53` — dead `v12a` comment (rejected experiment).
- `search/hybrid.py` — 3 references to obsolete `v8` reranker.
- `config.py` — `__legacy__` profile + root `config.json` backward-compat (ghost profile).
- `embedding_provider.py:121` — defensive guard against Gemini models that never existed here.
- `docs_vector_indexer.py:100` — legacy checkpoint format back-compat (untested dead weight).
- `docs_vector_indexer.py:592` — stale bench candidate name `docs-nomic-ft-v2`.

### Compression
- **`search/hybrid.py` — 1030 lines, 7 responsibilities**: intent classifier, stratum gating, cross-provider fanout, penalty scoring, sibling expansion, similar-repo annotation, RRF reranking.
- **`tools/analyze/shared_sections.py` — 1195 lines**: 140-line static `_KEYWORD_FILE_TRIGGERS` dict, 165-line `section_completeness` with duplicated brief/full branching.
- **`tools/analyze/core_analyzer.py` — 789 lines**: dense SQL + probability math, 118-line `_section_cascade`.
- **`tools/analyze/__init__.py` — 543 lines**: `_analyze_task_impl` with nested `_run_section` closure — `__init__.py` should only re-export.
- **`index/builders/docs_vector_indexer.py` — 691 lines**: nearly identical short/long row loops.

**Quick wins**: Extract `_delete_chunks_by_type()` helper (saves 60 lines), extract `table_exists()` helper (4 copies → 1), move doc-intent classifier to `search/intent.py`, remove `__legacy__` paths.

---

## 4. Tests (`tests/`)

### Duplicated Tests/Fixtures (13 clusters)
1. `_mock_wiring` fixture — identical in 4 files (`test_hybrid.py`, `test_rerank_skip.py`, `test_cross_provider_fanout.py`, `test_two_tower_routing.py`).
2. `_make_sr` helper — 3 files build `SearchResult` fixtures with identical defaults.
3. `sys.path.insert(0, str(REPO_ROOT))` + `importlib.util` — **~17 files** repeat the same 5-line script-import boilerplate.
4. HF_TOKEN early-abort test — cloned between `test_train_docs_embedder.py` ↔ `test_train_reranker_ce.py`.
5. B7 body-omission tests — 3 near-identical tests in `test_runpod_lifecycle.py` (only asserted key differs).
6. Provision-failure tests — 4 tests share 90% setup.
7. `@patch("src.search.hybrid.rerank", ...)` decorator stack — 6–12× across hybrid test files.
8. `_mock_conn` helper — `test_analyze.py` ↔ `test_classifier.py`.
9. `_write_jsonl` helper — 3 files.
10. `_mock_db_connection` context manager — `test_analyze.py` ↔ `test_env_vars.py`.
11. `_run_main_capture` helper — copy-pasted inline across 4 tests in `test_benchmark_doc_intent.py`.
12. `DaemonHandler` manual construction — 4+ tests in `test_daemon.py`.
13. Key-missing / bad-format tests — near-identical pairs in `test_runpod_lifecycle.py`.

### Stale Tests (3)
- `test_chunking.py` — tests through `scripts.build.build_index` re-export stub instead of `src.index.builders`.
- `test_integration.py` — fully skip-gated on `knowledge.db` existence → silent no-op in CI.
- `test_build_combined_train.py` — production-data contract test skipped in most environments.

### Compression
- **`test_runpod_lifecycle.py` — 1898 lines, 123 mock references** — largest offender.
- **`test_eval_file_gt.py` — 538 lines** — mixes 3 unrelated modules in one file.
- **`test_hybrid.py` — 520 lines, 122 mock references** — `TestRerankPenalties` could be parametrized.
- **`test_analyze.py` — 443 lines** — same 4-layer `@patch` stack on 12 methods.
- **`test_daemon.py` — 427 lines** — manual `DaemonHandler` construction repeated 4+ times.
- **15 files total >300 lines**.

**Quick wins**: Extract `_mock_wiring` to `conftest.py`, create shared `_import_script()` helper, parametrize B7 omission tests, split `test_eval_file_gt.py`.

---

## 5. Root / Data / Configs

### Duplicates
- `.claude/plans/codebase-cleanup-AFK.md` ↔ `.claude/worktrees/codebase-cleanup-AFK.md` — byte-identical.
- `.gitignore` — 5 patterns listed twice (`graph.html`, `clone_log.json`, `repo_state.json`, `extract_log.json`, `blind_spots_results.json`).

### Stale Files/Configs
- **`config.json`** (root) — gitignored but present with stale pay-com data; superseded by profile system.
- **`daemon.pid`** — runtime artifact, not in `.gitignore`.
- **`graph.html`** — 374KB generated viz, gitignored but stale (Mar 15).
- **`.pre-commit-config.yaml`** — references 7 doc validators at old `scripts/` path (moved to `scripts/maint/`); hooks will fail.
- **`ARCHITECTURE.md`** — claims `db/vectors.lance.coderank` is "~238MB"; actual is **~27GB**.
- **`NEXT_SESSION_PROMPT.md`** — referenced in docs but file no longer exists.
- **`setup_wizard.py`** — AGENTS.md flags as stale (8+ weeks untouched), still wired into Makefile.
- **`AGENTS.md`** — duplicated "Open Questions" heading; Q4 about validate_doc scripts is outdated (they were committed to `scripts/maint/`).

### Compression
- **`db/`** — 56GB (gitignored). `vectors.lance.coderank` 27G + `vectors.lance.docs` 29G.
- **`logs/`** — 77MB. 6 files >30 days old.
- **`bench_runs/`** — 79MB. 63 files >14 days old (Apr 26 intermediate retry artifacts).
- **`.claude/debug/`** — 2MB stale April debate/eval artifacts.
- **`.DS_Store`** — 10KB at root, **not gitignored**.
- **`models/`** — empty directory tree.

**Quick wins**: Delete root `config.json`/`graph.html`/`daemon.pid`; add `.DS_Store` + `daemon.pid` to `.gitignore`; dedupe `.gitignore`; update pre-commit paths; fix ARCHITECTURE.md size claims; purge old logs/bench_runs.

---

## Recommended Execution Order

### Phase 1 — Safe Deletions (no code risk)
1. Delete 16 identical duplicate file pairs in `providers/` (~15MB)
2. Delete `providers/evo/` PDFs (~5.5M)
3. Delete `notes/_moc/` (11 files)
4. Delete REMOVED provider dirs (`aircash/`, `neosurf/`)
5. Delete root `config.json`, `graph.html`, `daemon.pid`
6. Delete `.claude/worktrees/codebase-cleanup-AFK.md`
7. Delete `eval_parallel.sh`
8. Purge `logs/` files >30 days old
9. Purge `bench_runs/` intermediate retry artifacts

### Phase 2 — Quick Fixes (low risk)
10. Dedupe `.gitignore` entries
11. Add `.DS_Store` + `daemon.pid` to `.gitignore`
12. Fix `.pre-commit-config.yaml` paths (`scripts/` → `scripts/maint/`)
13. Fix `ARCHITECTURE.md` size claims
14. Fix `AGENTS.md` duplicate heading + stale Q4
15. Remove `NEXT_SESSION_PROMPT.md` references
16. Update `eval_jidm.py` docstring
17. Fix `scripts/AGENTS.md` errors

### Phase 3 — Refactoring (medium risk, needs tests)
18. Extract `pause_daemon()` to `_common.py`
19. Extract `classify_file()` to shared module
20. Unify `preclean_for_fts()` regex
21. Migrate ~40 scripts to `_common.setup_paths()`
22. Extract `_mock_wiring` to `conftest.py`
23. Extract `_delete_chunks_by_type()` in `orchestrator.py`
24. Move orphans to `attic/` or delete

### Phase 4 — Major Surgery (high risk, needs careful review)
25. Split `search/hybrid.py` into 5–6 modules
26. Split `tools/analyze/shared_sections.py`
27. Split `test_runpod_lifecycle.py`
28. Deduplicate `build_vectors.py` ↔ `docs_vector_indexer.py` (~200 lines)
29. Consolidate `graph/builders/_common.py` + `index/builders/_common.py` into `config.py`
30. Compress `providers/ach/` 274 files → ~15 bundles
