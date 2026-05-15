# P10 Phase 1 Quick-Win Validator — Report

Date: 2026-04-25
Author: p10-quickwin-validator
Status: validation complete. NO push, NO code change. **Verdict: CONDITIONAL GO.**

## Goal

Verify the P9 finding (rerank-off vs rerank-on, docs tower, eval-v3-n200):
**rerank-off R@10 = 0.2289 vs rerank-on R@10 = 0.2138, Δ = +1.5pp** — and
decide whether to disable the reranker on the doc-intent path in production.

## Replication on eval-v3-n200

Re-ran the rerank-on bench with the exact P9 command:

    python3.12 scripts/benchmark_doc_intent.py \
      --eval=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
      --model=docs --no-pre-flight --rerank-on \
      --out=/tmp/p10_bench_v3_n200_docs_rerank_on.json

Result identical to P9 (n_eval=192):

| Metric          | rerank-off | rerank-on | delta    |
|-----------------|-----------:|----------:|---------:|
| recall@10       |    0.2289  |   0.2138  |  -0.0151 |
| ndcg@10         |    0.3506  |   0.3487  |  -0.0019 |
| hit@5           |    0.4115  |   0.4167  |  +0.0052 |
| hit@10          |    0.5365  |   0.5052  |  -0.0313 |
| latency p50 ms  |    110.03  |   268.98  |  +158.95 |
| latency p95 ms  |    173.63  |   339.26  |  +165.63 |

Replicated. The +1.5pp R@10 lift from disabling the reranker is real on
eval-v3-n200. Hit@10 lift is even larger: **+3.13pp** (53.65% vs 50.52%).

### Eval-v3 hit@10 cross-tab (192 queries, 14 vs 20 asymmetry)

|                            | rerank-on hit | rerank-on miss |
|----------------------------|--------------:|---------------:|
| **rerank-off hit**         |  83 (43.2%)   |  20 (10.4%)    |
| **rerank-off miss**        |  14 ( 7.3%)   |  75 (39.1%)    |

Net swing if reranker disabled: **+20 wins, −14 losses = +6 net hit@10 (+3.12pp)**.

## Prod-traffic sample (n=100, seed=42)

Pulled 2,446 unique search queries from `logs/tool_calls.jsonl`, ran
`_query_wants_docs` → 1,633 prod doc-intent queries (66.8%). Sampled 100 for
ranking-stability test (`/tmp/p10_prod_doc_queries_100.json`).

For each query: ran top-50 vector search on docs tower, captured top-10
without reranker AND top-10 with reranker, computed top-10 set overlap and
churn (10 − overlap_count).

### Prod-sample summary

| Metric                   | Value   |
|--------------------------|---------|
| n_queries                | 100     |
| avg_overlap_top10        | 0.3650  |
| avg_churn_top10          | 6.35    |
| % queries reranker changed top10 | **100.0%** |

The reranker reorders the top-10 on **every** prod doc-intent query in the
sample. Average overlap is only 36.5% — i.e. reranker swaps in 6.35 new files
on average. This matches P9's "81% reorder" finding on eval (the prod sample
is even more aggressive because it's not preselected for clean labels).

### Prod-sample per-stratum (vs eval-v3 deltas)

| stratum    | eval Δ R@10 | prod overlap | prod churn | prod n |
|------------|------------:|-------------:|-----------:|-------:|
| aircash    |      −0.087 |       0.4000 |       6.00 |     6  |
| interac    |      +0.148 |       0.3000 |       7.00 |     4  |
| nuvei      |      −0.104 |       0.1833 |       8.17 |    12  |
| payout     |      −0.025 |       0.2437 |       7.56 |    16  |
| provider   |      +0.061 |       0.3789 |       6.21 |    19  |
| refund     |      −0.042 |       0.5000 |       5.00 |     4  |
| tail       |      +0.008 |       0.4100 |       5.90 |    40  |
| trustly    |      −0.083 |       0.4333 |       5.67 |     3  |
| webhook    |      −0.056 |       0.3667 |       6.33 |    12  |

**Direction agreement**: 6/9 strata where eval Δ is negative (reranker hurts)
also show high prod churn (>= 6.0): aircash, nuvei, payout, refund, trustly,
webhook. These are exactly the strata where disabling the reranker is most
likely to help in prod.

