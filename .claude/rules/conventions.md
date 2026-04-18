# Conventions

- All org-specific data in profiles/{name}/, zero hardcoded org names in src/
- Keep every search result — deprioritize or annotate low-confidence ones, but include them in the output
- Improve base recall before tuning reranker — reranker is polish, not fix
- Ground truth = repos_changed from Jira; cross-validate with files_changed and pr_urls
- Recall over precision: false negatives worse than false positives
- Benchmark baselines in profiles/{profile}/RECALL-TRACKER.md — never regress
- Parallel agents by default; serialize any agents that touch the same DB or files
- Priority: generic mechanisms (src/) > profile data > private profile repo
- Report what the tool found, not how full the context window is
- Crons either act on findings or exit silently — no report-only jobs
- Build pipeline and data constraints: see .claude/docs/data-changes.md
- Git push: via `mcp__github__push_files` (owner: vtarsh, repo: code-rag-mcp). This repo is accessed from the personal vtarsh account only.
