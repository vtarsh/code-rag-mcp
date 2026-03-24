# /pattern-mine — Pattern Mining from Task History

Analyze task_history data to discover actionable patterns for improving recall/precision.

## Usage
```
/pattern-mine                       # Full pattern mining suite
/pattern-mine --co-change           # Co-change pairs only
/pattern-mine --hubs                # Hub analysis only
/pattern-mine --domain=PI           # Domain-specific patterns
/pattern-mine --since=2026-03-01    # Only recent tasks
```

## What It Does

Launches pattern-miner agent (`.claude/agents/pattern-miner.md`) to run:

1. **Co-change pairs** — repos that always change together (>80% conditional probability, n>=3)
2. **Never-alone repos** — repos that never change in isolation (schemas, credentials, features)
3. **Hub analysis** — repos with disproportionate BFS fan-out (libs-types has 423 dependents)
4. **Domain template validation** — check if domain templates match actual task patterns
5. **Missed repo analysis** — find repos frequently missed by the tool and root causes

## Output

Structured JSON with:
- Actionable co-change rules for conventions.yaml
- Hub penalty candidates
- Domain template updates
- New mechanism suggestions

## When to Run

Trigger points (from continuous improvement cycle):
- After any recall/precision benchmark shows improvement
- After adding new tasks to task_history
- After modifying cascade/co-occurrence/classifier logic
- After every 10th deep analysis task
- After every overnight batch completes

## Rules
- Patterns from <3 tasks are noise — ignore them
- Focus on ACTIONABLE findings that can become mechanisms
- Report sample sizes with every finding
