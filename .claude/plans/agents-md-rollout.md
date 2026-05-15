# AGENTS.md Rollout Plan

> Cycle: 0 → 1 → 1.5 → 2 → 3
> Repo: ~/.code-rag-mcp (vtarsh/code-rag-mcp, personal account)
> Goal: Replace CLAUDE.md with model-agnostic AGENTS.md (root + profile-level)

---

## Goal

Create a backlinks-style navigation catalog (Obsidian/Foam/Logseq-inspired) so AI models can orient themselves without reading every file. Replace root `CLAUDE.md` → `AGENTS.md` (new standard, not Claude-specific). Migrate CLAUDE.md content into AGENTS.md sections, then delete CLAUDE.md.

Two hierarchies:
- Root: `~/.code-rag-mcp/AGENTS.md` — catalog of entire repo + quick links to profile-AGENTS
- Profile-level: `~/.code-rag-mcp/profiles/<org>/AGENTS.md` — per-org catalog (pay-com definitely needs one)

Both linked via backlinks.

---

## Phases

| Phase | What | Output |
|-------|------|--------|
| **Cycle 0** | Lead creates this plan + agent roster | This file |
| **Cycle 1** | Parallel agents: research backlinks-style + recursive inventory + write shards | `.claude/research/agents-md/<agent-id>.md` |
| **Cycle 1.5** | Synthesis agent merges shards into root AGENTS.md + profile AGENTS.md + deletes CLAUDE.md | `AGENTS.md`, `profiles/pay-com/AGENTS.md`, `CLAUDE.md` deleted |
| **Cycle 2** | V1 verify citations vs live code; V2 verify completeness + backlinks coherence + storage classification accuracy → fix agent | Updated AGENTS.md files |
| **Cycle 3** | Final cross-check: all backlinks resolve, no orphan mentions, storage classification consistent between Root and Profile levels → final fix | Final AGENTS.md files, staged in git |

---

## Agent Roster

| ID | Scope | Scope-Prefix | Task |
|----|-------|-------------|------|
| **R1** | Root core: `src/`, `tests/`, root configs (`pyproject.toml`, `config.json`, `Makefile`, `.gitignore`, `.pre-commit-config.yaml`, `setup_wizard.py`), root markdowns (`README.md`, `ARCHITECTURE.md`, `TESTING.md`, `ROADMAP.md`, `NEXT_SESSION_PROMPT.md`) | `root-core` | Inventory + storage classification + backlinks for all root Python modules, tests, configs, docs. Detect dead code at root level. |
| **R2** | `scripts/` catalog (~100 files, ~408KB Python) | `scripts` | Group scripts by purpose, classify storage (all git-tracked except gitignored ones per `.gitignore`), detect dead/legacy scripts via git log + cross-grep. |
| **R3** | `profiles/pay-com/docs/` curated docs: `gotchas/`, `references/`, `dictionary/`, `flows/`, `notes/` | `profile-docs` | Inventory curated docs, classify storage (git-tracked), map backlinks between docs. |
| **R4** | `profiles/pay-com/` generated artifacts: `bench/`, `benchmarks/`, `churn_replay/`, `finetune_history/`, `finetune_data_*/`, `models/`, `traces/`, `generated/`, `manual_gt/`, `provider_types/`, `.archive/`, root jsonl/json/yaml data files | `profile-generated` | Classify all as gitignored (except `.archive/` which may have mixed status). Document what each artifact type is. |
| **R5** | `.claude/` structure: `rules/`, `docs/`, `agents/`, `skills/`, `debug/` (gitignored), `fix/` | `claude-meta` | Inventory agent infrastructure. Note that `.claude/debug/` and `.claude/worktrees/` are ephemeral/gitignored. |
| **R6** | Dead/Legacy detection across entire repo | `dead-detector` | Cross-grep imports, check git log mtimes, identify CONFIRMED dead / SUSPECTED / OUTDATED-but-might-revive. Focus on: root json dumps, old benchmark files, archive contents, commented-out blocks, scripts without callers. |
| **R7** | Backlinks-style research | `backlinks-research` | Recommend convention: Obsidian wikilinks `[[path]]` vs markdown `[text](path)` vs hybrid. Consider: GitHub renders `[[...]]` as plaintext, so provide BOTH `[[...]]` for Obsidian users AND markdown link for GitHub. Write style guide shard. |

