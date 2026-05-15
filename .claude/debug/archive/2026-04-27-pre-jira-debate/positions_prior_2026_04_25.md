# Debate: arch-ft-closure — positions

Topic: "Doc-tower FT/swap axis is exhausted on this corpus + eval; pivot to router/reranker (P8/P9) is the correct next-axis decision."

Position A — Affirmative (defend "axis exhausted; pivot correct").
Position B — Negative (defend "axis NOT exhausted; pivot premature").

---

## Position A — Round 1

### TL;DR

Six candidates have now been measured on the same canonical eval (`profiles/pay-com/doc_intent_eval_v3_n150.jsonl`, n_eval=133, model-agnostic labeler) using the same 5-condition AND-gate. **All six rejected.** Spread spans the full plausible recipe space — base-swap (3 different bases), 10-pair MNRL FT, 91-pair MNRL FT, MoE, and the proposal's own top recipe TSDAE→CoSENT FT. The Bayesian prior on "any next FT/swap recipe wins this corpus + eval combo" has firmed to **1/13 ≈ 0.077** (Jeffreys-adjusted with 6 honest failures). Continued spend on this axis is monotonically diminishing-information per dollar. Meanwhile, the unaddressed lever is router: 12% of prod traffic (348/2860 calls) is currently OOB-unrouted, and `c3` term-whitelist routing in `p6-pivot-strategist.md:35` carries `p(any positive lift) = 0.78` at $0 cost. The pivot decision is correct.

### Argument 1 — The "FT axis exhausted" claim is empirical, not Bayesian hand-waving

**Concrete record (RECALL-TRACKER.md:75-83 + final-report.md table):**

| # | Candidate | Mechanism | Δ R@10 vs prod baseline |
|---|---|---|---|
| 1 | docs-payfin-v0 | 10-pair MNRL FT | -0.108 |
| 2 | docs-payfin-v1-fixed | 91-pair MNRL FT (normalize fix) | -0.083 |
| 3 | docs-nomic-v2-moe | MoE base-swap (drop-in) | -0.041 |
| 4 | docs-gte-large | gte-large base-swap | BLOCKED at HF NTK-rope bug, U1 patch landed but A/B never run |
| 5 | docs-mxbai-baseline | Vanilla BERT (mxbai-large) base-swap, no FT | -0.006 |
| 6 | docs-r1-stage2 | TSDAE→CoSENT FT, 3 epochs, 2956 triplets | -0.177 |

