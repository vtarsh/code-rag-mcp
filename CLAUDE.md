# code-rag-mcp — MCP RAG Server

Generic RAG system for indexing any GitHub org's codebase.
Git: vtarsh/code-rag-mcp (personal account).
Python 3.12, FastMCP, SQLite FTS5, LanceDB, CrossEncoder reranker.

**Key docs** (read for full context):
- `ARCHITECTURE.md` — system design, analyze_task package, 20 mechanisms, conventions.yaml
- `.claude/rules/` — workflow, testing, conventions, lessons, deep-analysis-tiers, deep-analysis-agent, data-collection
- `TESTING.md` — recall methodology, how to measure/improve, validation without MCP
- `profiles/pay-com/RECALL-TRACKER.md` — current scores, improvement log
- `profiles/pay-com/NEXT-SESSION-PROMPT.md` — context for new sessions

## Commands

```bash
# Tests
cd ~/.pay-knowledge && python -m pytest tests/ -q

# Benchmarks (run after search pipeline changes)
python scripts/benchmark_queries.py && python scripts/benchmark_realworld.py
python scripts/benchmark_flows.py  # Flow completeness (Q1-Q5 validation)

# Blind spot detection
python scripts/detect_blind_spots.py

# Full rebuild (clones → extracts → indexes → graph → vectors → benchmarks)
make build  # or: ACTIVE_PROFILE=my-org ./scripts/full_update.sh --full

# Start daemon (normally auto-started by launchd, or by mcp_server.py proxy)
python daemon.py

# MCP proxy (auto-started by Claude Code/Desktop — forwards to daemon)
python mcp_server.py
```

## Profile System

Org-specific data lives in `profiles/{name}/` (git-ignored except `profiles/example/`).
Active profile: `ACTIVE_PROFILE` env var or `.active_profile` file.

```
profiles/{name}/
├── config.json           # org, npm_scope, embedding_model, display_name
├── glossary.yaml         # Domain abbreviations → expanded terms
├── phrase_glossary.yaml  # Multi-word concept expansion rules
├── known_flows.yaml      # Business flow entry points for trace_chain
├── conventions.yaml      # Org repo naming patterns, infra repos, impact hints, domain_patterns
├── install.sh            # Symlinks profile scripts into project scripts/
├── uninstall.sh          # Removes symlinked scripts
├── scripts/              # Org-specific analysis scripts (symlinked into project)
└── docs/
    ├── flows/            # YAML flow annotations (indexed as chunks)
    ├── gotchas/          # Markdown gotchas (indexed as high-priority chunks, 1.5x boost)
    ├── tasks/            # Markdown task journals (1 file per task, section-aware chunking)
    ├── references/       # YAML/MD stable lookup docs (e.g., APM ranking, 1.3x boost)
    └── domain_registry.yaml  # Domain-to-repo mapping
```

Profile setup: `cd profiles/{name} && ./install.sh` — creates symlinks for org scripts into `scripts/`.

## Architecture

```
daemon.py              # Persistent HTTP server on localhost:8742 — holds ML models (~230 MB)
mcp_server.py          # Thin stdio MCP proxy — forwards tool calls to daemon (~20 MB)
src/
├── server.py          # MCP tool registration (wiring only, used by daemon)
├── container.py       # DI: DB connections, ML models, preload
├── config.py          # Paths, profile loading, glossary from YAML
├── models.py          # Embedding model registry (coderank, minilm)
├── types.py           # Pydantic models (SearchResult, GraphEdge, etc.)
├── formatting.py      # Shared snippet formatting (strip_repo_tag)
├── cache.py           # LRU cache with TTL
├── feedback.py        # JSONL feedback logging
├── search/            # FTS5 + LanceDB vector + RRF fusion + CrossEncoder
│   ├── hybrid.py      # Core: hybrid_search + rerank (RRF K=60, 70/30 blend)
│   ├── fts.py         # SQLite FTS5 search
│   ├── vector.py      # LanceDB vector search (model-aware query prefix)
│   └── service.py     # Tool implementations
├── graph/             # Dependency graph (29 edge types)
│   ├── queries.py     # BFS, shortest path, hub penalty
│   └── service.py     # Tool implementations
└── tools/             # Composite tools
    ├── analyze/       # analyze_task package (modular, domain-aware)
    │   ├── __init__.py         # Orchestrator + backward compat re-exports
    │   ├── base.py             # AnalysisContext, shared types
    │   ├── classifier.py       # Task domain classifier (7 domains)
    │   ├── core_analyzer.py    # CORE: cascade prediction, keyword scan
    │   ├── pi_analyzer.py      # PI: provider, webhooks, impact, checklist
    │   ├── shared_sections.py  # Universal: gotchas, patterns, proto, gateway, completeness
    │   ├── github_helpers.py   # GitHub API interaction
    │   └── method_helpers.py   # gRPC method checks
    ├── context.py     # context_builder (search + deps + proto)
    └── service.py     # repo_overview, list_repos, health_check, visualize_graph
profiles/              # Org-specific data (git-ignored except example/)
scripts/               # Build pipeline, benchmarks, graph builder, env index
```

