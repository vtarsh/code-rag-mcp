## Position A — Defender (Round 1)

**Claim:** A2's +2.89pp R@10 (0.2427 vs 0.2138 rerank-on baseline; `/tmp/p10_a2_stratum_gated.json:26` vs `/tmp/p10_rerank_on_parity.json:20`) is a real, deployable retrieval gain — not a labeler artifact, not post-hoc cherry-picking, not eval-set overfit.

### Argument 1 — A2 is a deterministic router between two pre-measured pipelines, not a new fit.

A2's per-stratum recalls are NOT free parameters. On OFF strata it MUST equal rerank-off; on KEEP strata it MUST equal rerank-on. Verify against `/tmp/bench_v3_n200_docs.json:17-28` (rerank-off) and `/tmp/p10_rerank_on_parity.json:24-35` (rerank-on):

- nuvei: A2=0.3978 ≡ rerank-off=0.3978 (identical)
- aircash: A2=0.3778 ≡ rerank-off=0.3778
- trustly: A2=0.2667 ≡ rerank-off=0.2667
- webhook: A2=0.2797 ≡ rerank-off=0.2797
- refund: A2=0.3987 ≡ rerank-off=0.3987
- interac: A2=0.6296 ≡ rerank-on=0.6296
- provider: A2=0.2435 ≈ rerank-on=0.2522 (-0.87pp, within noise)

A2's macro is the size-weighted average of two priors measured weeks ago. No new model, no new training, no new hyperparameters. The +2.89pp cannot be "training noise"; it is the arithmetic consequence of routing on n=192 (OFF n=72, KEEP n=32, unknown n=88).

### Argument 2 — Per-stratum AND-gate passes everywhere; no stratum drops >1pp on n≥8.

`profiles/pay-com/RECALL-TRACKER.md:38-50`: largest negative is provider -0.87pp (n=23), tail -0.67pp (n=50) — both within label noise. Seven strata gain: nuvei +10.43pp, aircash +8.71pp, trustly +8.34pp, webhook +5.58pp, refund +4.23pp, method +2.35pp, payout +1.59pp. All four ranking metrics move the same direction (nDCG@10 +2.51pp, hit@5 +1.04pp, hit@10 +4.17pp), with latency p50 -34.5ms (`p10_a2_stratum_gated.json:54`). 5-condition AND-gate clears.

### Argument 3 — Telemetry confirms the gate fired as designed.

`p10_a2_stratum_gated.json:11-19`: skipped 100, reranked 102, breakdown {nuvei:31, aircash:21, webhook:21, refund:15, trustly:12}. The lift is causally tied to the gate, not coincidental top-k churn. Pytest 857/857+1 (RECALL-TRACKER:13) certifies helpers match spec.

### Strongest counter (honest acknowledgment)

The Skeptic's best attack is **post-hoc stratum selection**: OFF strata were chosen *from* per-stratum deltas already measured on eval-v3-n200, then A2 is re-scored on the *same* n=192 — textbook test-set leakage / regression-to-the-mean.

## Position A — Pre-rebuttal of strongest counter

**Counter:** OFF set chosen from in-sample deltas → +2.89pp will not transfer.

**Three rebuttals.**

(a) **Independent prod-traffic corroboration.** `p10-quickwin-report.md:73-83` shows that 6/9 strata with negative eval delta (aircash, nuvei, payout, refund, trustly, webhook) ALSO show ≥6.0 top-10 churn on a 100-query production sample drawn from `logs/tool_calls.jsonl` (seed=42, never seen by eval-v3 builder). All 5 OFF strata clear that bar. Selection signal exists outside eval-v3.

(b) **Independent LLM-judge corroboration.** `p10-llm-judge-report.md:54-78`: on the 14 hardest reranker-ON queries, Opus direct-rate is +17.9pp, graded DCG +0.67. The reranker IS real on KEEP-style queries — exactly what A2 preserves. The labeler's bias (G2, mean |Δ|=0.46) is largest on tail/payout/provider (+0.55 to +0.70), which A2 routes to rerank-ON; smallest on aircash/refund (+0.18, +0.20), which A2 routes OFF. Even under LLM correction, every OFF/KEEP routing decision is vindicated.

(c) **Selection optimism is bounded below the gain.** The largest single-stratum noise band observed is ~1pp (provider -0.87pp). Discounting +2.89pp by a generous 1pp in-sample-optimism leaves +1.89pp — still above the +1.5pp AND-gate floor and the §5 +1.5pp pre-registered forecast.

**Bottom line:** OFF-set choice is informed by eval, but the underlying signal is corroborated in prod traffic AND by an independent Opus judge, and the macro lift survives a generous in-sample-optimism discount.

