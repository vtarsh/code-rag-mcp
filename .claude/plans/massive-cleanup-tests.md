# Tests Cleanup Findings

## Duplicated Tests/Fixtures

### 1. `_mock_conn` helper
- `test_analyze.py` vs `test_classifier.py`: Nearly identical in-memory SQLite DB setup with `conn.row_factory = sqlite3.Row` and `CREATE TABLE repos`. The classifier version only adds an optional `provider_repos` arg.

### 2. `_write_jsonl` helper
- `test_build_train_pairs.py` vs `test_merge_dual_judge_labels.py` vs `test_sample_real_queries.py`: Same JSONL writer pattern (`path.open("w")`, `json.dumps(r) + "\n"`).

### 3. `_make_sr` helper
- `test_hybrid.py` vs `test_rerank_skip.py` vs `test_cross_provider_fanout.py`: All three build `SearchResult` fixtures with nearly identical defaults. `test_rerank_skip.py` adds `_make_doc_sr` variant.

### 4. `_mock_wiring` fixture
- `test_hybrid.py` vs `test_rerank_skip.py` vs `test_cross_provider_fanout.py` vs `test_two_tower_routing.py`: Identical `autouse` fixture that patches `code_facts_search` and `env_var_search` to `[]`.

### 5. `sys.path.insert(0, str(REPO_ROOT))` + `importlib.util` pattern
- Repeated in ~10 files (`test_bench_v2.py`, `test_benchmark_doc_intent.py`, `test_build_train_pairs.py`, `test_chunking.py`, `test_code_intent_eval.py`, `test_eval_file_gt.py`, `test_eval_jidm.py`, `test_eval_verdict.py`, `test_finalize_scrape.py`, `test_label_v12_candidates_minilm.py`, `test_listwise_conversion.py`, `test_lpt_schedule.py`, `test_merge_dual_judge_labels.py`, `test_rerank_pointwise_eval.py`, `test_sample_real_queries.py`, `test_scrape_link_rewrite.py`, `test_validate_provider_paths.py`). Each file manually inserts `REPO_ROOT` and uses `importlib.util.spec_from_file_location`.

### 6. HF_TOKEN early-abort test
- `test_train_docs_embedder.py` (`test_train_aborts_early_if_hf_token_missing`) vs `test_train_reranker_ce.py` (`test_train_aborts_early_if_hf_token_missing`): Same `_Poison` pattern, same assertion, different modules.

### 7. `_mock_db_connection` context manager
- `test_analyze.py` vs `test_env_vars.py`: Both build a `@contextmanager` that yields a mock connection.

### 8. Key-missing / bad-format tests in `test_runpod_lifecycle.py`
- `test_cost_guard_aborts_when_key_missing` vs `test_pod_lifecycle_aborts_when_key_missing`
- `test_cost_guard_aborts_on_bad_key_format` vs `test_pod_lifecycle_aborts_on_bad_key_format`
- Same assertion structure, only the imported exception class differs.

### 9. B7 body-omission tests
- `test_start_pod_body_omits_idle_timeout`, `test_start_pod_body_omits_termination_time`, `test_start_pod_body_omits_minvcpu_minmemory`: Identical `_fake_request` capture pattern, only the asserted key changes.

### 10. Provision failure tests in `test_runpod_lifecycle.py`
- `test_full_pipeline_provision_fails_when_setup_env_returns_nonzero`, `test_full_pipeline_provision_fails_when_tar_overlay_returns_nonzero`, `test_full_pipeline_provision_fails_when_knowledge_db_missing`, `test_full_pipeline_provision_fails_when_scp_knowledge_db_returns_nonzero`: All share the same `fake_root`, `stop_pod`/`get_pod` monkeypatch, and result assertions.

