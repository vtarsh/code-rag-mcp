# Roadmap — code-rag-mcp

> Production focus: recall > precision, local-only, zero external APIs.

## Production State

| Component | Status |
|-----------|--------|
| Code tower | `nomic-ai/CodeRankEmbed` (768d) → `db/vectors.lance.coderank/` (~27GB) |
| Docs tower | `nomic-ai/nomic-embed-text-v1.5` (768d) → `db/vectors.lance.docs/` (~29GB) |
| Reranker | `reranker_ft_gte_v8` (listwise LambdaLoss, 285MB bf16) |
| Hybrid retrieval | FTS5 (150) + vector (50) → RRF → rerank top-200 → top-K |

## Benchmark Baselines

| Variant | r@10 | Hit@5 | Source |
|---|---:|---:|---|
| Baseline (FTS5 + fallback) | 0.7112 | 0.8339 | Jira n=909 |
| v8 FT reranker | 0.7622 | 0.9131 | Jira n=909 |

## Open Work

### Benchmark gaps (priority)
- **Zero doc-intent queries in gold** — v8's doc-strip regression never caught by current bench.
- **Structural overfit** — `benchmarks.yaml` LOO tickets = gold set; changes reorder those by construction.
- **Provider coverage 3/17** — gold tests Trustly/EPX/Worldpay only; 38% of real traffic uncovered.
- **Eval metric is repo-level**, not file-level — r@10 rewards "repo in top-10" even when target file absent.

### Known bugs (1 P0 remaining)
- `daemon.py /admin/unload` — no drain window; new requests in the 500ms pre-exit window re-trigger model load then get killed.

### Architecture debt
- `src/search/hybrid.py` split complete (hybrid_query + hybrid_rerank extracted).
- `scripts/_common.py` now hosts `pause_daemon`, `classify_file`, `preclean_for_fts`.
- ~30 scripts migrated to `setup_paths()` bootstrap.

### Profile cleanup done
- Eval datasets moved to `profiles/pay-com/eval/`.
- Duplicate provider docs deduplicated (72 files).
- Stale directories removed (`notes/_moc`, `providers/aircash`, `providers/neosurf`, `evo/` PDFs).

## Historical context

Pre-2026-04-24 milestones (full detail in git history):
- v8 deployed 2026-04-21 (+5.09pp over baseline).
- Two-tower v13 deployed 2026-04-24 (docs get own embedding tower).
- 13-agent audit 2026-04-22 found + fixed 4 P0/P1 bugs; 3 of 4 now resolved.
- P1c penalty extensions landed 2026-04-22 (doc-query regex + CI penalty).
