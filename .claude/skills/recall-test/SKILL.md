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
   cd ~/.pay-knowledge
   CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 scripts/benchmark_queries.py
   CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 scripts/benchmark_realworld.py
   CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 scripts/benchmark_flows.py
   CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 scripts/benchmark_recall.py
   ```

2. **Compare against baselines** (do not regress):
   - Conceptual: 0.85
   - Real-world: 0.83
   - Flows: 0.875
   - analyze_task recall: 96.4% total (phantom-filtered) on 361 tasks
   - PI: 97.6%, CORE: 94.3%, BO: 98.0%, HS: 92.9%

3. **Report** pass/fail for each benchmark with delta from baseline

## Rules
- Run BEFORE and AFTER any search/indexing/analyze_task changes
- Phantom-filtered recall is the primary metric
- Always report which ground truth is used
- Never regress baselines without explicit approval
