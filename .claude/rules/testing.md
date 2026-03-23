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

- Conceptual: 0.85
- Real-world: 0.83
- Flows: 0.875
- analyze_task recall: 96.4% total (phantom-filtered) on 361 tasks
  - PI: 97.6%, CORE: 94.3%, BO: 98.0%, HS: 92.9%
  - 20 mechanisms + hub penalty + domain templates + Gemini re-ranker
  - Re-ranker (--rerank): 81% recall, 34% precision, F1=48% on PI
- Unit tests: 133

## Recall Methodology

- Ground truth from task_history table + manual validation.
- See `TESTING.md` for full methodology and how to add new ground truth.
- Recall over precision: false negatives are worse than false positives.
