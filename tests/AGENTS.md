# tests/ — Navigation Catalog

> **Parent:** [[../AGENTS.md|↑ Root Catalog]]  
> **Scope:** Test suite — unit, integration, benchmark tests

## Structure

```
tests/
├── conftest.py              # Shared fixtures (db, tmp_path profiles)
├── test_analyze.py          # analyze_task core logic
├── test_bench_v2.py         # Benchmark v2 infrastructure
├── test_benchmark_doc_intent.py  # Doc intent benchmarks
├── test_build_combined_train.py  # Training data builder
├── test_build_train_pairs.py     # Pair builder
├── test_cache.py
├── test_chunking.py
├── test_code_intent_eval.py
├── test_eval_file_gt.py
├── test_eval_jidm.py
├── test_eval_verdict.py
├── test_fts_preclean.py
├── test_listwise_conversion.py
├── test_lpt_schedule.py
├── test_merge_dual_judge_labels.py
├── test_prepare_finetune_data.py
├── test_prepare_train_data.py
├── test_rerank_pointwise_eval.py
├── test_router_whitelist.py
├── test_sample_real_queries.py
├── test_scripts_common.py
├── test_search_service.py
├── test_train_docs_embedder.py
├── test_train_reranker_ce.py
├── test_validate_provider_paths.py
├── test_vector.py
├── smoke_search.py          # 2026-05-22: search-quality regression baseline (12 tests, 60s wall)
└── ... (30+ more)
```

## Coverage Map

| Test File | Tests | Target Module |
|-----------|-------|---------------|
| `test_search_service.py` | 45+ | `src/search/service.py` |
| `test_analyze.py` | 30+ | `src/tools/analyze/` |
| `test_benchmark_doc_intent.py` | 25+ | `scripts/bench/benchmark_doc_intent.py` |
| `test_build_combined_train.py` | 15 | `scripts/build/build_combined_train.py` |
| `test_vector.py` | 10 | `src/search/vectors.py` |
| `smoke_search.py` | 12 | End-to-end search quality (loads DB + reranker) |

## Running Tests

```bash
make test                    # Full suite
pytest tests/ -q             # Quiet mode
pytest tests/test_search_service.py -v   # Single file
pytest tests/ --collect-only             # List without running
pytest tests/smoke_search.py -v          # Quality regression baseline (60s)
```

## Search quality smoke suite (`tests/smoke_search.py`)

12 canonical queries asserting expected top-3 files for real engineering
tasks (paypal disputes, webhook signature, merchant onboarding, etc.) plus
2 negative-noise sanity tests + 1 PI-56 hard-filter regression test.

Built 2026-05-22 as a baseline so future env tweaks can be measured against
known-good behavior. Wired into `scripts/full_update.sh` tail — runs after
every cron rebuild and logs to `logs/post_rebuild_smoke.log`.

If a test fails after a code change, the output shows actual top-5 → diff
makes the regression obvious.

## Conventions

- Tests importing moved scripts use categorized paths: `scripts.bench.*`, `scripts.build.*`
- `conftest.py` provides `db` fixture with in-memory SQLite
- Benchmark tests mock GitHub API to avoid timeouts

## Backlinks

- [[../AGENTS.md|Root Catalog]] — top-level overview
- [[../src/AGENTS.md|src/]] — source code under test
- [[../scripts/AGENTS.md|scripts/]] — scripts under test