All sessions share one daemon process. Proxy auto-starts daemon if not running.

## Dependency direction

`server.py` → `{search,graph,tools}/service.py` → internals → `{container,config,models,types}`

## Key invariants

- Search pipeline order: expand query → FTS5 + vector → RRF fusion → CrossEncoder rerank → format
- Never silently drop search results — deprioritize/annotate, never exclude
- Recall over precision — false negatives worse than false positives
- Local only — zero external services
- All tool functions return `str` (error strings on failure, formatted results on success)
- Org-specific data MUST live in profiles/ — never hardcode org names in src/
- Run benchmark_recall.py before and after analyze_task changes

## Code Facts (added 2026-03-21)

- **2,279 code_facts** from **397 repos** stored in `code_facts` table + `code_facts_fts` (FTS5)
- Also inserted as `file_type='code_fact'` chunks for hybrid search
- Fact types: validation_guard (1311), const_value (684), joi_schema (231), temporal_retry (31), env_var (22)
- Extraction: regex-based in `build_index.py::extract_code_facts()` — runs during `index_repo()`
- Coverage: methods/, libs/, consts/, handlers/, routes/, utils/, services/, workflows/, env/, src/ + root consts.js/config.js
- ~150 repos have no facts (boilerplate, CI, gitops, or non-standard structure)
- To rebuild facts only: delete from code_facts + code_facts_fts, re-run index_repo for affected repos

## Tools (12 total)

- `search` — hybrid search with `exclude_file_types` parameter (e.g. "gotchas")
- `find_dependencies`, `trace_impact`, `trace_flow`, `trace_chain` — graph tools
- `repo_overview`, `list_repos` — browsing
- `analyze_task` — domain-aware multi-section analysis (see `src/tools/analyze/` package)
  - Shared: gotchas, task patterns, file patterns, proto, gateway, methods, GitHub, completeness, CI risk
  - PI: provider repos, webhooks, impact, change impact, provider checklist
  - CORE/BO/HS: domain classifier, cascade (up+downstream), co-occurrence, fan-out, keyword scan
  - Recall (phantom-filtered): CORE 94.3%, PI 97.6%, BO 98.0%, HS 92.9% (96.4% total on 361 tasks)
  - 20 mechanisms + hub penalty + domain templates + Gemini re-ranker (optional --rerank flag)
  - Re-ranker: Gemini 3.1 Pro filters candidates (100% precision on simple tasks, --rerank flag)
- `context_builder` — search + deps + proto in one call
- `diff_provider_config` — compares two providers' feature flags from seeds.cql (handles multi-PMT)
- `health_check`, `visualize_graph` — diagnostics

## Benchmark Regression Suite

```bash
# Run after any search/indexing changes (uses profile benchmarks.yaml)
CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=my-org python3 scripts/benchmark_queries.py
CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=my-org python3 scripts/benchmark_realworld.py
CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=my-org python3 scripts/benchmark_flows.py
```

Current scores: conceptual 0.850, realworld 4/6 passing, flows 0.875

## Gotchas

- `build_index.py` recreates DB from scratch — run `build_graph.py` after to restore graph_edges
- `build_graph.py` also runs `build_env_index.py` (step 19) — env var table + map-type var chunks
- `analyze/` is a package (8 modules) — add new domains via classifier.py + new analyzer file
- Glossaries loaded from profile YAML at import time — restart daemon after editing
- Daemon restart: `kill -9 $(lsof -ti:8742); sleep 2; CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 daemon.py &disown`
- Graph viz virtual nodes (pkg:, proto:, route:) are filtered in visualize_graph.py and queries.py
- FTS5 virtual tables cannot have columns added — use separate tables (chunk_meta, env_vars)
- Tests mock DB layer — no integration tests with real SQLite yet
- Pre-commit runs ruff + ruff-format + pytest (133 tests) — fix lint before committing
- Daemon uses ~400 MB real memory (VSZ shows ~2.5 GB but that's virtual/mmap — not real usage)
- Embedding model selected via profile config `embedding_model` key or `CODE_RAG_MODEL` env var
- After adding tasks/references, run incremental vectors: `build_vectors.py --repos=task-slug,ref-slug`
- Task boost tiers: plan/decisions 1.1x, api_spec/description 1.05x, metadata 0.95x, progress 0.7x
- Tasks in analyze_task are filtered by provider name in repo_name to avoid false positives
- All scripts use conventions.yaml — zero hardcoded org repo names in src/ and scripts/
- Full rebuild: extract_artifacts.py → build_index.py → build_graph.py → build_vectors.py (~30 min)
- build_index.py recreates repos/chunks tables — restore task_history etc. from backup after rebuild
