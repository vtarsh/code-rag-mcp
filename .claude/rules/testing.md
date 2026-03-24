# Testing Rules

## Unit Tests

```bash
cd ~/.pay-knowledge && python -m pytest tests/ -q
```

- Pre-commit hook runs: ruff + ruff-format + pytest. Fix lint before committing.
- Always run full test suite before committing.

## Benchmark Suite

Run after any search, indexing, or analyze_task changes:

```bash
python scripts/benchmark_queries.py    # Conceptual search quality
python scripts/benchmark_realworld.py  # Real-world search quality
python scripts/benchmark_flows.py      # Flow completeness (Q1-Q5)
python scripts/benchmark_recall.py     # analyze_task recall regression
```

## Current Baselines (do not regress)

Live baselines: `profiles/pay-com/RECALL-TRACKER.md` (single source of truth).
Key thresholds: conceptual ≥0.85, realworld ≥0.83, flows ≥0.875.

## Recall Methodology

- Ground truth from task_history table + manual validation.
- See `TESTING.md` for full methodology and how to add new ground truth.
- Recall over precision: false negatives are worse than false positives.