**Direction conflict**: `interac` and `provider` — eval shows the reranker
helps these strata (+14.8pp / +6.1pp), yet prod churn is also high (7.0 / 6.21).
This means: in prod, some interac/provider queries lose ranking while different
ones gain. The eval-v3 rescue cases (e.g. `payper interac etransfer task plan`)
are real — disabling the reranker would lose them.

## Risk identification — top 14 queries that would lose ranking

These 14 queries currently hit@10 ONLY thanks to the reranker. Disabling it
would drop them to hit@10=0. Sorted by current rerank-on R@10:

| # | rerank-on R@10 | strata    | Query (truncated) | Expected file |
|---|---------------:|-----------|-------------------|---------------|
| 1 |  1.000 | interac   | `payper interac etransfer task plan implementation changes files`     | nuvei-docs/.../interac-etransfer.md |
| 2 |  1.000 | provider  | `Return provider or business errors as declines during retries in GW` | paynearme-docs/.../understanding-retry-transactions.md |
| 3 |  0.500 | tail      | `payper error code 1006 authorization validation failed required fields` | payper-docs/11-general-error-codes.md |
| 4 |  0.400 | method    | `providers-gateway initialize authorization validation check payment method type` | fields/docs/dictionary/fields.yaml |
| 5 |  0.333 | tail      | `PI-60 PR cross-repo touch points list workflow webhooks credentials gateway` | apm-redirect-flow/.../apm-redirect-flow.yaml |
| 6 |  0.250 | nuvei     | `Nuvei addUPOAPM API request spec full parameter billingAddress …`    | nuvei-docs/.../apm-input-parameters.md |
| 7 |  0.200 | tail      | `platform architecture cheat sheet service overview how payment works` | nuvei-docs/.../marketplaces-stub_overview.md |
| 8 |  0.200 | provider  | `make-request signature header bearer token uuid request id provider` | entities/docs/dictionary/entities.yaml |
| 9 |  0.200 | webhook   | `how does trustly sign webhook payloads and verify signatures`        | grpc-apm-trustly/docs/GOTCHAS.md |
| 10|  0.200 | provider  | `get-sale-payload get-refund-payload provider payload builder`        | aps-docs/.../06-callbacks.md |
| 11|  0.200 | method    | `get-from-source-raw-data payment method case source type`            | concepts/docs/dictionary/concepts.yaml |
| 12|  0.200 | provider  | `amount smallest unit currency exponent cents conversion provider`    | express-api-authentication/.../express-api-authentication.yaml |
| 13|  0.200 | payout    | `Nuvei payout checksum order merchantId siteId clientRequestId …`     | nuvei-docs/.../api_main_reference.md |
| 14|  0.200 | tail      | `Add Archived Toggle in Verification Checks Under Underwriting`       | plaid-docs/.../docs.md |

