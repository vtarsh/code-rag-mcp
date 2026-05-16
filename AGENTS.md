# AGENTS.md — code-rag-mcp Root Catalog

> Generic RAG system for indexing any GitHub org's codebase and serving it through an MCP server.
> Maintainer: [vtarsh](https://github.com/vtarsh) (personal account) · `vtarsh/code-rag-mcp`
> Stack: Python 3.12, FastMCP, SQLite FTS5, LanceDB, SentenceTransformer / CrossEncoder reranker.

---

## Overview

This repo provides a **model-context-protocol (MCP) RAG server** that indexes codebases, documentation, and dependency graphs for a GitHub organization, then exposes search, analysis, and tracing tools to AI agents. It is designed to be **org-agnostic**: all organization-specific data lives in `profiles/{name}/`, while `src/` contains fully generic mechanisms.

The runtime is split into two processes: [`daemon.py`](daemon.py) is a persistent HTTP server (~1.4 GB RAM) that holds embedding and reranker models in memory and serves tool endpoints on `localhost:8742`; [`mcp_server.py`](mcp_server.py) is a thin stdio MCP proxy (~20 MB) that forwards every tool call to the daemon and auto-starts it if needed. A separate [`cli.py`](cli.py) HTTP client lets sub-agents without MCP access call the same tools. There are **11 MCP tools** (search, analyze_task, trace_field, trace_chain, trace_flow, trace_impact, trace_internal, repo_overview, list_repos, provider_type_map, health_check) and **5 daemon-only tools** (find_dependencies, context_builder, visualize_graph, diff_provider_config, search_task_history).

---

## Navigable Index

### Profiles
- [[profiles/pay-com/AGENTS.md|Pay-Com Profile Catalog]] ([profiles/pay-com/AGENTS.md](profiles/pay-com/AGENTS.md)) — active production profile
- `profiles/example/` — small template profile (no separate AGENTS.md)

### Core Sections in This File
- [Directory Tree](#directory-tree) — layout with one-line descriptions
- [Storage Classification](#storage-classification) — git vs generated
- [Entry Points](#entry-points) — how to run / build / test
- [Key Files](#key-files) — start here for orientation
- [Profile System](#profile-system) — org-specific data model
- [Scripts Catalog](#scripts-catalog) — build, bench, analysis, ML
- [Conventions](#conventions) — naming, hooks, LFS, lint
- [Dead / Legacy](#dead--legacy) — files to avoid or delete
- [Tests Structure](#tests-structure) — how to run and what's covered
- [Open Questions](#open-questions) — ambiguities discovered during research

### Claude-Code Meta-Infrastructure
- [[.claude/rules/conventions.md|Generic Rules]] — always-loaded agent conventions
- [[.claude/docs/data-changes.md|Build Pipeline Reference]] — rebuild constraints & pipeline order
- [[.claude/docs/workflow-cycles.md|Workflow Cycles]] — task validation + continuous improvement loops
- [[.claude/docs/development-pattern.md|Development Pattern]] — capability ladder, pre-flight checklist
- [[.claude/agents/deep-analysis.md|Deep-Analysis Agent]] — independent Jira task analyzer
- [[.claude/agents/pattern-miner.md|Pattern-Miner Agent]] — task-history pattern miner
- [[.claude/skills/collect-tasks/SKILL.md|/collect-tasks Skill]] — Jira task collection
- [[.claude/skills/deep-analysis/SKILL.md|/deep-analysis Skill]] — deep analysis launcher
- [[.claude/skills/pattern-mine/SKILL.md|/pattern-mine Skill]] — pattern mining launcher
- [[.claude/skills/recall-test/SKILL.md|/recall-test Skill]] — benchmark suite launcher
- [[.claude/skills/scrape-docs/SKILL.md|/scrape-docs Skill]] — provider doc scraper

### Key Docs
- [`README.md`](README.md) — human quick-start
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system design, two-repo layout, 10 generic mechanisms
- [`TESTING.md`](TESTING.md) — recall methodology, ground-truth construction, agent validation
- [`ROADMAP.md`](ROADMAP.md) — chronological dev diary (550+ lines)
- [`NEXT_SESSION_PROMPT.md`](NEXT_SESSION_PROMPT.md) — session handoff doc

---

## Directory Tree

```text
~/.code-rag-mcp/
├── AGENTS.md                 ← this file
├── README.md                 human quick-start
├── ARCHITECTURE.md           system design & module map
├── TESTING.md                recall measurement methodology
├── ROADMAP.md                dev diary
├── NEXT_SESSION_PROMPT.md    session handoff
├── CLAUDE.md                 ~~legacy Claude-specific guidance~~ (deleted, superseded by this file)
│
├── mcp_server.py             thin stdio MCP proxy (auto-starts daemon)
├── daemon.py                 persistent HTTP server with ML models
├── cli.py                    bash HTTP client for daemon tools
├── setup_wizard.py           interactive profile creation wizard
├── pyproject.toml            deps, ruff, pytest config
├── Makefile                  build / test / health / register targets
├── .gitignore                generated data + secrets exclusions
├── .pre-commit-config.yaml   ruff + pytest + doc validators
│
├── src/                      core Python modules (generic, org-agnostic)
│   ├── __init__.py
│   ├── cache.py              LRU+TTL query cache with runtime stats
│   ├── config.py             profile loading, paths, glossary expansion
│   ├── container.py          DI container (SQLite, embeddings, LanceDB)
│   ├── embedding_provider.py local SentenceTransformer + CrossEncoder
│   ├── feedback.py           JSONL query-result logger for tuning
│   ├── formatting.py         snippet cleaning for search display
│   ├── js_field_extractor.py scans provider JS for field usage
│   ├── models.py             embedding model registry (coderank, minilm, docs)
│   ├── proto_parser.py       regex-based protobuf parser (no protoc)
│   ├── types.py              Pydantic contracts for results / edges / outputs
│   ├── search/               search pipeline
│   │   ├── code_facts.py     FTS5 over code_facts (schemas, guards, env)
│   │   ├── env_vars.py       targeted env var retrieval
│   │   ├── fts.py            FTS5 keyword search + abbreviation expansion
│   │   ├── hybrid.py         RRF fusion + CrossEncoder rerank + doc-intent routing
│   │   ├── service.py        MCP search_tool registration
│   │   ├── suggestions.py    zero-result fallback suggestions
│   │   └── vector.py         LanceDB vector similarity (two-tower)
│   ├── graph/                dependency graph
│   │   ├── queries.py        BFS, shortest path, hub-penalty traversal
│   │   ├── service.py        MCP graph tools (trace_impact, trace_flow, etc.)
│   │   └── builders/         edge builders (14 modules; see appendix below)
│   ├── index/                index builders
│   │   └── builders/         chunkers, indexers, orchestrator, memguard (18 modules; see appendix below)
│   └── tools/                MCP tools
│       ├── analyze/          analyze_task package (11 modules)
│       ├── context.py        context_builder tool
│       ├── fields.py         trace_field tool
│       ├── service.py        utility tools (repo_overview, list_repos, …)
│       └── shadow_types.py   provider_type_map tool
│
├── tests/                    test suite (62 test files + conftest.py + __init__.py)
│   ├── conftest.py           shared pytest fixtures
│   ├── test_*.py             grouped: search, graph, index, analyze, core, daemon, integration, benchmark, data-pipeline, script-utils
│
├── scripts/                  ~116 scripts + helpers
│   ├── _common.py            shared boilerplate (setup_paths, daemon_post)
│   ├── bench_utils.py        shared benchmark utilities
│   ├── build_index.py        thin entry → src.index.builders
│   ├── build_vectors.py      code-tower vector build (LanceDB)
│   ├── build_docs_vectors.py docs-tower vector build (two-tower)
│   ├── build_graph.py        dependency graph build
│   ├── full_update.sh        master pipeline (clone→extract→index→graph→vectors)
│   ├── clone_repos.sh        shallow-clone all org repos
│   ├── extract_artifacts.py  phase-1 artifact extractor
│   ├── analyze_calls.py      MCP call log analyzer
│   ├── analyze_churn.py      reranker churn post-processor
│   ├── benchmark_*.py        benchmark harnesses
│   ├── eval_*.py             evaluation / verdict / harness scripts
│   ├── finetune_reranker.py  CrossEncoder fine-tuning
│   ├── prepare_finetune_data.py  train data from Jira GT
│   └── runpod/               RunPod training orchestrators
│       ├── full_pipeline.py  e2e RunPod pipeline
│       ├── oneshot_docs.py   one-shot docs-tower train
│       ├── oneshot_rerank.py one-shot reranker train
│       ├── pod_lifecycle.py  RunPod CLI (start/stop/status)
│       ├── train_docs_embedder.py  docs embedder fine-tune
│       └── train_reranker_ce.py    CrossEncoder fine-tune on pod
│
├── profiles/                 org-specific data (gitignored except example/)
│   ├── example/              template profile
│   ├── my-org/               placeholder
│   └── pay-com/              active production profile (private repo clone)
│
├── db/                       generated — SQLite + LanceDB vector stores
│   ├── code_index.db         FTS5 + graph SQLite DB
│   ├── knowledge.db          alias / task-history DB
│   ├── vectors.lance.coderank/   code-tower LanceDB
│   └── vectors.lance.docs/       docs-tower LanceDB
│
├── raw/                      generated — cloned repo artifacts (gitignored)
├── extracted/                generated — extracted chunks / configs (gitignored)
├── logs/                     generated — daemon.log, tool_calls.jsonl (gitignored)
├── bench_runs/               generated — benchmark JSON dumps (gitignored, few baselines tracked)
├── models/                   ~~generated — fine-tuned reranker artifacts~~ (directory absent; artifacts live in profiles/pay-com/models/)
├── .secrets/                 gitignored — API keys, tokens
├── .ruff_cache/              generated lint cache
├── .pytest_cache/            generated test cache
└── .claude/                  agent infrastructure (mostly git-tracked)
    ├── rules/                always-loaded conventions
    ├── docs/                 build pipeline, workflow, dev pattern refs
    ├── agents/               agent definitions (generic + pay-com scoped)
    ├── skills/               slash-command skill packages
    ├── debug/                ephemeral debug artifacts (git-tracked but ruff-excluded)
    ├── fix/                  historical fixed-issue records
    ├── worktrees/            ephemeral per-session branches (gitignored)
    ├── plans/                planning docs (tracked — agents-md-rollout.md)
    └── research/             research shards for AGENTS.md rollout
```

---

## Storage Classification

Every top-level item is classified so agents know what is safe to edit versus what is generated.

| Path | Classification | Notes |
|------|----------------|-------|
| `AGENTS.md` | **git** | this file |
| `README.md`, `ARCHITECTURE.md`, `TESTING.md`, `ROADMAP.md`, `NEXT_SESSION_PROMPT.md` | **git** | curated docs |
| `mcp_server.py`, `daemon.py`, `cli.py`, `setup_wizard.py` | **git** | entry points |
| `pyproject.toml`, `Makefile`, `.gitignore`, `.pre-commit-config.yaml` | **git** | project config |
| `src/` | **git** | 71 Python modules, all tracked |
| `tests/` | **git** | 62 test files + `conftest.py` + `__init__.py`, all tracked |
| `scripts/` | **hybrid** | ~72 tracked, ~44 gitignored (global rule + untracked); see [Scripts Catalog](#scripts-catalog) per-file |
| `profiles/example/` | **git** | template profile |
| `profiles/my-org/` | **untracked** | placeholder directory; no files in git index |
| `profiles/pay-com/` | **gitignored** | Private repo clone (`vtarsh/code-rag-mcp-profile`); entire directory ignored by root `.gitignore`. Curated docs *should* be tracked but currently are not in the public repo index. |
| `db/` | **gitignored generated** | SQLite + LanceDB recreated by build pipeline |
| `raw/` | **gitignored generated** | cloned repo artifacts |
| `extracted/` | **gitignored generated** | extracted chunks / configs |
| `logs/` | **gitignored generated** | runtime logs, tool_calls.jsonl |
| `bench_runs/` | **hybrid** | mostly untracked JSON dumps; a few early baselines tracked |
| `models/` | **hybrid** | mostly untracked fine-tuned artifacts; a few metadata files tracked |
| `.secrets/` | **gitignored** | API keys, tokens, credentials |
| `.ruff_cache/`, `.pytest_cache/` | **gitignored generated** | lint / test caches |
| `.claude/rules/`, `.claude/docs/`, `.claude/agents/`, `.claude/skills/` | **git** | agent infrastructure |
| `.claude/debug/` | **git** (ruff-excluded) | ephemeral debug artifacts (149 tracked files) |
| `.claude/fix/` | **git** | historical fixed-issue records |
| `.claude/worktrees/` | **gitignored** | ephemeral per-session branches |
| `.claude/plans/` | **git** | planning docs (agents-md-rollout.md tracked) |
| `config.json` (root) | **gitignored** | legacy root config (migrated to profile) |
| `.active_profile` | **gitignored** | runtime state file |
| `repo_state.json`, `clone_log.json`, `extract_log.json` | **gitignored** | runtime state |
| `benchmark_*.json`, `blind_spots_results.json` | **gitignored** | benchmark outputs |
| `ab_test_baseline.json`, `eval_baseline.json`, `patterns-export.json` | **gitignored** | **CONFIRMED dead** (see [Dead / Legacy](#dead--legacy)) |

---

## Entry Points

### Development / Day-to-Day

```bash
# Run tests (Python 3.12 required)
python3.12 -m pytest tests/ -q

# Start the daemon directly (normally auto-started by mcp_server.py or launchd)
python daemon.py

# MCP proxy (stdio → HTTP localhost:8742)
python mcp_server.py

# CLI client for sub-agents without MCP access
python cli.py <tool> <args>
```

### Build Pipeline

```bash
# Full rebuild (~2-4h, peaks ~20GB RAM; run overnight on 16GB Macs)
make build
# Equivalent: ACTIVE_PROFILE=pay-com ./scripts/full_update.sh --full

# Incremental update (only changed repos; ~30-60 min typical)
make update
# Equivalent: ACTIVE_PROFILE=pay-com ./scripts/full_update.sh

# Rebuild vectors with a different embedding model
make switch-model MODEL=minilm

# Clean generated data (db/, raw/, extracted/, logs/)
make clean
```

### Make Targets

| Target | Purpose |
|--------|---------|
| `make init` | Run [`setup_wizard.py`](setup_wizard.py) for a new profile |
| `make build` | Full pipeline: clone → extract → index → vectors → graph |
| `make update` | Incremental update (changed repos only) |
| `make test` | Run full pytest suite |
| `make health` | Run daemon health check tool |
| `make register` | Register MCP server in Claude Code `settings.json` |
| `make profile PROFILE=pay-com` | Write `.active_profile` |

### Setup Wizard

[`setup_wizard.py`](setup_wizard.py) creates a new profile interactively (org name, npm scope, embedding model), registers the MCP server in `~/.claude/settings.json`, and optionally installs a launchd plist. Non-interactive mode is supported via CLI flags.

### Daemon Lifecycle

```bash
# Hard restart (kill + restart)
kill -9 $(lsof -ti:8742); sleep 2
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 daemon.py &disown
```

---

## Key Files

Read these first when orienting to the codebase.

| File | Why It Matters |
|------|----------------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | System design, two-repo layout (public + private), `analyze_task` package decomposition, data flow, 10 generic mechanisms, conventions.yaml keys, build pipeline invariants |
| [`TESTING.md`](TESTING.md) | Recall measurement methodology, ground-truth construction from `task_history`, benchmark commands, agent-based validation, auto-collection, `benchmarks.yaml` format |
| [`README.md`](README.md) | Human-facing quick-start: install, build, tools list, profiles, embedding models, troubleshooting |
| [`pyproject.toml`](pyproject.toml) | Project metadata, dependencies (`mcp`, `lancedb`, `sentence-transformers`, `pydantic`, `PyYAML`), ruff (target py312, line-length 120), pytest config |
| [`src/config.py`](src/config.py) | Profile loading, paths, domain glossary expansion — single source of truth for config values |
| [`src/container.py`](src/container.py) | DI container: SQLite connections, embedding/reranker providers, LanceDB tables (per-key singletons for two-tower) |
| [`mcp_server.py`](mcp_server.py) | Thin stdio MCP proxy; forwards all tool calls to daemon; auto-starts daemon |
| [`daemon.py`](daemon.py) | Persistent HTTP server holding ML models; serves `/tool/<name>`, `/health`, `/admin/unload`, `/admin/shutdown` |

---

## Profile System

Organization-specific data lives in `profiles/{name}/`. The generic `src/` code reads the active profile at runtime and loads org-specific configs, docs, scripts, and ground-truth data from that directory.

### Activation

1. `ACTIVE_PROFILE` environment variable (highest priority)
2. `.active_profile` file in repo root (written by `make profile`)
3. Falls back to `example` if neither is set

### Profile Structure

```text
profiles/{name}/
├── config.json              org config (repo list, glossary, conventions.yaml path)
├── AGENTS.md                ← profile-level catalog (see pay-com)
├── docs/
│   ├── gotchas/             runtime traps
│   ├── references/          stable structural knowledge
│   ├── flows/               payment / business flows
│   ├── dictionary/          domain term definitions
│   └── providers/           scraped provider API docs (git-lfs)
├── scripts/                 org-specific data-collection scripts
├── benchmarks.yaml          eval query definitions
├── RECALL-TRACKER.md        baseline scores + improvement log
└── … (generated artifacts: bench/, models/, traces/, etc.)
```

### Active Profile: pay-com

- **Path:** `profiles/pay-com/`
- **Catalog:** [[profiles/pay-com/AGENTS.md|Pay-Com Profile Catalog]] ([profiles/pay-com/AGENTS.md](profiles/pay-com/AGENTS.md))
- **Origin:** private repo clone (`vtarsh/code-rag-mcp-profile`)
- **Note:** `profiles/pay-com/scripts/` contains 25 real Python files (tracked-but-ignored — force-added before the root `.gitignore` rule took effect). They contain org-specific repo names, Jira tokens, and provider logic.

Other profiles:
- `profiles/example/` — small template, sufficient for bootstrapping a new org
- `profiles/my-org/` — placeholder

---

## Scripts Catalog

> **Storage anomaly:** `~/.gitignore_global` ignores `scripts/` by default, so the directory is **hybrid**: ~72 files are force-tracked in git, ~44 are gitignored / untracked. The badge `(git)` or `(gitignored)` is noted below where relevant.

### Build Pipeline (index, vectors, graph)

| Script | Description |
|--------|-------------|
| [`scripts/build/build_index.py`](scripts/build/build_index.py) | Thin entry → `src.index.builders.build_index()` |
| [`scripts/build_vectors.py`](scripts/build_vectors.py) | Code-tower vectors (CodeRankEmbed / MiniLM) in LanceDB |
| [`scripts/build/build_docs_vectors.py`](scripts/build/build_docs_vectors.py) | Docs-tower vectors (nomic-embed-text-v1.5) |
| [`scripts/build/build_graph.py`](scripts/build/build_graph.py) | Dependency graph into `knowledge.db` |
| [`scripts/data/embed_missing_vectors.py`](scripts/data/embed_missing_vectors.py) | Incremental backfill of missing chunk vectors |
| [`scripts/scrape/extract_artifacts.py`](scripts/scrape/extract_artifacts.py) | Phase-1 artifact extractor (proto, docs, config, env, k8s, workflows, webhooks, CI) |
| [`scripts/build/build_env_index.py`](scripts/build/build_env_index.py) | Parse `.env.example` / `consts.js` into `env_vars` table |
| [`scripts/build/build_internal_traces.py`](scripts/build/build_internal_traces.py) | Parse CommonJS `require()` chains into `internal_traces` |
| [`scripts/full_update.sh`](scripts/full_update.sh) | Master pipeline: lockfile → clone → extract → index → graph → vectors → docs vectors → blind-spot detection |
| [`scripts/clone_repos.sh`](scripts/clone_repos.sh) | Shallow-clone all repos from a GitHub org |
| [`scripts/run_with_timeout.sh`](scripts/run_with_timeout.sh) | Portable `timeout` for macOS (called by `full_update.sh`) |
| [`scripts/gen_repo_facts.py`](scripts/gen_repo_facts.py) | Machine-derivable repo metadata (consumed by staleness detector) |

### Benchmarking / Evaluation

| Script | Description |
|--------|-------------|
| [`scripts/bench/benchmark_bench_v2.py`](scripts/bench/benchmark_bench_v2.py) | Run `bench_v2` eval and emit metrics JSON |
| [`scripts/bench/bench_v2_gate.py`](scripts/bench/bench_v2_gate.py) | Regression gate: exits 1 on >2pp regression |
| [`scripts/bench/sample_bench_v2.py`](scripts/bench/sample_bench_v2.py) | Stratified sampler from real MCP traffic |
| [`scripts/eval/bootstrap_eval_ci.py`](scripts/eval/bootstrap_eval_ci.py) | Paired bootstrap 95% CI for reranker deltas |
| [`scripts/bench/benchmark_flows.py`](scripts/bench/benchmark_flows.py) | Flow-based query benchmark |
| [`scripts/bench/benchmark_queries.py`](scripts/bench/benchmark_queries.py) | Conceptual query benchmark |
| [`scripts/bench/benchmark_realworld.py`](scripts/bench/benchmark_realworld.py) | Real-world scenario benchmark |
| [`scripts/bench/benchmark_recall.py`](scripts/bench/benchmark_recall.py) | Recall-focused benchmark |
| [`scripts/bench/benchmark_file_recall.py`](scripts/bench/benchmark_file_recall.py) | File-level recall benchmark |
| [`scripts/analysis/autoresearch_eval.py`](scripts/analysis/autoresearch_eval.py) | Rank-sensitive MRR eval for autoresearch loop |
| [`scripts/analysis/autoresearch_loop.py`](scripts/analysis/autoresearch_loop.py) | Karpathy-style RAG scalar tuning loop |
| [`scripts/analysis/detect_blind_spots.py`](scripts/analysis/detect_blind_spots.py) | Detect structurally important but search-invisible repos |
| [`scripts/eval/eval_verdict.py`](scripts/eval/eval_verdict.py) | **SSOT** for reranker eval verdict logic |
| [`scripts/eval/eval_finetune.py`](scripts/eval/eval_finetune.py) | Full fine-tune evaluation with shard support |
| [`scripts/data/merge_eval_shards.py`](scripts/data/merge_eval_shards.py) | Merge N shard JSONs into one snapshot |
| [`scripts/eval/eval_jidm.py`](scripts/eval/eval_jidm.py) | IM-NDCG judge-free direction metric |
| [`scripts/eval/eval_harness.py`](scripts/eval/eval_harness.py) | Blind eval harness for `analyze_task` |
| [`scripts/analysis/ab_lost_tickets.py`](scripts/analysis/ab_lost_tickets.py) | A/B analysis on lost tickets |
| [`scripts/analysis/proactivity_eval.py`](scripts/analysis/proactivity_eval.py) | Does `analyze_task` flag known reviewer bugs? |
| [`scripts/sanity_v2_gate.py`](scripts/sanity_v2_gate.py) | Sanity-check v1 vs v2 verdict flips |

### Analysis / Auditing

| Script | Description |
|--------|-------------|
| [`scripts/analysis/analyze_calls.py`](scripts/analysis/analyze_calls.py) | MCP call log analysis: usage, frequency, timing, sessions |
| [`scripts/analysis/analyze_churn.py`](scripts/analysis/analyze_churn.py) | Post-process churn replay: slice metrics, export diff-pairs |
| [`scripts/analysis/churn_replay.py`](scripts/analysis/churn_replay.py) | Top-K churn replay between two rerankers |
| [`scripts/analysis/churn_reranker_judge.py`](scripts/analysis/churn_reranker_judge.py) | Neutral MiniLM judge for churn diff pairs (zero API cost) |
| [`scripts/analysis/churn_p1c_validate.py`](scripts/analysis/churn_p1c_validate.py) | Re-run v8 with P1c pipeline; measure ranking flips |
| [`scripts/analysis/analyze_feedback.py`](scripts/analysis/analyze_feedback.py) | Search feedback log analysis |
| [`scripts/analysis/analyze_session_quality.py`](scripts/analysis/analyze_session_quality.py) | Session quality from `tool_calls.jsonl` |
| [`scripts/maint/validate_recipe.py`](scripts/maint/validate_recipe.py) | Validate recipe repo predictions against ground truth |
| [`scripts/analysis/detect_doc_staleness.py`](scripts/analysis/detect_doc_staleness.py) | Flag curated docs with newer upstream commits |
| [`scripts/maint/generate_housekeeping_report.py`](scripts/maint/generate_housekeeping_report.py) | Docs housekeeping report (duplicates, divergences, reciprocity) |
| [`scripts/scrape/finalize_scrape.py`](scripts/scrape/finalize_scrape.py) | Post-`/scrape-docs` validator / auto-injector |
| [`scripts/build/build_audit_context.py`](scripts/build/build_audit_context.py) | Structured markdown context for audit agent prompts |

> **MCP Call Tracker:** Every tool call is logged to `logs/tool_calls.jsonl` by the daemon (tool name, args, duration, result preview, source). Use `scripts/analysis/analyze_calls.py` for usage summaries, `scripts/analysis/analyze_calls.py --last 20` for recent calls, and `scripts/analysis/analyze_calls.py --sessions` for per-session breakdown.

### Data Collection (Jira, CI, tasks)

Most are **symlinks** to `profiles/pay-com/scripts/` (gitignored). Non-symlink:

| Script | Description |
|--------|-------------|
| [`scripts/build/build_audit_context.py`](scripts/build/build_audit_context.py) | Structured audit context from `knowledge.db` + flow YAMLs |

### Fine-Tuning / ML

| Script | Description |
|--------|-------------|
| [`scripts/data/prepare_finetune_data.py`](scripts/data/prepare_finetune_data.py) | CrossEncoder fine-tune data from Jira GT |
| [`scripts/data/finetune_reranker.py`](scripts/data/finetune_reranker.py) | Fine-tune CrossEncoder (streaming, low-memory, MPS hygiene) |
| [`scripts/build/build_combined_train.py`](scripts/build/build_combined_train.py) | Combine code + docs training sources |
| [`scripts/build/build_shadow_types.py`](scripts/build/build_shadow_types.py) | Per-provider shadow-type YAML |
| [`scripts/data/label_v12_candidates_minilm.py`](scripts/data/v12_candidates.py) | Local MiniLM judge for candidate labeling |
| [`scripts/data/v12_candidates.py`](scripts/data/v12_candidates.py) | Gold-label candidate queue from real MCP queries |
| [`scripts/data/convert_to_listwise.py`](scripts/data/convert_to_listwise.py) | Pointwise → listwise format conversion |
| [`scripts/runpod/full_pipeline.py`](scripts/runpod/full_pipeline.py) | End-to-end RunPod pipeline (spawn → smoke → train → bench → push → stop) |
| [`scripts/runpod/oneshot_docs.py`](scripts/runpod/oneshot_docs.py) | One-shot docs-tower orchestrator |
| [`scripts/runpod/oneshot_rerank.py`](scripts/runpod/oneshot_rerank.py) | One-shot reranker orchestrator |
| [`scripts/runpod/pod_lifecycle.py`](scripts/runpod/pod_lifecycle.py) | RunPod CLI (start, stop, status, list, cost-guard) |
| [`scripts/runpod/train_docs_embedder.py`](scripts/runpod/train_docs_embedder.py) | Fine-tune docs embedder on pod |
| [`scripts/runpod/train_reranker_ce.py`](scripts/runpod/train_reranker_ce.py) | Fine-tune CrossEncoder on pod |

### Utilities / Helpers

| Script | Description |
|--------|-------------|
| [`scripts/_common.py`](scripts/_common.py) | Shared boilerplate: `setup_paths()`, `daemon_post()` |
| [`scripts/bench/bench_utils.py`](scripts/bench/bench_utils.py) | Shared benchmark utilities (imported by many bench scripts) |
| [`scripts/visualize_graph.py`](scripts/visualize_graph.py) | Generate `graph.html` from `knowledge.db` |
| [`scripts/graph_template.html`](scripts/graph_template.html) | Sigma.js WebGL template for graph viz |
| [`scripts/parse_jaeger_trace.py`](scripts/parse_jaeger_trace.py) | Parse Jaeger traces into compact summaries |
| [`scripts/data/sample_real_queries.py`](scripts/data/sample_real_queries.py) | Sample real MCP queries for eval labeling |
| [`scripts/analysis/mine_co_changes.py`](scripts/analysis/mine_co_changes.py) | Mine co-change rules from `task_history` |
| [`scripts/health_check_agents_md.py`](scripts/health_check_agents_md.py) | Validate AGENTS.md links, counts, storage, orphans |

### Additional Scripts Present on Disk

The following scripts exist but are not catalogued in detail above (some are gitignored, untracked, or niche):

| Script | Category |
|--------|----------|
| [`scripts/bench/benchmark_doc_indexing_ab.py`](scripts/bench/benchmark_doc_indexing_ab.py) | Benchmark |
| [`scripts/bench/benchmark_doc_intent.py`](scripts/bench/benchmark_doc_intent.py) | Benchmark |
| [`scripts/bench/benchmark_rerank_ab.py`](scripts/bench/benchmark_rerank_ab.py) | Benchmark |
| [`scripts/build/build_clean_jira_eval.py`](scripts/build/build_clean_jira_eval.py) | Build |
| [`scripts/build/build_code_eval.py`](scripts/build/build_code_eval.py) | Build |
| [`scripts/build/build_rerank_pointwise_eval.py`](scripts/build/build_rerank_pointwise_eval.py) | Build |
| [`scripts/build/build_train_pairs_v2.py`](scripts/build/build_train_pairs_v2.py) | Build |
| [`scripts/data/dedup_docs_lance.py`](scripts/data/dedup_docs_lance.py) | Index |
| [`scripts/docs_validate_all.sh`](scripts/docs_validate_all.sh) | Validation (launchd daily) |
| [`scripts/eval_parallel.sh`](scripts/eval_parallel.sh) | Evaluation |
| [`scripts/bench/local_code_bench.py`](scripts/bench/local_code_bench.py) | Benchmark |
| [`scripts/data/local_smoke_candidates.py`](scripts/data/local_smoke_candidates.py) | Benchmark |
| [`scripts/data/merge_dual_judge_labels.py`](scripts/data/merge_dual_judge_labels.py) | Evaluation |
| [`scripts/runpod/bench_large_models.py`](scripts/runpod/bench_large_models.py) | ML / RunPod |
| [`scripts/runpod/cost_guard.py`](scripts/runpod/cost_guard.py) | ML / RunPod |
| [`scripts/runpod/pod_watcher.py`](scripts/runpod/pod_watcher.py) | ML / RunPod |
| [`scripts/runpod/setup_env.sh`](scripts/runpod/setup_env.sh) | ML / RunPod |
| [`scripts/data/v12_candidates_regen_doc.py`](scripts/data/v12_candidates_regen_doc.py) | ML |
| [`scripts/maint/validate_doc_anchors.py`](scripts/maint/validate_doc_anchors.py) | Validation |
| [`scripts/maint/validate_doc_file_line_refs.py`](scripts/maint/validate_doc_file_line_refs.py) | Validation |
| [`scripts/maint/validate_doc_frontmatter.py`](scripts/maint/validate_doc_frontmatter.py) | Validation |
| [`scripts/maint/validate_doc_related_repos.py`](scripts/maint/validate_doc_related_repos.py) | Validation |
| [`scripts/maint/validate_doc_size.py`](scripts/maint/validate_doc_size.py) | Validation |
| [`scripts/maint/validate_overlay_vs_proto.py`](scripts/maint/validate_overlay_vs_proto.py) | Validation |
| [`scripts/maint/validate_provider_paths.py`](scripts/maint/validate_provider_paths.py) | Validation |

---

## Appendix: Builder Modules

### `src/graph/builders/` (14 modules)
[`__init__.py`](src/graph/builders/__init__.py), [`_common.py`](src/graph/builders/_common.py), [`db.py`](src/graph/builders/db.py), [`domain_edges.py`](src/graph/builders/domain_edges.py), [`express_edges.py`](src/graph/builders/express_edges.py), [`grpc_edges.py`](src/graph/builders/grpc_edges.py), [`k8s_edges.py`](src/graph/builders/k8s_edges.py), [`manual_edges.py`](src/graph/builders/manual_edges.py), [`npm_edges.py`](src/graph/builders/npm_edges.py), [`pkg_resolution.py`](src/graph/builders/pkg_resolution.py), [`proto_edges.py`](src/graph/builders/proto_edges.py), [`similarity_edges.py`](src/graph/builders/similarity_edges.py), [`temporal_edges.py`](src/graph/builders/temporal_edges.py), [`webhook_edges.py`](src/graph/builders/webhook_edges.py)

### `src/index/builders/` (18 modules)
[`__init__.py`](src/index/builders/__init__.py), [`_common.py`](src/index/builders/_common.py), [`_memguard.py`](src/index/builders/_memguard.py), [`code_chunks.py`](src/index/builders/code_chunks.py), [`code_facts.py`](src/index/builders/code_facts.py), [`config_chunks.py`](src/index/builders/config_chunks.py), [`cql_chunks.py`](src/index/builders/cql_chunks.py), [`db.py`](src/index/builders/db.py), [`detect.py`](src/index/builders/detect.py), [`dispatcher.py`](src/index/builders/dispatcher.py), [`docs_chunks.py`](src/index/builders/docs_chunks.py), [`docs_indexer.py`](src/index/builders/docs_indexer.py), [`docs_vector_indexer.py`](src/index/builders/docs_vector_indexer.py), [`incremental.py`](src/index/builders/incremental.py), [`orchestrator.py`](src/index/builders/orchestrator.py), [`proto_chunks.py`](src/index/builders/proto_chunks.py), [`raw_indexer.py`](src/index/builders/raw_indexer.py), [`repo_indexer.py`](src/index/builders/repo_indexer.py)

---

## Conventions

### Code Style
- **Target Python:** 3.12 (ParamSpec / `datetime.UTC` used)
- **Line length:** 120 (ruff config in `pyproject.toml`)
- **Lint / format:** ruff (`make test` runs ruff + pytest via `.pre-commit-config.yaml`)
- **Excluded from ruff:** `.claude/debug/` (`extend-exclude` in `pyproject.toml`)

### Git & Storage
- **No hardcoded org names in `src/`**: all org-specific data lives in `profiles/{name}/`
- **LFS filter:** `profiles/pay-com/docs/providers/**` tracked via git-lfs (4000+ scraped API doc files)
- **Global gitignore anomaly:** `~/.gitignore_global` contains `scripts/`, so many scripts must be force-added or were added before the rule
- **Generated dirs:** `db/`, `raw/`, `extracted/`, `logs/`, `bench_runs/`, `models/` are recreated by build pipeline — do not commit

### Pre-Commit Hooks (`.pre-commit-config.yaml`)
1. ruff — lint + format
2. pytest — `arch -arm64 python3.12 -m pytest tests/ -q --tb=short`
3. Manual-stage doc validators: anchors, related_repos, size, frontmatter, overlay-vs-proto

### Agent Rules (from [[.claude/rules/conventions.md]])
- Keep every search result — deprioritize low-confidence but include them
- Improve base recall before tuning reranker; reranker is polish, not fix
- Ground truth = `repos_changed` from Jira; cross-validate with `files_changed` and `pr_urls`
- Recall over precision: false negatives are worse than false positives
- Parallel agents by default; serialize any agents that touch the same DB or files
- Report what the tool found, not how full the context window is
- Crons either act on findings or exit silently — no report-only jobs

### Build Pipeline Order
```
extract_artifacts.py → build_index.py → build_graph.py → build_vectors.py → build_docs_vectors.py
```
- Full rebuild ~2–4h, peaks ~20GB RAM
- `build_index.py --incremental` re-indexes only changed repos (SHA comparison)
- Backup `task_history` before full rebuild (`build_index.py` recreates repos/chunks tables)

---

## Dead / Legacy

Items detected by cross-grep, git log, and Makefile analysis. Confidence levels: **CONFIRMED**, **SUSPECTED**, **OUTDATED**.

### CONFIRMED Dead

| Item | Evidence |
|------|----------|

### SUSPECTED Dead

| Item | Evidence |
|------|----------|
| `scripts/analysis/analyze_change_impact.py (DELETED)` | 719 LOC, zero imports, 8+ weeks old, exists but likely unused |
| `scripts/maint/audit_index_gaps.py (DELETED)` | 370 LOC, never committed, no imports, exists but likely unused |
| `scripts/bench/bench_routing_e2e.py (DELETED)` | Untracked, never committed, no imports, exists but likely unused |
| `scripts/build/build_eval_rebuild_bundle.py (DELETED)` | Untracked, never committed, no imports, exists but likely unused |
| `scripts/build/build_eval_v2_llm_calibrated.py (DELETED)` | Untracked, never committed, no imports, exists but likely unused |
| `scripts/data/rescore_against_clean.py (DELETED)` | Untracked, never committed, no imports (may be one-off CLI) |
| `scripts/maint/generate_housekeeping_report.py` | Untracked; only caller is `docs_validate_all.sh`, which itself lacks active CI hook evidence |
| `scripts/analysis/ab_lost_tickets.py` | Tracked, 3 weeks old, 1 reference only |
| `scripts/analysis/analyze_calls.py` | Tracked, 3 weeks old, 1 reference only |
| `scripts/analysis/analyze_session_quality.py` | Tracked, 5 weeks old, 1 reference only |
| `scripts/analysis/autoresearch_eval.py` | Tracked, 5 weeks old, 1 reference only |
| `scripts/bench/benchmark_file_recall.py` | Tracked, 4 weeks old, 1 reference only |
| `scripts/benchmark_investigation.py` | Tracked, 5 weeks old, 1 reference only |
| `ab_test_baseline.json` (root) | 125 B, 8+ weeks old, zero consumers, exists on disk |
| `eval_baseline.json` (root) | 502 B, 8+ weeks old, zero consumers, exists on disk |
| `patterns-export.json` (root) | 2.9 KB; pay-com scripts expect it in `~/.pay-knowledge/`, not repo root; exists on disk |
| `config.json` (root) | Legacy root config; `.gitignore` says "migrated to profile"; exists on disk |
| `profiles/pay-com/.archive/2026-04-17-cleanup/` | 18 gitignored files; only referenced by deprecated scan report; exists on disk |
| `.claude/fix/` (`F1_done.md`, `F2_done.md`, `F3_done.md`, `summary.md`) | Completed todo list; not referenced by active code; exists on disk |
| `setup_wizard.py` | Tracked, 8+ weeks old; still wired into `Makefile` but may be superseded by manual onboarding |

### OUTDATED (but might revive)

| Item | Evidence |
|------|----------|
| `NEXT_SESSION_PROMPT.md` | Tracked, last touch 3 weeks ago; referenced by tests and scripts, but content may be stale |
| `.claude/debug/` | 149+ tracked files; historical research context (April 26–29, 2026); not referenced by active code, but may aid understanding past decisions |
| `bench_runs/` | Mostly untracked; active directory, but older baseline files likely superseded |

### Note on Doc Validators
The five `scripts/validate_doc_*.py` scripts plus `validate_overlay_vs_proto.py` and `docs_validate_all.sh` are **gitignored and never committed**, yet they are **actively used** by `docs_validate_all.sh` (launchd daily run). Treat them as operational but ephemeral, not dead.

---

## Tests Structure

### Runner

```bash
python3.12 -m pytest tests/ -q
# Equivalent: make test
# Pre-commit runs: arch -arm64 python3.12 -m pytest tests/ -q --tb=short
```

### Test Groups

| Group | Files | Focus |
|-------|-------|-------|
| **Search** | `test_fts.py`, `test_fts_preclean.py`, `test_vector.py`, `test_hybrid.py`, `test_hybrid_doc_intent.py`, `test_search_service.py`, `test_suggestions.py`, `test_code_facts.py`, `test_env_vars.py`, `test_rerank_skip.py`, `test_router_whitelist.py`, `test_cross_provider_fanout.py` | FTS5, vector, hybrid RRF, reranking, doc-intent routing |
| **Graph** | `test_graph_queries.py` | BFS, path-finding |
| **Index / Builders** | `test_chunking.py`, `test_index_builders.py`, `test_docs_chunks.py`, `test_docs_vector_indexer.py`, `test_memguard.py`, `test_proto_parser.py`, `test_provider_doc_dedup.py` | Chunking, orchestration, streaming, memory guards |
| **Analyze** | `test_analyze.py`, `test_classifier.py`, `test_meta_guard.py`, `test_context.py`, `test_fields.py`, `test_shadow_types.py`, `test_shared_files_warning.py` | Task analysis, classification, memoization guard |
| **Core Modules** | `test_config.py`, `test_container.py`, `test_types.py`, `test_cache.py`, `test_singleflight.py` | Config, DI, models, cache, concurrency |
| **Daemon / MCP** | `test_daemon.py` | HTTP request handling |
| **Integration** | `test_integration.py` | End-to-end against real `knowledge.db` (marked `integration`) |
| **Benchmark / Eval** | `test_bench_v2.py`, `test_benchmark_doc_intent.py`, `test_churn_replay.py`, `test_churn_reranker_judge.py`, `test_eval_jidm.py`, `test_eval_verdict.py`, `test_eval_file_gt.py`, `test_code_intent_eval.py`, `test_rerank_pointwise_eval.py` | Metrics, verdict boundaries, churn math |
| **Data-Pipeline / FT** | `test_build_combined_train.py`, `test_build_train_pairs.py`, `test_label_v12_candidates_minilm.py`, `test_listwise_conversion.py`, `test_lpt_schedule.py`, `test_merge_dual_judge_labels.py`, `test_prepare_finetune_data.py`, `test_prepare_train_data.py`, `test_sample_real_queries.py`, `test_train_docs_embedder.py`, `test_train_reranker_ce.py`, `test_two_tower_foundation.py`, `test_two_tower_routing.py` | Training data prep, embedding/reranker training, two-tower wiring |
| **Script Utilities** | `test_analyze_churn.py`, `test_scripts_common.py`, `test_finalize_scrape.py`, `test_scrape_link_rewrite.py`, `test_validate_provider_paths.py`, `test_runpod_lifecycle.py` | Script helpers, RunPod lifecycle |

### Shared Fixtures
- [`tests/conftest.py`](tests/conftest.py) — shared pytest fixtures (DB paths, temp dirs)
- [`tests/__init__.py`](tests/__init__.py) — package marker

### Coverage Gaps
Many `src/` modules are imported and used but lack dedicated unit tests. This is a known coverage gap, not dead code. Notable untested areas:
- `src/embedding_provider.py`, `src/feedback.py`, `src/formatting.py`, `src/js_field_extractor.py`
- All `src/graph/builders/*.py` (14 modules)
- All `src/index/builders/*.py` (18 modules) except those covered by integration tests
- Most `src/search/*.py` modules (covered indirectly via service tests)
- Most `src/tools/analyze/*.py` modules (covered via `test_analyze.py` end-to-end only)

---

## Open Questions

1. **`patterns-export.json` at root** — Is it ever read by anything, or can it be safely deleted? Profile scripts write to `~/.pay-knowledge/`, not the repo root.
2. **`bench_runs/` mixed storage** — 628 of ~644 files appear untracked; only a few early baselines are tracked. Which baselines are still referenced by active gates?
3. **`models/` mixed storage** — Only 4 of ~7 files tracked (`README.md`, `config.json`, `model.safetensors`, `training_summary.json`). The remaining files may be untracked artifacts.
4. **Profile files unexpectedly untracked** — Some curated `profiles/pay-com/` config files are untracked despite being part of the private repo clone. Is this an expected `.gitignore` interaction or a sync gap?
5. **Tests failing** — 20 collection errors are known from parallel sessions. These are acknowledged but not yet fixed. They do not block AGENTS.md work.
6. **`setup_wizard.py` stale but wired** — Last touched 8+ weeks ago. Still referenced by `Makefile` (`make init`). May need refresh or deprecation.
7. **`scripts/runpod/prepare_train_data.py`** — Referenced in tests and `NEXT_SESSION_PROMPT.md`. File exists at `scripts/runpod/prepare_train_data.py`.
8. **Doc validators not in git** — Six `validate_doc_*.py` scripts are actively used by `docs_validate_all.sh` but never committed. Should they be tracked or kept ephemeral?
9. **`.claude/plans/` untracked** — Contains `agents-md-rollout.md`; not committed, not ignored. Intentional planning infrastructure or should it be tracked?

---

## Backlinks

- (none inbound — this is the root catalog)
- Child catalogs:
  - [[profiles/pay-com/AGENTS.md|Pay-Com Profile Catalog]]
- Research shards (used to synthesize this file):
  - [[.claude/research/agents-md/R1.md|R1 — Root Core Inventory]]
  - [[.claude/research/agents-md/R2.md|R2 — Scripts Catalog]]
  - [[.claude/research/agents-md/R5.md|R5 — .claude/ Meta Inventory]]
  - [[.claude/research/agents-md/R6.md|R6 — Dead/Legacy Detection]]
  - [[.claude/research/agents-md/R7.md|R7 — Backlinks Style Guide]]
