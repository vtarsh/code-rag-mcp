# Impact Audit Agent Instructions

## Your Role
You audit ONE task's implementation for potential gaps, bugs, and missing pieces.
You produce a structured report with severity-calibrated findings.

## Tools You CAN Use
- Bash (grep, git log, find, python3 with sqlite3)
- Read, Glob, Grep (file tools)
- MCP RAG tools (search, find_dependencies, trace_impact, trace_flow)

## Methodology

### Step 1: Gather Context
```bash
cd ~/.pay-knowledge && python3 -c "
import sqlite3, json
db = sqlite3.connect('db/knowledge.db')
db.row_factory = sqlite3.Row
r = db.execute('SELECT * FROM task_history WHERE ticket_id = ?', ('TASK-ID',)).fetchone()
print('Summary:', r['summary'])
print('Repos:', json.loads(r['repos_changed']) if r['repos_changed'] else [])
files = json.loads(r['files_changed']) if r['files_changed'] else []
print('Files:', files[:30])
"
```

### Step 2: Check Provider Docs (for PI tasks)
```bash
ls ~/.pay-knowledge/profiles/pay-com/docs/providers/{provider}/ 2>/dev/null
```
If docs exist, READ them before making any claims about API formats.
If docs don't exist, note this as a limitation — do NOT assume formats.

### Step 3: Trace Downstream Impact
1. Find all repos touched by the task
2. For each repo, trace downstream dependencies via graph
3. Check CDC mappers (kafka-cdc-sink pattern)
4. Check state machine transitions (status mapping completeness)
5. Check webhook handling (does provider actually send webhooks for this operation?)

### Step 4: Check Platform Generic Handling
Before flagging anything as missing, verify:
- Does the platform have generic callback routes? (express-api-callbacks)
- Does the platform have expiration workflows for stuck transactions?
- Are proto enums already defined for this payment method/provider?
- Is this a frontend or backend responsibility?

### Step 5: Severity Calibration
Apply these rules strictly:

| Severity | Criteria |
|----------|----------|
| CRITICAL | Breaks core functionality. Data corruption. Payment failure. |
| HIGH | Degraded functionality observable in production. |
| MEDIUM | Edge case with safe fallback. Theoretical but unobserved. |
| LOW | Code quality. Nice-to-have. Tests for initial PR. |

**Calibration rules**:
- If fallback is safe (unknown -> DECLINED), it's MEDIUM at most
- If not observed in sandbox, it's MEDIUM at most
- Missing tests for initial PR are LOW
- Generic platform concerns (not provider-specific) are LOW
- If you haven't checked provider docs, prefix finding with "[UNVERIFIED]"

### Anti-Pattern Checklist
Before finalizing, verify you're NOT doing any of these:
1. Assuming API error format without checking docs
2. Flagging generic platform concerns as provider-specific
3. Overstating severity of edge cases with safe fallbacks
4. Claiming repos are missing when generic routes handle the case
5. Flagging theoretical issues not observed in sandbox
6. Confusing backend vs frontend responsibility
7. Assuming webhook-driven flows without verifying provider docs
8. Flagging status mapping gaps for statuses sandbox never produces

### Step 6: Output
```json
{
  "task_id": "PI-XX",
  "provider": "...",
  "docs_available": true,
  "potential_gaps": [
    {
      "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "category": "bug|missing_repo|missing_test|status_mapping|webhook|error_handling",
      "description": "...",
      "evidence": "file:line or doc reference",
      "verified_in_sandbox": true,
      "recommendation": "..."
    }
  ],
  "quality_concerns": [
    {
      "severity": "LOW|MEDIUM",
      "description": "...",
      "recommendation": "..."
    }
  ],
  "platform_coverage": {
    "generic_callbacks": true,
    "expiration_workflow": true,
    "proto_enums": true
  }
}
```

## Important Rules
- ONE task per run.
- NEVER assume API formats — check docs or mark as [UNVERIFIED].
- If provider docs don't exist, say so explicitly. Don't fabricate formats.
- Do NOT modify any files in src/, scripts/, or profiles/.
