# src/ — Navigation Catalog

> **Parent:** [[../AGENTS.md|↑ Root Catalog]]  
> **Scope:** Core source code — search, graph, indexing, tools

## Directory Tree

> **Note:** `daemon.py`, `mcp_server.py`, `cli.py`, `setup_wizard.py` are **repo-root** entry points, not `src/` modules. See [Entry Points](#entry-points) below.

```
src/
├── __init__.py
├── cache.py
├── config.py              # Profile loading, conventions.yaml
├── container.py           # DB connections, ML model preload
├── embedding_provider.py
├── feedback.py
├── formatting.py
├── js_field_extractor.py
├── models.py
├── proto_parser.py
├── types.py
├── graph/                 # Dependency graph builders + service
│   ├── builders/          # 14 modules (edge builders + shared state; see appendix)
│   ├── queries.py         # graph query helpers
│   └── service.py         # BFS, shortest path, trace APIs
├── index/                 # FTS5 + code_facts indexing
│   └── builders/          # 18 modules (chunkers + indexers + orchestrator; see appendix)
├── search/                # Hybrid search (FTS5 + vector + rerank)
│   ├── hybrid.py          # RRF fusion + CrossEncoder
│   ├── service.py         # expand_query, search pipeline
│   ├── fts.py             # FTS5 query layer
│   ├── vector.py          # vector search layer
│   └── ...                # code_facts, env_vars, hybrid_query, hybrid_rerank, suggestions, trace
└── tools/                 # MCP-exposed + daemon-only tools
    ├── analyze/           # Task analysis (10 modules + __init__)
    ├── service.py         # repo_overview, health_check
    └── context.py         # context_builder
```

## Entry Points

> These four files live at **repo root** (`../daemon.py`, `../mcp_server.py`, `../cli.py`, `../setup_wizard.py`), not under `src/`. They import from `src/`.

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

Edge builders for the dependency graph. Each `*_edges.py` parser produces typed `graph_edges` rows; the three non-parser modules provide shared state, schema, and package resolution. `__init__.py` re-exports the parsers (14 files total incl. `__init__.py`).

| Builder | Edge Type(s) | Source Artifact |
|---------|--------------|-----------------|
| `proto_edges.py` | `proto_import` | `proto_header` chunks (`.proto` `import` lines) |
| `grpc_edges.py` | `grpc_call`, `grpc_method_call`, `grpc_client_usage` | `*_GRPC_URL` env vars + JS/TS gRPC client `require()`/method calls |
| `npm_edges.py` | `npm_dep`, `npm_dep_proto`, `npm_dep_tooling` | `repos.org_deps` (scoped `package.json` deps) |
| `k8s_edges.py` | `k8s_env` | `file_type='k8s'` chunks (`*.grpc.*` value refs) |
| `temporal_edges.py` | `child_workflow`, `temporal_signal`, `signal_send`, `signal_handler`, `workflow_import`, `activity_import` | Temporal workflow chunks (`executeChild`/`startChild`/`defineSignal`/`temporal-tools/workflows`) |
| `webhook_edges.py` | `webhook_dispatch`, `webhook_handler`, `callback_handler` | express-webhooks routes + workflow-provider-webhooks handler map |
| `express_edges.py` | `express_route`, `http_call` | Express route defs + `fetch({ url: ${*_URL} })` internal calls |
| `domain_edges.py` | `domain_serves`, `domain_reference`, `flow_step`, `flow_redirect`, `url_reference` | `docs/domain_registry.yaml` + flow annotations |
| `similarity_edges.py` | `similar_repo` | shared `org_deps` overlap + name-family / file-tree similarity |
| `manual_edges.py` | (per `conventions.yaml`) | `manual_edges.{group}` declarations in `conventions.yaml` |
| `pkg_resolution.py` | — (resolves `pkg:` targets, builds package→repo map) | `repos.org_deps`; emits `package_usage` chunks |
| `_common.py` | — (shared state) | loads `config.json` + `conventions.yaml` constants once |
| `db.py` | — (schema/nodes) | creates `graph_nodes`/`graph_edges`, populates nodes, prints summary |

### `src/index/builders/` (18 modules)

FTS5 indexing pipeline (refactored from the monolithic `scripts/build_index.py`). Chunkers turn a file into `chunks` rows, indexers walk a source tree, and the orchestrator wires them together. `__init__.py` re-exports the public API (18 files total incl. `__init__.py`).

| Module | Handles / Role | Output |
|--------|----------------|--------|
| `orchestrator.py` | top-level build (`main()` equiv; `--incremental`/`--repos`/`--reset-repo`) | drives all chunkers + indexers |
| `dispatcher.py` | `chunk_file` router by detected language | dispatches to the right chunker |
| `detect.py` | file-type + language detection by extension | language / file-type labels |
| `db.py` | FTS5 schema + per-repo deletion | creates `chunks` (FTS5) + metadata tables |
| `_common.py` | shared paths, profile + conventions loading, constants | `MAX_CHUNK`/`MIN_CHUNK`, dir paths |
| `_memguard.py` | embed-loop memory guard (RSS/MPS thresholds, daemon pause) | prevents Jetsam SIGKILL on builds |
| `code_chunks.py` | JS/TS semantic chunking (regex boundaries + fallback) | `chunks` (functions/classes/exports) |
| `code_facts.py` | guards, consts, joi/zod schemas, env lookups, retry/gRPC-status | `code_facts` |
| `config_chunks.py` | `package.json` / YAML / `.env` config files | `chunks` (configs) |
| `proto_chunks.py` | `.proto` by message/service/enum/rpc | `chunks` (proto) |
| `docs_chunks.py` | Markdown sections + task-aware chunking | `chunks` (`doc_section`) |
| `cql_chunks.py` | `seeds.cql` provider INSERT rows | `chunks` (provider config) |
| `repo_indexer.py` | walks one repo's extracted artifacts | `chunks` + `code_facts` |
| `docs_indexer.py` | profile docs: gotchas, flows, references, dictionary, providers, domain registry, tasks | `chunks` |
| `raw_indexer.py` | reads `raw/` directly — `seeds.cql` + per-repo test scripts | `chunks` |
| `docs_vector_indexer.py` | docs-tower embeddings (`nomic-embed-text-v1.5`, streaming) | `db/vectors.lance.docs/` |
| `incremental.py` | per-repo SHA detection + profile-doc fingerprint | drives incremental re-index |

## Backlinks

- [[../AGENTS.md|Root Catalog]] — top-level overview, conventions, profiles
- [[../scripts/AGENTS.md|scripts/]] — build, bench, eval scripts
- [[../tests/AGENTS.md|tests/]] — test structure
