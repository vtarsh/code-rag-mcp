# Workflow Rules

## Core Principle: Parallel Agents by Default

- Distribute ALL independent tasks to parallel background agents. Don't ask — just do it.
- Never run parallel features that touch the same DB/files.
- Sequential feature dev: build → audit (5 parallel agents) → test (5 parallel agents) → conclusions → next feature.

## Task Validation Cycle (for recall/quality work)

1. **Collect** — gather tasks from Jira in batches, parallel agents for GitHub PR enrichment.
2. **Validate independently** — launch background agents that search for repos WITHOUT MCP RAG (only grep on `raw/`, git log, file reads). Verify agent output contains zero `mcp__pay-knowledge__*` tool calls — if it does, result is invalid.
3. **Validate via MCP RAG** — in parallel, other agents use MCP RAG tools (analyze_task, search, etc.) to find repos.
4. **Compare** — main session compares both results, identifies misses, categorizes root causes.
5. **Improve** — implement fixes in generic code (src/), update profile data (profiles/pay-com/), add patterns.
6. **Benchmark** — run `benchmark_recall.py` before AND after changes. Never regress.
7. **Repeat** — next batch of tasks or next improvement area.

## Continuous Improvement Cycle (automatic, don't wait for user)

After EVERY code change or mechanism improvement:
1. **Benchmark** — run recall + precision before and after
2. **Pattern mine** — launch background agent to search for new patterns in task_history
3. **Implement** — if pattern found, implement and benchmark again
4. **Update .claude** — update lessons.md, testing.md baselines, NEXT-SESSION-PROMPT

This cycle runs automatically — don't wait for user to say "search for patterns".
Trigger points:
- After any recall/precision benchmark shows improvement
- After adding new tasks to task_history
- After modifying cascade/co-occurrence/classifier logic
- After every 5th Tier 1 deep analysis

When running overnight/background: set a recurring cron every 60 min that checks if improvements were made and triggers pattern mining if so.

## Improvement Targets (priority order)

1. **Generic mechanisms** (src/) — patterns that work for any org.
2. **Profile data** (profiles/pay-com/) — conventions, glossary, known_flows, gotchas.
3. **Private profile repo** (vtarsh/pay-knowledge-profile) — org-specific scripts, configs.
4. **PI is primary focus** — other groups help find patterns, but PI recall matters most.

## GitHub Policy

- **pay-com org is READ-ONLY**: no PR comments, no issues, no pushes, no reviews via automation.
- All output → local logs only. User reviews and posts manually.
- Only lift when user explicitly says "enable writing to pay-com".

## GitHub Accounts

- `vtarsh` — personal GitHub (code-rag-mcp, pay-knowledge-profile).
- `tarshevskiy-v` — work GitHub (pay-com org repos).

## Gotchas Are Temporary

- Gotchas bootstrap the system; the goal is to eliminate them.
- Build smarter analysis, not bigger checklists.
