# /deep-analysis — Deep Task Analysis

Deeply analyze Jira tasks to find ALL repos that should be involved, comparing independent grep discovery vs MCP RAG tool output.

## Usage
```
/deep-analysis PI-40           # Single task, Tier 1
/deep-analysis PI-40,PI-5      # Multiple tasks
/deep-analysis --batch=PI      # All PI tasks
/deep-analysis --tier=1        # Only Tier 1 tasks
```

## What It Does

1. **Classify task tier** (1-4) based on repo count and summary keywords
2. **Gather task context** from task_history (summary, description, repos_changed, files_changed)
3. **Independent discovery** — grep raw/ for provider names, keywords, graph edges, similar tasks
4. **Tool discovery** — run benchmark_recall.py for comparison
5. **Compare & classify** each expected repo as found/missed with root cause codes
6. **Output** structured JSON with recall metrics and improvement suggestions

## Tier Classification

| Tier | Criteria | Agent Ratio |
|------|----------|-------------|
| 1 | 5+ repos, "integration"/"provider"/"apm" | 1 agent : 1 task |
| 2 | 2-5 repos, "add"/"implement"/"webhook" | 1 agent : 2-3 tasks |
| 3 | 2+ repos, cross-cutting/config | 1 agent : 3-4 tasks |
| 4 | 1 repo, bug fix/refactor | 1 agent : 5-10 tasks |

## Agent Prompt
Use `.claude/agents/deep-analysis.md` for sub-agent instructions.

## Batching Rules
- Launch 3 parallel agents per batch
- Pattern mine every 10 tasks
- Independent agents must NOT use MCP tools (verify output)