### 11. `_run_main_capture` helper
- `test_benchmark_doc_intent.py`: The helper is copy-pasted inline across 4 tests (`test_rerank_model_path_default_uses_production_model`, `test_rerank_model_path_flag_overrides_manifest`, `test_rerank_env_var_still_used_when_flag_absent`, `test_rerank_flag_takes_precedence_over_env_var`).

### 12. DaemonHandler manual construction
- `test_daemon.py`: The raw-handler assembly (`rfile = BytesIO(...)`, `wfile = BytesIO()`, `handler = DaemonHandler.__new__(...)`, `handler.rfile = ...`) is repeated in 4+ test methods.

### 13. `@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])` decorator stack
- Repeated 6+ times in `test_cross_provider_fanout.py`, 7+ times in `test_two_tower_routing.py`, 8+ times in `test_rerank_skip.py`, and 12+ times in `test_hybrid.py`.

## Stale Tests

### 1. `test_chunking.py` — tests through a re-export stub
- Imports `chunk_code`, `chunk_markdown`, `chunk_proto` from `scripts.build.build_index`, which is now a 23-line re-export stub (`from src.index.builders import *`). The test exercises the real logic indirectly. Should import from `src.index.builders` directly.

### 2. `test_integration.py` — fully conditional
- Every test class is guarded by `@pytest.mark.skipif(not _db_exists, ...)`. When `knowledge.db` is absent (typical in CI / fresh clones), the entire file is a no-op. No regression signal.

### 3. `test_build_combined_train.py` — conditional production-data test
- `test_build_combined_real_sources_meet_spec` is skipped unless `DEFAULT_CODE_SRC` and `DEFAULT_DOCS_SRC` exist. In most environments this test never runs.

## Compression Opportunities

### Oversized files (>300 lines)
| File | Lines | Issues |
|------|-------|--------|
| `test_runpod_lifecycle.py` | 1898 | Massive boilerplate; many near-identical tests; 123 mock/patch references |
| `test_train_docs_embedder.py` | 736 | Repeated `_Poison` / `_StopAfterGuard` classes; 5+ MIN_TRAIN_ROWS guard tests that differ only in row count or flag |
| `test_eval_file_gt.py` | 538 | Mixes 3 unrelated modules (`eval_finetune`, `eval_verdict`, `prepare_finetune_data`) — should split |
| `test_hybrid.py` | 520 | 12 identical `@patch` stacks; `TestRerankPenalties` has 10 tests with identical `mock_get_reranker` setup |
| `test_prepare_train_data.py` | 477 | Eval-disjoint section repeats the same `_write_labeled` + `_build_db` + `_write_eval` setup in every test |
| `test_analyze.py` | 443 | 12 tests use the exact same 4-layer `@patch` stack (`db_connection`, `check_db_health`, `_find_task_branches`, `_find_task_prs`) |
| `test_validate_provider_paths.py` | 443 | Large but well-structured; could still compress `_write_spec` usage |
| `test_daemon.py` | 427 | Manual `DaemonHandler` assembly repeated; `test_shutdown_drains_and_exits` and `test_shutdown_waits_for_inflight_request` share huge setup blocks |
| `test_benchmark_doc_intent.py` | 415 | `_run_main_capture` pasted 4 times; CLI flag tests are ~80 lines each |
| `test_docs_vector_indexer.py` | 350 | `_patch_model_and_lance` is a good helper, but `TestBuildDocsVectors` repeats the same `tmp_path` + `fake_model` + `fake_lance` setup |
| `test_merge_dual_judge_labels.py` | 345 | `_input_row` helper is large and used heavily; truth-table test could be more compact |
| `test_finalize_scrape.py` | 343 | `_outcome` helper is fine, but `decide_action` matrix tests repeat `_decide()` wrapper |
| `test_rerank_skip.py` | 311 | 8 end-to-end tests share the same 3-patch decorator; only `mock_fts.return_value` and assertion differ |
| `test_train_reranker_ce.py` | 308 | Happy-path `train()` test duplicates `fake_st_module` / `fake_input_example` setup |
| `test_build_combined_train.py` | 304 | `test_load_code_pairs_rejects_*` and `test_load_docs_triplets_rejects_*` are 4 near-identical negative-path tests |

