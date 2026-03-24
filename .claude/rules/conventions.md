# Code Conventions

## Org Isolation

- Zero hardcoded org names in `src/` — everything via `conventions.yaml`.
- Org-specific data MUST live in `profiles/{name}/`.
- Profile setup: `cd profiles/{name} && ./install.sh` to symlink scripts.

## Secrets & Credentials

- **NEVER hardcode API keys, tokens, or passwords in source code.** Use `os.getenv()`.
- **NEVER commit secrets to git** — even in test files. GitHub Push Protection will block, and leaked keys get flagged by providers.
- Secrets go in: `~/.zshrc` (env vars), `.secrets/` (gitignored), or `.env` files (gitignored).
- Before committing, grep staged files: `git diff --cached | grep -iE "api.key|token|password|secret"`.
- If a key leaks: rotate immediately, filter-branch to remove from history, force push.

## Code Style

- All tool functions return `str` (error strings on failure, formatted results on success).
- Never silently drop search results — deprioritize/annotate, never exclude.
- Recall over precision — false negatives are worse than false positives.
- Search pipeline order: expand query -> FTS5 + vector -> RRF fusion -> CrossEncoder rerank -> format.

## Adding New Domains

- Add domain to `classifier.py`, create new analyzer file in `src/tools/analyze/`.
- `analyze/` is a package (8 modules) — follow existing pattern.

## Data Changes

- After adding tasks/references: `build_vectors.py --repos=task-slug,ref-slug`.
- Full rebuild: `extract_artifacts.py -> build_index.py -> build_graph.py -> build_vectors.py` (~30 min).
- `build_index.py` recreates repos/chunks tables — backup task_history first.
- FTS5 virtual tables cannot have columns added — use separate tables.
- Incremental build: `build_index.py --incremental` re-indexes only changed repos (by SHA comparison).
- Restart daemon after editing glossaries: they load at import time.
