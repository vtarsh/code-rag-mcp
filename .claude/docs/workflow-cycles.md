# Workflow Cycles Reference

## Task Validation Cycle (recall/quality work)

1. **Collect** — gather tasks from Jira (`/collect-tasks`)
2. **Validate independently** — agents search WITHOUT MCP RAG (grep on raw/, git log). Zero mcp__code-rag-mcp__* calls.
3. **Validate via MCP RAG** — parallel agents use MCP tools
4. **Compare** — main session compares, identifies misses, categorizes root causes
5. **Improve** — fix generic code (src/), update profile data (profiles/)
6. **Benchmark** — `/recall-test` before AND after. Never regress.
7. **Repeat**

## Continuous Improvement (trigger points)

After benchmark improvement, adding tasks, modifying cascade/classifier, every 10th deep analysis:

1. Benchmark recall + precision before and after
2. Pattern mine (`/pattern-mine`)
3. Implement if pattern found, benchmark again
4. Update RECALL-TRACKER.md baselines
