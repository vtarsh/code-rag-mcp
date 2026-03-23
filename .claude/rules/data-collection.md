# Data Collection Rules

## Jira Task Collection

Primary tool: `profiles/pay-com/scripts/auto_collect.py`

```bash
# Collect all projects, last 365 days
JIRA_EMAIL="vyacheslav.t@pay.com" CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com \
  python3 profiles/pay-com/scripts/auto_collect.py --projects=PI,CORE,BO,HS --days=365

# Dry-run first to see counts
# add --dry-run flag

# Single task collection
python3 scripts/collect_task.py PI-54
```

- Uses Jira REST API v3 `/search/jql` with cursor-based pagination (`nextPageToken`)
- Enriches each task with GitHub PR data (repos_changed, files_changed)
- JIRA_EMAIL fallback: hardcoded `vyacheslav.t@pay.com` in collect_task.py
- JIRA_API_TOKEN: from env or ~/.zshrc fallback
- Runs hourly via launchd (`com.pay-knowledge.auto-collect.plist`)

## After Bulk Collection

Always verify data shape:
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
Location: managed by build pipeline, not manual.
pay-com org repos need `tarshevskiy-v` GitHub auth (work account).
