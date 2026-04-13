# Architecture — code-rag-mcp

## System Overview

MCP RAG server that indexes any GitHub org's codebase and provides intelligent code search,
dependency tracing, and task analysis tools via Model Context Protocol.

```
┌─────────────────────────────────────────────────────────────┐
│ Claude Code / Claude Desktop                                │
│   └── MCP stdio connection                                  │
├─────────────────────────────────────────────────────────────┤
│ mcp_server.py (thin proxy, ~20MB)                           │
│   └── HTTP forwarding to daemon                             │
├─────────────────────────────────────────────────────────────┤
│ daemon.py (persistent, ~400MB, holds ML models)             │
│   └── localhost:8742                                        │
├─────────────────────────────────────────────────────────────┤
│ src/                                                        │
│   config.py ─── profile loading, conventions.yaml           │
│   container.py ── DB connections, ML model preload          │
│   ├── search/  (FTS5 + LanceDB + CrossEncoder)              │
│   ├── graph/   (BFS, shortest path, 29 edge types)          │
│   └── tools/                                                │
│       ├── analyze/  (task analysis, 8 modules)              │
│       ├── context.py (context_builder)                      │
│       └── service.py (repo_overview, health_check, etc.)    │
├─────────────────────────────────────────────────────────────┤
│ db/knowledge.db  (SQLite FTS5, ~130MB)                      │
│ db/vectors.lance.coderank/ (LanceDB, ~211MB — active or fallback) │
│ db/vectors.lance.gemini/   (LanceDB, ~278MB — coexists)     │
└─────────────────────────────────────────────────────────────┘
```

## Repository Structure

Two repos, one private:

```
vtarsh/code-rag-mcp (PUBLIC)        vtarsh/code-rag-mcp-profile (PRIVATE)
├── src/                            ├── config.json
├── scripts/                        ├── conventions.yaml    ← org prefixes, domains
├── tests/                          ├── glossary.yaml
├── profiles/example/               ├── known_flows.yaml
├── CLAUDE.md                       ├── benchmarks.yaml
├── ARCHITECTURE.md (this file)     ├── install.sh          ← symlinks scripts
├── TESTING.md                      ├── uninstall.sh
└── Makefile                        ├── scripts/            ← 20 org-specific scripts
                                    ├── docs/flows/
                                    ├── docs/gotchas/
                                    ├── docs/references/
                                    ├── docs/tasks/
                                    ├── RECALL-TRACKER.md
                                    └── NEXT-SESSION-PROMPT.md
```

**Dependency**: Private repo is cloned into `profiles/pay-com/` (gitignored in public repo).
`install.sh` creates symlinks from `profiles/pay-com/scripts/` into `scripts/`.
All org-specific configuration loaded at runtime via `conventions.yaml`.

## analyze_task Package (src/tools/analyze/)

The core intelligence — classifies tasks and finds relevant repos.

```
__init__.py          Orchestrator: classify → dispatch → assemble output
base.py              AnalysisContext dataclass, shared utilities
classifier.py        7 domains (pi, core-risk/api/3ds/platform/payment, bo, hs)
                     Multi-domain when scores close. Uses conventions.yaml domain_patterns.
core_analyzer.py     Non-PI analysis: cascade, co-occurrence, fan-out, function search, keyword scan
pi_analyzer.py       Provider analysis: provider repos, webhooks, impact, checklist, bulk detection
shared_sections.py   Universal: gotchas, task patterns, file patterns, proto, gateway, GitHub, completeness, CI
github_helpers.py    GitHub API (branches, PRs, task ID matching)
method_helpers.py    gRPC method existence checks
```

### Data Flow

```
User description
    ↓
classify_task() → TaskClassification(domain, provider, seed_repos)
    ↓
┌─── Shared sections (all tasks) ───┐
│ gotchas, task patterns, file       │
│ patterns, proto, gateway           │
├─── PI sections (if provider) ──────┤
│ provider repos, webhooks, impact,  │
│ bulk detection, change impact,     │
│ provider checklist                 │
├─── CORE sections (if not PI) ──────┤
│ domain repos, cascade (up+down),   │
│ provider fan-out, function search, │
│ keyword scan                       │
├─── Universal post-analysis ────────┤
│ co-occurrence boost, methods,      │
│ GitHub activity, completeness,     │
│ CI risk                            │
└────────────────────────────────────┘
    ↓
Markdown output with **bold repo names**
```

### 10 Mechanisms (all generic, zero hardcoded repo names)

1. **Classifier** — keywords + task prefix + repo patterns → domain. Multi-domain union when close.
2. **BFS cascade upstream** — `bfs_dependents(seed, depth=2)` finds who depends on seeds.
3. **Downstream walk** — outgoing edges from seeds, filter by in-degree ≥5 (hub repos), exclude tooling.
4. **Co-occurrence** — from task_history, same-prefix scoped (CORE↔CORE, PI↔PI). ≥40% conditional probability, ≥3 tasks.
5. **Universal repos** — repos changed in ≥25% of same-prefix tasks.
6. **Provider fan-out** — when proto/types repos in findings, enumerate all providers via gateway `runtime_routing`.
7. **Bulk provider detection** — regex for "all/every/each providers" → list all via gateway routing.
8. **Keyword scan** — compound terms (camelCase/underscore), repo-name matching (4+ chars), content FTS (6+ chars, 2+ matches).
9. **Function search** — camelCase/snake_case function names from description + prefix generation (createAuditLog → createAudit).
10. **Domain registry** — URL→repo edges from `domain_registry.yaml` for frontend repos.

### conventions.yaml Keys

```yaml
provider_prefixes: [grpc-apm-, grpc-providers-, grpc-card-, grpc-mpi-]
provider_type_map: {apm: "grpc-apm-{provider}", ...}
provider_methods: [sale, payout, refund, ...]
proto_repos: [providers-proto, libs-types, grpc-core-schemas]
gateway_repo: grpc-payment-gateway
webhook_repos: {dispatch: express-webhooks, handler: workflow-provider-webhooks}
feature_repo: grpc-providers-features
credentials_repo: grpc-providers-credentials
impact_hints: [{prefix: "grpc-apm-", hint: "..."}]
infra_repos: [{repo: ..., description: ..., weight: ...}]
infra_suffixes: [credentials, features, ...]
domain_patterns:
  core-risk: {keywords: [...], repo_patterns: [...], seed_repos: [...]}
  core-api: ...
  bo: ...
  hs: ...
```

## Build Pipeline

```bash
make build                    # Full: clone → extract → index → graph → vectors (~2-4h; ~20GB RAM peak)

# Individual steps:
scripts/extract_artifacts.py  # Parse repos → extracted/ (fills repos.org_deps)
scripts/build_index.py        # Build FTS5 chunks + code_facts
scripts/build_graph.py        # Build graph_edges (29 edge types, ~15.5k edges)
scripts/build_vectors.py      # Build LanceDB embeddings
```

**Important**: `build_index.py` recreates repos/chunks tables. After rebuild, restore
`task_history` and analysis tables from backup (or they'll be lost).

## Key Invariants

- All org-specific strings in `conventions.yaml`, never in `src/` or tracked scripts
- Search: expand → FTS5 + vector → RRF fusion → CrossEncoder rerank → format
- Recall > precision — false negatives worse than false positives
- Local only — zero external services
- All tool functions return `str`
- Profile system: `profiles/{name}/` with config.json + conventions.yaml + docs/
