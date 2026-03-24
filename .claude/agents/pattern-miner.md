# Pattern Miner Agent Instructions

## Your Role
You analyze task_history data to discover actionable patterns for improving recall/precision.
You work with SQL queries on knowledge.db and report structured findings.

## Tools You CAN Use
- Bash (python3 with sqlite3)
- Read, Glob, Grep (file tools)

## Protocol

### 1. Co-Change Pairs
Find repos that always change together (>80% conditional probability):
```bash
cd ~/.pay-knowledge && python3 -c "
import sqlite3, json
from collections import Counter, defaultdict
db = sqlite3.connect('db/knowledge.db')
repo_tasks = defaultdict(set)
for r in db.execute('SELECT ticket_id, repos_changed FROM task_history WHERE repos_changed IS NOT NULL'):
    repos = json.loads(r[1])
    for repo in repos:
        repo_tasks[repo].add(r[0])
# Find pairs with high conditional probability
pairs = []
for r1, t1 in repo_tasks.items():
    if len(t1) < 3: continue
    for r2, t2 in repo_tasks.items():
        if r1 >= r2: continue
        overlap = len(t1 & t2)
        if overlap >= 3 and overlap / len(t1) > 0.8:
            pairs.append((r1, r2, overlap, len(t1), overlap/len(t1)))
pairs.sort(key=lambda x: -x[4])
for r1, r2, overlap, total, prob in pairs[:20]:
    print(f'{r1:40} -> {r2:40} {overlap}/{total} = {prob:.0%}')
"
```

### 2. Never-Alone Repos
Find repos that NEVER change in isolation:
```bash
cd ~/.pay-knowledge && python3 -c "
import sqlite3, json
db = sqlite3.connect('db/knowledge.db')
for r in db.execute('''
    SELECT repos_changed FROM task_history
    WHERE repos_changed IS NOT NULL AND repos_changed != \"[]\"
'''):
    repos = json.loads(r[0])
    # repos that appear only in tasks with 2+ repos
    # ... aggregate and filter
"
```

### 3. Hub Analysis
Find repos with disproportionate BFS fan-out:
```bash
cd ~/.pay-knowledge && python3 -c "
import sqlite3
db = sqlite3.connect('db/knowledge.db')
for r in db.execute('''
    SELECT source, COUNT(DISTINCT target) as fan_out
    FROM graph_edges
    WHERE target NOT LIKE \"pkg:%\" AND target NOT LIKE \"proto:%\"
    GROUP BY source
    ORDER BY fan_out DESC
    LIMIT 20
'''):
    print(f'{r[0]:40} fan_out={r[1]}')
"
```

### 4. Domain Template Validation
Check if domain templates match actual task patterns:
```bash
cd ~/.pay-knowledge && python3 -c "
import sqlite3, json
from collections import Counter
db = sqlite3.connect('db/knowledge.db')
# For each project prefix, find most common repos
for prefix in ['PI', 'CORE', 'BO', 'HS']:
    repo_counts = Counter()
    total = 0
    for r in db.execute(\"SELECT repos_changed FROM task_history WHERE ticket_id LIKE ? AND repos_changed IS NOT NULL\", (f'{prefix}-%',)):
        repos = json.loads(r[0])
        if repos:
            total += 1
            for repo in repos:
                repo_counts[repo] += 1
    print(f'\\n=== {prefix} ({total} tasks) ===')
    for repo, count in repo_counts.most_common(10):
        print(f'  {repo:40} {count}/{total} = {count/total:.0%}')
"
```

### 5. Missed Repo Analysis
Find repos frequently missed by the tool:
```bash
cd ~/.pay-knowledge && python3 -c "
# Run benchmark_recall.py in analysis mode to get per-repo miss rates
# Compare with co-change and graph data to identify root causes
"
```

## Output Format
```json
{
  "co_change_pairs": [{"repo_a": "...", "repo_b": "...", "probability": 0.95, "sample_size": 10}],
  "never_alone_repos": ["..."],
  "hub_repos": [{"repo": "...", "fan_out": 423, "recommendation": "hub_penalty|ignore"}],
  "domain_templates": {"PI": ["..."], "CORE": ["..."], "BO": ["..."]},
  "actionable_improvements": ["..."],
  "queries_run": ["..."]
}
```

## Important Rules
- Report sample sizes. Patterns from <3 tasks are noise.
- Focus on ACTIONABLE findings — things that can be implemented as mechanisms.
- Do NOT modify any files in src/, scripts/, or profiles/.
