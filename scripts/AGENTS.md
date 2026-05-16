# scripts/ — Navigation Catalog

> **Parent:** [[../AGENTS.md|↑ Root Catalog]]  
> **Scope:** Build scripts, benchmarks, evals, analysis, maintenance

## Directory Tree

```
scripts/
├── _common.py              # Shared utilities (DaemonError, setup_paths)
├── build_vectors.py        # LanceDB embeddings builder (root — Makefile refs)
├── health_check_agents_md.py  # AGENTS.md validation (root — test refs)
├── full_update.sh          # Full pipeline entry point (Makefile)
├── clone_repos.sh          # Shallow-clone GitHub org repos
├── build/                  # Index, graph, vector builders
│   ├── build_index.py
│   ├── build_graph.py
│   ├── build_vectors.py
│   ├── build_env_index.py
│   ├── build_audit_context.py
│   ├── build_clean_jira_eval.py
│   ├── build_code_eval.py
│   ├── build_combined_train.py
│   ├── build_docs_vectors.py
│   ├── build_internal_traces.py
│   ├── build_rerank_pointwise_eval.py
│   ├── build_shadow_types.py
│   └── build_train_pairs_v2.py
├── bench/                  # Benchmarks
│   ├── bench_utils.py
│   ├── bench_v2_gate.py
│   ├── benchmark_bench_v2.py
│   ├── benchmark_doc_indexing_ab.py
│   ├── benchmark_doc_intent.py
│   ├── benchmark_file_recall.py
│   ├── benchmark_flows.py
│   ├── benchmark_investigation.py
│   ├── benchmark_queries.py
│   ├── benchmark_realworld.py
│   ├── benchmark_recall.py
│   ├── benchmark_rerank_ab.py
│   ├── local_code_bench.py
│   └── sample_bench_v2.py
├── eval/                   # Eval harnesses
│   ├── bootstrap_eval_ci.py
│   ├── eval_finetune.py
│   ├── eval_harness.py
│   ├── eval_jidm.py
│   ├── eval_verdict.py
│   └── eval_verdict.py
├── analysis/               # Analytics, churn, mining
│   ├── analyze_calls.py
│   ├── analyze_feedback.py
│   ├── analyze_session_quality.py
│   ├── autoresearch_eval.py
│   ├── autoresearch_loop.py
│   ├── detect_blind_spots.py
│   ├── detect_doc_staleness.py
│   ├── gen_repo_facts.py
│   ├── method_level_gaps.py
│   ├── mine_co_changes.py
│   ├── predict_failures.py
│   ├── proactivity_eval.py
│   └── semantic_gap_scorer.py
├── maint/                  # Maintenance, validation
│   ├── generate_housekeeping_report.py
│   ├── validate_doc_anchors.py
│   ├── validate_doc_file_line_refs.py
│   ├── validate_doc_frontmatter.py
│   ├── validate_doc_related_repos.py
│   ├── validate_doc_size.py
│   ├── validate_gaps.py
│   ├── validate_overlay_vs_proto.py
│   ├── validate_provider_paths.py
│   └── validate_recipe.py
├── data/                   # Data prep, finetune
│   ├── convert_to_listwise.py
│   ├── dedup_docs_lance.py
│   ├── embed_missing_vectors.py
│   ├── finetune_reranker.py
│   ├── label_v12_candidates_minilm.py
│   ├── local_smoke_candidates.py
│   ├── merge_dual_judge_labels.py
│   ├── merge_eval_shards.py
│   ├── prepare_finetune_data.py
│   ├── sample_real_queries.py
│   ├── v12_candidates.py
│   └── v12_candidates_regen_doc.py
├── scrape/                 # Doc scraping
│   ├── extract_artifacts.py
│   ├── finalize_scrape.py
│   └── tavily-docs-crawler.py
└── runpod/                 # RunPod training pipeline
    ├── train_docs_embedder.py
    ├── pod_lifecycle.py
    └── pod_watcher.py
```

## Entry Points

| Script | Called By | Purpose |
|--------|-----------|---------|
| `full_update.sh` | `make build`, `make update` | Full / incremental pipeline |
| `build_vectors.py` | `make switch-model` | Rebuild embeddings |
| `clone_repos.sh` | `full_update.sh` | Shallow clone org repos |
| `health_check_agents_md.py` | Manual, CI | Validate AGENTS.md files |

## Conventions

- All scripts use `_common.py` for path setup and error handling
- Profile scripts (from `profiles/pay-com/scripts/`) are symlinked to root `scripts/`
- Benchmark scripts write to `bench_runs/` (timestamped or named)
- Eval scripts read from `profiles/{name}/benchmarks.yaml`

## Backlinks

- [[../AGENTS.md|Root Catalog]] — top-level overview, storage, profiles
- [[../src/AGENTS.md|src/]] — core source code
- [[../tests/AGENTS.md|tests/]] — test structure
