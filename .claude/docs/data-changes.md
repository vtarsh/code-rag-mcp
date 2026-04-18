# Data Changes Reference

## Build Pipeline (full rebuild ~2-4h; peaks ~20GB RAM)

```
extract_artifacts.py -> build_index.py -> build_graph.py -> build_vectors.py
```

- `build_index.py` recreates repos/chunks tables — backup task_history first
- Incremental: `build_index.py --incremental` re-indexes only changed repos (SHA comparison)
- After adding tasks/references: `build_vectors.py --repos=task-slug,ref-slug`
- Full rebuild: `make build` or `ACTIVE_PROFILE=my-org ./scripts/full_update.sh --full`

## Constraints

- FTS5 virtual tables cannot have columns added — use separate tables
- Restart daemon after editing glossaries (loaded at import time)
- `analyze/` is a package (13 modules) — add domains via classifier.py + new analyzer file
