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
Structure: see `ARCHITECTURE.md`. Setup: see `.claude/rules/conventions.md` (Org Isolation).

## Architecture

See `ARCHITECTURE.md` for full system design, dependency direction, and module map.

Summary: `daemon.py` (HTTP on :8742, holds ML models) + `mcp_server.py` (thin stdio proxy).
All sessions share one daemon process. Proxy auto-starts daemon if not running.

## Tools (12 total)

Single source of truth for tool list: `~/.claude/CLAUDE.md` (Available MCP Tools section).
Key tools: `search`, `analyze_task`, `context_builder`, `trace_flow`, `find_dependencies`.

## Gotchas (critical)

- `analyze/` is a package (8 modules) -- add new domains via classifier.py + new analyzer file
- Daemon restart: `kill -9 $(lsof -ti:8742); sleep 2; CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 daemon.py &disown`
- See `.claude/rules/conventions.md` (Data Changes) for build pipeline and FTS5/glossary constraints.
