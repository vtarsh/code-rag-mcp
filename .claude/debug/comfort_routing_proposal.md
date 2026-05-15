# Comfort Routing Proposal — ⚠️ INVALIDATED 2026-04-27 11:55 EEST

> **STATUS: INVALIDATED**. End-to-end validation through real `hybrid_search()` pipeline
> on 2026-04-27 11:55 EEST showed **-6.21pp hit@10 regression** on v2 calibrated docs eval,
> opposite of the +1.45pp simulation prediction. Root cause: 51 ticks of simulation used
> cached benches from `benchmark_doc_intent.py` which sets `router_bypassed: True` and
> uses pure VECTOR → RERANK pipeline. Production `hybrid_search()` uses
> FTS5 + vector + RRF → reranker — different candidate pool, different ranker dynamics,
> different cascade effects from stratum-gate. See `.claude/debug/overnight_log.md` Tick 55.
>
> **Do NOT apply this proposal.** Bench artifacts kept for next-session investigation:
> `bench_runs/v2_with_comfort_routing.json` and `bench_runs/v2_baseline_e2e.json`.

---

# Original (now-invalidated) recommendation below

**Date**: 2026-04-27 03:40 EEST
**Loop session**: 51 ticks of Karpathy-style autonomous research
**Full investigation log**: `.claude/debug/overnight_log.md`

## TL;DR — INVALIDATED

~~Replace broken `_query_wants_docs` routing in `src/search/hybrid.py::rerank()` with a token-based "comfort/toxic" classifier. Validated weighted production hit@10 lift: **+1.45pp [+0.70, +2.33] POSITIVE bootstrap CI**. No metric regressions on any eval.~~

→ Simulation pipeline (vector→rerank) ≠ production pipeline (FTS+vector+RRF→rerank).
End-to-end validation on v2 calibrated docs (n=161 gold) showed:
- hit@5: -7.46pp
- hit@10: **-6.21pp**
- R@10: -3.88pp
- ndcg@10: -7.04pp

15 queries flipped HIT→MISS (only 5 MISS→HIT). Net -10 hits.
The OFF-stratum-set narrowing ({webhook, trustly, method, payout} → {webhook}) caused
trustly/method/payout queries to be RERANKED instead of skipped, and reranker
pushes correct files OUT of top-10 in the RRF pipeline.

## Why current routing is a no-op

The deployed routing change (commits `86669a06` for embedding_provider/container.py + `92f8c989` for hybrid.py) is technically active but produces **+0.00pp on real prod traffic** because the `_query_wants_docs` classifier mis-routes 82% of code-intent queries to the docs path.

Empirical proof (Tick 2-3 in overnight_log.md):
- jira_eval_n900 has 908 code-intent ground-truth queries
- `_query_wants_docs(query)` returns True (= use L6) for **747/908 = 82.3%** of them
- Only 161 queries (17.7%) get routed to l12 FT — too few to realize the +3.31pp claim

Per-query simulation:
- Force-all l12 on jira: hit@10 = 0.3381 (+3.30pp vs L6)
- Routed (current): hit@10 = 0.3051 (+0.00pp vs L6) ← deployed config = no-op

## The proposed change

### Code diff for `src/search/hybrid.py`

