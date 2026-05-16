# Scripts Cleanup Findings

## Duplicated Logic

### 1. `classify_file()` + regex constants — 100% copy-paste
- `data/v12_candidates.py` (lines 36–65) vs `data/v12_candidates_regen_doc.py` (lines 66–98)
- Same `_CI_PATH_RE`, `_TEST_PATH_RE`, and `classify_file()` body. Only docstring differs.
- **Fix:** Extract to `data/_v12_common.py` or `_common.py`.

### 2. `pause_daemon()` — 3× identical urllib POST
- `data/embed_missing_vectors.py` (lines 44–75)
- `data/finetune_reranker.py` (lines 61–92)
- `eval/eval_finetune.py` (lines 59–80)
- All POST to `/admin/shutdown` with identical error handling (ECONNREFUSED 61/111).
- `_common.py` already has `daemon_post()` but lacks `pause_daemon()`.
- **Fix:** Add `pause_daemon()` to `_common.py`; migrate all three scripts.

### 3. `preclean_for_fts()` + `_FTS_PRECLEAN` regex — divergent twins
- `bench/benchmark_rerank_ab.py` (lines 30–34): strips `[\[\]{}():"',;]`
- `data/prepare_finetune_data.py` (lines 43–54): strips `[^\w\s\.\-]`
- Docstrings claim to serve the same purpose (prevent FTS5 syntax errors) but use different regexes.
- `eval/eval_finetune.py` imports from `benchmark_rerank_ab` but `prepare_finetune_data.py` rolls its own.
- **Fix:** Single source of truth in `src.search.fts` or `_common.py`.

### 4. `run_hybrid_search()` / `run_fts_search()` — re-implemented instead of reused
- Canonical versions live in `bench/bench_utils.py`.
- `analysis/detect_blind_spots.py` (line 200) defines its own `run_hybrid_search()` that wraps `_run_hybrid_search_base` identically, but does NOT import from `bench_utils`.
- `bench/benchmark_flows.py` at least imports from `bench_utils`, but `detect_blind_spots.py` duplicates the pattern.
- **Fix:** Import from `bench_utils` in `detect_blind_spots.py`.

### 5. `_parse_out()` — explicitly duplicated with parity comment
- `runpod/train_docs_embedder.py` (line 54)
- `runpod/train_reranker_ce.py` (line 55)
- Comment explicitly says "Identical to `train_docs_embedder._parse_out` so the two trainers' output-routing semantics stay in lockstep".
- **Fix:** Extract to `runpod/_common.py`.

### 6. `resolve_profile_dir()` / `get_db()` — duplicated
- `bench/bench_utils.py` (lines 22–38) defines both.
- `build/build_audit_context.py` (line 60) defines its own `get_db()` with identical body.
- **Fix:** Import from `bench_utils` or move both to `_common.py`.

### 7. `sys.path.insert()` bootstrap — ~40 scripts roll their own
- Almost every script has a bespoke `sys.path.insert(0, str(Path(__file__).resolve().parents[N]))` block.
- Only **2 scripts** (`maint/validate_recipe.py`, `bench/benchmark_investigation.py`) actually use `_common.setup_paths()`.
- **Fix:** Migrate all scripts to `_common.setup_paths()`; it handles `CODE_RAG_HOME`, `ACTIVE_PROFILE`, and idempotent path insertion.

### 8. `percentile()` — 2× implementations
- `bench/benchmark_rerank_ab.py` (line 133)
- `data/merge_eval_shards.py` (line 29)
- `eval/eval_finetune.py` imports from `benchmark_rerank_ab`, but `merge_eval_shards.py` keeps its own.
- **Fix:** Import from `benchmark_rerank_ab` or move to `_common.py`.

---

## Stale References

### 1. `eval_parallel.sh` — **completely broken**, not referenced by Makefile/tests
- Line 18: `BASELINE=profiles/pay-com/finetune_history/gte_v1.json` → `finetune_history/` was deleted in commit `5306851`.
- Line 47: `OUT=profiles/pay-com/finetune_history` → same deleted dir.
- Line 88: `python3.12 scripts/eval_finetune.py` → script was moved to `scripts/eval/eval_finetune.py`.
- Line 125: `python3.12 scripts/merge_eval_shards.py` → script was moved to `scripts/data/merge_eval_shards.py`.
- **No Makefile, test, or other script references `eval_parallel.sh`.**
- **Fix:** Delete or rewrite from scratch.

### 2. `eval/eval_jidm.py` docstring
- Line 34: `--snapshot profiles/pay-com/churn_replay/v8_vs_base.json` → `churn_replay/` was deleted.
- **Fix:** Update docstring example to a current snapshot path.

### 3. `scripts/AGENTS.md` catalog errors
- Line 50: `eval_verdict.py` listed **twice** under `eval/`.
- Line 59: `gen_repo_facts.py` listed under `analysis/` but actual file is at `scripts/gen_repo_facts.py` (root).
- Line 110: Claims "All scripts use `_common.py`" — reality: only 2 scripts import from it.
- **Fix:** Deduplicate entry, correct path, update convention claim.

### 4. `data/embed_missing_vectors.py` import comment
- Line 33: `from scripts.build_vectors import embed_simple` — relies on root-level `build_vectors.py`.
- `build/build_vectors.py` does **not exist**; the AGENTS.md tree incorrectly lists it.
- **Fix:** Clarify comment that root `build_vectors.py` is the real target (or move it to `build/` and fix all refs).

---

## Compression Opportunities