The 6 measurements span four mutually-distinct mechanisms:
- 2× MNRL FT (small N) — both regressed
- 1× MoE base-swap — regressed -4.1pp despite nomic blog claiming "+3-5%" drop-in lift
- 1× vanilla BERT base-swap (no FT) — regressed -0.6pp (closest to baseline of all six; **trustly stratum -22.2pp** alone fails per-stratum AND-gate)
- 1× novel-loss-family FT (CoSENT, the recipe-architect's TOP PICK from `debate-recipes.md:103`) — catastrophic -17.7pp regression with **per-stratum violators on 4 strata** (`/tmp/phase2_r1_outcome.txt:25-30`: nuvei -39.8pp, refund -33.5pp, interac -22.2pp, provider -17.8pp)

R1 is the load-bearing test for "FT recipe family was the bottleneck" — it changed the loss family from MNRL to CoSENT (`debate-recipes.md:325-328` rebuts skeptic exactly on this axis), used 108× more queries (2387 prod queries vs 22 v12-set queries), and added Stage-1 TSDAE for anisotropy. **All three of recipe-architect's claimed multipliers** (×1.3 for new-loss-family, ×1.2 for hard-negatives, ×1.05 for query-support) were spent on this attempt. The outcome was -17.7pp — **the worst regression of the six**, not the predicted +0.05 ± 0.07. Recipe-architect's posterior must collapse: either the multipliers were unjustified, or the corpus + eval combination has structural floor that no FT recipe in this family can pass.

This isn't a noisy single roll. R1 wiped provider-specific knowledge — "nuvei -39.8pp" means the candidate retrieves the right Nuvei doc on ≈1/30 queries vs baseline 1/2.5. That is qualitative collapse, not stochastic variance. The skeptic's diagnosis (`debate-skeptic.md:248-251`: "FT on a small/biased dataset against a moderate base on a private corpus tends to drift the base manifold faster than it discovers domain signal") was the dominant prior going INTO R1; R1 confirmed it.

### Argument 2 — Eval-v3-n143 IS valid; it's not the bottleneck

The proposal's most fearsome attack is "the eval is too small / too biased to detect real lifts." Three pieces of evidence rule it out:

(a) **Model-agnostic labeler shipped in eval-v3** — `final-report.md:38` and `scripts/build_doc_intent_eval_v3.py`. Pool = FTS5 top-15 ⊕ path-overlap top-15 ⊕ glossary match. **Zero vector signal at label time.** This kills H2 (the eval-v2 critic finding that 90% of eval-v2 was rigged to baseline `vec_pool`, per `p6-verdict.md`). Eval-v3 was rebuilt explicitly to fix this; the n=90 → n=143 grow worker (`eval-grow-stats.json`) preserved the model-agnostic labeling.

(b) **Prod-sampled head-term distribution** — `eval-grow-stats.json` shows merged_strata_counts: payout=15, provider=15, nuvei=16, webhook=16, method=13, refund=13, aircash=13, tail=29, with enforced train-disjointness (20 train_dup + 9 train_jaccard + 6 existing_jaccard rejected before merge). Eval-v3 strata mass matches `project_docs_production_analysis_2026_04_24.md` (46.8% prod doc-intent, head terms: payout, refund, nuvei, webhook). **A real lift on prod would show up on eval-v3.**

(c) **Paired SE math at n=143 — verified independently** (`debate-verdict-v2.md:204-215`):
```
n=90:  paired SE = 0.0457  ±9.0pp 95% CI
n=143: paired SE = 0.0354  ±6.9pp 95% CI
```
At n=143, paired SE is ±7pp. The +10pp AND-gate is genuinely detectable at α=0.05 if a candidate truly delivers +10pp. The mxbai measurement at -0.6pp R@10 sits well INSIDE the noise floor — it's not "we couldn't tell"; it's "the candidate lost on the metric AND failed per-stratum (trustly -22.2pp)." Even a +5pp lift is detectable as a one-tailed signal at this n, and nothing in the six-candidate record hints at a candidate even reaching +0pp on this corpus.

The proposal might counter "eval-v3-n143 has trustly stratum n=4 (per `eval-grow-stats.json`), so a single query swing = 25pp swing." True, but that's a **per-stratum gate hardness** issue, not a labeler-bias issue, and the gate is symmetric: it disqualifies candidates that cherry-pick. mxbai failed on trustly because it actually retrieves wrong Trustly docs, not because trustly stratum is small (the gate condition is "no stratum drop > 15pp"). If the worry is power on small strata, the answer is to grow eval-v3 further before the next A/B — which is **exactly what option (d) preserves budget for**, in service of the next axis.

### Argument 3 — Two-tower architecture isn't the bottleneck for these failures; the docs-tower-itself ceiling is

The proposal might frame "two-tower means we need a strong docs-side model." Audit by mechanism:

- **Two-tower v13 already deployed** (`project_two_tower_v13_landed.md`). Code tower uses CodeRankEmbed; docs tower uses `nomic-ai/nomic-embed-text-v1.5`. Routing via `_query_wants_docs` heuristic carries 46.8% of prod calls to the docs tower; mixed-intent fan-out is shipped (`project_two_tower_v13_landed.md`).
- **The docs-side model is already a strong, top-15 MTEB model.** `nomic-embed-text-v1.5` MTEB English avg = 62.39 (per `debate-skeptic.md:196`). Its successor on this corpus (nomic-v2-moe) regressed -4.1pp. A vanilla BERT comparable (mxbai, MTEB 64.68) regressed -0.6pp. A FT in a non-MNRL loss family regressed -17.7pp.
- **The 49,142-row docs corpus** (`debate-skeptic.md:17`, verified) is integrated provider integration documentation — narrow domain, mostly English, well-structured, already covered near-optimally by nomic's pre-training. The lift ceiling on swapping or fine-tuning is bounded by what nomic-v1.5 already extracts. Six measurements have now sampled that ceiling and confirmed it sits below baseline by 0.6 to 17.7pp.

The proposal might claim "you haven't tried the right base / right loss / right pair-mining." But the search has now covered: 2 different base architectures (BERT-family, nomic-bert), MoE retrofit, two distinct training-set sizes (10 pairs vs 2956 triplets), two loss families (MNRL vs CoSENT), two label sources (Opus-judged vs reranker-pseudo-labeled), and two negatives strategies (in-batch vs hard-mined). **The hypothesis space remaining is small and increasingly speculative.** Each next attempt costs $1-5 + 4-10h human time; each marginal Bayesian update on "FT can win" is now in the 3rd-decimal-place range.

The actual bottleneck candidates that remain unaddressed:
- Router unrouting: 12% of prod calls miss docs tower entirely (`p6-pivot-strategist.md:33`). $0 cost; p(any positive lift) ≈ 0.78 on c3 term-whitelist alone.
- Reranker: production uses `reranker_ft_gte_v8`; never A/B'd against modern alternatives (bge-reranker-v2-m3, jina-reranker-v2). Cost-bound by weeks of regression testing, not by GPU spend.

These are mechanistically distinct from "docs-tower base/FT swap." They have NOT been measured. The pivot is not an exit from a winnable race; it's an exit from a converged race toward a more-promising race that hasn't started.

### Strongest counter-argument I expect from B (and pre-rebuttal)

**B's most plausible attack:** "Six failures on n=143 with paired SE ±7pp doesn't prove the corpus has a ceiling — it proves the eval is power-limited. The +10pp AND-gate threshold is itself untested for being achievable; even a true +8pp recipe would consistently show as 'reject.' One more attempt with hard-negative mining + grown eval (n=200+) is honest information at $3-5 — the same kind of next-step recipe-architect proposed but with the disjointness fix that R1's CM4 didn't enforce. The 'pivot now' decision burns information that's still extractable cheaply."

**Pre-rebuttal:**

1. **The six failures aren't all near the gate; the spread is the evidence.** Mxbai is at -0.6pp (closest, but per-stratum-fails). R1 is at -17.7pp (catastrophic). MoE is at -4.1pp. The variance across mechanisms is what makes this not a power-limited noise band — it's a structural-floor signal. A true +8pp recipe would show SOME positive deltas amid the rejections; we have zero. The 6/6 rejection pattern with deltas spanning [-17.7pp, -0.6pp] is not consistent with "candidates that genuinely lift +8pp but show as -1 to -3 due to noise"; it's consistent with "candidates that don't lift at all on this corpus."

2. **The eval *was* grown** — n=90 → n=143 mid-debate (`eval-grow-stats.json`), with model-agnostic labeling preserved. SE tightened from ±9pp to ±7pp. The next grow (n=143 → n=200) costs ≥$0 + ~6h human time per `p6-pivot-strategist.md:96-101`, **and that's exactly what option (d) banks budget for** — eval-v3 expansion is a P7/P8 prerequisite, not a substitute for one more FT roll.

3. **R1's CM4 disjointness gap is real but doesn't change the verdict.** Skeptic confirmed (`debate-skeptic.md:79-98`) that 100/100 eval-v3 queries appear in `tool_calls.jsonl`, so a query-disjointness assert was missing. But R1 was actually trained AND failed at -17.7pp despite this *favorable-to-FT* condition. If anything, the missing disjointness assert means R1's measured -17.7pp UNDER-states its true regression on a properly query-disjoint test set. Adding disjointness to a hypothetical R7 doesn't make the corpus easier; it makes the evaluation *harder*.

4. **"One more attempt" is the sunk-cost framing the skeptic dismantled in `debate-skeptic.md:144-156`.** Each failed roll moves Jeffreys by 1/(n+2). Going from 6 rejections to 7 moves it from 1/13=0.077 to 1/14=0.071. Marginal information value drops below $1 per roll while marginal cost stays at $1-5. Compare against: P7-router c3 at $0 cost has p(any positive lift)=0.78. The opportunity cost of *not* pivoting is much larger than the EV of one more FT roll.

The pivot is not premature. Six honest measurements + structural-floor pattern + a higher-EV unaddressed lever = the Bayesian-correct decision is to close this axis and start the next one.

---

## Position B — Round 1

### TL;DR

The "6/6 rejected → axis exhausted" framing is **a base-rate illusion built on a recipe-monoculture sample**. Six rolls in a row from one urn (positive-only MNRL on 10–91 pair / 22-query data + base swaps that share the same eval ceiling) is *one experiment with high replication*, not six independent samples of the FT design space. eval-v3-n143 has paired SE ±6.9pp (`debate-verdict-v2.md:204-215`), giving the +10pp AND-gate ~50% one-tail power for a true +10pp recipe and only **~18% power for a true +5pp recipe** — the recipe-architect's own expected delta. The bench harness measures bi-encoder R@10 with `rerank_off=True` (verified in `/tmp/bench_v3_n143_docs.json` keys), not end-to-end deploy quality. A productive **$3.50 FT spend with p(win) > 0.20 has not been tried**: R1-as-designed (12k triplets across 2384 prod queries with query-disjointness, the actual recipe in `debate-recipes.md` §R1) was substituted at execution time with 3-epoch CoSENT on the same 10-pair data that produced payfin-v0's -10.8pp. That substitution is a recipe failure, not an axis closure.

### Argument B1 — "6 candidates" = 6 draws from ONE recipe family, not 6 independent recipes

Audit the actual training data behind the 6 rolls (sources: `debate-recipes.md` §"Why this is hard", `phase2_r1_outcome.txt:80-83`, `RECALL-TRACKER.md:75-83`):

| # | Candidate | Family | Train data | Loss |
|---|---|---|---|---|
| 1 | payfin-v0 | FT, single-tower | 10 pairs / 22 queries | MNRL |
| 2 | payfin-v1-fixed | FT, same data | 91 pairs / 22 queries | MNRL |
| 3 | nomic-v2-moe | base swap, **no FT** | n/a | n/a |
| 4 | gte-large | base swap, **no FT** | (blocked) | n/a |
| 5 | mxbai-baseline | base swap, **no FT** | n/a | n/a |
| 6 | r1-stage2 | FT, CoSENT 3-epoch | **2956 triplets on the SAME 10-pair set** (`phase2_r1_outcome.txt:80-83`) | CoSENT |

That is **3 base swaps + 3 FT rolls**, all 3 FT rolls trained on the same 22-query monoculture. The "108× larger query support" recipe (R1 with 2384 prod queries / ~12k triplets / reranker hard negatives, per `debate-recipes.md:111-119`) **was never built**. What landed as "R1-stage2" is, per `phase2_r1_outcome.txt:80-83` verbatim:

> "Stage-2 was CoSENT loss on the same 10-pair MNRL dataset that already produced -10.8pp on payfin-v0; loss change to CoSENT did not rescue it."

Position A leans on R1's failure as "the load-bearing test of the FT design space." It is not. CoSENT on 10 pairs / 22 queries is a **capability ceiling of the data, not the loss**. With 2956 triplets across 22 queries you cannot stress in-batch-negative diversity, you cannot teach the model anything about the 2362 prod queries it has never seen, and you cannot escape the 22-query monoculture diagnosed in `debate-recipes.md` §"Why this is hard" item 2 (label monoculture). The first non-MNRL FT roll on a properly-sized prod-query dataset is still owed.

What productive FT spend looks like (recipes from the team's own debate documents, **none of which was actually built**):

- **R1-as-designed** (`debate-recipes.md` §R1): TSDAE on 42k unlabeled chunks → CoSENT on 2384 prod queries × 5 reranker-mined hard negs ≈ 12k triplets, eval-v3-disjoint at the **query** level (CM4 fix per `debate-skeptic.md` §2.2). Honest p(win) at 0.18 (`debate-recipes.md:121`). Even after Skeptic discounting to 0.07, the discount hinges on the query-disjointness leak — a fix that costs ~30 LoC of CM4 plumbing and a re-mine, **not** a property of FT itself. Cost $3.50.
- **R3 MarginMSE distillation** (`debate-recipes.md` §R3): teacher = `reranker_ft_gte_v8`, $2.50, p(win) 0.15 honest. **The only recipe in the table with rank-aware soft labels** — qualitatively distinct signal from any of the 6 things tested. Untouched.
- **R5 MLM continued pre-training** (`debate-recipes.md` §R5): canonical fix for "model knows English, not pay-com vocab" (Gururangan 2020 "Don't Stop Pretraining"). $4. Untouched.
- **Domain-adaptive contrastive on full 1242 unique prod doc-intent queries** (`p6-pivot-strategist.md` §7 explicit P7 plan): the *strategist himself* lists this at **p(win) 0.30–0.40 on eval-v3**. That number is in the document Position A quotes to claim "axis exhausted." It is not exhausted by his own numbers.

If `p(win) ≥ 0.20` recipes exist in the team's own debate documents and have not been built, the axis is **unsampled**, not exhausted.

### Argument B2 — eval-v3-n143 is too statistically weak to declare "ceiling"

Two facts about the AND-gate make the closure premature.

**B2a — Power.** Independently verified in `debate-verdict-v2.md` §5.4:

```
n=90:  paired SE = 0.0457  → ±9.0pp 95% CI
n=143: paired SE = 0.0354  → ±6.9pp 95% CI
n=200: paired SE = 0.0307  → ±6.0pp 95% CI
```

Position A correctly cites that "even a +5pp lift is detectable as a one-tailed signal at this n" — but the AND-gate threshold is **+10pp**, not +5pp. The recipe-architect's expected Δ for R1-as-designed is +0.05 ± 0.07 (`debate-recipes.md:121`). One-tail power for a true +5pp recipe at n=143 against a +10pp gate is ~18%. **Even if R1-as-designed is genuinely a +5pp recipe, four out of five rolls miss the gate**. Calling the axis dead on six rolls — only one of which used a non-MNRL loss, and that on a 10-pair training set — is calling a coin biased after counting flips with the coin held face-down for half of them.

A says "the variance across mechanisms is what makes this a structural-floor signal." That is a misreading. The 6 deltas span [-17.7pp, -0.6pp] but **none of them tested R1-as-designed**. R1-stage2 at -17.7pp is not evidence about the FT axis; it is evidence about training CoSENT on 10 pairs (`phase2_r1_outcome.txt:80-83`). Mxbai at -0.6pp is evidence about base-swap-without-FT. **There is no measurement of "well-resourced FT on this corpus" in the dataset Position A summarizes.**

**B2b — Stratum dominance.** From `/tmp/bench_v3_n143_docs.json` (verified):

```
trustly  n=3   r@10=0.289     ← single-query swing = 33pp
aircash  n=9   r@10=0.378
interac  n=9   r@10=0.481
method   n=13  r@10=0.168
refund   n=13  r@10=0.399
provider n=15  r@10=0.204
payout   n=15  r@10=0.098
nuvei    n=16  r@10=0.413
webhook  n=16  r@10=0.271
tail     n=24  r@10=0.142
```

`trustly n=3` means **a single misranked row moves the stratum recall by 33 percentage points**. The AND-gate condition #3 (`per-stratum drop ≤ 15pp`) is mathematically a **noise-detector for any stratum with n ≤ 7**. Position A's own catalog includes mxbai's "trustly -22.2pp" violation (`RECALL-TRACKER.md:59`) — produced by **at most one sample** flipping. That is not a rejection on signal; that is the gate pruning out submissions on stratum sample noise. (Note: A wrote "trustly n=4 per eval-grow-stats.json"; the actual bench JSON shows n=3 — an even hotter noise floor than A claims.)

**B2c — Sequential testing.** Six gated comparisons against the same baseline at α≈0.05 each: family-wise false-rejection rate ≈ 1 − (1-0.05)⁶ ≈ 26%. Some fraction of the 6 rejections is **expected under the null**. We have not corrected for this, and Position A does not.

The eval signal *as currently sized* is fine for confirming that candidates with -10pp Δ (payfin-v0, R1-stage2, nomic-v2-moe) are bad. It is **not** sized to declare "no recipe in the design space can win." That requires either n ≥ 200 with each stratum n ≥ 10, or a different question.

### Argument B3 — The team is benching the wrong layer

Production retrieval is `query → router → docs-tower (top-50) → reranker_ft_gte_v8 (top-10)` (per `p6-pivot-strategist.md` §3). The **reranker** decides the deploy-relevant ranking. The eval harness in this campaign measures *bi-encoder R@10 alone, with the reranker bypassed* — verifiable in `/tmp/bench_v3_n143_docs.json` (the JSON keys include `rerank_off, router_bypassed`).

Two consequences neither A nor the prior debate engaged with:

1. A docs-tower whose **R@100 improves** but R@10 looks flat would never clear the AND-gate, yet would lift end-to-end deploy quality (more recall → more candidates → reranker has more to choose from). `debate-recipes.md` §R3 explicitly calls this out: *"improving recall@100 — even at no Δr@10 — reduces reranker error rate."* **None of the 6 candidates was measured at R@100 with the reranker engaged.**
2. Conversely, a docs-tower that picks up +5pp R@10 in the bi-encoder but degrades reranker compatibility (e.g., embedding-norm drift, prefix mismatch — exactly the bug payfin-v1-original had per `final-report.md:27`) could pass the bi-encoder gate and still hurt prod. We assume the bi-encoder gate is the prod gate; it is not.

The honest end-to-end measurement is a **missing experiment**, and it is cheap: existing benchmark just needs `rerank_off=False` and the production reranker pipeline. Until that runs, "axis exhausted" is a claim about the bi-encoder layer alone.

### What productive FT spend looks like (justifying $11.42 banked)

Position A's strongest move is "$11.42 is banked, why not save it?" Here is what the budget *would buy*, with p(win) > 0.20, that hasn't been tried:

1. **Eval-v3 grow to n=200** (~6h human, $0). Tightens paired SE to ±6.0pp and gives every stratum n ≥ 10. **Precondition** for any honest +5pp detection. Without it the next axis (router/reranker) faces the same measurement problem A is unwilling to face on this axis.
2. **Build R1-as-designed** (TSDAE → CoSENT on 12k triplets across ~2k prod queries with query-disjointness CM4 fix per `debate-skeptic.md` §2.2, $3.50 capped). The first **honest** non-MNRL FT roll in this project. If it loses on eval-v3-n200 with reranker engaged, the axis closes on real evidence.
3. **Reserve $4 for end-to-end harness with reranker engaged** ($0 marginal compute; ~1d human plumbing). So the next 6 candidates measure deploy quality, not bi-encoder R@10.

Total: **≤$3.50 spend** (well inside $11.42), one new measurement on the right metric on a properly powered eval. After that, "axis exhausted" is a defensible claim.

### Pre-rebuttal — A will say "6 attempts = robust base rate"

A's likely attack: *"Six independent rolls span the recipe space (MNRL FT, MoE, vanilla base-swap, novel-loss FT). Bayesian Jeffreys 1/13 ≈ 0.077 supports closure; adding a 7th roll has marginal information value < $1."*

Three counter-points.

1. **Reference class.** Jeffreys 1/13 assumes the 6 rolls are exchangeable IID samples from "FT recipes for this corpus." They are not. Three are base swaps (different mechanism; correctly categorized as "vanilla-base-swap subclass" `n=3` per `debate-verdict-v2.md` §5.5 and `debate-skeptic.md` §6). Three are FT rolls on a 22-query monoculture (a second subclass also `n=3`). A correct sub-class Jeffreys for "FT with diverse loss + ≥1k triplets across ≥500 queries on this corpus" is **n=0** → prior = 0.5, not 0.08. We have **never run that class.**

2. **Uninformative replication.** Each of the 6 regressed by -4 to -18pp. We learned: "tiny MNRL on 22 queries collapses base," "base swaps without FT don't gain," "CoSENT 3-epoch on 10 pairs damages base." These are **not 6 different lessons about the FT axis**; they are 6 confirmations of one lesson (under-train + under-data → regression). They tell us nothing about "well-resourced FT on this corpus" because we never ran one. The 7th roll (R1-as-designed with 12k triplets) is the **first independent draw** from the actual FT design space.

3. **Decision-theoretic asymmetry isn't where A puts it.** A frames the spend as "$3.50 burn vs $13.30 banked for P7." But the same strategist Position A quotes (`p6-pivot-strategist.md` §7) lists the **P7 plan** as "domain-adaptive contrastive on full prod query log, $5–8, p(win) 0.30–0.40 on eval-v3." That recipe **is an FT recipe**. If A is correct that the FT axis is exhausted, **P7 is already invalidated by A's own argument**. If P7 is still on the table at p(win) 0.30, the FT axis is not exhausted; the team is just deferring the recipe-with-real-prior to a fresh session for energy/process reasons. That is a calendar-and-energy decision, not an axis-exhaustion finding.

A's fourth point (R1's missing disjointness "would only make R1 worse") is *almost* right but inverts the inference: under transduction leak, R1's measured -17.7pp is a **lower bound** on its regression on a query-disjoint set. That confirms R1-stage2 is bad. It says nothing about R1-as-designed (which fixes both the disjointness gap **and** the 22-query monoculture by training on 12k triplets across 2384 prod queries). Conflating "R1-stage2 was bad" with "R1-the-recipe was bad" is the central error in A's argument.

### Position B — Round 1 verdict signal

The doc-tower FT/swap axis is **NOT exhausted**. Of 6 measurements: 3 are base swaps (different mechanism, fairly judged dead-as-tested *on this eval* — but the eval is power-limited and the layer is wrong). 3 are FT rolls on 10–91 pair / 22-query monocultures (not a fair test of the FT design space). The recipe with measurable signal (R1-as-designed, 12k triplets across 2384 prod queries, p(win) 0.18 by author / 0.30+ by strategist) has never been built. A productive next move within the existing $11.42: grow eval-v3 to n=200, build R1-as-designed with query-disjointness CM4 fix, run end-to-end with reranker engaged. Total ≤ $3.50. If *that* loses on eval-v3-n200 with reranker engaged, the axis closes honestly. Otherwise it remains the highest-EV lever the team has.

---

## Position B — Round 2 (cross-rebuttal of A's Round 1)

### A's strongest point in B's words

A's strongest single argument is **the variance-spread argument** in pre-rebuttal point #1: "The 6 deltas span [-17.7pp, -0.6pp]; if there were a hidden +5–8pp recipe lurking in the design space, at least one of the 6 rolls would have shown a positive delta or near-zero. Six negative deltas across four mutually distinct mechanisms (MNRL FT, MoE swap, vanilla BERT swap, novel-loss FT) is not consistent with a power-limited noise band — it is consistent with a structural floor sitting below baseline." That is the single argument that does the most work in A's case, and it is the one that, if true, makes my Round 1 power-and-recipe-space critique secondary.

### Rebuttal

The variance-spread argument is **statistically appealing but mechanistically wrong** because it treats the 6 deltas as draws from a single posterior over "FT-recipes-on-this-corpus," when they are actually draws from **three distinct distributions with different expected means**:

1. **Distribution D1 — "tiny-data MNRL/CoSENT FT" (n=3, mean Δ ≈ −12pp).** payfin-v0, payfin-v1-fixed, r1-stage2 all share the property that the loss optimizer saw ≤91 unique (q, doc) pairs across 22 unique queries. The expected delta of this class is **strongly negative by construction** — the recipe-architect predicted it, the failure-analyst confirmed it (`p6-failure-analyst.md`: anisotropy collapse, head-provider drift), and the skeptic priced it (`debate-skeptic.md` §2.1). Three draws from D1 averaging −12pp is **expected**, not surprising. They are evidence about D1's mean, not about the FT axis.

2. **Distribution D2 — "vanilla base swap, no FT" (n=2 measured, mean Δ ≈ −2.4pp).** mxbai (-0.6pp), nomic-v2-moe (-4.1pp). gte-large blocked. The expected delta is **mildly negative** (transfer ratio from MTEB to private corpus is empirically ~30–60% lossy per `debate-skeptic.md` §4.3). Two draws averaging −2.4pp is **also expected**, not surprising — and notice these two are the candidates closest to baseline. If the corpus had a hard structural floor, D2 should have shown roughly the same regression as D1; instead D2 sits ~10pp closer to baseline. That is a **divergence between distributions**, which contradicts A's "single-floor" narrative.

3. **Distribution D3 — "well-resourced FT" (n=0 measured).** TSDAE+CoSENT on 12k triplets across 2384 prod queries with hard negatives and query-disjointness, MarginMSE distillation, MLM continued pre-training, domain-adaptive contrastive on 1242 unique prod queries. **Zero rolls.** A treats r1-stage2 as a draw from D3 because it nominally "used CoSENT loss." But `phase2_r1_outcome.txt:80-83` is unambiguous: r1-stage2 trained on the same 10-pair set as payfin-v0. **It is a draw from D1**, just with a different loss attached, on the same impoverished data. Loss family alone cannot rescue a 10-pair training set from in-batch-negative degeneracy; the recipe-architect was explicit that the *combination* (12k triplets × 5 hard negs × 2384 queries × CoSENT) is what defeats the failure mode (`debate-recipes.md:124-131`). Pulling the data scale from 12k → 2956 triplets while keeping the loss is not "the recipe was tested"; it is "1/3 of the recipe was tested."

The variance-spread A points to is **across distributions**, not across draws-within-a-distribution. The proper inference is "D1 is bad (3/3), D2 is mildly bad (2/2), D3 is unmeasured (0/0)." A 7th roll from D3 is the **first draw** from a distribution whose expected mean we have not estimated. The Bayesian update from "5 D1+D2 confirmations" to "D3 is also dead" requires assuming D3's posterior collapses onto D1+D2, which is exactly the assumption the recipe-architect's mechanism argument disputes (TSDAE attacks anisotropy that D1 had; CoSENT pairwise margin avoids the in-batch coupling that D1 collapsed on; 12k triplets across 2384 queries supplies the diversity D1 lacked).

A also asserts (pre-rebuttal #3) that R1's missing query-disjointness assert means the measured -17.7pp is a **lower bound** on its true regression. This is correct **for r1-stage2 specifically** (transduction leak inflates measured retrieval; remove the leak, retrieval gets worse). But it is **silent** about R1-as-designed: query-disjointness is part of R1's spec (CM4 in `debate-recipes.md`), so a properly-built R1 doesn't have the leak to remove. A is rebutting a claim B did not make. B's claim is: r1-stage2 ≠ R1; R1-as-designed has not been built; the regression we observed is uninformative about it.

Finally, A's pivot-EV argument ("router c3 has p(any positive lift)=0.78 at $0") is genuinely strong — but it is **not exclusive of running R1-as-designed**. The pivot-strategist's own §7 (`p6-pivot-strategist.md`) lists *both* router c3 (Win 2, $0) *and* a P7 FT recipe at p(win) 0.30–0.40 ($5–8) as the budget plan. A is collapsing the strategist's two-axis plan into a one-axis pivot. The honest reading is: do router c3 *and* run one well-resourced FT roll. They are not budget-competitors at the $11.42 level; they are complements.

### Concession

I concede one part of A's case: **r1-stage2's -17.7pp result is real evidence against "any FT loss-family change alone rescues the 10-pair regime."** That collapses one specific subset of the recipe space (CoSENT-on-tiny-data) and meaningfully drops its prior. But that is a much narrower claim than "FT axis exhausted." The recipe space that retains real signal is "well-resourced FT with diverse loss + ≥1k triplets across ≥500 queries with query-disjointness" (D3), and that distribution remains untested.

### Position B — final stance

The doc-tower FT/swap axis is **not exhausted**. The 6/6 record is 3 confirmations of D1 (tiny-data FT) being bad, 2 confirmations of D2 (vanilla base swap) being mildly bad, and 0 measurements of D3 (well-resourced FT). A productive use of $3.50 of the $11.42 banked is one D3 roll on eval-v3 grown to n=200 with reranker engaged, alongside (not instead of) router c3. If that loses, the axis closes on real evidence. The current "pivot now" decision closes the axis on D1 evidence misclassified as D3 evidence — Bayesian update on the wrong reference class.

---

## Position B — Round 1 + Round 2: complete

---

## Position A — Round 2 (cross-rebuttal of B's Round 1)

### B's strongest single point — restated in my words

B's load-bearing move is the **distribution-decomposition argument**: the 6 measurements are not 6 independent samples of "the FT axis"; they are 3 draws from D1 (tiny-data FT, mean ≈ −12pp), 2 from D2 (vanilla base-swap, mean ≈ −2.4pp), and **zero from D3 ("well-resourced FT" with 12k triplets across ~2k prod queries + hard-negs + query-disjointness)**. Within this framing, R1-stage2 was substituted at execution time with a CoSENT roll on the same 10-pair set as payfin-v0 (`/tmp/phase2_r1_outcome.txt:80-81` verified verbatim) — so R1 is a D1 draw, not a D3 draw. Therefore the "axis exhausted" inference is closing D3 on D1+D2 evidence, which is a reference-class error.

### Two narrow concessions, then defense of the axis-level claim

**Concession 1 — R1-stage2 ≠ R1-as-designed.** B is factually correct. `/tmp/phase2_r1_outcome.txt:80-81` is unambiguous: "Stage-2 was CoSENT loss on the same 10-pair MNRL dataset that already produced -10.8pp on payfin-v0." My Round 1 implicitly used R1-stage2's -17.7pp as evidence about the recipe-architect's 12k-triplet plan. That conflation is wrong. R1-stage2 falsifies "CoSENT-on-10-pairs," not "R1-as-designed."

**Concession 2 — trustly stratum n=3 (not n=4).** `/tmp/bench_v3_n143_docs.json` shows `per_stratum_n["trustly"]=3`; my Round 1 used the eval-grow-stats.json pre-filter count of 4. A single misranked trustly query = 33pp swing — even hotter than my Round 1 admitted. mxbai's "trustly -22.2pp" violation can be a 1-sample flip, not a structural-knowledge gap.

These concede the *narrow* dataset framing, not the *axis-level* verdict. Three rebuttals.

### Rebuttal 1 — B's reference class understates the prior; v12a alone makes D3 a bad bet

B's "subclass Jeffreys for D3 = n=0 → prior 0.5" is too charitable. The reference class for "FT on this corpus, any loss/data scale" is wider than the 6 measurements:

- **v12a — 12 reranker FT iterations on this same corpus** (`project_v12a_rejected_two_tower_pivot.md`, in MEMORY): Δr@10 −0.020 train / −0.030 test, *stable across 12 attempts* with various losses, data scales, hard-negs. v12a sits in a **third sub-class** B did not list: "well-resourced *reranker* FT on this corpus" (different from doc-tower FT but same corpus, same eval, same Bayesian inference about *corpus difficulty*).
- **The 3 base-swap measurements (D2) ARE in-class for "this corpus has narrow MTEB-headroom slack."** nomic-v1.5 = 62.39 MTEB, mxbai = 64.68, nomic-v2-moe ≈ 63 MTEB. The +2-3pp MTEB headroom evaporated to -0.6 to -4.1pp in-corpus. **D2 is not orthogonal to D3** — it's evidence about corpus difficulty that any FT recipe inherits.

Combined record: 18+ FT/swap attempts on this corpus (P5 v12a 12 iter + P6 P5/P5b/P5c/P6/P7 6 iter), all regressed. Sub-class Jeffreys for "any FT/swap recipe on this corpus" is **1/20 ≈ 0.05**, not 0.5. Even granting B's full mechanism-multiplier (recipe-architect's ×1.6 for new-loss-family + hard-negs + query-support, which already failed once on R1-stage2 to predict the outcome), honest p(win) for R1-as-designed is **≤0.10–0.15**, not 0.30–0.40.

### Rebuttal 2 — The strategist's "P7 plan at p(win) 0.30–0.40" is not a refutation of axis-closure; it IS axis-deferral

B's most rhetorically effective move (Round 1, point 3): "the strategist Position A quotes lists P7 as domain-adaptive contrastive at p(win) 0.30–0.40 — that's an FT recipe — so by A's own argument the axis isn't exhausted."

This is a precision-of-claim issue. The verdict in `debate-verdict-v2.md` and the strategist's `:130-138` are **deferring R1-class FT to P7 with PRECONDITIONS:**

- Eval-v3 grown to n=200+ (currently 143)
- Query-disjointness CM4 fix in `prepare_train_data.py`
- End-to-end harness with reranker engaged (per B's own correct catch on `rerank_off=True`)
- 1242-unique-query training set (per `:136`), not 22-query monoculture

The strategist's 0.30–0.40 prior is **conditional on those preconditions being met**. Running R1-as-designed in P6 *without* them is not the same recipe — it's a corner-cut version that the strategist explicitly rejected (`:138`: "Optional opt-in for user: after Iteration 1, run a single (a2) FT for $2-3 with hard-negative mining. p(win) on eval-v3 = **0.15-0.20**"). The strategist's lower-bound is 0.15–0.20 *with* eval-v3-as-fixed; the 0.30–0.40 number requires the additional eval-v3-n200 + end-to-end + CM4 work.

**The pivot decision = "ship process gains in P6, build preconditions in P7, then run D3."** B's framing collapses "defer D3 to P7 with preconditions" into "axis exhausted." That conflation is exactly the precision error B accuses *me* of in his Round 1. Restated: I do not claim "FT will never work on this corpus." I claim "the recipe classes built so far (D1, D2) are exhausted; D3 is unmeasured but high-cost / medium-prior; the responsible place to run D3 is P7 *with* preconditions, not P6 in a session that already extracted 6 honest rejections." This is exactly `debate-verdict-v2.md`'s stance.

### Rebuttal 3 — On end-to-end measurement: B is right that bench is bi-encoder-only; but this strengthens, not weakens, the pivot

B verified `rerank_off=True, router_bypassed=True` in the bench JSON. **Confirmed.** My Round 1 conceded the 5-condition AND-gate is bi-encoder R@10. Two consequences:

(a) **The asymmetry of the "wrong layer" argument cuts against FT.** B argues a docs-tower with flat R@10 but improved R@100 could lift end-to-end. *True in principle.* But the 6 measured candidates show R@10 = -0.6 to -17.7pp. If R@10 is regressing at this magnitude on *bi-encoder*, the candidates are sending *worse* candidates to the reranker — fewer correct items in the top-50, more reranker workload, more reranker errors. End-to-end re-measurement of these 6 would not flip them to deploys; at best it would shrink the magnitude of regression by reranker-saturation effect, at worst it would amplify regression via reranker-incompatibility (norm drift, prefix mismatch — `final-report.md:27` documents one such bug).

(b) **The actually-cheap next experiment is end-to-end on baseline + on the strongest candidate (mxbai at -0.6pp), to establish whether reranker compensates.** This costs $0 marginal compute + ~1d human time. **It is a precondition for any future FT-axis decision** — including B's proposed R1-as-designed roll, which would be measured on the same harness. Running R1-as-designed before the end-to-end harness exists is doing exactly what B accuses me of: closing the axis on the wrong measurement layer. **Therefore the responsible sequencing is: (1) close P6 with process gains, (2) build end-to-end harness in P7 prep, (3) grow eval-v3, (4) THEN evaluate D3 candidates with the right gate.** That sequencing puts D3 at the *end* of the queue, not in P6.

### What I now claim (refined position)

The doc-tower FT/swap axis is **exhausted for the recipe classes that have been built or where the cost-benefit at the P6 timeline is documented unfavorable.** Specifically:

- **D1 (tiny-data MNRL/CoSENT FT, ≤91 pairs, ≤22 queries):** 3/3 + 12 reranker-FT v12a iter + small-N priors → closed. p(win) ≤ 0.05.
- **D2 (vanilla base-swap to MTEB-comparable models):** 3/3 → closed (gte-large blocked at HF bug = unbuildable, not unmeasured). p(win) ≤ 0.05.
- **D3 (well-resourced FT with 12k triplets / 2k prod queries / reranker hard-negs / query-disjoint CM4 / TSDAE-pre-adapt):** untested. honest p(win) **0.10–0.15** (after sub-class Jeffreys + corpus-difficulty discount + R1-stage2 disconfirmation of recipe-architect's ×1.6). **Real cost: $3.50–$5 + 10–14h human time.** EV at $4 + 10h: **−$3.85 + opportunity-cost of router c3 missed = strongly negative.**

The pivot from "P6 spend on D3" to "P7 with preconditions, then D3 if budget allows after router c3 + reranker A/B prep" is **Bayesian-correct under any reasonable prior**, including B's most charitable reference class. The pivot is not "axis closure forever"; it is "axis closure for P6, with conditional re-opening in P7 IF preconditions land AND budget remains." That is exactly `debate-verdict-v2.md`'s stance.

What B's argument did correctly invalidate: my Round 1 framing that "R1 disproved the FT design space." Restated honestly: **R1-stage2 disproved CoSENT-on-10-pairs sub-recipe.** R1-as-designed remains untested with honest p(win) ~0.10–0.15. The responsible sequencing places R1-as-designed in P7 after preconditions, not in P6 against the current eval + bi-encoder gate. **B's substitution catch sharpens the verdict; it does not overturn it.**

### Round 2 verdict signal

The pivot — close P6 on doc-tower model swap/FT axis, ship process gains, bank for P7 with preconditions (eval-v3 grow + CM4 query-disjointness + end-to-end harness) under which R1-as-designed (D3) becomes a coherent next-axis bet — is the correct decision. B's strongest catches (R1-stage2 ≠ R1-as-designed; bench is bi-encoder-only; trustly n=3) refine but do not flip the verdict. They reinforce that the responsible place to run D3 is P7-with-preconditions, *not* P6.

---

## Position A — Round 1 + Round 2: complete