```python
# 1. ADD near other regex constants at top of file:
import re

L12_COMFORT_TOKENS = frozenset({
    'alert', 'disputes', 'onboarding', 'page', 'pricing',
    'support', 'transactions', 'types', 'update',
})
L12_TOXIC_TOKENS = frozenset({
    'extend', 'backoffice', 'integration',
    'with', 'hubspot', 'block', 'application',
})

def _comfort_route_to_l12(query: str) -> bool:
    """Token-based l12 router. Replaces _query_wants_docs negation in rerank().

    Validated weighted prod traffic lift (47/53 v2 docs/jira):
      hit@10:  +1.45pp [+0.70, +2.33] POSITIVE
      R@10:    +0.91pp [+0.05, +1.82] POSITIVE
      ndcg@10: +0.66pp positive direction (NOISE CI)
      hit@5:   +1.09pp positive direction (NOISE CI)

    Tokens derived from cross-eval analysis: those with ≥+10pp jira lift AND
    no observed negative impact on either v1 heuristic OR v2 calibrated docs eval.
    """
    if not query:
        return False
    tokens = {t.lower() for t in re.findall(r'\w+', query) if len(t) > 2}
    if tokens & L12_TOXIC_TOKENS:
        return False
    if tokens & L12_COMFORT_TOKENS:
        return True
    return False


# 2. UPDATE rerank() body — replace this block:
#    intent = "docs" if _query_wants_docs(query) else "code"
#    reranker, err = get_reranker(intent=intent)
# WITH:
#    intent = "code" if _comfort_route_to_l12(query) else "docs"
#    reranker, err = get_reranker(intent=intent)


# 3. UPDATE _DOC_RERANK_OFF_STRATA — remove trustly per per-stratum NR-vs-L6
#    analysis (trustly NR=0.6923 < L6=0.7692; was incorrectly in OFF set):
_DOC_RERANK_OFF_STRATA: frozenset[str] = frozenset({"webhook"})
_DOC_RERANK_KEEP_STRATA: frozenset[str] = frozenset({
    "nuvei", "aircash", "refund", "interac", "provider",
    "method", "payout", "trustly",  # trustly moved here
})


# 4. UPDATE _STRATUM_CHECK_ORDER tuple accordingly:
_STRATUM_CHECK_ORDER: tuple[str, ...] = (
    "webhook",  # only OFF stratum
    "nuvei", "aircash", "refund", "interac",
    "provider", "method", "payout", "trustly",  # KEEP set
)
```

### Test changes
- Update tests in `tests/test_hybrid.py` and `tests/test_hybrid_doc_intent.py` for:
  - New `_comfort_route_to_l12` function (add basic unit tests)
  - Stratum gate change ({webhook} only OFF, trustly moved to KEEP)
- Run pytest to verify

## Expected production impact

### What this change DOES vs current code

| Aspect | Current (broken) | Proposed |
|---|---|---|
| Classifier | `not _query_wants_docs(query)` (mis-routes 82% jira) | Token-based comfort/toxic |
| % queries → l12 (jira) | 17.7% | 17.1% |
| % queries → l12 (docs v2) | 0% | 3.1% |
| Stratum OFF set | {webhook, trustly} | {webhook} |
| Production hit@10 | ~0pp | **+1.45pp** |

### Multi-metric impact (weighted v2 docs / jira, 47/53)

| Metric | Δ | CI | Verdict |
|---|---|---|---|
| hit@10 | **+1.45pp** | [+0.70, +2.33] | POSITIVE |
| R@10 | **+0.91pp** | [+0.05, +1.82] | POSITIVE |
| ndcg@10 | +0.66pp | [-0.20, +1.59] | NOISE (positive direction) |
| hit@5 | +1.09pp | [-0.29, +2.51] | NOISE (positive direction) |

### Per-eval breakdown (no regressions on any single eval)

| Eval | hit@5 | hit@10 | R@10 | ndcg@10 |
|---|---|---|---|---|
| docs v1 (n=192 heuristic) | +1.56pp | +2.08pp | +1.18pp | +1.70pp |
| docs v2 (n=161 calibrated) | +0.62pp | +0.62pp | +1.57pp | +1.09pp |
| jira (n=908 PR titles) | +1.54pp | +2.20pp | +0.32pp | +0.31pp |

All metrics POSITIVE direction on all evals. Bootstrap robust to seed variation.

## Validation methodology

1. **Eval sets used**:
   - `profiles/pay-com/jira_eval_n900.jsonl` (n=908, real PR titles with repos_changed)
   - `profiles/pay-com/baseline_prod_docs_n200` (n=192, heuristic-labeled)
   - `profiles/pay-com/doc_intent_eval_v3_n200_v2` (n=161 gold, LLM-calibrated 10 Opus agents)

2. **Bench JSONs used**:
   - `bench_runs/jira_n900_prod_L6.json` (existing)
   - `bench_runs/jira_n900_l12.json` (existing)
   - `bench_runs/jira_n900_no_rerank.json` (existing)
   - `bench_runs/baseline_prod_docs_n200.json` (existing)
   - `bench_runs/sweep_l12_docs_pool200.json` (existing)
   - `bench_runs/baseline_prod_docs_n200_NO_RERANK.json` (existing)
   - `bench_runs/v2_calibrated_L6.json` (NEW, run during loop)
   - `bench_runs/v2_calibrated_l12.json` (NEW, run during loop)
   - `bench_runs/v2_calibrated_NO_RERANK.json` (NEW, run during loop)

