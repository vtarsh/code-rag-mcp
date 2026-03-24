# code-rag-mcp — MCP RAG Server

Generic RAG system for indexing any GitHub org's codebase.
Git: vtarsh/code-rag-mcp (personal account).
Python 3.12, FastMCP, SQLite FTS5, LanceDB, CrossEncoder reranker.

**Key docs** (read for full context):
- `ARCHITECTURE.md` — system design, analyze_task package, 20 mechanisms, conventions.yaml
- `.claude/rules/` — conventions, impact-audit, lessons-active, provider-code-rules, provider-docs-first, testing, workflow
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

# Full rebuild (clones -> extracts -> indexes -> graph -> vectors -> benchmarks)
make build  # or: ACTIVE_PROFILE=my-org ./scripts/full_update.sh --full

# Start daemon (normally auto-started by launchd, or by mcp_server.py proxy)
python daemon.py

# Incremental build (only changed repos, seconds vs 30min)
python scripts/build_index.py --incremental

# MCP proxy (auto-started by Claude Code/Desktop -> forwards to daemon)
python mcp_server.py
```

## Profile System

Org-specific data lives in `profiles/{name}/` (git-ignored except `profiles/example/`).
Active profile: `ACTIVE_PROFILE` env var or `.active_profile` file.

```
profiles/{name}/
├── config.json           # org, npm_scope, embedding_model, display_name
├── glossary.yaml         # Domain abbreviations -> expanded terms
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

Profile setup: `cd profiles/{name} && ./install.sh` -- creates symlinks for org scripts into `scripts/`.

## Architecture

See `ARCHITECTURE.md` for full system design, dependency direction, and module map.

Summary: `daemon.py` (HTTP on :8742, holds ML models) + `mcp_server.py` (thin stdio proxy).
All sessions share one daemon process. Proxy auto-starts daemon if not running.

## Tools (12 total)

- `search` -- hybrid search with `exclude_file_types` parameter (e.g. "gotchas")
- `find_dependencies`, `trace_impact`, `trace_flow`, `trace_chain` -- graph tools
- `repo_overview`, `list_repos` -- browsing
- `analyze_task` -- domain-aware multi-section analysis (see `src/tools/analyze/` package)
- `context_builder` -- search + deps + proto in one call
- `diff_provider_config` -- compares two providers' feature flags from seeds.cql
- `health_check`, `visualize_graph` -- diagnostics

## Gotchas (critical)

- `build_index.py` recreates DB from scratch -- run `build_graph.py` after to restore graph_edges
- `analyze/` is a package (8 modules) -- add new domains via classifier.py + new analyzer file
- Glossaries loaded from profile YAML at import time -- restart daemon after editing
- Daemon restart: `kill -9 $(lsof -ti:8742); sleep 2; CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 daemon.py &disown`
- FTS5 virtual tables cannot have columns added -- use separate tables (chunk_meta, env_vars)