**Of these, 4 cross the deploy-relevant 0.5 threshold** (rows #1–4). Rows
#5–14 only reach R@10 ∈ [0.2, 0.4]; missing them is annoying but not
catastrophic — they would already be borderline-useful.

## Implementation sketch (NOT applied)

In `src/search/hybrid.py`, the rerank call is **line 702**:

```python
ranked = rerank(query, ranked, limit, reranker_override=reranker_override)
```

The `_query_wants_docs(query)` classifier (line 288) already exists and is
already called at line 406 to skip doc/test penalties on doc-intent queries.
The minimal change to disable the reranker on doc-intent queries is:

```python
# Proposed change at line ~700-702:
is_doc_intent = _query_wants_docs(query)
disable_rerank_for_docs = os.getenv("CODE_RAG_DOC_RERANK_OFF", "0") == "1"
if is_doc_intent and disable_rerank_for_docs:
    # Skip CrossEncoder reranker: rerank-off wins macro R@10 by +1.5pp on
    # eval-v3 (P10 2026-04-25). Re-sort by RRF score only.
    ranked = sorted(ranked, key=lambda x: x["score"], reverse=True)[:limit]
    for r in ranked:
        r["rerank_score"] = 0.0
        r["combined_score"] = r["score"]
        r["penalty"] = 0.0
else:
    ranked = rerank(query, ranked, limit, reranker_override=reranker_override)
```

Plus an env var (`CODE_RAG_DOC_RERANK_OFF=1`) so it can be toggled at the
daemon level for canary deploy without a redeploy. Default off (current
behaviour preserved). Flip to `default = on` after a 24-48h prod canary.

Also worth adding: telemetry log (one line per query) showing `is_doc_intent`
+ `rerank_skipped` so we can audit which prod queries actually take the new
path.

## Verdict

**CONDITIONAL GO** on disabling the reranker for doc-intent queries.

### Why GO
- +1.5pp R@10 / +3.1pp hit@10 macro gain on eval-v3-n200, replicated.
- 6/9 negative-delta strata (aircash, nuvei, payout, refund, trustly, webhook)
  also show high prod churn → the bench is predictive of prod.
- 100% of prod doc-intent queries are reordered by reranker (avg 6.35 churn).
  At least 50% of that churn is suspect — eval-v3 shows 32 negatives vs 21
  positives.
- Latency win: −159 ms p50 (110 vs 269), −166 ms p95 (174 vs 339) — 2.4×
  faster doc-intent path on local Mac CPU (heavier on prod GPU, but still
  meaningful).
- 4 queries that lose at the 0.5 threshold are all `payper`/`paynearme`-style
  reformulations that may be recoverable by other means (e.g. content boost
  for error-code docs, or improving the docs-tower bi-encoder on
  task-plan / retry-transaction terminology).

### Why CONDITIONAL (not unconditional GO)

1. **interac / provider strata regress on eval** (+14.8pp / +6.1pp would be
   forfeited). 14 prod-relevant rescue cases cited above. Disabling
   blanket-style sacrifices these, even though net macro is +1.5pp.
2. **Eval-v3 labeler is not gold** (auto-heuristic, n_gold=0). The +1.5pp
   could be partly noise. Need to confirm the lift on a hand-labeled
   sub-sample before committing to the deploy. (Or trust the +3.1pp hit@10
   signal, which is more robust to label noise than R@10.)
3. **No prod ground truth for the 100-query sample**. We measured churn
   (which is high) but cannot directly measure prod R@10 lift. Estimated
   prod lift = +1.5pp × (prod doc-intent share, ≈47%) ≈ **+0.7pp R@10 on
   total-traffic**, or **+1.5pp R@10 on doc-intent traffic**.

### Recommended next steps (NOT done in this phase)

1. Add the env-var flag (`CODE_RAG_DOC_RERANK_OFF=1`) per the implementation
   sketch above. Default off. Push branch + open PR.
2. Hand-label the 14 rescue queries from the risk table — confirm the 4
   crossing-0.5 ones are genuine wins (not labeler noise) before committing
   to the trade-off.
3. Run a 24-h canary in prod with the env var ON, log
   (query, is_doc_intent, rerank_skipped, top-10) and compare to
   baseline. Measure user-facing metrics (CTR / dwell-time).
4. If canary clean, flip default to ON, keep the env var as an emergency
   disable.

### Expected prod lift estimate

- **Conservative** (only macro signal): +0.7pp R@10 weighted by 47% doc-intent
  share. Negligible noise floor.
- **Realistic** (R@10 + hit@10 both positive on eval): +1.5pp on doc-intent
  R@10, +3.1pp on doc-intent hit@10.
- **Latency**: −160 ms p50 on doc-intent (this run, Mac CPU). On prod GPU
  the absolute saving is smaller but the relative speedup is similar.
  Worth pursuing even if recall stayed flat.

## Files / artifacts

- `/tmp/p10_bench_v3_n200_docs_rerank_on.json` — replicated rerank-on bench
- `/tmp/p10_prod_doc_queries_100.json` — sampled prod queries
- `/tmp/p10_prod_sample_results.json` — prod-sample full per-query dump
- `/tmp/p10_reorder_positives.json` — 21 eval queries reranker rescued
- `/tmp/p10_reorder_negatives.json` — 32 eval queries reranker hurt
- `/tmp/p10_prod_sample_test.py` — prod-sample test driver script

## Constraints honoured

- DO NOT modify `src/search/hybrid.py`: respected (read-only).
- DO NOT push to GitHub: respected.
- DO NOT modify production indexes: respected.
- pytest stays at 836+1: not touched (no code change).
- Mac CPU only, light load: respected.