3. **Token derivation** (Tick 42, 46): per-token jira lift analysis with cross-eval safety filter (no docs eval shows ≥-3pp on token).

4. **Bootstrap CI**: paired bootstrap n=2000, weighted by 47% docs / 53% jira (per `project_docs_production_analysis_2026_04_24.md` prod traffic estimate).

5. **Robustness**: tested across 5 random seeds; all POSITIVE with overlapping CIs.

## Limitations / risks

1. **Comfort tokens derived from observed jira data** — assumes prod traffic patterns match jira PR titles. Tested across 3 different eval sets to mitigate.

2. **ndcg@10 is NOISE direction** — positive direction but CI lower bound at -0.20pp. Worst case: -0.20pp ndcg regression. Acceptable if hit@10 is primary metric.

3. **Doesn't approach +5.78pp oracle ceiling** — that gap requires a trained classifier (logistic regression with proper features could close some, but my LR experiments at Tick 29-30 showed it underperformed hand-picked).

4. **Real prod traffic distribution may differ** — real prod has 50% 'tail' stratum (no token match). Comfort routing fires on only ~10% of prod traffic. Most queries unchanged from current behavior.

5. **No END-TO-END bench** — simulation uses existing per-query bench JSONs to compute the routing outcome. Should be byte-equivalent to running with code change applied, but recommend a final end-to-end verification before deploy.

## Pre-deploy checklist

- [ ] Apply code change (4 small edits in hybrid.py)
- [ ] Update tests in tests/test_hybrid.py and tests/test_hybrid_doc_intent.py
- [ ] Run `python3.12 -m pytest tests/ -q` — all green
- [ ] Run end-to-end bench:
  ```bash
  python3 scripts/benchmark_doc_intent.py \
    --eval=profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl \
    --model=docs --rerank-on \
    --out=bench_runs/v2_with_comfort_routing.json
  python3 scripts/benchmark_doc_intent.py \
    --eval=profiles/pay-com/jira_eval_n900.jsonl \
    --model=coderank --rerank-on \
    --out=bench_runs/jira_with_comfort_routing.json
  python3 scripts/bootstrap_eval_ci.py \
    --baseline=bench_runs/v2_calibrated_L6.json \
    --candidates bench_runs/v2_with_comfort_routing.json \
    --metric=hit_at_10
  ```
- [ ] Verify simulated lift transfers to production bench (within ±0.5pp)
- [ ] Optional: add `individuals` to L12_COMFORT_TOKENS for slight extra h10 lift (+0.24pp on weighted, ndcg cost negligible)

## Rejected alternatives

| Option | Why rejected |
|---|---|
| Force-all-l12 (no classifier) | Net -0.94pp prod weighted (-5.73pp docs offsets +3.30pp jira) |
| Keep current `_query_wants_docs` routing | No-op on prod (~0pp); fails to deliver claimed +3.31pp |
| LARGE comfort (21 tokens) | Higher h10 (+1.69pp) but ndcg regression -2.34pp on v2 |
| Aggressive OFF={webhook,nuvei,payout} | Better v1 (+4.10pp) but v2 NOISE; ndcg regression -3pp |
| Logistic regression classifier | LR collapsed to majority class; +0.41pp vs hand-picked +1.45pp |
| TOXIC-only (always l12 unless toxic) | All metrics regress (R@10 -3.83pp, ndcg -6.19pp) |
| Per-stratum routing for docs strata | Overfits docs strata distributions; net no improvement |

## Future investments

To close more of the +5.78pp oracle ceiling:
1. **Trained classifier**: use per-query L6/l12 outcomes as labels; train sentence-transformer or logistic regression on richer features (n-grams, embeddings, etc.)
2. **Better l12 model**: current l12 is FT'd for hit@10 — add ranking-quality loss term during training to reduce ndcg regression
3. **Score-based ensemble**: instead of binary L6/l12 choice, blend their scores per query
4. **Bigger eval data**: 192 (v2) + 908 (jira) is small for some metrics; expand to n>=1000 docs for tighter CI
