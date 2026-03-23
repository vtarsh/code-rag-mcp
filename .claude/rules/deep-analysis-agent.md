# Deep Analysis Agent Instructions

## Your Role
You deeply analyze ONE Jira task to find ALL repos that should be involved.
You work INDEPENDENTLY — without MCP RAG tools.

## Tools You CAN Use
- Bash (grep, git log, find, python3 with sqlite3)
- Read, Glob, Grep (file tools)
- DO NOT use any `mcp__pay-knowledge__*` tools. If your output contains MCP calls, the result is INVALID.

## Protocol

### Step 1: Gather Task Context
```bash
cd ~/.pay-knowledge && python3 -c "
import sqlite3, json
db = sqlite3.connect('db/knowledge.db')
db.row_factory = sqlite3.Row
r = db.execute('SELECT * FROM task_history WHERE ticket_id = ?', ('TASK-ID',)).fetchone()
print('Summary:', r['summary'])
print('Description:', (r['description'] or '')[:1000])
print('Type:', r['ticket_type'])
print('Developer:', r['developer'])
print('Expected repos:', json.loads(r['repos_changed']) if r['repos_changed'] else [])
files = json.loads(r['files_changed']) if r['files_changed'] else []
print('Files count:', len(files))
print('Files (first 20):', files[:20])
# Identify phantom repos (in repos_changed but 0 files)
repos = set(json.loads(r['repos_changed']) if r['repos_changed'] else [])
repos_with_files = {f.split('/')[0] for f in files if '/' in f}
phantoms = repos - repos_with_files if repos_with_files else set()
print('Phantom repos (0 files):', sorted(phantoms))
"
```

### Step 2: Independent Repo Discovery
Extract keywords from summary + description. For each keyword/entity:

1. **Provider name search** (if PI task):
   ```bash
   grep -rl "provider_name" ~/.pay-knowledge/raw/*/methods/ ~/.pay-knowledge/raw/*/libs/ ~/.pay-knowledge/raw/*/consts* 2>/dev/null | sed 's|.*/raw/||;s|/.*||' | sort -u
   ```

2. **Keyword search across all repos**:
   ```bash
   grep -rl "keyword" ~/.pay-knowledge/raw/*/methods/ ~/.pay-knowledge/raw/*/libs/ ~/.pay-knowledge/raw/*/src/ 2>/dev/null | sed 's|.*/raw/||;s|/.*||' | sort -u | head -20
   ```

3. **Graph edges** — find what connects to known repos:
   ```bash
   cd ~/.pay-knowledge && python3 -c "
   import sqlite3
   db = sqlite3.connect('db/knowledge.db')
   for row in db.execute('SELECT DISTINCT target, edge_type FROM graph_edges WHERE source = ? AND target NOT LIKE \"pkg:%\"', ('REPO-NAME',)):
       print(f'  {row[0]:40} ({row[1]})')
   "
   ```

4. **Similar past tasks** — find tasks with overlapping keywords:
   ```bash
   cd ~/.pay-knowledge && python3 -c "
   import sqlite3, json
   db = sqlite3.connect('db/knowledge.db')
   for r in db.execute(\"SELECT ticket_id, summary, repos_changed FROM task_history WHERE summary LIKE '%KEYWORD%' AND ticket_id != 'TASK-ID' LIMIT 5\"):
       print(f'{r[0]}: {r[1][:60]} -> {json.loads(r[2]) if r[2] else []}')
   "
   ```

5. **File path trace** — if files_changed exist, find what repos they connect to via graph

### Step 3: Also Run analyze_task (for comparison)
```bash
cd ~/.pay-knowledge && CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 scripts/benchmark_recall.py --task=TASK-ID 2>&1
```

### Step 4: Compare & Classify
For EACH expected repo, classify as one of:

| Status | Root Cause Code | Meaning |
|--------|----------------|---------|
| found | - | Your independent search found it |
| missed | `phantom` | Repo has 0 files_changed — likely not really involved |
| missed | `no_keyword_signal` | Task description has no words matching this repo |
| missed | `no_code_reference` | Provider/keyword not in repo code on main branch |
| missed | `graph_gap` | No graph edge connecting to this repo from found repos |
| missed | `synonym_mismatch` | Description uses different term than repo code |
| missed | `no_provider_prefix` | Repo doesn't follow naming convention |
| missed | `transitive_only` | Reachable only via 2+ hops, cascade too shallow |
| missed | `co_change_only` | Always changes with others but no static dependency |
| missed | `description_missing` | Task description too vague to infer this repo |
| missed | `domain_misclass` | Task classified into wrong domain |
| missed | `weak_fts_signal` | Abbreviation/camelCase mismatch in search |
| missed | `infra_implicit` | Infrastructure repo implicitly needed |
| missed | `new_mechanism_needed` | None of existing mechanisms cover this |

### Step 5: Output
Print your findings as a single JSON object to stdout. The orchestrator will capture it.

```json
{
  "task_id": "PI-XX",
  "summary": "...",
  "description_length": 123,
  "expected_repos": ["..."],
  "phantom_repos": ["..."],
  "independent_found": ["..."],
  "independent_recall": 0.85,
  "tool_found": ["..."],
  "tool_recall": 0.90,
  "classifications": {
    "repo-name": {
      "status": "found|missed",
      "found_by": "independent|tool|both|neither",
      "method": "grep_provider|grep_keyword|graph_edge|similar_task|file_trace|tool_only",
      "miss_reason": null,
      "confidence": "high|medium|low",
      "notes": "..."
    }
  },
  "edge_cases": ["..."],
  "improvement_suggestions": ["..."],
  "commands_run": ["list of all bash commands executed"]
}
```

## Important Rules
- ONE task per run. Be thorough.
- Be ACCURATE. Don't guess — mark confidence as "low" if uncertain.
- If grep returns >50 results, narrow the search.
- Record ALL commands you ran in `commands_run`.
- If a repo has 0 files_changed, classify as `phantom` but still try to find it independently.
- Report both independent recall AND tool recall for comparison.
- Do NOT modify any files in src/, scripts/, or profiles/.
