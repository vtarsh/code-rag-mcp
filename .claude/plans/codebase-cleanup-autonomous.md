# Autonomous Codebase Cleanup — Master Plan

> **Session ID:** `cleanup-2026-05-16`  
> **Goal:** Remove dead code, reorganize scripts/, clean generated artifacts, sync AGENTS.md  
> **Mode:** AFK (autonomous, no user prompts)  
> **Min cycles:** 3 (Research → Plan → Execute → Verify)  
> **Agents:** Unlimited, single-responsibility per agent  
> **Progress tracker:** `.claude/worktrees/codebase-cleanup-progress.json`

---

## Principles

1. **One agent = one task.** Never overload an agent with >1 responsibility.
2. **Write before doing.** Every agent MUST update the progress file BEFORE and AFTER work.
3. **Fail fast.** If a blocking dependency fails, stop the pipeline, record why.
4. **Git safety.** Commit at phase boundaries. Never leave repo in broken state.
5. **Verification is mandatory.** No phase completes without explicit verification agent.

---

## Phase 0: Bootstrap (1 agent)

**Agent:** `B0-Bootstrap`

**Tasks:**
1. Read `.claude/worktrees/codebase-cleanup-progress.json` (create if missing).
2. Record `start_time`, set `status: "in_progress"`.
3. Run `git status --short` → record unstaged changes count.
4. Verify `make test` baseline (expect 20 collection errors known, record exact count).
5. Initialize Phase 1 agents with their input data.
6. Write initial progress file.

**Output:** Updated progress file with all phases initialized.

---

## Phase 1: Research (6 agents, parallel)

### R1-Scripts-Usage
**Task:** Map every `.py` file in `scripts/` to its consumers.

**Method:**
- `grep -r 'from scripts\.\|import scripts\.\|scripts/[a-z_]*\.py' src/ tests/ --include='*.py'`
- Check which scripts are referenced in `Makefile` (`full_update.sh`, `build_vectors.py`)
- Check `pyproject.toml` for entry points or script references
- Check if scripts import each other (internal dependency graph)

**Output:** JSON map `script → {consumers: [...], imported_by: [...], is_entrypoint: bool}`

### R2-Tests-Refs
**Task:** Map every test file to the script(s) it tests.

**Method:**
- For each `tests/test_*.py`, grep for `scripts/` references
- Record which scripts have tests, which don't

**Output:** Map `test_file → tested_scripts[]`

### R3-Dead-File-Confirmation
**Task:** Confirm the dead files from context are truly unused.

**Files to verify:**
- Root: `ab_test_baseline.json`, `eval_baseline.json`, `patterns-export.json`, `config.json`
- Root scripts: `audit_index_gaps.py`, `bench_routing_e2e.py`, `build_eval_rebuild_bundle.py`, `build_eval_v2_llm_calibrated.py`, `rescore_against_clean.py`, `generate_housekeeping_report.py`
- Symlinks: `analyze_change_impact.py`, `analyze_developer_patterns.py`, `analyze_file_patterns.py`, `analyze_gaps.py` (root → profile)
- Profile scripts: `analyze_change_impact.py`, `analyze_gaps.py`, `analyze_file_patterns.py`, `analyze_developer_patterns.py`
- Orphans: `graph-grpc-apm-payper.html`, `benchmark_results.json`, `benchmark_realworld_results.json`, `benchmark_flows_results.json`

**Method:** `grep -r` for each filename across entire repo (excluding `.git/`, `__pycache__/`)

**Output:** `{filename: {confirmed_dead: bool, references_found: [...]}}`

### R4-Generated-Artifacts
**Task:** Analyze `bench_runs/` and `.claude/debug/` for git status.

**Method:**
- `git ls-files bench_runs/ | wc -l` (how many tracked)
- `git ls-files .claude/debug/ | wc -l` (how many tracked)
- Check `.gitignore` for existing patterns
- Check if any code references these paths

**Output:** `{path: {tracked_count, total_count, gitignored, code_references: [...]}}`

### R5-Profile-Scripts
**Task:** Catalog `profiles/pay-com/scripts/` (25 files).

**Method:**
- List all `.py` files
- Check which are symlinks from root `scripts/`
- Check which are referenced anywhere outside profiles/
- Note: all are gitignored by root `.gitignore`

**Output:** Profile scripts catalog with usage status.

### R6-Archive-Contents
**Task:** Catalog existing `.archive/` directories.

**Method:**
- `find . -type d -name '.archive' -o -name 'archive'`
- List contents and sizes

**Output:** Archive inventory.

---

