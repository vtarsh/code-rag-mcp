# Testing Methodology — analyze_task recall

## How Recall is Measured

### Ground Truth (what actually changed)

Source: `task_history` table in knowledge.db, populated by `scripts/collect_task.py`.

For each Jira task (PI-54, CORE-2586, BO-1598, etc.):
- `repos_changed` — JSON list of repos from merged PRs (GitHub API)
- `files_changed` — JSON list of specific files
- `developer` — who did the work
- `summary` — Jira summary (short)
- `description` — Jira description (detailed)

Collection: `python3 scripts/collect_task.py PI-54` or `python3 scripts/auto_collect.py --projects=PI,CORE,BO,HS`

### Prediction (what analyze_task finds)

We call `_analyze_task_impl(conn, summary + " " + ticket_id, "")` and extract all **bold repo names** from the markdown output via regex:

```python
found = {m.group(1) for m in re.finditer(r'\*\*([a-z][a-z0-9-]+)\*\*', result)
         if not m.group(1).startswith(('todo','ok','done','in-progress','check','found'))}
```

### Recall Formula

```
recall = len(actual ∩ found) / len(actual) × 100%
```

Per-task recall, then averaged by group (CORE, PI, BO, HS).

### Running Benchmarks

```bash
# Full recall on all 105 tasks
python3 scripts/benchmark_recall.py

# By group
python3 scripts/benchmark_recall.py --group=CORE
python3 scripts/benchmark_recall.py --group=PI,BO

# Single task (verbose — shows missed repos)
python3 scripts/benchmark_recall.py --task=CORE-2586

# Search quality benchmarks
python3 scripts/benchmark_queries.py      # conceptual (baseline: 0.85)
python3 scripts/benchmark_realworld.py    # realworld (baseline: 0.83)
python3 scripts/benchmark_flows.py        # flow completeness (baseline: 0.875)
```

### Important Caveats

1. **Summary-only input**: Tests use Jira summary, not full description. Real usage with full description gives +5-10% for BO tasks.
2. **GitHub API patched out**: `benchmark_recall.py` mocks GitHub API calls (branches/PRs) to avoid timeouts. Section 7 (GitHub Activity) returns empty in tests.
3. **Batch PRs**: Some tasks have repos changed "за компанію" (developer fixed unrelated things in same PR). These inflate ground truth and lower measured recall.
4. **Ground truth noise**: Package.json bumps (dependency version updates) appear as repo changes but aren't real code changes.

## How We Improve Recall (Iterative Process)

### Step 1: Identify Missed Repos

```bash
python3 scripts/benchmark_recall.py --task=CORE-2580
# Output: 57% (4/7) missed=['grpc-core-settings', 'grpc-core-transactions', 'grpc-risk-engine']
```

### Step 2: Analyze Why (Background Agents)

Launch audit agents that investigate each missed repo:
- Is it in the dependency graph? What edges connect it?
- Does task_history show co-occurrence with found repos?
- Are there keywords in the description that match repo content?
- Is it reachable from seed repos via cascade?

**Critical**: Agents must NOT know what was changed — they investigate independently, like a developer would.

### Step 3: Add Generic Mechanism

Pattern-based fix, never hardcoded repo name. Examples:
- "repos with high in-degree that seeds depend on" (downstream walk)
- "repos that co-change ≥40% from task_history" (co-occurrence)
- "all providers via gateway routing when proto repos affected" (fan-out)

### Step 4: Test Before/After

```bash
# Before
python3 scripts/benchmark_recall.py --group=CORE  # 78.1%

# ... make change ...

# After
python3 scripts/benchmark_recall.py --group=CORE  # 83.2%

# Verify no regression in other groups
python3 scripts/benchmark_recall.py               # total still ≥ 80%
```

### Step 5: Commit + Update Tracker

Update `profiles/pay-com/RECALL-TRACKER.md` with new scores.

## Validation Without MCP (Manual/Agent Approach)

For validating a specific task end-to-end without using the MCP tool:

### 1. Pull task from Jira
```bash
python3 scripts/collect_task.py CORE-2586
```

### 2. Independent research (background agents)
Each agent gets only the task summary, no hints. They:
- Clone relevant repos if needed (`gh repo clone pay-com/grpc-core-schemas`)
- `grep -r "createAudit" extracted/` to find function usage
- Trace dependency graph: `SELECT source FROM graph_edges WHERE target = 'node-libs-tools'`
- Search PRs: `gh pr list --repo pay-com/grpc-core-schemas --search "audit"`

### 3. Compare with MCP output
Separate agents call analyze_task via MCP and compare what it found vs what manual research found.

### 4. Identify gaps
Whatever manual agents found but MCP missed = improvement opportunity.

## Current Scores (2026-03-22)

| Group | Recall | Tasks |
|-------|--------|-------|
| CORE | 83.6% | 50 |
| PI | 78.5% | 41 |
| BO | 66.7% | 9 |
| HS | 100% | 5 |
| **Total** | **80.8%** | **105** |

Search benchmarks: conceptual 0.85, realworld 0.83, flows 0.875
Unit tests: 126 passing
