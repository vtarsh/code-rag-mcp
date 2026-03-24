# /overnight — Overnight Autonomous Work

Set up overnight autonomous improvement cycle with recurring crons.

## Usage
```
/overnight                      # Start default overnight cycle
/overnight --focus=recall       # Focus on recall improvement
/overnight --focus=analysis     # Focus on deep analysis batch
/overnight --roadmap            # Show current overnight TODO list
```

## What It Does

Sets up a recurring work cycle (cron every 30-60 min) that:

1. **Checks what's done** from the overnight TODO list
2. **Picks next item** and executes it
3. **Benchmarks** after each change
4. **Pattern mines** every 10 tasks analyzed
5. **Updates .claude rules** with new lessons

## Default Overnight Cycle

1. Run deep analysis on next batch of tasks (3 agents per batch)
2. After every 10 tasks, run pattern mining agent
3. If patterns found, implement improvements in src/
4. Run benchmark_recall.py after each improvement
5. If recall improved, update baselines in testing.md
6. Pick next batch and repeat

## Cron Setup
```bash
# Recurring "continue" cron every 30 min
# Checks overnight-todo.yaml, picks next item, executes
```

## Rules
- Add recurring "continue" cron (every 30-60 min) — one-shot crons waste time if they finish early
- Don't create watchdog crons that only report — make them ACT
- 3 agents per batch for deep analysis
- Pattern mine every 10 tasks
- Never run parallel features that touch the same DB/files
- Benchmark before AND after every change — never regress
