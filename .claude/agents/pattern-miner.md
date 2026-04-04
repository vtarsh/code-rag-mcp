# Pattern Miner Agent Instructions

> **Path assumption**: All commands use `~/.code-rag-mcp`. If `$CODE_RAG_HOME` is set, substitute it.

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
cd ~/.code-rag-mcp && python3 -c "
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
cd ~/.code-rag-mcp && python3 -c "
import sqlite3, json
from collections import defaultdict
db = sqlite3.connect('db/knowledge.db')
solo = defaultdict(int)    # times repo changed alone
group = defaultdict(int)   # times repo changed with others
for r in db.execute('''
    SELECT repos_changed FROM task_history
    WHERE repos_changed IS NOT NULL AND repos_changed != \"[]\"
'''):
    repos = json.loads(r[0])
    if len(repos) == 1:
        solo[repos[0]] += 1
    else:
        for repo in repos:
            group[repo] += 1
all_repos = set(solo) | set(group)
never_alone = [(repo, group[repo]) for repo in all_repos if solo[repo] == 0 and group[repo] >= 3]
never_alone.sort(key=lambda x: -x[1])
for repo, count in never_alone[:20]:
    print(f'{repo:40} always with others ({count} tasks)')
"
```

### 3. Hub Analysis
Find repos with disproportionate BFS fan-out:
```bash
cd ~/.code-rag-mcp && python3 -c "
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
cd ~/.code-rag-mcp && python3 -c "
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
cd ~/.code-rag-mcp && python3 -c "
import sqlite3, json
from collections import Counter
db = sqlite3.connect('db/knowledge.db')
missed = Counter()
total_tasks = 0
for r in db.execute('''
    SELECT ticket_id, repos_changed, repos_predicted
    FROM task_history
    WHERE repos_changed IS NOT NULL AND repos_predicted IS NOT NULL
'''):
    expected = set(json.loads(r[1]))
    predicted = set(json.loads(r[2])) if r[2] else set()
    if not expected: continue
    total_tasks += 1
    for repo in expected - predicted:
        missed[repo] += 1
print(f'Total tasks with predictions: {total_tasks}')
print(f'\\nMost frequently missed repos:')
for repo, count in missed.most_common(20):
    print(f'  {repo:40} missed {count}/{total_tasks} = {count/total_tasks:.0%}')
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
