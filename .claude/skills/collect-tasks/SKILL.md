# /collect-tasks — Jira Task Collection

Collect tasks from Jira, enrich with GitHub PR data, and verify data shape.

## Usage
```
/collect-tasks                              # Default: all projects, last 365 days
/collect-tasks --projects=PI --days=30      # Specific project and timeframe
/collect-tasks --dry-run                    # Preview counts without collecting
/collect-tasks PI-54                        # Single task
/collect-tasks --verify                     # Verify data shape only
```

## Collection

Primary tool: `profiles/pay-com/scripts/auto_collect.py`

```bash
# Bulk collection
JIRA_EMAIL="vyacheslav.t@pay.com" CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com \
  python3 profiles/pay-com/scripts/auto_collect.py --projects=PI,CORE,BO,HS --days=365

# Single task
cd ~/.code-rag-mcp && python3 scripts/collect_task.py PI-54
```

- Uses Jira REST API v3 `/search/jql` with cursor-based pagination (`nextPageToken`)
- Enriches each task with GitHub PR data (repos_changed, files_changed)
- JIRA_EMAIL fallback: hardcoded `vyacheslav.t@pay.com` in collect_task.py
- JIRA_API_TOKEN: from env or ~/.zshrc fallback
- Runs hourly via launchd (`com.code-rag-mcp.auto-collect.plist`)

## Post-Collection Verification

Always verify data shape after bulk collection:
```sql
-- Type distribution
SELECT ticket_type, COUNT(*) FROM task_history GROUP BY ticket_type;
-- Empty fields
SELECT COUNT(*) FROM task_history WHERE description IS NULL OR description = '';
SELECT COUNT(*) FROM task_history WHERE repos_changed IS NULL OR repos_changed = '[]';
-- Group distribution
SELECT substr(ticket_id,1,instr(ticket_id,'-')-1) as grp, COUNT(*) FROM task_history GROUP BY grp;
```

## Repo Cloning

Repos are cloned during `full_update.sh --full` or `extract_artifacts.py`.
pay-com org repos need `tarshevskiy-v` GitHub auth (work account).

## Rules
- Always verify data collection tools are actually working. Silent failures are the worst kind.
- After bulk collection, expect recall to shift — more data reveals true weaknesses.