## Position N — Skeptic (Round 1)

The +2.89pp claim (`/tmp/p10_a2_stratum_gated.json` `recall_at_10=0.2427` vs `/tmp/p10_rerank_on_parity.json` 0.2138) almost doubled the §5 forecast (+1.5pp). On the SAME eval the rule was fit to, that gap is the alarm bell. Three orthogonal reasons it is artifact, not signal.

**Attack 1 — Selector trained AND tested on the same n=192 (textbook double-dip).** OFF (`nuvei/aircash/trustly/webhook/refund`) and KEEP (`interac/provider`) were chosen FROM per-stratum eval-v3-n200 deltas (`p10-quickwin-report.md:70-82`, `RECALL-TRACKER.md:159-167`) and then A2 is re-scored on that same n=192. `per_stratum_n` shows `trustly=4, aircash=9, refund=13, interac=9` — at trustly n=4 a single query swap = 25pp/4 quanta, well below any honest paired-test SE. The macro `+0.0289` is a size-weighted average dominated by selection on these tiny strata. There is no held-out fold, no per-query CI, no permutation test in any artifact. **Falsifier:** rebuild the gate using a stratified split-half (n≈96 train) and score on the disjoint half. Real signal survives; selection optimism collapses toward 0.

**Attack 2 — A2 produces ZERO new retrieval signal on OFF strata; it only stops the reranker.** `per_stratum_recall` shows 5 exact-equality identities between A2 and rerank-off (`bench_v3_n200_docs.json`): nuvei `0.3978==0.3978`, aircash `0.3778==0.3778`, trustly `0.2667==0.2667`, webhook `0.2797==0.2797`, refund `0.3987==0.3987`. A2 is a token-router (`hybrid.py:365-373` `_STRATUM_TOKENS`) gating between two fixed pipelines — the gain is contingent on those tokens firing IDENTICALLY on prod's 1242 unique doc-intent queries as on the eval (which was grown FROM prod head terms — `project_loop_2026_04_25.md`). The eval is biased to fire the gate; the prod long-tail is unmeasured. **Falsifier:** measure `_STRATUM_TOKENS` substring coverage on `logs/tool_calls.jsonl` 1242 doc-intent queries; if <50% match, A2 = plain rerank-on for the unmatched majority and the macro lift evaporates OOD.

**Attack 3 — Labeler-noise floor (G2: |Δ|=0.460, Pearson r=0.446) is ~16x the claimed gain.** `p10-llm-judge-report.md §2`: heuristic R@10 mean=0.293, LLM rel-rate mean=0.713 — under-counts relevance by 46pp. Per-stratum bias is largest on `tail +0.700, payout +0.683, provider +0.550, method +0.383` — exactly KEEP-side strata. The +2.89pp lift is 6.3% of the bias gap and lies inside r=0.446's directional-only zone. Worse: G1 shows rerank-on direct-rate +18pp / DCG +0.67 on the 14 risk queries — proving the heuristic-counted "reranker hurts OFF strata" partly mis-counts direct hits the reranker actually found. The OFF-stratum "wins" on aircash/nuvei/trustly may live entirely inside this noise. **Falsifier:** Opus-judge top-10 of A2 vs rerank-on on a stratified n=30 OFF-sample. If LLM rel-rate Δ < +0.5pp, +2.89pp is heuristic illusion.

## Position N — Pre-rebuttal of strongest counter

**Anticipated Defender counter (their (a)):** "Independent prod-traffic corroboration — 6/9 negative-delta strata also show ≥6.0 prod top-10 churn (`p10-quickwin-report.md:73-83`). Selection signal exists outside eval-v3."

**Rebuttal:** churn ≠ recall. The prod sample has NO ground truth (`p10-quickwin-report.md:181-187` admits this explicitly: "No prod ground truth for the 100-query sample"). High churn only proves the reranker *changes* the order; it does not prove rerank-off *wins*. The same prod table shows interac (KEEP) churn 7.0 / overlap 0.30 (n=4) — high churn on a stratum the gate KEEPs — which by Defender's own logic should also predict regression. They cannot have it both ways: either churn predicts harm (then KEEP strata also harm in prod), or it does not (then the OFF-side prod corroboration collapses). Their (b) Opus-judge corroboration is on the 14 risk queries — a non-stratified, non-random subset selected for being reranker-rescue cases — i.e. another in-sample slice. Their (c) "1pp discount → still +1.89pp" assumes the noise band IS 1pp; that is asserted, not measured (no CI, no permutation test exists). Until a held-out re-evaluation or a stratum-token coverage check on the 1242 prod queries lands, the +2.89pp is a same-data routing-table number, not a deployable forecast.
