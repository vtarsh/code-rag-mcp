# code-rag-mcp — MCP RAG Server

Generic RAG system for indexing any GitHub org's codebase.
Git: vtarsh/code-rag-mcp (personal account).
Python 3.12, FastMCP, SQLite FTS5, LanceDB, CrossEncoder reranker.

## Commands

```bash
# Tests
cd ~/.code-rag && python -m pytest tests/ -q

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
└── docs/
    ├── flows/            # YAML flow annotations (indexed as chunks)
    ├── gotchas/          # Markdown gotchas (indexed as high-priority chunks, 1.5x boost)
    ├── tasks/            # Markdown task journals (1 file per task, section-aware chunking)
    ├── references/       # YAML/MD stable lookup docs (e.g., APM ranking, 1.3x boost)
    └── domain_registry.yaml  # Domain-to-repo mapping
```

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
├── graph/             # Dependency graph (26 edge types)
│   ├── queries.py     # BFS, shortest path, hub penalty
│   └── service.py     # Tool implementations
└── tools/             # Composite tools
    ├── analyze.py     # analyze_task (8 section helpers + GitHub API)
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

## Gotchas

- `build_index.py` recreates DB from scratch — run `build_graph.py` after to restore graph_edges
- `build_graph.py` also runs `build_env_index.py` (step 19) — env var table + map-type var chunks
- `analyze.py` has 9 section helpers (0–8) — Section 0.5 searches tasks, add new sections as separate functions
- Glossaries loaded from profile YAML at import time — restart daemon after editing
- Graph viz virtual nodes (pkg:, proto:, route:) are filtered in visualize_graph.py and queries.py
- FTS5 virtual tables cannot have columns added — use separate tables (chunk_meta, env_vars)
- Tests mock DB layer — no integration tests with real SQLite yet
- Pre-commit runs ruff + ruff-format + pytest — fix lint before committing
- Daemon uses ~400 MB real memory (VSZ shows ~2.5 GB but that's virtual/mmap — not real usage)
- Embedding model selected via profile config `embedding_model` key or `CODE_RAG_MODEL` env var
- After adding tasks/references, run incremental vectors: `build_vectors.py --repos=task-slug,ref-slug`
- Task boost tiers: plan/decisions 1.1x, api_spec/description 1.05x, metadata 0.95x, progress 0.7x
- Tasks in analyze_task are filtered by provider name in repo_name to avoid false positives
