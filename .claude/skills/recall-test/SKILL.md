# /recall-test — Recall Benchmark Suite

Run the full recall/precision benchmark suite and report results.

## Usage
```
/recall-test                    # Full suite (all 4 benchmarks)
/recall-test --recall-only      # Only benchmark_recall.py
/recall-test --task=PI-40       # Single task recall
/recall-test --compare          # Compare before/after (stash baseline)
```

## What It Does

1. **Run benchmarks** in sequence:
   ```bash
   cd ~/.code-rag-mcp
   CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 scripts/benchmark_queries.py
   CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 scripts/benchmark_realworld.py
   CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 scripts/benchmark_flows.py
   CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 scripts/benchmark_recall.py
   ```

2. **Compare against baselines** (do not regress) — live numbers in `profiles/pay-com/RECALL-TRACKER.md` (single source of truth). Conceptual / real-world / flows baselines + per-group recall (PI/CORE/BO/HS) all live there.

3. **Report** pass/fail for each benchmark with delta from baseline

## Rules
- Run BEFORE and AFTER any search/indexing/analyze_task changes
- Phantom-filtered recall is the primary metric
- Always report which ground truth is used
- Never regress baselines without explicit approval
