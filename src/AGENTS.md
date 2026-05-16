# src/ — Navigation Catalog

> **Parent:** [[../AGENTS.md|↑ Root Catalog]]  
> **Scope:** Core source code — search, graph, indexing, tools

## Directory Tree

```
src/
├── __init__.py
├── cache.py
├── config.py              # Profile loading, conventions.yaml
├── container.py           # DB connections, ML model preload
├── embedding_provider.py
├── daemon.py              # Persistent HTTP server (main entry)
├── mcp_server.py          # MCP stdio proxy (thin wrapper)
├── cli.py                 # HTTP client for daemon
├── setup_wizard.py        # Interactive profile setup
├── graph/                 # Dependency graph builders + service
│   ├── builders/          # 14 modules (edge extractors)
│   ├── service.py         # BFS, shortest path, trace APIs
│   └── ...
├── index/                 # FTS5 + code_facts indexing
│   ├── builders/          # 18 modules (per-artifact extractors)
│   └── fts.py, vectors.py, chunks.py
├── search/                # Hybrid search (FTS5 + vector + rerank)
│   ├── hybrid.py          # RRF fusion + CrossEncoder
│   ├── service.py         # expand_query, search pipeline
│   └── ...
└── tools/                 # MCP-exposed + daemon-only tools
    ├── analyze/           # Task analysis (13 modules)
    ├── service.py         # repo_overview, health_check
    └── context.py         # context_builder
```

## Entry Points

| File | Purpose | Called By |
|------|---------|-----------|
| `daemon.py` | Persistent HTTP server (~400MB RAM) | `launchd`, manual, `mcp_server.py` auto-start |
| `mcp_server.py` | MCP stdio → HTTP proxy (~20MB) | Claude Code / Claude Desktop |
| `cli.py` | HTTP client for sub-agents | Shell scripts, sub-agents |
| `setup_wizard.py` | Interactive profile bootstrap | `make init`, first-time setup |

## Key Files

| File | Role |
|------|------|
| `config.py` | Profile loading, conventions.yaml parsing, path resolution |
| `container.py` | Singleton DB connections, model preload, dependency injection |
| `search/service.py` | `expand_query`, `hybrid_search`, result formatting |
| `search/hybrid.py` | RRF fusion, CrossEncoder rerank, scoring |
| `graph/service.py` | BFS, pathfinding, impact/trace APIs |
| `tools/analyze/__init__.py` | Task analysis orchestrator (classify → dispatch → assemble) |

## Appendix: Builder Modules

### `src/graph/builders/` (14 modules)

Edge extractors for dependency graph. Each produces typed edges:

| Builder | Edge Type | Source Artifact |
|---------|-----------|-----------------|
| `proto_imports.py` | `proto_import` | `.proto` files |
| `grpc_calls.py` | `grpc_call` | JS/TS gRPC client stubs |
| `npm_deps.py` | `npm_dep` | `package.json` |
| `env_vars.py` | `env_ref` | `.env`, `docker-compose.yml` |
| `temporal_signals.py` | `temporal_signal` | Temporal workflow defs |
| `k8s_refs.py` | `k8s_ref` | K8s YAML manifests |
| `domain_registry.py` | `domain_url` | `domain_registry.yaml` |
| `provider_methods.py` | `provider_method` | Provider JS mappers |
| `webhook_routes.py` | `webhook_route` | Express webhook handlers |
| `feature_flags.py` | `feature_flag` | Feature flag configs |
| `db_migrations.py` | `db_migration` | SQL migration files |
| `ci_refs.py` | `ci_ref` | `.github/workflows/` |
| `api_specs.py` | `api_spec` | OpenAPI / swagger specs |
| `gateway_routing.py` | `gateway_route` | Gateway routing tables |

### `src/index/builders/` (18 modules)

Artifact extractors for FTS5 indexing. Each handles one file type:

| Builder | Handles | Output Table |
|---------|---------|--------------|
| `proto_extractor.py` | `.proto` | `chunks` (proto messages) |
| `js_extractor.py` | `.js`, `.ts` | `chunks` + `code_facts` |
| `yaml_extractor.py` | `.yaml`, `.yml` | `chunks` (configs) |
| `sql_extractor.py` | `.sql` | `chunks` (schemas) |
| `markdown_extractor.py` | `.md` | `chunks` (docs) |
| `dockerfile_extractor.py` | `Dockerfile` | `chunks` |
| `env_extractor.py` | `.env` | `chunks` |
| `json_extractor.py` | `.json` | `chunks` (configs) |
| `graphql_extractor.py` | `.graphql` | `chunks` (schemas) |
| `ci_extractor.py` | `.github/workflows/*` | `chunks` |
| `migration_extractor.py` | `migrations/` | `chunks` + `code_facts` |
| `temporal_extractor.py` | Temporal workflow files | `chunks` |
| `joi_extractor.py` | Joi schemas in JS | `code_facts` |
| `grpc_status_extractor.py` | gRPC status mappings | `code_facts` |
| `const_extractor.py` | `UPPER_CASE` constants | `code_facts` |
| `guard_extractor.py` | `if-throw` validation guards | `code_facts` |
| `env_var_extractor.py` | `process.env.*` | `code_facts` |
| `retry_extractor.py` | Temporal retry policies | `code_facts` |

## Backlinks

- [[../AGENTS.md|Root Catalog]] — top-level overview, conventions, profiles
- [[../scripts/AGENTS.md|scripts/]] — build, bench, eval scripts
- [[../tests/AGENTS.md|tests/]] — test structure