**Rules:**
- Each agent receives: scope-prefix, path to this plan file, format for progress log entry.
- Each agent writes output to `.claude/research/agents-md/<agent-id>.md`.
- Each agent appends one line to "Progress log" below on start and on finish.
- No overlapping scopes. Agents stay within their prefix.

---

## Backlinks Style Decision (preliminary — R7 to confirm)

**Hybrid convention** (tentative):
- Internal cross-references between AGENTS.md sections: `[[Relative/Path.md]]` (Obsidian wikilink) + fall-back markdown link: `[Relative/Path.md](Relative/Path.md)`.
- References to specific code files from AGENTS.md: `[`src/file.py`](src/file.py)` — standard markdown for GitHub compatibility.
- In practice, write wikilinks as `[[path/to/file.md|Display Text]]` when available, but always keep a standard markdown link nearby for renderers that don't support wikilinks.
- Since most AI tools read raw markdown, wikilinks are fine as long as they are unambiguous.

**Final decision:** TBD by R7 in Cycle 1.

---

## Progress Log (chronological — append only)

| Timestamp (ISO) | Agent | Event | Notes |
|-----------------|-------|-------|-------|
| 2026-05-16T01:02 | Lead | Plan created | Cycles defined, roster assigned |
| 2026-05-16T01:16 | R3 | Started | Inventorying profiles/pay-com/docs/ |
| 2026-05-16T01:16 | R3 | Finished | R3.md shard written |
| 2026-05-16T01:02 | R1 | Started | root-core inventory |
| 2026-05-16T01:04 | R1 | Finished | root-core inventory shard written to `.claude/research/agents-md/R1.md` |
| 2026-05-16T01:02 | R5 | Started | Inventorying .claude/ directory |
| 2026-05-16T01:03 | R5 | Finished | Output written to .claude/research/agents-md/R5.md |
| 2026-05-16T01:03 | R4 | Started | profile-generated artifact inventory |
| 2026-05-16T01:06 | R4 | Finished | Cataloged 12 dirs + 30+ root files; classified storage (gitignored/mixed); noted 4 config files unexpectedly untracked. Output: `.claude/research/agents-md/R4.md` |
| 2026-05-16T01:20 | R2 | Started | Inventorying scripts/ directory (~116 files) |
| 2026-05-16T01:24 | R2 | Finished | Cataloged scripts/ by purpose; classified git vs gitignored; detected dead/legacy; cross-grep imports/callers. Output: `.claude/research/agents-md/R2.md` |

---

## Open Questions

1. Should `profiles/example/` and `profiles/my-org/` also get AGENTS.md, or only `pay-com`?
   - **Decision needed:** pay-com definitely gets one. example/ is small; maybe just root AGENTS.md references it.
2. Are `.claude/debug/` and `.claude/fix/` contents ever referenced by active agents, or purely historical?
   - **Assumption:** Historical / ephemeral. Note in AGENTS.md.
3. Should we include `db/`, `logs/`, `extracted/`, `raw/` in directory tree with explicit "gitignored generated" labels?
   - **Assumption:** Yes — AI needs to know these are runtime artifacts.
4. How deep to recurse into `profiles/pay-com/docs/providers/` (LFS, 4000+ files)?
   - **Assumption:** Catalog at provider-folder level only (e.g., `ach/`, `aeropay/`), not individual API doc files.
5. Is `NEXT_SESSION_PROMPT.md` actively used or legacy?
   - **To verify:** Check git log, grep references.
6. Are root JSON dumps (`ab_test_baseline.json`, `benchmark_*.json`, etc.) actively read by scripts or dead?
   - **To verify:** R6 to cross-grep.

