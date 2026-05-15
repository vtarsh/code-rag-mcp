# Autonomous A/B loop — FINAL REPORT (2026-04-25)

## TL;DR

- **Outcome:** BASELINE WINS. Vanilla `docs (nomic-ai/nomic-embed-text-v1.5)` stays in production. 4 challengers rejected on honest eval-v3, 1 blocked at upstream HF modeling.py level. No deploy needed.
- **Spend:** $1.70 of $15 RunPod budget. **$13.30 banked** for next session.
- **What's next:** (a) hard-negative FT on eval-v3 (~$3-5, biggest ROI), (b) reranker A/B (separate session), (c) router heuristic improvements.

## Final results table (eval-v3, n=90, model-agnostic labeler)

| Candidate | R@10 | nDCG@10 | Hit@5 | Hit@10 | p95 ms | Δ R@10 | Verdict |
|---|---|---|---|---|---|---|---|
| **docs (nomic-v1.5)** — WINNER | **0.2509** | 0.3813 | 0.3778 | 0.5333 | 20.46 | (ref) | KEEP PROD |
| docs-nomic-v2-moe | 0.2100 | 0.3396 | 0.3667 | 0.4667 | 35.47 | -0.041 | REJECT |
| docs-payfin-v1-fixed | 0.1678 | 0.3411 | 0.3556 | 0.4444 | 20.72 | -0.083 | REJECT |
| docs-payfin-v0 | 0.1428 | 0.2205 | 0.3222 | 0.4222 | 20.13 | -0.108 | REJECT |
| docs-gte-large | — | — | — | — | — | — | BLOCKED (intrinsic HF NTK-rope bug) |

## Iterations summary (all 18)

| # | Phase | Outcome | Cost |
|---|---|---|---|
| 0 | fix-benchmark | Schema bug + true Recall@K + 5-condition AND-gate landed (`bba2d63a`). Pytest 719/719 green. | $0 |
| 1 | phase-2a | Built v2 candidate set (n=100, 67 prod-sampled + 33 v1-kept, path-disjoint from FT train). | $0 |
| 2-11 | phase-2b | 10 sequential batches × 10 queries labeled with 3-signal pool. 0 train leakage. | $0 |
| 12 | phase-2c | Aggregated v2 (n=100), pushed to private repo, smoke baseline R@10=0.5082 on Mac. | $0 |
| 13 | stage-d | Trained v1 on 91 pairs (Tarshevskiy/pay-com-docs-embed-v1) — saved with double-encoder prefix bug. | $0.069 |
| 14 | phase-5 | A/B 5 candidates on pod. db md5 changed (launchd 03:00 incremental) → re-baseline 0.3277. v0/nomic-v2-moe REJECT, v1+gte-large BLOCKED. | $0.864 |
| 15 | phase-5b | Local v1 fix (key-remap into v0 scaffold) → upload as v1-fixed. v1-fixed REJECT (-7.1pp). gte-large BLOCKED at modeling.py:392 (CPU repro confirms NTK-rope bug). | $0.284 |
| 16 | phase-6 | Team-debate (3 critics) detected eval-v2 was 90% rigged (labeler used vec_pool of baseline). All Phase 5/5b results invalidated. Pivot to Phase 5c. | $0 |
| 17 | phase-5c | Built eval-v3 with model-agnostic labeler (FTS+overlap only, no vec). Re-benched all 4 candidates locally. **All 4 still REJECT** — but signal is honest now. Genuine ceiling reached. | $0 |
| 18 | phase-8-finalize | Ship process gains, write final report, update memory + RECALL-TRACKER + NEXT_SESSION_PROMPT. No deploy. | $0 |

## Process gains shipped (reusable infrastructure)

These survived even though no model deployed. Next session can use them immediately.