### Mega-scripts (>500 lines, candidates for splitting)
| Script | Lines | Suggestion |
|--------|-------|------------|
| `data/prepare_finetune_data.py` | 1,578 | Split into `query_builder.py`, `fts_miner.py`, `splitter.py`, `manifest_writer.py` |
| `eval/eval_finetune.py` | 1,159 | Split into `daemon_ctrl.py`, `model_loader.py`, `eval_loop.py`, `report_gen.py` |
| `runpod/full_pipeline.py` | 1,065 | Split into `ssh_thunks.py`, `smoke_runner.py`, `train_orchestrator.py`, `bench_orchestrator.py` |
| `data/finetune_reranker.py` | 963 | Large but justified (legacy + Trainer dual paths). Could still extract `MpsCacheHygieneCallback` to shared module. |
| `bench/benchmark_doc_intent.py` | 972 | Extract `rerank_candidates()`, metric helpers, and report formatting to `bench/_doc_intent_common.py` |
| `maint/validate_provider_paths.py` | 798 | Extract `generate_facade_candidates()`, `pick_facade()`, and provider schema validators to `maint/_validate_common.py` |
| `build/build_shadow_types.py` | 781 | Extract `_apm_config()` and provider-specific mappings to `build/_provider_configs.py` |
| `runpod/train_docs_embedder.py` | 682 | Extract `_parse_out()` (shared with `train_reranker_ce.py`) and dataset builder helpers |
| `eval/eval_verdict.py` | 572 | Already extracted from `eval_finetune.py` and `merge_eval_shards.py` — good pattern, keep it. |
| `runpod/pod_lifecycle.py` | 554 | Extract SSH command builders and RunPod API thunks to `runpod/_ssh_common.py` |
| `scrape/extract_artifacts.py` | 529 | Extract markdown/HTML sanitizers and Tavily response parsers to `scrape/_parsers.py` |

### Root-level legacy scripts (likely stale / unreferenced)
| Script | Lines | Status |
|--------|-------|--------|
| `health_check_agents_md.py` | 583 | Referenced by `.claude/rules/` and plans, but NOT by Makefile or tests. Keep or move to `maint/`. |
| `cross_validate_task.py` | 517 | **Zero references** in Makefile, tests, src, or docs outside of itself. |
| `build_vectors.py` | 513 | Referenced by `Makefile`, `full_update.sh`, and `embed_missing_vectors.py`. Keep at root or move and fix refs. |
| `collect_task.py` | 500 | **Zero references** in Makefile, tests, src, or docs outside of `.claude/skills/collect-tasks/` SKILL.md. |
| `manage_ground_truth.py` | 428 | **Zero references** in Makefile, tests, src, or docs. |
| `auto_collect.py` | 299 | **Zero references** in Makefile, tests, src, or docs. |
| `analyze_gaps.py` | 366 | **Zero references** outside itself. |
| `ci-pattern-checker.py` | 313 | **Zero references** outside itself. |
| `task_type_checklist.py` | 312 | **Zero references** outside itself. |
| `build_method_matrix.py` | 309 | **Zero references** outside itself. |
| `parse_jaeger_trace.py` | 273 | **Zero references** outside itself. |
| `method_level_gaps.py` | 268 | **Zero references** outside itself. |
| `build_test_map.py` | 213 | **Zero references** outside itself. |
| `collect_ci_runs.py` | 208 | **Zero references** outside itself. |
| `gen_repo_facts.py` | 206 | **Zero references** outside itself. |
| `check_pr_patterns.py` | 116 | **Zero references** outside itself. |
| `export_patterns.py` | 92 | **Zero references** outside itself. |

### Merge candidates
- `data/v12_candidates.py` (170 lines) + `data/v12_candidates_regen_doc.py` (288 lines) = 458 lines of 80% overlapping logic.
  - **Suggestion:** Add `--regen-doc` flag to `v12_candidates.py` and delete `v12_candidates_regen_doc.py`.
- `runpod/train_docs_embedder.py` (682 lines) + `runpod/train_reranker_ce.py` (326 lines) share `_parse_out()`, CLI shape, and MIN_TRAIN_ROWS guard.
  - **Suggestion:** Extract shared trainer CLI framework to `runpod/_train_common.py` (~100 lines saved).

---

## Quick Wins

| Action | Reasoning |
|--------|-----------|
| **Delete `eval_parallel.sh`** | Zero external references + 4 broken paths inside (deleted dirs + moved scripts). |
| **Fix `scripts/AGENTS.md`** | Duplicate `eval_verdict.py`, wrong `gen_repo_facts.py` path, false claim about `_common.py` usage. |
| **Extract `classify_file()` to shared module** | 100% duplication across 2 active v12 scripts. |
| **Extract `pause_daemon()` to `_common.py`** | 3× identical copies; `_common.py` is the intended home. |
| **Unify `preclean_for_fts()`** | Two regexes for the same FTS5 sanitization goal — divergence risk. |
| **Migrate scripts to `_common.setup_paths()`** | ~40 bespoke `sys.path.insert()` blocks → 1 line each. |
| **Move root-level orphans to `attic/` or delete** | 14+ root scripts have zero Makefile/test/src references. High confidence they are dead. |
| **Merge `v12_candidates_regen_doc.py` into `v12_candidates.py`** | Same `classify_file`, same query selection, same output format. Only difference is reranker override and penalty disable. |
| **Fix `eval_jidm.py` docstring** | References deleted `churn_replay/` directory. |
| **Delete `scripts/__pycache__`** | 70+ stale `.pyc` files from moved/deleted scripts (e.g., `build_v12_holdouts.cpython-312.pyc`, `churn_replay.cpython-312.pyc`). |
