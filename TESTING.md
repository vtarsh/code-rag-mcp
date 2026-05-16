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
# Full recall on all benchmarkable tasks (current set: 361 — see RECALL-TRACKER)
python3 scripts/bench/benchmark_recall.py

# By group
python3 scripts/bench/benchmark_recall.py --group=CORE
python3 scripts/bench/benchmark_recall.py --group=PI,BO

# Single task (verbose — shows missed repos)
python3 scripts/bench/benchmark_recall.py --task=CORE-2586

# Search quality benchmarks
python3 scripts/bench/benchmark_queries.py      # conceptual (baseline: 0.85)
python3 scripts/bench/benchmark_realworld.py    # realworld (baseline: 0.83)
python3 scripts/bench/benchmark_flows.py        # flow completeness (baseline: 0.875)
```

### Important Caveats

1. **Summary-only input**: Tests use Jira summary, not full description. Real usage with full description gives +5-10% for BO tasks.
2. **GitHub API patched out**: `benchmark_recall.py` mocks GitHub API calls (branches/PRs) to avoid timeouts. Section 7 (GitHub Activity) returns empty in tests.
3. **Batch PRs**: Some tasks have repos changed "за компанію" (developer fixed unrelated things in same PR). These inflate ground truth and lower measured recall.
4. **Ground truth noise**: Package.json bumps (dependency version updates) appear as repo changes but aren't real code changes.

## How We Improve Recall (Iterative Process)

### Step 1: Identify Missed Repos

```bash
python3 scripts/bench/benchmark_recall.py --task=CORE-2580
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
python3 scripts/bench/benchmark_recall.py --group=CORE  # 78.1%

# ... make change ...

# After
python3 scripts/bench/benchmark_recall.py --group=CORE  # 83.2%

# Verify no regression in other groups
python3 scripts/bench/benchmark_recall.py               # total still ≥ 80%
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

## Auto-Collection

`auto_collect.py` — batch collection of recently closed Jira tasks.

```bash
# Collect PI + CORE tasks closed in last 30 days
python3 scripts/auto_collect.py --projects=PI,CORE --days=30

# Dry run (show what would be collected)
python3 scripts/auto_collect.py --projects=PI,CORE,BO,HS --days=90 --dry-run
```

Runs: Jira JQL search → for each new task → `collect_task.py` → `cross_validate_task.py` → `analyze_gaps.py`.
Scheduled via launchd every 6 hours.

## benchmarks.yaml

Profile-specific benchmark queries in `profiles/{name}/benchmarks.yaml`:

```yaml
queries:
  - id: Q1-concept
    query: "What retry constants does X use?"
    expected_repos: [repo-a, repo-b]
    expected_content: ["RETRY_COUNT", "MAX_RETRIES"]

  - id: Q2-realworld
    query: "How does payment flow work for provider Y?"
    expected_repos: [repo-c, repo-d]
    min_recall: 0.8
```

Used by `benchmark_queries.py` (conceptual) and `benchmark_realworld.py` (real-world).
Each query has expected repos/content — score = weighted recall of expected items found.

## Agent-Based Validation (Isolated, No Hints)

The most thorough validation uses **parallel background agents** that work independently:

### Phase 1: Background Agents (parallel, isolated, no hints)

Two types of agents run in parallel. Each gets ONLY the task summary. No ground truth, no expected repos.

**MCP agents** — call `analyze_task` MCP tool, return what the tool found.

**Manual agents** — search independently WITHOUT MCP:
- `grep -r "functionName" extracted/` to find usage across repos
- Direct SQL on graph: `SELECT source FROM graph_edges WHERE target = 'repo-x'`
- `gh pr list --repo org/repo --search "keyword"`
- `git clone` + local code analysis if needed
- task_history queries for similar past tasks

### Phase 2: Synthesis by Main Session

The main session collects results from ALL agents and compares:
- What did MCP agents find? (= what our tool produces)
- What did manual agents find? (= what a thorough developer would find)
- What's in the ground truth? (= what actually changed)

**MCP found but manual didn't** → tool works well here.
**Manual found but MCP didn't** → gap, needs new mechanism.
**Neither found** → very hard to predict, likely edge case.

Main session decides what generic mechanism to add based on gaps.

### Why Isolated Agents Matter

- **No confirmation bias**: agents don't know what we expect to find
- **Different angles**: one agent greps code, another traces graph, another checks PRs
- **Honest assessment**: if agent finds something MCP missed, it's a real gap
- **Reproducible**: any session can re-run the same validation

Example agent prompt (for PI task validation):
```
"You are analyzing task PI-54: Trustly verification flow.
Find ALL repos that would need changes. Use grep, graph queries,
task_history. Report your findings."
```

No mention of analyze_task, no expected repos, no hints.

## Current Scores

Live baselines (per profile) live in `profiles/{name}/RECALL-TRACKER.md` — that file is the source of truth, including the improvement log.
