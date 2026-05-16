# AGENTS.md — code-rag-mcp Root Catalog

> Generic RAG system for indexing any GitHub org's codebase and serving it through an MCP server.  
> Maintainer: [vtarsh](https://github.com/vtarsh) (personal account) · `vtarsh/code-rag-mcp`  
> Stack: Python 3.12, FastMCP, SQLite FTS5, LanceDB, SentenceTransformer / CrossEncoder reranker.

---

## Overview

This repo provides a **model-context-protocol (MCP) RAG server** that indexes codebases, documentation, and dependency graphs for a GitHub organization, then exposes search, analysis, and tracing tools to AI agents. It is designed to be **org-agnostic**: all organization-specific data lives in `profiles/{name}/`, while `src/` contains fully generic mechanisms.

The runtime is split into two processes: [`daemon.py`](daemon.py) is a persistent HTTP server (~1.4 GB RAM) that holds embedding and reranker models in memory and serves tool endpoints on `localhost:8742`; [`mcp_server.py`](mcp_server.py) is a thin stdio MCP proxy (~20 MB) that forwards every tool call to the daemon and auto-starts it if needed. A separate [`cli.py`](cli.py) HTTP client lets sub-agents without MCP access call the same tools.

**11 MCP tools:** search, analyze_task, trace_field, trace_chain, trace_flow, trace_impact, trace_internal, repo_overview, list_repos, provider_type_map, health_check.  
**5 daemon-only tools:** find_dependencies, context_builder, visualize_graph, diff_provider_config, search_task_history.

---

## Navigable Index

### Sub-Catalogs (Fractal Navigation)
- [[src/AGENTS.md|src/]] — core source code (search, graph, index, tools)
- [[scripts/AGENTS.md|scripts/]] — build, bench, eval, analysis, maintenance scripts
- [[tests/AGENTS.md|tests/]] — test suite structure and coverage
- [[profiles/pay-com/AGENTS.md|profiles/pay-com/]] — active production profile

### Sections in This File
- [Directory Tree](#directory-tree) — top-level layout
- [Storage Classification](#storage-classification) — git vs generated
- [Entry Points](#entry-points) — run / build / test
- [Key Files](#key-files) — root-level orientation
- [Profile System](#profile-system) — org-specific data model
- [Conventions](#conventions) — naming, hooks, LFS, lint
- [Dead / Legacy](#dead--legacy) — cleanup status
- [Open Questions](#open-questions) — ambiguities

### Key Docs
- [`README.md`](README.md) — human quick-start
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system design, two-repo layout, 10 generic mechanisms
- [`TESTING.md`](TESTING.md) — recall methodology, benchmark commands

---

## Directory Tree

```
~/.code-rag-mcp/
├── src/                  # Core source — see [[src/AGENTS.md]]
├── scripts/              # Build, bench, eval — see [[scripts/AGENTS.md]]
├── tests/                # Test suite — see [[tests/AGENTS.md]]
├── profiles/
│   ├── example/          # Template profile (shipped in git)
│   └── pay-com/          # Active profile — see [[profiles/pay-com/AGENTS.md]]
├── db/                   # SQLite + LanceDB (generated)
├── raw/                  # Cloned repo artifacts (generated)
├── extracted/            # Extracted chunks / configs (generated)
├── logs/                 # Runtime logs (generated)
├── bench_runs/           # Benchmark JSON dumps (mostly untracked)
├── models/               # Fine-tuned artifacts (mostly untracked)
├── .claude/              # Agent infrastructure (rules, skills, plans)
├── .secrets/             # API keys, tokens (gitignored)
├── AGENTS.md             # This file
├── README.md
├── ARCHITECTURE.md
├── TESTING.md
├── ROADMAP.md
├── Makefile
├── pyproject.toml
├── .gitignore
└── .pre-commit-config.yaml
```

---

## Storage Classification

| Path | Classification | Notes |
|------|----------------|-------|
| `AGENTS.md`, `README.md`, `ARCHITECTURE.md`, `TESTING.md`, `ROADMAP.md` | **git** | curated docs |
| `mcp_server.py`, `daemon.py`, `cli.py`, `setup_wizard.py` | **git** | entry points |
| `pyproject.toml`, `Makefile`, `.gitignore`, `.pre-commit-config.yaml` | **git** | project config |
| `src/`, `tests/` | **git** | source + tests, all tracked |
| `scripts/` | **hybrid** | ~60 tracked, ~30 gitignored (profile symlinks + untracked) |
| `profiles/example/` | **git** | template profile |
| `profiles/pay-com/` | **gitignored** | private repo clone; curated docs force-tracked inside |
| `db/`, `raw/`, `extracted/`, `logs/` | **gitignored generated** | recreated by build pipeline |
| `bench_runs/` | **hybrid** | baselines tracked; timestamped dumps untracked |
| `models/` | **hybrid** | metadata tracked; model weights untracked |
| `.secrets/` | **gitignored** | credentials |
| `.claude/rules/`, `.claude/docs/`, `.claude/skills/` | **git** | agent infrastructure |
| `.claude/plans/`, `.claude/research/` | **git** | planning docs |
| `.claude/debug/` | **gitignored** | ephemeral debug artifacts |
| `.claude/worktrees/` | **gitignored** | ephemeral per-session branches |

---

## Entry Points

### Development

```bash
make test              # Run pytest suite
make build             # Full pipeline (~2-4h, ~20GB RAM peak)
make update            # Incremental update (~30-60 min)
make health            # Daemon health check
make switch-model MODEL=minilm   # Rebuild vectors
```

### Manual

```bash
python3 daemon.py              # Start daemon directly
python3 mcp_server.py          # MCP proxy (stdio → HTTP)
python3 cli.py <tool> <args>   # CLI client for sub-agents
python3 setup_wizard.py        # Interactive profile setup
```

### Daemon Lifecycle

```bash
# Hard restart
kill -9 $(lsof -ti:8742); sleep 2
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 daemon.py &disown
```

---

## Key Files

Read these first when orienting to the codebase.

| File | Why It Matters |
|------|----------------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | System design, two-repo layout, `analyze_task` package, 10 generic mechanisms |
| [`TESTING.md`](TESTING.md) | Recall measurement methodology, benchmark commands, agent-based validation |
| [`README.md`](README.md) | Human-facing quick-start: install, build, tools, profiles, models |
| [`pyproject.toml`](pyproject.toml) | Dependencies, ruff (py312, line-length 120), pytest config |
| [`src/config.py`](src/config.py) | Profile loading, paths, glossary expansion — config SOT |
| [`src/container.py`](src/container.py) | DI container: DB, models, LanceDB tables |
| [`mcp_server.py`](mcp_server.py) | Thin stdio MCP proxy; auto-starts daemon |
| [`daemon.py`](daemon.py) | Persistent HTTP server with ML models; serves tools |

For `src/` details → [[src/AGENTS.md]]  
For `scripts/` details → [[scripts/AGENTS.md]]  
For `tests/` details → [[tests/AGENTS.md]]

---

## Profile System

Each org's data lives in `profiles/{name}/`:

```
profiles/example/          # Template (shipped in git)
├── config.json            # org, npm_scope, embedding_model
├── glossary.yaml          # Domain abbreviations for search expansion
├── phrase_glossary.yaml   # Multi-word concept expansion
├── known_flows.yaml       # Business flow entry points
└── docs/
    ├── flows/             # Flow documentation (YAML)
    └── gotchas/           # Gotchas & tips (Markdown)

profiles/pay-com/          # Active production profile (private repo)
├── config.json
├── conventions.yaml       # Org prefixes, domains, provider mappings
├── glossary.yaml
├── benchmarks.yaml        # Profile-specific benchmark queries
├── scripts/               # ~22 org-specific scripts (gitignored)
└── docs/
    ├── flows/             # 20 flow YAMLs
    ├── gotchas/           # Runtime traps
    ├── references/        # Stable structural knowledge
    └── providers/         # 52 provider API docs (Git LFS)
```

Switch profiles: `make profile PROFILE=pay-com`

---

## Conventions

### Code Style
- **Target Python:** 3.12 (ParamSpec / `datetime.UTC`)
- **Line length:** 120 (ruff in `pyproject.toml`)
- **Lint / format:** ruff (`make test` runs ruff + pytest)
- **Excluded from ruff:** `.claude/debug/`

### Git & Storage
- **No hardcoded org names in `src/`** — all org-specific data in `profiles/`
- **LFS filter:** `profiles/pay-com/docs/providers/**` via git-lfs
- **Generated dirs:** `db/`, `raw/`, `extracted/`, `logs/` — do not commit

### Build Pipeline Order
```
scripts/scrape/extract_artifacts.py → scripts/build/build_index.py → scripts/build/build_graph.py → scripts/build_vectors.py
```
- Full rebuild ~2–4h, peaks ~20GB RAM
- `build_index.py --incremental` re-indexes only changed repos
- Backup `task_history` before full rebuild

### Agent Rules
- Keep every search result — deprioritize low-confidence but include
- Improve base recall before tuning reranker
- Ground truth = `repos_changed` from Jira
- Recall over precision: false negatives worse than false positives
- Parallel agents by default; serialize agents touching same DB/files

---

## Dead / Legacy

Files deleted in the 2026-05-16 cleanup:

| File | Status |
|------|--------|
| `ab_test_baseline.json` | ✅ deleted |
| `eval_baseline.json` | ✅ deleted |
| `benchmark_results.json` | ✅ deleted |
| `graph-grpc-apm-payper.html` | ✅ deleted |
| `scripts/maint/audit_index_gaps.py` | ✅ deleted |
| `scripts/bench/bench_routing_e2e.py` | ✅ deleted |
| `scripts/build/build_eval_rebuild_bundle.py` | ✅ deleted |
| `scripts/build/build_eval_v2_llm_calibrated.py` | ✅ deleted |
| `scripts/data/rescore_against_clean.py` | ✅ deleted |
| `scripts/analysis/analyze_change_impact.py` (symlink) | ✅ deleted |
| `scripts/analysis/analyze_developer_patterns.py` (symlink) | ✅ deleted |
| `scripts/analysis/analyze_file_patterns.py` (symlink) | ✅ deleted |
| `profiles/pay-com/scripts/analyze_change_impact.py` | ✅ deleted |
| `profiles/pay-com/scripts/analyze_developer_patterns.py` | ✅ deleted |
| `profiles/pay-com/scripts/analyze_file_patterns.py` | ✅ deleted |

Still alive but note:
- `scripts/maint/generate_housekeeping_report.py` — called by `docs_validate_all.sh`
- `scripts/analysis/analyze_gaps.py` — symlink; imported by `auto_collect.py`
- `config.json` (root) — legacy; used by `setup_wizard.py`, `src/config.py`
- `patterns-export.json` — referenced by profile scripts

---

## Open Questions

1. **`bench_runs/` baselines** — Which early baselines are still referenced by active gates?
2. **`models/` mixed storage** — Only metadata tracked; weights untracked. Is this intentional?
3. **`setup_wizard.py` stale** — Last touched 8+ weeks ago. Still wired into `Makefile`.
4. **Doc validators not in git** — Six `validate_doc_*.py` scripts actively used but never committed. Keep ephemeral?
5. **`.claude/plans/` untracked** — Contains planning docs; some tracked, some not. Intentional?

---

## Backlinks

### To This Catalog
- [[profiles/pay-com/AGENTS.md|Pay-Com Profile]] links here via `[[../../AGENTS.md|Root Catalog]]`
- [[src/AGENTS.md|src/]] links here via `[[../AGENTS.md|Root Catalog]]`
- [[scripts/AGENTS.md|scripts/]] links here via `[[../AGENTS.md|Root Catalog]]`
- [[tests/AGENTS.md|tests/]] links here via `[[../AGENTS.md|Root Catalog]]`

### Cross-Catalog Navigation
| From | To | Wikilink |
|------|----|----------|
| Root | Pay-Com Profile | `[[profiles/pay-com/AGENTS.md|Pay-Com Profile]]` |
| Root | src/ | `[[src/AGENTS.md|src/]]` |
| Root | scripts/ | `[[scripts/AGENTS.md|scripts/]]` |
| Root | tests/ | `[[tests/AGENTS.md|tests/]]` |
| Pay-Com | Root | `[[../../AGENTS.md|Root Catalog]]` |
| src/ | Root | `[[../AGENTS.md|Root Catalog]]` |
| scripts/ | Root | `[[../AGENTS.md|Root Catalog]]` |
| tests/ | Root | `[[../AGENTS.md|Root Catalog]]` |

### Code References (Standard Markdown)
- `[src/config.py](src/config.py)` — for GitHub clickability
- `[Makefile](Makefile)` — for GitHub clickability
