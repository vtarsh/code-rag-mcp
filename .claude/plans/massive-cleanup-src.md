# Source Cleanup Findings

## Duplicated Logic

### `scripts/build_vectors.py` vs `index/builders/docs_vector_indexer.py` — ~200+ lines of copy-paste
- `prepare_text` / `_prepare_text` — identical body formatting (`[{repo}] [{file_type}/{chunk_type}] {content}`)
- `make_record` / `_make_record` — identical dict construction for LanceDB rows
- `_encode` / `_encode` — identical SentenceTransformer batch encoding wrapper
- `_progress_line` / `_progress` — identical ETA/rate formatting logic
- `_open_or_create_writer` — duplicate LanceDB streaming writer with fast-fail NaN probe, `writer_fn`, `optimize_cb`, `get_table`
- `_build_ivfpq_index` / `_build_or_replace_index` — identical IVF-PQ tuning (num_partitions=min(64, n//4), num_sub_vectors=min(48, dim))
- **Impact:** Any memguard fix or index tuning must be applied in two places. Historical drift already happened (COMPACT_EVERY_BATCHES=20 vs 25, CHECKPOINT_EVERY=5000 vs 2000).

### `graph/builders/_common.py` vs `index/builders/_common.py` vs `config.py` — profile/config loading
- All three independently resolve `CODE_RAG_HOME`, `ACTIVE_PROFILE`, `BASE_DIR`, and load `config.json` / `conventions.yaml`
- `graph/builders/_common.py` lines 11-21 replicate `config.py` lines 23-43 almost line-for-line
- `index/builders/_common.py` lines 14-19 replicate the same profile resolution logic again
- **Impact:** Profile-switching bugs have to be fixed in 3 places; conventions loading is inconsistent between graph and index builders.

### `index/builders/orchestrator.py` — 9× identical row-by-row DELETE loop
- Lines 199-200, 209-210, 219-220, 229-230, 239-240, 249-250, 262-263, 272-273, 293-294 all do:
  ```python
  for (rowid,) in deleted_XX:
      conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
  ```
- Only the variable name and print message differ.
- **Impact:** 70+ lines of noise that should be a 3-line helper `_delete_chunks_by_type(conn, file_type)`.

### `search/hybrid.py` `_expand_siblings` & `_annotate_similar_repos` — duplicate table-check + fallback
- Both wrap a DB query in `try/except Exception: return results`
- Both independently check `SELECT name FROM sqlite_master WHERE type='table' AND name='...'`
- Pattern is also duplicated in `tools/service.py:318` and `tools/analyze/shared_sections.py:1161`
- **Impact:** 4 copies of the same defensive table-existence pattern.

### `graph/builders/npm_edges.py` vs `graph/builders/pkg_resolution.py` — both parse `org_deps` JSON
- `npm_edges.py:43-48` and `pkg_resolution.py:24-29` both iterate repos with `org_deps IS NOT NULL`, `json.loads`, strip `NPM_SCOPE/` prefix
- **Impact:** Shared dep-parsing logic should live in `_common.py`.

## Stale Comments/Code

### `src/models.py:53`
- Comment: "Two-tower docs tower (2026-04-23): v12a single-tower FT rejected after 12 eval rounds with near-random embeddings."
- **Issue:** `v12a` model no longer exists in codebase; the comment documents a rejected experiment that is not referenced anywhere else. Dead historical note.

### `src/search/hybrid.py` — multiple stale `v8` reranker references
- Line 211: "v8 FT reranker systematically surfaces them on short repo queries"
- Line 222: "v8's doc-penalty demoted the exact doc file the user asked for"
- Line 489: "v8 surfaces 5+ CI files on short repo queries"
- **Issue:** The project is on `l12` FT run1 (per `embedding_provider.py:206`). References to `v8` are historical breadcrumbs that will confuse future readers.

### `src/config.py` — legacy `config.json` backward-compat (lines 41-53)
- `__legacy__` profile marker + root `config.json` fallback still exists even though the repo has been profile-based for months.
- Line 105: `ACTIVE_PROFILE not in ("example", "__legacy__")` — `__legacy__` is a ghost profile.

### `src/graph/builders/_common.py:19-21`
- `_profile_config` / `_legacy_config` / `_config_path` triplicate path resolution that mirrors `config.py` but uses its own variable names.
- Line 209 comment: "Check profile first, then legacy" — legacy path is dead.

### `src/embedding_provider.py:121`
- Comment: "Guard: ignore legacy Gemini model IDs that may still sit in stale configs."
- **Issue:** No Gemini model IDs exist anywhere in the repo; this is a defensive guard against a config migration that never happened here.

### `src/index/builders/docs_vector_indexer.py:100`
- Comment: "Accepts both the legacy format `{"done_rowids": [...], "data": [...]}`"
- **Issue:** The legacy format was from an early streaming migration (pre-2026-04-24). The checkpoint files in `db/` are all streaming format now. Back-compat code is untested dead weight.

### `src/search/hybrid.py:745`
- Comment: "pure code intent → code tower only (unchanged legacy path)"
- **Issue:** "legacy path" label implies this is temporary; it has been production behavior for months.

### `src/index/builders/docs_vector_indexer.py:592`
- Comment: "Switch to a Run-1 candidate key (e.g. `docs-nomic-ft-v2`)"
- **Issue:** `docs-nomic-ft-v2` does not exist in `src/models.py`; stale bench candidate name.

## Compression Opportunities

### `src/search/hybrid.py` — 1030 lines, 7 distinct responsibilities
- **Doc-intent classifier** (`_query_wants_docs`, 10 regexes, 7-tier decision tree) — should be `search/intent.py`
- **Stratum-gated rerank skip** (`_DOC_RERANK_OFF_STRATA`, `_STRATUM_TOKENS`, `_detect_stratum`, `_should_skip_rerank`) — should be `search/rerank_gating.py`
- **Cross-provider fan-out** (`_KNOWN_PROVIDERS`, `_TOPIC_VERBS`, `_detect_provider_topic`, `_sibling_provider_repos`, `_cross_provider_fanout`) — should be `search/cross_provider.py`
- **Penalty classification** (`_classify_penalty`, `_DOC_FILE_TYPES`, 4 regexes) — should be `search/scoring.py`
- **Sibling expansion + similar-repo annotation** (`_expand_siblings`, `_annotate_similar_repos`) — should be `search/enrichment.py`
- **Deep nesting:** `rerank()` is 70 lines; `hybrid_search()` is 220 lines with 5 inline comment blocks longer than some modules.

### `src/tools/analyze/shared_sections.py` — 1195 lines, 15 sections
- `section_completeness` (lines 991-1156) is 165 lines with 2 nested render functions (`_render_row`, `_render_row_brief`) and duplicated brief/full branching in 4 places
- `section_task_patterns` (lines 589-756) is 167 lines of dense keyword → repo mapping
- `_KEYWORD_FILE_TRIGGERS` dict (lines 65-207) is 140+ lines of static data that should live in `conventions.yaml` or a separate `task_keywords.py`
- **SRP violation:** File claims to be "shared sections that run for ALL task types" but also contains PI-specific helpers (`_has_apm_context`, `_pick_siblings`) and review-mode logic.

### `src/tools/analyze/core_analyzer.py` — 789 lines, 8 sections
- `_section_keyword_scan` (lines 704-789) is 85 lines with 3 phases (compound terms, repo names, single keywords) that share the same `try/except Exception` wrapper
- `_section_cascade` (lines 332-450) is 118 lines mixing upstream, downstream, and reverse cascade logic
- `_section_co_occurrence` (lines 452-556) is 104 lines of dense SQL + probability math
- **Nested functions:** `trace_flow_tool` in `graph/service.py` defines `path_score` and `format_tree` inside the outer function; these should be module-level.

### `src/tools/analyze/__init__.py` — 543 lines
- `_analyze_task_impl` (lines 360-543) is 183 lines with a nested `_run_section` closure (lines 397-404)
- `_section_npm_dep_scan` (lines 196-287) is 91 lines inside the same file
- `_extract_repo_refs` (lines 105-157) is 52 lines of fuzzy string matching
- **Issue:** `__init__.py` should not contain implementation logic; it should only re-export. `_analyze_task_impl` and `_section_npm_dep_scan` belong in `orchestrator.py` or `sections.py`.

### `src/index/builders/docs_vector_indexer.py` — 691 lines
- `_embed_and_write_streaming` (lines 193-309) is 116 lines with 2 nearly identical loops (short rows, long rows) that differ only in `batch_size` and `limit`
- `_fix_gte_persistent_false_buffers` (lines 466-503) is 37 lines of torch buffer surgery that is also duplicated in `scripts/runpod/train_docs_embedder.py`
- `_load_sentence_transformer` (lines 533-567) is 34 lines of device-selection + gte-fix logic that should be in `src/embedding_provider.py`

### `src/index/builders/orchestrator.py` — 422 lines
- Full-build path (lines 326-395) and incremental path (lines 167-324) both call the same 8 indexer functions (`index_gotchas`, `index_domain_registry`, `index_flows`, `index_seeds`, `index_test_scripts`, `index_tasks`, `index_references`, `index_dictionary`, `index_providers`)
- **Issue:** The only difference is transaction wrapping and delete-before-insert. A single parameterized loop would cut ~120 lines.

### `src/tools/fields.py` — 392 lines, 12 helper functions
- Every `_mode_*` function (`_mode_contract`, `_mode_trace`, `_mode_consumers`, `_mode_compare`) repeats the same YAML-cache lookup + provider-filtering + grep-fallback pattern
- `_find_field_in_contracts`, `_find_chain`, `_find_snapshots` are thin wrappers around `_load_ref_yaml`
- **Issue:** Mode dispatch is a hand-rolled switch that should use a registry dict or dataclass.

### `src/graph/service.py` — 329 lines
- `trace_flow_tool` (127-216) embeds `path_score` and `edge_weight` dict that are only used there
- `trace_chain_tool` (220-329) embeds `format_tree` (55 lines) which is not reused only because it is trapped inside the outer function
- **Issue:** Nested functions prevent reuse and make unit testing impossible without calling the full tool.

## Quick Wins

1. **Extract `_delete_chunks_by_type(conn, file_type)` in `orchestrator.py`** — Replaces 9 identical DELETE loops; saves ~60 lines.
2. **Extract `table_exists(conn, name)` helper** — Replaces 4 copies of `SELECT name FROM sqlite_master WHERE type='table' AND name='...'` in `hybrid.py`, `service.py`, `shared_sections.py`.
3. **Hoist `_run_section` from `tools/analyze/__init__.py` to module level** — The closure captures nothing from `_analyze_task_impl` except `failed_sections`, which can be passed in. Enables testing and shrinks `__init__.py`.
4. **Move doc-intent classifier to `search/intent.py`** — `hybrid.py` drops from 1030 to ~750 lines immediately. The classifier is self-contained (only needs `query: str`).
5. **Deduplicate `prepare_text`/`make_record`/`_encode` into `index/builders/_vector_common.py`** — Both `scripts/build_vectors.py` and `docs_vector_indexer.py` can import from here. Prevents the next memguard drift.
6. **Remove `__legacy__` profile path from `config.py` and `graph/builders/_common.py`** — If root `config.json` no longer exists in production, the fallback is dead code.
7. **Delete `_fix_gte_persistent_false_buffers` from `docs_vector_indexer.py` and import from `embedding_provider.py`** — Or move to a shared `model_fixes.py` module; it is duplicated in `scripts/runpod/train_docs_embedder.py`.
8. **Consolidate `graph/builders/_common.py` profile loading to use `config.py`** — `config.py` already exports `BASE_DIR`, `PROFILE_DIR`, `ACTIVE_PROFILE`. Re-importing them removes ~20 lines and eliminates divergence risk.
