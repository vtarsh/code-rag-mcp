# AFK Mode: Autonomous Codebase Cleanup

> **Session starter:** Read this file first when resuming the cleanup session.  
> **Plan:** `.claude/plans/codebase-cleanup-autonomous.md`  
> **Progress:** `.claude/worktrees/codebase-cleanup-progress.json`  
> **Created:** 2026-05-16

## Quick Start (for the AI that picks this up)

1. **Read the plan:** `cat .claude/plans/codebase-cleanup-autonomous.md`
2. **Read the progress:** `cat .claude/worktrees/codebase-cleanup-progress.json`
3. **Determine current phase:** Find the first phase with `"status": "pending"` whose dependencies are satisfied.
4. **Launch agents:** Use the `Agent` tool with exact task descriptions from the plan.
5. **Update progress:** After each agent completes, update the JSON progress file.
6. **Never ask the user.** Make decisions. If blocked, record in `blockers[]` and stop.

## Critical Rules

- **NO user prompts.** The user is AFK. Decide independently.
- **One agent = one task.** Do not combine tasks.
- **Update progress BEFORE and AFTER each agent.**
- **Git safety:** `git status` before destructive ops. Commit at phase boundaries.
- **If verification fails:** Stop. Record blocker. Do NOT proceed.
- **Health check path may change** during execution (script moved to `scripts/maint/`). Read progress file.

## Decision Authority Matrix

| Decision | Authority |
|----------|-----------|
| Delete confirmed dead file | Auto-approve |
| Move script to new category | Auto-approve per P1 plan |
| Update test import | Auto-approve |
| Update Makefile | Auto-approve |
| Modify `.gitignore` | Auto-approve |
| Archive `.claude/fix/` | Auto-approve |
| Commit | Auto-approve |
| If verification fails → stop | Auto-execute |
| If unexpected error → retry once | Auto-execute |
| If retry fails → block | Auto-execute |

## Known Baseline (recorded by B0)

- pytest collection errors: ~20 (known from previous sessions)
- git unstaged count: [to be recorded]
- Health check: PASS (0 broken links, 31 non-blocking warnings)

## Blocker Resolution

If `status` becomes `"blocked"`:
1. Read `blockers[]` from progress file.
2. Attempt targeted fix agent.
3. If fix succeeds, set phase status back to `"pending"` and continue.
4. If fix fails after 2 attempts, leave blocked for user.

## Resume Checklist

When picking up a partially completed session:
- [ ] Read progress file
- [ ] Run `git status --short` to verify repo state matches progress
- [ ] Run `python3 scripts/health_check_agents_md.py` (or wherever it moved) to verify docs
- [ ] Identify next pending agent
- [ ] Launch agent
