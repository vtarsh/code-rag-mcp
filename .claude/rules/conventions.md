# Conventions

- All org-specific data in profiles/{name}/, zero hardcoded org names in src/
- Never drop search results — deprioritize or annotate, never exclude
- Improve base recall before tuning reranker — reranker is polish, not fix
- Ground truth = repos_changed from Jira; cross-validate with files_changed and pr_urls
- Recall over precision: false negatives worse than false positives
- Benchmark baselines in profiles/{profile}/RECALL-TRACKER.md — never regress
- Parallel agents by default; never parallel on same DB/files
- Priority: generic mechanisms (src/) > profile data > private profile repo
- Don't report context window percentages
- Don't create crons that only report — make them act or skip
- Build pipeline and data constraints: see .claude/docs/data-changes.md
- Git push: `gh auth switch --user vtarsh && git push && gh auth switch --user tarshevskiy-v`
