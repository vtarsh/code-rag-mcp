# code-rag-mcp — MCP RAG Server

Generic RAG system for indexing any GitHub org's codebase.
Git: vtarsh/code-rag-mcp (personal account).
Python 3.12, FastMCP, SQLite FTS5, LanceDB, CrossEncoder reranker.

**Key docs** (read for full context):
- **`profiles/pay-com/ROADMAP.md` P0** — migrate to local embedding + reranker (Gemini-free, weekend work; see 2026-04-14 incident)
- `ARCHITECTURE.md` — system design, analyze_task package, 10 generic mechanisms, conventions.yaml
- `.claude/rules/conventions.md` — always-loaded generic rules
- `.claude/docs/` — data-changes, workflow-cycles, development-pattern (on-demand generic reference)
- `.claude/agents/` — generic agents (deep-analysis, pattern-miner); agents prefixed `pay-` are profile-scoped and assume ACTIVE_PROFILE=pay-com
- `profiles/pay-com/docs/gotchas/` — runtime traps only: per-provider (grpc-apm-nuvei/payper/trustly, grpc-providers-features, next-web-pay-with-bank, phone-for-payout), clickhouse-cdc, e2e-tests, debugging, global-conventions
- `profiles/pay-com/docs/references/` — stable structural knowledge: impact-audit-rules, impact-audit-catalog, investigation-framework, payment-flow, provider-response-mapping, async-flow-processors (s2s vs SDK queueing), s2s-vs-sdk (provider integration modes), do-not-expire-matrix (per-provider expiration handling), code-style
- `TESTING.md` — recall methodology, how to measure/improve, validation without MCP
- `profiles/pay-com/RECALL-TRACKER.md` — current scores, improvement log

## Commands

```bash
# Tests (Python 3.11+ required for ParamSpec / datetime.UTC)
cd ~/.code-rag-mcp && python3.12 -m pytest tests/ -q

# Benchmarks (run after search pipeline changes)
python scripts/benchmark_queries.py && python scripts/benchmark_realworld.py
python scripts/benchmark_flows.py  # Flow completeness (Q1-Q5 validation)

# Blind spot detection
python scripts/detect_blind_spots.py

# Full rebuild (clones -> extracts -> indexes -> graph -> vectors -> benchmarks)
make build  # or: ACTIVE_PROFILE=my-org ./scripts/full_update.sh --full

# Start daemon (normally auto-started by launchd, or by mcp_server.py proxy)
python daemon.py

# Incremental build (only changed repos; ~30-60 min typical when many repos changed)
# NOTE: full rebuild (`make build`) peaks ~20GB RAM, run overnight on 16GB Macs
python scripts/build_index.py --incremental

# MCP proxy (auto-started by Claude Code/Desktop -> forwards to daemon)
python mcp_server.py
```

## Profile System

Org-specific data lives in `profiles/{name}/` (git-ignored except `profiles/example/`).
Active profile: `ACTIVE_PROFILE` env var or `.active_profile` file.
Structure: see `ARCHITECTURE.md`. Setup: `cd profiles/{name} && ./install.sh`.

## Architecture

See `ARCHITECTURE.md` for full system design, dependency direction, and module map.

Summary: `daemon.py` (HTTP on :8742, holds ML models) + `mcp_server.py` (thin stdio proxy).
All sessions share one daemon process. Proxy auto-starts daemon if not running.

## Tools (11 MCP + 5 daemon-only)

11 tools exposed via `mcp_server.py` for Claude Code: search, analyze_task, trace_field,
trace_chain, trace_flow, trace_impact, trace_internal, repo_overview, list_repos,
provider_type_map, health_check.

5 additional tools routed through `daemon.py` (accessible via `cli.py` HTTP client for
sub-agents without MCP access): find_dependencies, context_builder, visualize_graph,
diff_provider_config, search_task_history.

Tool inventory is generated from `mcp_server.py` + `daemon.py` (source of truth is the code itself, not a duplicated list).

## MCP Call Tracker

Every tool call is logged to `logs/tool_calls.jsonl` by the daemon (tool name, args, duration, result preview, source).
Captures both MCP calls and CLI calls (subagents). Source field: `mcp`, `cli`, or `direct`.

```bash
python scripts/analyze_calls.py              # usage summary
python scripts/analyze_calls.py --last 20    # recent calls
python scripts/analyze_calls.py --sessions   # per-session breakdown
```

**When starting a new session**: if `logs/tool_calls.jsonl` has data, run `analyze_calls.py` first to see tool usage patterns before making UX/tool changes.

## Gotchas (critical)

- `analyze/` is a package (13 modules) -- add new domains via classifier.py + new analyzer file
- Daemon restart: `kill -9 $(lsof -ti:8742); sleep 2; CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 daemon.py &disown`
- See `.claude/docs/data-changes.md` for build pipeline and FTS5/glossary constraints.
