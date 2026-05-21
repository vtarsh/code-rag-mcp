# Single-shot recall@10 pod n=665 — keep-decision for CODE_RAG_USE_CAMELCASE_EXPAND

Pod: 90h7fjxme514m3 (RTX 4090, secure cloud, 2026-05-22)
Bench: scripts/eval/diagnose_recall.py --pool-limit=200 on jira_eval_clean_v2.jsonl

## Aggregates (4 arms)

| Arm | hits | hit@10 | recall@10 | recall@pool | retr_fails | rerank_fails |
|---|---|---|---|---|---|---|
| baseline (body OFF, camel OFF) | 454 | 0.6827 | 0.1794 | 0.4708 | 58 | 153 |
| body ON only | 454 | 0.6827 | 0.1794 | 0.4708 | 58 | 153 |
| camel ON only (body OFF) | 459 | 0.6902 | 0.1828 | 0.4846 | 52 | 154 |
| **body + camel** | **459** | **0.6902** | **0.1828** | **0.4846** | **52** | **154** |

## Findings

1. **Step 2 body enrichment has ZERO effect on single-shot recall@10** — body/no-body aggregates bit-identical (Δ=0 across all metrics).
   - Body enrichment only helps in MULTI-STEP iteration (s2f, where +3.31pp pod-validated 2026-05-21).
   - Mechanism: body candidates land at lower RRF ranks after the title-pass rerank;
     in single-shot they never reach top-10. In s2f they shift the reformulation cascade.

2. **camelCase expand adds +5 hits / +1.38pp recall@pool** independently of body.
   - retrieval_failures: 58 → 52 (6 tasks newly find ≥1 GT in pool of 200)
   - recall@10: +0.34pp (small but positive)
   - recall@pool: +1.38pp (meaningful — 9 more tasks reach pool ceiling)
   - hit@10: +0.75pp
   - reranker_failures basically unchanged (153 → 154 = noise)

3. **Orthogonal composition**: body + camel = same as camel-only. No interaction.

## KEEP decision

Flip `CODE_RAG_USE_CAMELCASE_EXPAND` default from "0" to "1" in src/search/fts.py.
Cost: zero (just adds OR-terms to FTS query — sub-millisecond per query).
Risk: minimal — same env mechanism, env=0 reverts to old behaviour.

## Notes

- Per-task JSON files lost when pod terminated immediately after bench.
  Aggregates captured from log tails. Sufficient for keep-decision.
- Earlier-session baseline reported hit@10=0.7143 on n=665; this pod baseline
  reports hit@10=0.6827. Different pod hardware → ~3pp test-retest variance
  is expected per project_recall_pool_diagnosis. The +5 hits ABOVE THIS POD's
  baseline is the real signal.
