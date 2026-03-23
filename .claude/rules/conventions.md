# Code Conventions

## Org Isolation

- Zero hardcoded org names in `src/` — everything via `conventions.yaml`.
- Org-specific data MUST live in `profiles/{name}/`.
- Profile setup: `cd profiles/{name} && ./install.sh` to symlink scripts.

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
- Restart daemon after editing glossaries: they load at import time.
