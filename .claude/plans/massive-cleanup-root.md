# Root Cleanup Findings

## Duplicates

- **`.claude/plans/codebase-cleanup-AFK.md`** vs **`.claude/worktrees/codebase-cleanup-AFK.md`**: byte-for-byte identical. The worktrees copy is stale debris from an earlier AFK session.
- **`.gitignore` duplicate entries**: `graph.html` (lines 26 & 99), `clone_log.json` (lines 10 & 100), `repo_state.json` (lines 9 & 101), `extract_log.json` (lines 11 & 102), `blind_spots_results.json` (lines 18 & 103). Five patterns listed twice.
- **`.claude/plans/codebase-cleanup-progress.json`** vs **`.claude/worktrees/codebase-cleanup-progress.json`**: both track the same cleanup pipeline but have diverged content. The worktrees copy is from an older completed run.

## Stale Files/Configs

- **`config.json`** (root): Gitignored but physically present. Contains stale `pay-com` org data and has been superseded by the profile system (`profiles/{name}/config.json`). The `.gitignore` even has a legacy comment: "Legacy root config and docs (migrated to profile)".
- **`daemon.pid`**: Runtime artifact (May 12) not in `.gitignore`. Should be added to gitignore or removed.
- **`graph.html`**: 374KB generated viz, gitignored but physically present, stale (Mar 15).
- **`setup_wizard.py`**: AGENTS.md explicitly flags this as "stale — Last touched 8+ weeks ago. Still wired into `Makefile`." The `init` target and README both reference it.
- **`.pre-commit-config.yaml` stale paths**: All 7 doc validator hooks reference `scripts/validate_doc_*.py` and `scripts/validate_overlay_vs_proto.py`, but these files were moved to `scripts/maint/` during the massive cleanup. The hooks will fail if anyone runs them.
- **`ARCHITECTURE.md` outdated sizes**: Claims `db/vectors.lance.coderank` is "~238MB" and `db/knowledge.db` is "~144MB". Actual sizes: 27GB coderank, 29GB docs, 253MB knowledge.db.
- **`NEXT_SESSION_PROMPT.md`**: Referenced in `AGENTS.md` and `ROADMAP.md` but file does not exist (deleted at some point).
- **`ROADMAP.md` orphan reference**: Mentions `scripts/install_launchd.sh` as orphan superseded by `setup_wizard.py::install_launchd()` — but setup_wizard itself is stale, creating a chain of orphaned references.
- **`AGENTS.md` duplicated heading**: "Open Questions" appears twice (lines 212 and 214).
- **`AGENTS.md` Q4**: Notes "Six `validate_doc_*.py` scripts actively used but never committed" — this is outdated; they were committed to `scripts/maint/`.
- **`.active_profile`**: Listed in `.gitignore` line 49 but physically present; acceptable as local state marker, but verify it's not accidentally tracked.

## Compression Opportunities

- **`db/`**: 56GB total. `vectors.lance.coderank` (27G) + `vectors.lance.docs` (29G) dominate. Already gitignored; no action needed unless purging old model tables.
- **`raw/`**: 1.1GB cloned repo artifacts. Already gitignored.
- **`extracted/`**: 127MB extracted chunks. Already gitignored.
- **`logs/`**: 77MB. `search_feedback.jsonl` (21MB) and eval shard logs (`eval_gte_v8*.log`, ~25MB combined) are the heavy items. 6 files are >30 days old (Mar 28–Apr 10).
- **`bench_runs/`**: 79MB with 183 files; 15 files >1MB. 63 files are >14 days old (all from Apr 26 run). Most are intermediate retry/run1/run2 artifacts.
- **`.claude/debug/`**: 2MB total. Contains stale April debate/eval artifacts (`debate-*.md`, `eval-*.json`, `loop-log.md`, `overnight_log.md`, etc.). The `archive/` subdir (716KB) has tarballs and old session dumps.
- **`.DS_Store`**: 10KB at root. **Not gitignored** — add to `.gitignore`.
- **`models/`**: Directory exists but is empty of files (only empty subdirs). `.gitignore` already ignores it, but the empty dir tree could be removed.
- **Cache dirs**: `.ruff_cache/` (144K), `.pytest_cache/` (108K), `__pycache__/` (72K) — already gitignored but present locally.

## Quick Wins

1. **Delete `config.json`, `graph.html`, `daemon.pid` from root** — all are generated/gitignored artifacts.
2. **Add `daemon.pid` and `.DS_Store` to `.gitignore`** — prevent future leakage.
3. **Deduplicate `.gitignore` entries** — remove the 5 repeated patterns.
4. **Delete `.claude/worktrees/codebase-cleanup-AFK.md`** — identical duplicate.
5. **Update `.pre-commit-config.yaml` paths** — change `scripts/validate_doc_*.py` → `scripts/maint/validate_doc_*.py` for all 7 hooks.
6. **Fix `ARCHITECTURE.md` DB size claims** — replace ~238MB with actual ~56GB to avoid confusion.
7. **Remove `NEXT_SESSION_PROMPT.md` references** from `AGENTS.md` and `ROADMAP.md` since the file is gone.
8. **Fix duplicated "Open Questions" heading** in `AGENTS.md`.
9. **Purge logs/ files >30 days old** — 6 files including zero-byte `launchd_vectors_stderr.log`.
10. **Archive or delete old bench_runs/** — 63 files from Apr 26 are stale intermediate retries; consider keeping only the final baselines.