### Excessive mocking
| File | Mock references | Notes |
|------|-----------------|-------|
| `test_runpod_lifecycle.py` | 123 | Every test patches `pod_lifecycle` internals |
| `test_hybrid.py` | 122 | `rerank`, `vector_search`, `fts_search` stacked repeatedly |
| `test_analyze.py` | 85 | `db_connection` + `check_db_health` duplicated on almost every method |
| `test_rerank_skip.py` | 63 | Same 3-patch stack on 8+ methods |
| `test_cross_provider_fanout.py` | 48 | Same rerank/vector/fts stack repeated |

### Parametrization candidates
- `test_router_whitelist.py` — already uses `@pytest.mark.parametrize` heavily; could externalize query lists to JSON/YAML to reduce line count.
- `test_runpod_lifecycle.py` — B7 omission tests (4 tests → 1 parametrized), key-missing tests (2→1), provision failures (4→1 parametrized or table-driven).
- `test_hybrid.py` — `TestRerankPenalties` tests all share identical `mock_get_reranker` setup; could be a single parametrized test with `(query, items, expected_first_repo, expected_penalty_repo)` tuples.
- `test_train_docs_embedder.py` — MIN_TRAIN_ROWS guard tests (`test_train_aborts_if_too_few_rows`, `test_train_aborts_message_calls_out_legacy_dataset`, `test_train_logs_absolute_train_path_at_startup`, `test_train_logs_first_three_row_queries`) all use the same `_StopAfterGuard` / `_Poison` pattern.
- `test_bench_v2.py` — intent classifier tests (`test_intent_doc_by_md_extension`, `test_intent_doc_by_rules_keyword`, etc.) are 11 one-line assertions that could be a single parametrized test.

## Quick Wins

1. **Extract `_mock_wiring` to `conftest.py`** — Used by 4 hybrid-related test files; eliminates 16 lines per file.
2. **Extract `_make_sr` to `conftest.py`** — Used by 3 files; standardize `SearchResult` fixture generation.
3. **Create `tests/_import_script(path)` helper** — Replace the `sys.path.insert(0, str(REPO_ROOT))` + `importlib.util.spec_from_file_location` + `exec_module` boilerplate in ~10 files with a one-liner.
4. **Parametrize B7 omission tests in `test_runpod_lifecycle.py`** — `test_start_pod_body_omits_idle_timeout`, `test_start_pod_body_omits_termination_time`, `test_start_pod_body_omits_minvcpu_minmemory` → single `@pytest.mark.parametrize` test.
5. **Parametrize key-missing tests in `test_runpod_lifecycle.py`** — `test_cost_guard_aborts_when_key_missing` + `test_pod_lifecycle_aborts_when_key_missing` → one parametrized test with `(module, exception_cls)`.
6. **Extract `_run_main_capture` in `test_benchmark_doc_intent.py`** — Already defined as a helper; used by 4 tests but the helper itself is good—just ensure it's not copy-pasted elsewhere.
7. **Consolidate MIN_TRAIN_ROWS guard tests in `test_train_docs_embedder.py`** — The 4 guard tests share the same `_Poison` / `_StopAfterGuard` monkeypatch setup; extract a fixture.
8. **Extract `DaemonHandler` builder in `test_daemon.py`** — The 4 manual constructions differ only in body bytes/headers; a `_make_handler(body_bytes, headers)` would cut ~60 lines.
9. **Split `test_eval_file_gt.py`** — It tests `eval_finetune` helpers, `eval_verdict` v2 logic, AND `prepare_finetune_data` sampling. Split into 3 focused files.
10. **Move `test_chunking.py` imports to `src.index.builders`** — Stop testing through the `scripts.build.build_index` re-export stub; import directly from the source.