## Phase 2: Planning (2 agents, sequential after R* complete)

### P1-Scripts-Restructure
**Input:** R1, R2, R5 outputs

**Task:** Design new `scripts/` directory structure.

**Proposed categories:**
- `scripts/build/` — index, vectors, graph, env builders (`build_*.py`, `build_graph.py`, `build_index.py`)
- `scripts/bench/` — benchmarks and evaluation (`bench_*.py`, `benchmark_*.py`, `local_code_bench.py`)
- `scripts/eval/` — eval harnesses, judges, validators (`eval_*.py`, `bootstrap_eval_ci.py`)
- `scripts/analysis/` — analytics, churn, mining (`analyze_*.py`, `mine_co_changes.py`, `detect_*.py`)
- `scripts/maint/` — maintenance, health, validation (`health_check_agents_md.py`, `validate_*.py`, `generate_housekeeping_report.py`)
- `scripts/data/` — data prep, finetune, vectors (`build_vectors.py` if not in build/, `prepare_finetune_data.py`, `finetune_reranker.py`)
- `scripts/scrape/` — doc scraping, crawling (`tavily-docs-crawler.py`, `finalize_scrape.py`)
- `scripts/` (root) — entry points used by Makefile (`full_update.sh` — keep here), `_common.py`, `clone_repos.sh`

**Constraints:**
- `Makefile` references: `$(SCRIPTS)/full_update.sh`, `$(SCRIPTS)/build_vectors.py` → must update Makefile or keep these at root
- `tests/` import scripts directly → must update test imports OR keep tested scripts at root OR use `scripts/__init__.py` with re-exports

**Decision rule:** If a script is imported by tests AND by Makefile → keep at root OR update both. If only by tests → can move if tests updated.

**Output:** `{new_path: old_path, migration_steps: [...], files_to_keep_at_root: [...]}`

### P2-Cleanup-Plan
**Input:** R3, R4, R6 outputs

**Task:** Create ordered deletion list.

**Order (safest first):**
1. Root JSON dumps (confirmed dead)
2. Orphan HTML/JSON (confirmed dead)
3. `.claude/fix/` → move to `.archive/2026-05-16-fix-cleanup/`
4. Root dead scripts (confirmed dead)
5. Profile dead scripts (confirmed dead)
6. `bench_runs/` → add to `.gitignore`, `git rm -r --cached` if tracked
7. `.claude/debug/` → add to `.gitignore`, `git rm -r --cached` if tracked

**Output:** Ordered list of deletion/migration operations.

---

## Phase 3: Execution (N agents, parallel where safe)

### E1-Delete-Root-Dumps
**Input:** P2 item 1
**Task:** `git rm` 4 root JSON files.

### E2-Delete-Orphans
**Input:** P2 item 2
**Task:** `git rm` graph HTML + 3 benchmark JSON orphans.

### E3-Archive-Fix-Dir
**Input:** P2 item 3
**Task:** `mkdir -p .archive/2026-05-16-fix-cleanup && mv .claude/fix/* .archive/...` (or `git mv` if tracked)

### E4-Delete-Root-Dead-Scripts
**Input:** P2 item 4
**Task:** `git rm` 7 dead root scripts. Note: 4 are symlinks → remove symlinks, NOT targets.

### E5-Delete-Profile-Dead-Scripts
**Input:** P2 item 5
**Task:** Remove 4 dead profile scripts. Since gitignored, just `rm`.

### E6-Gitignore-Generated
**Input:** P2 items 6-7
**Task:**
- Update `.gitignore` with `bench_runs/` and `.claude/debug/`
- `git rm -r --cached bench_runs/ .claude/debug/` for tracked files
- `git rm -r --cached` for any other generated artifacts found

### E7-Mkdir-Scripts-Categories
**Input:** P1
**Task:** Create `scripts/{build,bench,eval,analysis,maint,data,scrape}/` directories.

### E8-Move-Scripts
**Input:** P1
**Task:** `git mv` scripts to new locations per P1 plan.

### E9-Update-Test-Imports
**Input:** P1, E8
**Task:** Update all `tests/test_*.py` that import moved scripts.

### E10-Update-Makefile
**Input:** P1, E8
**Task:** Update Makefile references to moved scripts.

### E11-Add-Scripts-Init
**Input:** E7, E8
**Task:** Add `scripts/__init__.py` and category `__init__.py` files if needed for imports.

---

## Phase 4: Verification (4 agents, sequential)

### V1-Pytest-Smoke
**Task:** Run `make test` or `pytest tests/ -q --co` (collection only) to verify no import errors.
**Pass criteria:** Same or fewer collection errors than baseline. Zero NEW errors.