1. **eval-v3 model-agnostic labeler** — `scripts/build_doc_intent_eval_v3.py`. Pool = FTS5 top-15 ⊕ path-overlap top-15 ⊕ glossary match. Zero vector signal at label time.
2. **benchmark_doc_intent.py multi-metric AND-gate** — 5-condition scoreboard. `--compare baseline.json candidate.json` returns DEPLOY:yes/no with per-condition trace.
3. **normalize_embeddings fix** — removed mismatch with indexer that was producing 0% baseline on eval-v2.
4. **EmbeddingModel.max_seq_length cap + LONG_BATCH env override** — Mac MPS keeps default 4 (won't OOM on 16 GB), pod uses 32 via `CODE_RAG_DOCS_LONG_BATCH=32` (5-8x speedup).
5. **runpod skeleton matured** — ports as array, no idleTimeoutInMin (RunPod additionalProperties drops 4 fields silently), env-pre-injection for HF_TOKEN, cost_guard.py + pod_lifecycle.py + prepare_train_data.py production-ready.
6. **eval-v3 jsonl in private repo** — clean baseline for any future doc-tower attempt.

## Recommendations for next session

Ranked by expected ROI on remaining $13.30 budget.

1. **Hard-negative fine-tune on eval-v3 ($3-5, ~1-2h)** — Current FT used positive-only MultipleNegativesRankingLoss → embeddings collapsed (anisotropy). Mining hard negatives from FTS top-50 (excluding gold paths) gives discriminative signal. Best swing-for-cost option. Eval same harness, AND-gate same threshold.
2. **Reranker A/B (separate session, weeks)** — Doc-tower ceiling looks reached at this corpus + eval set. Now the lever is the reranker (currently `ms-marco-MiniLM-L-6-v2` baseline). Need a fresh A/B harness for cross-encoder docs-pair reranking. Potential: jina-reranker-v2-base-multilingual / bge-reranker-v2-m3 already-tested family.
3. **Router improvements** — `_query_wants_docs` heuristic catches ~46.8% of doc-intent queries. Mine production logs for false negatives (queries that retrieved code paths but should have hit docs), expand keyword set, possibly add small classifier.

## Files inventory

### Eval sets
- `~/.code-rag-mcp/profiles/pay-com/doc_intent_eval.jsonl` — v1, n=44 (preserved)
- `~/.code-rag-mcp/profiles/pay-com/doc_intent_eval_v2.jsonl` — v2, n=100 (preserved, but biased)
- `~/.code-rag-mcp/profiles/pay-com/doc_intent_eval_v3.jsonl` — v3, n=100 (n_eval=90 effective; canonical going forward)

### Bench JSONs (source-of-truth A/B numbers)
- `/tmp/bench_v3_docs.json` — baseline R@10=0.2509
- `/tmp/bench_v3_docs-nomic-v2-moe.json` — R@10=0.2100 REJECT
- `/tmp/bench_v3_docs-payfin-v0.json` — R@10=0.1428 REJECT
- `/tmp/bench_v3_docs-payfin-v1-fixed.json` — R@10=0.1678 REJECT

### Scripts (reusable for next session)
- `~/.code-rag-mcp/scripts/build_doc_intent_eval.py` — v1/v2 builder (legacy, biased)
- `~/.code-rag-mcp/scripts/build_doc_intent_eval_v3.py` — v3 builder (model-agnostic)
- `~/.code-rag-mcp/scripts/benchmark_doc_intent.py` — 5-condition AND-gate runner
- `~/.code-rag-mcp/scripts/runpod/` — pod_lifecycle.py + cost_guard.py + prepare_train_data.py + train_docs_embed.py

### Commits landed (vtarsh/code-rag-mcp main)
- `bba2d63a` — Phase 1 benchmark fix
- `89f08b5c` — Phase 2a v2 candidate builder
- `c30c8bab` — Phase 3 normalize_embeddings fix
- `6cfce1ab` — Phase 5 register 3 candidates + --model flag
- `fdc5c2a3` — Phase 5b max_seq_length cap + LONG_BATCH env

### Private repo (vtarsh/pay-knowledge-profile)
- `8a449851` — doc_intent_eval_v2_candidates.jsonl
- `47147b8f` — doc_intent_eval_v2.jsonl (final labeled v2, biased)
- (Phase 8) — RECALL-TRACKER.md update + doc_intent_eval_v3.jsonl

### Debug artifacts
- `~/.code-rag-mcp/.claude/debug/loop-log.md` — full 18-iteration journal
- `~/.code-rag-mcp/.claude/debug/loop-state.json` — final state with candidates_tested dict
- `~/.code-rag-mcp/.claude/debug/p6-verdict.md` — Phase 6 root-cause synthesis (eval-v2 rigged)
- `~/.code-rag-mcp/.claude/debug/eval_v3_bias_report.json` — v3 vs v2 bias quantification
- `~/.code-rag-mcp/.claude/debug/final-report.md` — this file

## What did NOT change

- `src/models.py` `docs` entry — still `nomic-ai/nomic-embed-text-v1.5`
- Production daemon — no restart needed
- v1, v2 eval files — preserved as historicals
- LanceDB indices — `db/vectors.lance.docs/` unchanged