---

## Blockers

| # | Blocker | Status |
|---|---------|--------|
| 1 | Tests failing (20 collection errors) — known from other session, not blocking this work | Acknowledged, not blocking |
| 2 | Need to avoid pushing to tarshevskiy-v (work account) — only vtarsh (personal) | Will verify before any git push |

---

## Decisions Log

| # | Decision | Rationale | When |
|---|----------|-----------|------|
| 1 | File name: `AGENTS.md` | User requirement — replaces CLAUDE.md, model-agnostic standard | 2026-05-16 |
| 2 | Two-level hierarchy: root + per-profile | pay-com is large enough to warrant separate catalog; keeps root manageable | 2026-05-16 |
| 3 | Storage classification mandatory per directory | AI must know what is git-tracked vs generated to avoid editing wrong files | 2026-05-16 |
| 4 | `profiles/pay-com/docs/providers/` cataloged at folder level only | 4000+ files; individual API docs are LFS data, not curated knowledge | 2026-05-16 |
| 5 | Do NOT run `git commit` without user confirmation | User rule; stage only, let user commit | 2026-05-16 |

---

## Output Paths

- Root AGENTS.md: `/Users/vaceslavtarsevskij/.code-rag-mcp/AGENTS.md`
- Profile AGENTS.md: `/Users/vaceslavtarsevskij/.code-rag-mcp/profiles/pay-com/AGENTS.md`
- Research shards: `/Users/vaceslavtarsevskij/.code-rag-mcp/.claude/research/agents-md/*.md`
- This plan: `/Users/vaceslavtarsevskij/.code-rag-mcp/.claude/plans/agents-md-rollout.md`
| 2026-05-15T22:13 | R6 | Started | dead-detector investigation beginning |
| 2026-05-16T01:02 | R7 | Started | backlinks-research investigation beginning |
| 2026-05-16T01:02 | R7 | Finished | Convention recommended: Tiered Hybrid (wikilinks for AGENTS.md graph, markdown links for code files). Shard written to .claude/research/agents-md/R7.md |
| 2026-05-15T22:22 | R6 | Finished | dead-detector report written to .claude/research/agents-md/R6.md |
| 2026-05-16T01:02 | S1 | Started | synthesis-root: merging shards R1/R2/R5/R6/R7 + migrating CLAUDE.md → AGENTS.md |
| 2026-05-16T01:02 | S1 | Finished | Root AGENTS.md written (585 lines). All 12 sections merged. CLAUDE.md preserved for S2 deletion.
| 2026-05-16T01:27 | S2 | Finished | Synthesized profiles/pay-com/AGENTS.md (482 lines) from R3/R4/R6/R7/CLAUDE.md inputs
| 2026-05-15T22:28 | Lead | CLAUDE.md deleted | Superseded by AGENTS.md + profiles/pay-com/AGENTS.md |
- **2026-05-16** — V1 (verify-citations) completed: verified ~290+ paths across root AGENTS.md and profiles/pay-com/AGENTS.md; 4 broken/missing, 13 ambiguous. Report written to .claude/research/agents-md/V1.md.
- **2026-05-16** — V2 (verify-completeness) completed: verified 12-section coverage, backlinks coherence, and storage classification accuracy. Found 13 completeness gaps, 4 broken/orphan links, 5 storage mismatches. Report written to .claude/research/agents-md/V2.md.
- **2026-05-16** — V3 (final-cross-check) completed: verified backlinks, storage consistency, count accuracy, completeness, dead/legacy accuracy, and orphan pages. Found 3 profile count mismatches (flows 22→20, MOC 11→10, providers 51→52), 5 existing files misclassified as Dead in profile, 2 root builder-module count errors. Verdict: **NO-GO**. Report written to `.claude/research/agents-md/V3.md`.
| 2026-05-15T22:45 | Lead | Cycle 3 complete | All V3 blockers fixed; counts corrected, dead/legacy reclassified, CLAUDE.md deleted |