### V2-Makefile-Check
**Task:** Verify `make help` works. Verify `make build` and `make update` would resolve paths correctly (dry-run if possible).

### V3-Health-Check-Script
**Task:** Run `python3 scripts/health_check_agents_md.py` (or new path). Must PASS.

### V4-Git-Status
**Task:** `git status --short`. Verify:
- No untracked dead files remain
- Expected files deleted/moved
- No unexpected modifications

---

## Phase 5: AGENTS.md Sync (2 agents)

### S1-Update-Root-AGENTS
**Task:** Update root AGENTS.md:
- Directory Tree (new scripts/ structure)
- Scripts Catalog (update paths)
- Dead/Legacy (remove deleted items, add new archive entries)
- Storage Classification (bench_runs/, .claude/debug/ now gitignored)
- Key Files (update script paths)

### S2-Update-Profile-AGENTS
**Task:** Update `profiles/pay-com/AGENTS.md`:
- Remove dead profile script references
- Update counts

---

## Phase 6: Final Commit (1 agent)

### F1-Commit
**Task:**
1. Stage all changes
2. Create commit with structured message:
```
codebase cleanup: remove dead code, reorganize scripts/

- Delete 4 root JSON dumps (ab_test_baseline.json, eval_baseline.json, patterns-export.json, config.json)
- Delete 4 orphan artifacts (graph HTML + 3 benchmark JSONs)
- Delete 7 dead root scripts (audit_index_gaps.py, bench_routing_e2e.py, build_eval_rebuild_bundle.py, build_eval_v2_llm_calibrated.py, rescore_against_clean.py, generate_housekeeping_report.py + 4 symlinks)
- Delete 4 dead profile scripts (analyze_change_impact.py, analyze_gaps.py, analyze_file_patterns.py, analyze_developer_patterns.py)
- Archive .claude/fix/ to .archive/2026-05-16-fix-cleanup/
- Gitignore bench_runs/ (144MB, 726 files) and .claude/debug/ (2MB, 149 files)
- Reorganize scripts/ into categories: build/, bench/, eval/, analysis/, maint/, data/, scrape/
- Update test imports for moved scripts
- Update Makefile paths
- Sync AGENTS.md documentation
```
3. Run final `python3 scripts/health_check_agents_md.py` → must PASS.
4. Update progress file: `status: "completed"`, `end_time`.

---

## Agent Roster Summary

| Phase | Agent | Responsibility | Parallel? |
|-------|-------|----------------|-----------|
| 0 | B0 | Bootstrap | — |
| 1 | R1 | Scripts usage map | ✓ |
| 1 | R2 | Test refs map | ✓ |
| 1 | R3 | Dead file confirmation | ✓ |
| 1 | R4 | Generated artifacts analysis | ✓ |
| 1 | R5 | Profile scripts catalog | ✓ |
| 1 | R6 | Archive inventory | ✓ |
| 2 | P1 | Scripts restructure design | After R* |
| 2 | P2 | Deletion plan | After R* |
| 3 | E1-E3 | Safe deletions | ✓ (independent) |
| 3 | E4-E6 | Script/profile deletions + gitignore | ✓ (independent) |
| 3 | E7-E11 | Scripts restructure | Sequential (E7→E8→E9→E10→E11) |
| 4 | V1-V4 | Verification | Sequential |
| 5 | S1-S2 | AGENTS.md sync | ✓ |
| 6 | F1 | Final commit | — |

---

## Progress File Schema

See `.claude/worktrees/codebase-cleanup-progress.json` for live state.

**Key fields:**
- `session_id`: `"cleanup-2026-05-16"`
- `status`: `"pending" | "in_progress" | "blocked" | "completed" | "failed"`
- `phases[].status`: per-phase status
- `phases[].agents[].status`: `"pending" | "running" | "done" | "failed"`
- `phases[].agents[].result`: agent output (JSON or summary)
- `blockers[]`: list of blocking issues

---

## Rollback Plan

If any verification fails:
1. DO NOT commit.
2. Record blocker in progress file.
3. Attempt fix with targeted agent.
4. If fix fails after 2 attempts, set `status: "blocked"` and STOP.
5. User can resume by fixing blocker and resetting phase status to `"pending"`.

---

## Notes for AFK Execution

- All agents must use `Agent` tool with `run_in_background=false` (foreground) for deterministic execution.
- Between phases, read progress file to decide next agents.
- If an agent fails, do NOT proceed to dependent phases.
- Health check script path may change during execution (moved to `scripts/maint/`). Always read progress file for current path.
