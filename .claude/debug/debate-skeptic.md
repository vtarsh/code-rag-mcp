---
name: debate-skeptic
date: 2026-04-25
author: skeptic (debate teammate, task #3)
team: debate-recipe-improvement
inputs:
  - .claude/debug/debate-recipes.md (R1..R5)
  - .claude/debug/debate-gte-unblock.md (U1..U3 + mxbai fallback)
  - .claude/debug/p6-pivot-strategist.md (option d, my anchor)
  - .claude/debug/p6-failure-analyst.md (per-candidate root cause)
  - .claude/debug/p6-verdict.md (eval-v2 bias, eval-v3 rebuild)
  - .claude/debug/final-report.md (4/4 reject track record)
  - .claude/debug/loop-log.md (~700 lines / 18 iterations)
  - profiles/pay-com/doc_intent_eval_v3.jsonl (n=100, n_eval=90)
  - profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl (197 rows / 22 unique queries)
  - logs/tool_calls.jsonl (verified 2384 unique queries — recipe-architect quoted 2387)
  - db/vectors.lance.docs/chunks (verified 49,142 rows — recipe-architect quoted 25k+16k=41k)
budget_remaining_usd: 13.30 (banked from $15 cap; recipe-architect "session cap" $11 is invented)
verdict: KILL R1, KILL R2/R3/R4/R5, KILL U1, ACCEPT-BASELINE wins as null hypothesis
---

# Skeptic critique — KILL the proposals, defend ACCEPT-BASELINE (option d)

> "If you find yourself agreeing with R1 or U1 without pushback, dig deeper. The default verdict is KILL." — task #3 brief

I read everything. **Both proposals fail the bar of "shows mechanism that distinguishes itself from the 4 prior rejections, with evidence the prior session would not have found."** Quantified below.

---

## §1. Per-proposal verdict table

| ID | Author claim p(win) | HONEST p(win) | Hidden costs | Verdict |
|---|---:|---:|---|---|
| **R1** TSDAE→CoSENT+HN-A | 0.18 | **0.07** | new script (~150 LoC), loss-flag patch (~30 LoC), reranker mining 80 min/Mac, eval rebuild risk, +CI for new code, +debug iterations on first-run failures | **KILL** |
| R2 CachedMNRL+HN-A | 0.10 | **0.05** | same infra cost as R1 minus TSDAE; same recipe family lost 4× | **KILL** |
| R3 MarginMSE distillation | 0.15 | **0.06** | teacher cross-encoder is the **production reranker**; distillation = double-use; same infra cost; teacher noise risk | **KILL** |
| R4 doc-internal InfoNCE | 0.05 | **0.03** | author admits ≤0.05 already; no benefit in P6 timeline | **KILL (cheap distractor)** |
| R5 MLM continued pre-train + MNRL | 0.13 | **0.05** | $4 (highest GPU cost), MLM head wiring on `trust_remote_code` model is risky, Stage 2 reverts to MNRL (the rejected family) | **KILL** |
| **U1** monkey-patch gte-large | 0.10 (clear AND-gate) | **0.03** | quality of `persistent=False` re-init is unverified at 49k-doc scale; MTEB→pay-com transfer prior collapses against 4/4 base-rate | **KILL** |
| U2 vendor copy | n/a | n/a | 1600 LoC of foreign code maintained forever | **KILL** (author also rejects) |
| U3 gte-base alone | n/a | n/a | same bug; useless | **KILL** (author also rejects) |
| **(d) ACCEPT-BASELINE** | 1.00 (process gain) | **1.00** | none — preserves $13.30 for P7 domain-adaptive on 1242 unique prod doc-intent queries with eval-v3 fairness | **KEEP** |

p(win) deltas applied below per proposal.

---

## §2. R1 — TSDAE → CoSENT + reranker hard negatives — `recipe-architect` top pick — **KILL**

### 2.1 Historical contradiction — partial. R1 *is* a recipe-family escape, but the failure mode it patches is not the bottleneck

Recipe-architect's central claim is that "anisotropy from positive-only MNRL on small N" is one of four named failure modes for the prior 4 rejections, and TSDAE attacks it. Let's audit.

`.claude/debug/p6-failure-analyst.md`:
- **v0 (10 pairs)** — *anisotropy CONFIRMED*: `top-10 cosine score spread for v0 is mean=0.064 with min=0.0042 ... The model has lost discriminative geometry — every doc looks ~equally similar to every query.` (l.55)
- **v1-fixed (91 pairs)** — *no anisotropy*: 34 queries that v0 missed are recovered, but `the fundamental bet — that FT on pay-com docs would beat vanilla nomic — is not paying off.` Failure-analyst diagnosed v1-fixed as a different problem (eval bias + eval ceiling), not anisotropy.
- **nomic-v2-moe** — *no anisotropy*: failure-analyst l.80 verbatim: `No anisotropy issue (spread mean 0.062, similar to baseline 0.041).`

So 1 of 3 measured candidates had anisotropy. The other 2 lost for different reasons (geometry mismatch, eval ceiling, head-provider distribution drift). R1's TSDAE Stage 1 *only* attacks the v0 mode. Stage 2's CoSENT loss is genuinely novel-to-this-project, but the assumption that "non-MNRL = will not collapse" is not load-bearing — v1-fixed didn't collapse and still lost.

**Mechanism assertion fails:** R1 fixes 1/4 confirmed failure modes, not 4/4 as the proposal frames it.

### 2.2 p(win) overestimation — Jeffreys 1/9 = 0.11 — modifiers don't justify ×1.6

R1 stack:
```
Jeffreys 1/9 = 0.11
× 1.3  (new loss family CoSENT)
× 1.2  (reranker hard negatives)
× 1.05 (108× more queries)
= 0.18
```

Each multiplier is *defensible* but *uncalibrated*:

- **×1.3 for new loss family** — CoSENT has a paper from 2022 (Su et al.); it's not a moonshot. But CoSENT vs MNRL has *not* been measured on *this corpus* against this baseline. The base rate for "new loss family wins on a domain-shifted retrieval task" in our project is **0/0** (never tried) — the prior on 0/0 must default to the global Jeffreys, not get a free 30% multiplier. Honest modifier: ×1.0.
- **×1.2 for reranker hard negatives** — nomic-v2-moe didn't have this and lost. v1-fixed didn't have it and lost. Author claims *"by-construction what the system currently mis-ranks"* — but distillation from production reranker is also what R3 does, and R3's own p(win) is 0.15 (lower than R1's). Self-inconsistent. Honest modifier: ×1.1 at most.
- **×1.05 for 108× larger query support** — and here's the strongest single attack: **the 108× claim mis-states the data:**

#### Verified by me, 2026-04-25:
```
$ python3.12 -c "<count unique queries in tool_calls.jsonl with tool=='search'>"
total search calls: 2973   unique queries: 2384
```
Not 2387. Off by 3. Cosmetic.

But more importantly, **eval-v3's 100 queries are ALL present in the prod log**:
```
eval-v3 query overlap with tool_calls log: 100 / 100
```
Recipe-architect's R1 claims path-disjointness via:
```python
eval_paths = {(r.repo, p.file_path) for r in eval_v3 for p in r.expected_paths}
leaked = [p for p in pairs if (...) in eval_paths]
```
**Path-disjointness is not query-disjointness.** A train pair (q, doc) where `q` IS one of eval-v3's queries but `doc` is not one of the 166 eval-v3 expected_paths still lets the model memorize a query-to-corpus shortcut at training that artificially boosts retrieval on the eval-v3 query at test time, because the candidate pool at eval time includes lots of documents the model trained on. This is a **silver-positive transduction leak**. The paper-style fix is also query-disjoint. Recipe-architect did not assert query-disjointness in CM4. Material gap.

If query-disjointness IS enforced, the support shrinks from 2384 → 2284 (only 4% loss, still huge), but the team must amend the script. If NOT enforced, eval-v3 numbers will be inflated by an unknown amount. Either way, the claimed +1.05× is not justified at face value.

**Honest p(win) recalculation:**
```
0.11 × 1.0 × 1.1 × 1.0 = 0.121
adjust for: silver labels (reranker pseudo-labels) noisier than gold ×0.85
adjust for: untested infra (build_train_pairs_v2 doesn't exist) ×0.85
adjust for: Stage-1 smoke kill at -3pp is a low bar — does not catch "no lift" ×0.9
≈ 0.07
```

### 2.3 Hidden costs — the "$3.50" budget excludes the work that actually matters

What's NOT in the $3.50 estimate:

1. **`scripts/build_train_pairs_v2.py` does not exist.** Recipe-architect admits in §"Open gaps" item 2: *"~150 LoC: load logs/tool_calls.jsonl, filter via _query_wants_docs mirror, run baseline retrieval+reranker on each query, emit rows ..."*. 150 LoC of new infra with mining, dedup, eval-disjoint enforcement, reranker score caching — and pytest coverage. That's not a 30-min job. Ballpark with reasonable code review + edge cases: **4–6 h human time**, plus iteration when first 12k pairs land with bad distributions.

2. **`train_docs_embedder.py` loss-flag plumbing.** Recipe-architect: *"~30 lines, must keep 719/719 green"*. The current `train_docs_embedder.py` is 5.8K bytes and hard-codes MNRL. Adding `--loss=mnrl|cosent|marginmse|tsdae|mlm` is not 30 lines; each loss has its own data-format expectation (CoSENT needs scored pairs, MarginMSE needs `(q,pos,neg,margin)`, TSDAE wants raw text, MLM wants tokenized streams), and the wrapper currently builds a single InputExample shape. **Realistic: 3–5 h**, plus a unit test per loss to keep the pytest count honest.

3. **Reranker mining 80 min on Mac CPU is a one-time cost** *if it works the first time*. Author admits: *"If Mac CPU is too slow, run mining on the same A40 pod"*. So the contingency is "spin a pod for mining" → another $0.50–$1.00 of unbudgeted GPU time. CPU mining at 30q/s × 2384 queries × 100 candidates = 80 min — but cross-encoders on 100-token segments with no batching are closer to 5 q/s on Mac CPU, not 30. Likely ~8 hours real wall, in which case "do it on the pod" becomes the default and adds $1.

4. **Pre-flight + memory budget.** Pod-only does not protect against the Mac smoke step (Stage 1 kill-gate at $0.50 floor). The Mac smoke loads a 768-d model on already-stressed RAM. If avail < 5 GB at smoke time (recall: launchd 03:00 incremental can run during the day too) → smoke fails for irrelevant reasons → "Stage 2 not pursued" decision driven by environment, not model.

5. **Eval-v3 has only 90 effective rows.** A 95% CI on a R@10 difference at p=0.25, n=90, paired (rho≈0.5): **±9pp**. The AND-gate is +10pp absolute. To clear the gate **with confidence**, R1 needs ≈+13pp lift. R1's expected Δ is +0.05 ± 0.07 (1σ). Expected p(landing ≥ +13pp) is **<5%** under the author's own variance estimate. The "kill at -3pp" smoke gate doesn't catch this; only the post-Stage-2 full bench does, by which point the full $3.50 is committed.

**Realistic total cost of R1 if it gets to Stage-2 bench:**
- Code: ~10 h human time (build_train_pairs_v2 + loss-flag + tests + debug)
- GPU + storage: $3.50–$5.00
- Eval rebuild risk: 0% if query-disjoint enforced rigorously; otherwise the result is uninterpretable
- Opportunity cost: **$3.50 toward P7 lost** (more on this in §6).

R1's per-iteration human time is the biggest hidden cost. The session has $13.30 banked but a *fixed* number of human iterations, and 10 h on R1 prep is 10 h not spent on:
- Eval-v3 expansion to n=200+ (per p6-pivot-strategist Win 1, ~6 h)
- Router term-whitelist (per c3, ~6 h, p(positive lift)=0.78)
- Reranker A/B harness (P8 setup)

### 2.4 Eval transferability — eval-v3 is **prod-sampled**, but the lift may not transfer to prod

`final-report.md` shows eval-v3 at n=90 with model-agnostic labeler. Good. But:

- eval-v3 strata: payout=11, provider=10, nuvei=11, webhook=11, method=9, interac=9, refund=11, trustly=4, aircash=10, tail=14. **Trustly has 4 rows.** A +1 query swing on trustly = +25pp on that stratum. The AND-gate forbids per-stratum drop > 15pp; +25pp swings dominate the gate.
- 100% of eval-v3 queries are in tool_calls.jsonl — eval-v3 IS prod. **But not weighted by prod frequency.** The 1339/2860 doc-intent prod calls have a long-tail distribution. Eval-v3 is uniform-sampled over strata. A +10pp eval-v3 lift could be a +0pp or even -2pp prod lift if the lift is on tail strata.
- R1's training set takes uniform queries from prod log. If the strata weights between train and eval are misaligned (likely, since train uses raw frequency and eval uses stratified), the gradient will pull the embedding toward whatever queries dominate the 12k pair pool — which are NOT eval-v3's underrepresented strata (trustly).

**Eval-v3 → prod transfer is unmeasured.** The team has no instrument to verify it. This was not a problem in the 4-rejection-streak (everything failed equally on both eval-v2 and eval-v3); it BECOMES a problem the moment a candidate "wins" on eval-v3.

### 2.5 Sunk-cost reasoning — recipe-architect partially admits it

> "If R1 fails entirely (Δr@10 ≤ 0), drop FT axis for the session and bank $7.50 toward router/reranker work — that's the strategist's verdict from p6, and R1 was the highest-prior FT shot. Don't double down by re-running R3/R5 after R1 fails."

This is honest. But **it's also the strategist's verdict applied AFTER spending $3.50.** The strategist's verdict before R1 was: **don't spend the $3.50 in the first place.** R1's structure is: "spend $3.50, and if it fails, take the strategist's advice." The strategist's advice was: take the advice now, save the $3.50.

The decision-theoretic asymmetry is brutal:
- **Run R1, p(win)=0.07**: 7% chance to deploy a +10pp R@10 doc tower. Value: real but unquantified ($/year savings).
- **Run R1, p(fail)=0.93**: $3.50 burned + 10 h human time + delayed P7 launch by 1 session.
- **Skip R1**: $13.30 + 10 h preserved for P7. P7's p(win) on 1242 prod queries with proper recipe is 0.30–0.40 (per pivot-strategist §1).

Under EV: skipping R1 dominates running it unless we believe R1 is *uncorrelated* with P7. If R1 fails because the corpus + nomic base + eval combo has a true ceiling (which is the failure-analyst's hypothesis), then P7 with the same corpus + same base will likely fail too. **Running R1 first is information for P7**, but it's $3.50 + 10 h of information we don't actually need — we already have 4 rejections on this corpus. The 5th rejection moves Jeffreys from 1/9 to 1/11. Marginal information value.

**Verdict: R1 KILL.**

---

## §3. R2 / R3 / R4 / R5 — quick kills

### R2 — **KILL.** Same MNRL family that lost 4×, just with grad-cache. Author honestly p(win)=0.10. Honest p(win) under Jeffreys with weak modifier: ~0.05. Cheap is not enough when expected information value is ~zero.

### R3 — **KILL.** Distilling production reranker into bi-encoder is **double-use of the reranker model**. End-to-end pipeline gain is bounded by what the reranker already does at serving. The "improving recall@100" defense is theoretical — at our deployment, retrieval is `top-50 → reranker → top-10`, so recall@10 lift is what matters, not recall@100. If reranker already covers the cases distillation teaches the bi-encoder, no gain. p(win) honest ≈ 0.06.

### R4 — **KILL** (author admits p=0.05). Heading-body InfoNCE is doc-internal and adds zero query-distribution signal. Cheap distractor. Skip.

### R5 — **KILL.** Stage-2 reverts to MNRL. The hypothesis "MLM domain adaptation rescues a base + lets MNRL win" is untested in this project. Cost $4 — the MOST EXPENSIVE recipe — to test a recipe family that is half-rejected. p(win) honest ≈ 0.05. Worst $/p(win) of the five.

---

## §4. U1 — gte-large monkey-patch — **KILL**

### 4.1 Diagnosis quality is a real upgrade, but doesn't justify the proposal

`gte-unblocker` did genuinely better forensics than the prior session: traced `persistent=False` buffer corruption, re-init via `_set_cos_sin_cache`, CPU smoke shows shape=(1,1024) and discrimination Δ=0.083 on a 3-doc payment-vs-fruit comparison. **Credit where due.** This is solid root-cause work and supersedes the prior session's "NTK-overflow" hypothesis.

### 4.2 But `p(unblock)=0.95` ≠ `p(produces honest A/B numbers)`

`gte-unblocker`'s 3-doc smoke proves:
- Model loads.
- Encoding doesn't IndexError.
- Cosine of two related strings differs from cosine of two unrelated strings.

That's **necessary but not sufficient** for production indexing. What it does NOT prove:
- The 49,142-row docs corpus indexes to a coherent space (vs e.g. some chunks producing nan/zero vectors at the patched buffers' mathematical limits).
- The cos-similarity quality is competitive with nomic-v1.5 on real eval queries.
- Build pipeline (`build_docs_vectors.py`) handles the model's quirks (different `auto_map`, custom modeling.py, NTK rope at sequence lengths > 8192).

Honest `p(unblock` for purposes of A/B `)`: I'll grant 0.85 — the patch is mathematically plausible and locally tested. But **`p(beats baseline +10pp on eval-v3)` is the actual win condition**, and the unblocker's own honest estimate is **~0.10**.

### 4.3 The gte-large MTEB → pay-com transfer prior is weak

`gte-unblocker` cites:
- gte-large MTEB English avg = 65.39 vs nomic-embed-text-v1.5 = 62.39 (+3pp)
- "Conditional on landing without regressing, the upside is bounded by MTEB gap"

But:
- MTEB is dominated by general-domain retrieval (NQ, FiQA, MS-MARCO, HotpotQA, etc.) — pay-com is a private corpus of provider integration docs. The MTEB → domain-shifted-retrieval transfer ratio is well-known to be lossy (often 30–60% of the MTEB gap evaporates on private corpora).
- nomic-v2-moe in this project: nomic blog claimed +3-5% over v1.5. Measured **−5.6pp** on eval-v3. Transfer ratio: **<0**.
- payfin-v0 / v1-fixed: in-domain trained, still lost. Transfer-from-public is even less reliable than transfer-from-domain.

Realistic priors on gte-large beating nomic-v1.5 on **this corpus**:
- Optimistic: +1.5pp (50% of MTEB gap transfers)
- Realistic: -1pp to +1pp (variance dominates)
- Pessimistic (consistent with nomic-v2-moe pattern): -3pp

`p(R@10 ≥ baseline + 10pp)` requires +10pp from -1pp expected = +11pp swing on n=90 against paired SE ≈ ±9pp. **<5% under any reasonable prior.**

### 4.4 Hidden costs U1 doesn't disclose

1. **The patch lives in production code.** `src/index/builders/docs_vector_indexer.py` — a hot path. A 10-line idempotent helper that monkey-patches buffers on a model class registered with `trust_remote_code=True` is a **maintenance liability** even though it's currently a no-op for non-gte models. Future contributor sees it, has to understand `accelerate.init_empty_weights` + `persistent=False` + `_set_cos_sin_cache` interaction. A helpful README or it ages badly.

2. **Pod cost is $0.30 IF the build succeeds first try.** 49k rows on gte-large @ 1024-d = ~200 MB float32 vectors + LanceDB overhead. Memory peak during indexing is harder to predict (gte-large is 335M params at fp32 = 1.4 GB resident, plus batched activation, plus LanceDB write buffers). A 24 GB GPU should handle it but author waved this off (only acknowledged in U3 fallback).

3. **Time cost.** 30 min code + 25 min pod = ~1 h human time. **This is the cheapest part.** It's not the cost concern; it's the *opportunity cost of mental focus*. While a teammate is monkey-patching gte-large, no one is rebuilding eval-v3 to n=200.

4. **The "5th rejection closes the doc-tower hypothesis" framing.** Author argues the information value of one more rejection is worth $0.30. **No.** The doc-tower hypothesis is already 4/4 closed in the practical sense — pivot-strategist correctly identified that the next axis is router/reranker. A 5th eval-v3 rejection doesn't change next-session strategy; it just adds noise to the project memory. Information value < $0.30.

### 4.5 Sunk-cost reasoning — gte-unblocker contradicts pivot-strategist by name

> "Pivot-strategist's '6h yak-shave' estimate is 12× too high. Actual cost: 30 min code + 25 min pod."

Two things wrong here:

1. **Pivot-strategist's verdict was not "6h is too long"; it was "this is yak-shave outside P6 scope."** Cost was secondary. The primary argument was scope: don't add candidates to a session about closing the doc-tower question. Even at 0 hours, U1 is misaligned with the strategist's framing.

2. **The 30-min estimate is patch-write time, not A/B-decision time.** Real wall-clock for a useful A/B: patch (30 min) + commit + push (10 min) + pod cycle (25 min) + lance build for 49k rows (15-30 min on GPU, depends on chunk distribution) + bench + compare (5 min). **Closer to 90 minutes**, not 55. Plus debug iteration if any of those steps surfaces a new bug. Realistic: 2-3 h before a deploy/reject decision.

Pivot-strategist's "6 h" was likely a worst-case-with-debug estimate. 2-3 h is best-case. Median: 4-5 h. **Pivot-strategist was not 12× off; he was 1.5–3× off worst-case, and 1.5× off best-case.** That's normal estimation noise, not "12× wrong."

**Verdict: U1 KILL.** $0.30 is small, but the gain is small × small. EV is negative-to-zero. The pod time goes toward P7 prep instead.

---

## §5. Strongest argument for accept-baseline (option d) — ≥500 words, brutal

The lead is going to be tempted to spend the $13.30 because $13.30 was budgeted. **That is sunk-cost reasoning in disguise.** Allocations are not obligations. Money preserved is money that funds a better swing later. Here is the brutal case for closing P6 with no spend.

**Item 1: 4/4 is real evidence.** The Bayesian update from a 4/4 streak under non-informative priors is significant. Jeffreys shifts the prior on "any FT recipe wins this corpus" from ~0.5 (uniform) to 0.11 (after 4 failures). Adding a 5th failure (R1) shifts it to 0.09. Adding a 6th (U1) shifts it to 0.08. **The information value of more failures decreases monotonically.** The team has already extracted the Bayesian update from the 4 measurements; spending money to extract more updates is paying for diminishing returns.

**Item 2: The specific failure modes already identified are not "loss family" or "loss form."** Failure-analyst's diagnosis was per-candidate:
- v0: anisotropy collapse (training set too small / too head-skewed)
- v1-fixed: no anisotropy, eval-bias-amplified, head-provider drift
- nomic-v2-moe: geometry incompatibility ("not a drop-in upgrade for our index")
- payfin-v0: same as v0 — head-provider overfit

**The pattern is "FT on a small/biased dataset against a moderate base on a private corpus tends to drift the base manifold faster than it discovers domain signal."** Recipe-architect's R1 ostensibly addresses this by 108× larger query support — but with silver labels, where the noise floor is set by the cross-encoder reranker's own errors. Reranker_ft_gte_v8 is itself an 8-iteration FT product on this corpus; its labels carry the reranker's bias, which is the reranker's correlation with the *baseline* embedding. Distilling reranker bias into the bi-encoder produces a bi-encoder that **agrees with the existing baseline more strongly**. That's the opposite of finding new signal.

This is a structural problem. R1 doesn't escape it; it reinforces it.

**Item 3: Eval-v3 ceiling is real.** Final-report.md confirms baseline R@10 = 0.2509 on eval-v3. That's already low — half of the labeled positives are not retrieved by the baseline at K=10. The eval is hard. A +10pp lift requires R@10 ≥ 0.3509, which is hard to clear under paired SE ±9pp. Recipe-architect's expected Δ of +0.05 is ~5.5pp **below** the gate. The math doesn't work. To clear +10pp in expectation, R1 needs Δ = +0.13, and a model that gets +0.13 on a 4/4-rejected corpus is exceptional. We have zero precedent.

**Item 4: Eval-v3 itself is fragile.** n_eval=90 with strata as small as 4 (trustly). A +25pp swing on trustly from a 1-query swap dominates the AND-gate (which requires no per-stratum drop > 15pp). The team can lose deployment on noise alone. This favors **growing the eval before doing more A/B**, not the reverse. Pivot-strategist's "Win 1: rebuild eval as v3 + grow to n=200" is the correct iteration #1 use of the next session.

**Item 5: The opportunity cost of $3.80 (R1+U1 combined) is concrete and quantified.** P7 in pivot-strategist's framing is "domain-adaptive contrastive on full prod query log (1242 unique doc-intent queries) with hard-negative mining." Estimated $5-8 pod cost. p(win) = 0.30–0.40 on eval-v3. **R1 partially overlaps with P7's recipe** (hard-negative mining + prod queries) — running R1 in P6 with hasty infra means P7 either repeats the same recipe or has to differentiate (smaller experiment surface). Spending $3.80 on R1+U1 in P6 cannibalizes P7's design. Far better: bank the $13.30, design P7 properly with eval-v3 that's been hardened, and run a single $5-8 candidate with proper infra.

**Item 6: Process gains already shipped.** Final-report.md item 1-6: eval-v3 model-agnostic labeler, 5-condition AND-gate, normalize_embeddings fix, max_seq cap + LONG_BATCH env, runpod skeleton, eval-v3 jsonl in private repo. **These are the durable wins of this session.** Adding R1 or U1 doesn't make them more valuable; it just adds risk of regression. P6's deliverable is already a 6-item infrastructure win + a 4/4 honest-rejection record + a closed doc-tower hypothesis. **That's success.**

**Item 7: Sycophancy resistance.** The lead will naturally feel "we have to do *something*." That's a cognitive trap. The strategist's option (d) is "ship process gain, close phase, write the negative result, bank money." It's not glamorous. It's correct. The 4/4 streak is the team telling itself a real thing about this corpus and this base; respect that signal by stopping.

**Total dollar impact of accept-baseline: $0 spent. $13.30 preserved. P6 deliverable: process gains + honest negative result.**

---

## §6. If you must spend — minimum viable spend that's NOT sunk-cost

If the lead overrides me and insists on spending some of the $13.30 in P6, here's the minimum-viable defensible plan. It is **NOT my recommendation**, but if the lead must spend, this is the least-bad way.

### Minimum-viable spend bound: $1.00 (not $3.80)

**Spend $1.00, NOT $3.80, capped at 1 iteration:**

1. **Skip R1 entirely.** R1's $3.50 + 10 h human cost + new infra is the single biggest opportunity cost. Defer to P7 with proper design.

2. **Skip U1 entirely.** $0.30 is small, but the EV is small × small. The patch CAN land as a no-op refactor in a future session if gte-large becomes the chosen base for P7 — at which point it's part of P7's budget, not P6's.

3. **Single $1.00 spend: build_docs_vectors run on a single new candidate from the ALREADY-VALIDATED list.** Honest candidates that have NOT been blocked or tested:
   - `mixedbread-ai/mxbai-embed-large-v1` (gte-unblocker §5; vanilla BERT, no patch, 1024d, MTEB 64.68)
   - One single-tower run with hard-neg-mined data (R2-lite without the loss-flag rabbit hole — just MNRL with hard negatives in the train pairs).
   
   Candidate of choice: **mxbai** (zero infrastructure risk; a true clean signal on whether ANY non-nomic base wins on this corpus).

4. **Iteration cap: 1.** If mxbai loses, the session closes immediately with no further spend. No "let's try one more." That's the rule that converts "if you must spend" into "minimum-viable spend."

5. **Eval rebuild before spend:** spend the first 4 hours of this iteration **growing eval-v3 from n=90 to n=150** (50 prod-sampled rows). This costs $0 and tightens paired SE from ±9pp to ±7pp. The +10pp AND-gate becomes statistically achievable. THEN run mxbai. If eval-v3 cannot grow this session, **skip the spend entirely** and revert to option (d).

### Why mxbai over R1/U1:

- **No infrastructure risk.** Vanilla BERT, no `trust_remote_code` quirks, no monkey-patches.
- **No FT.** Pure base-swap A/B. The 4 prior FT failures don't update p(win) for a base-swap (different mechanism).
- **Cheap.** Build vectors once, bench once, decide.
- **Honest closure.** If mxbai loses, the team has a 5th rejection on a *base-swap*, which is genuinely new information (4 prior were FT or MoE; mxbai is the first vanilla base-swap to a different family).
- **Cost: $1.00.** Build + bench in ~30 min on a single A40 hour at $0.34. Bank $12.30.

### What to NEVER spend on in P6:
- R1, R2, R3, R5 (FT recipes) — defer to P7 with proper design.
- R4 — author admits low p(win), no value.
- U1 — gte-large is a base-swap with infrastructure cost; mxbai is the same swing without the infrastructure cost. mxbai dominates U1 in EV.
- Reranker swap (b option in pivot-strategist) — out of P6 scope, weeks of regression.

---

## §7. Cross-cutting risks (R1 + U1 combined)

If lead approves BOTH R1 and U1 in same session:

1. **Combined burn $3.80.** That alone is fine. But the failure modes correlate: if pod is unstable (which happens), both fail, both burn, $3.80 lost with zero results. Stage-1 smoke kill on R1 may give a false-pass that triggers Stage-2 spend ($3.00 burn) on a recipe that wasn't actually showing signal.

2. **Cognitive bandwidth.** Running R1 (multi-stage, multi-script, new loss code) AND U1 (monkey-patch on a `trust_remote_code` model) AND a build pipeline for both in parallel is multitasking risk. One of them will get less attention; whichever loses attention will silently surface bugs that aren't caught. Failure-analyst already noted v1's "double-encoder prefix bug" was a silent bug from inadequate review.

3. **Decision noise.** Two candidates in flight = two A/B compare calls = two AND-gate decisions in one session. With paired SE ±9pp on n=90, false-positive rate of a single comparison is non-trivial; doing two doubles the risk that ONE of them looks like a winner by chance, and the team ships the noise.

4. **Memory pollution.** This session already has 6 process-gain memories to write. Adding R1 + U1 results adds 2-4 more memory entries (per-iteration; per outcome). Future-team has to read all of this. Pivot-strategist's option (d) writes ONE memory: "P6 closed with 4 rejections + 6 process gains." Clean.

**Combined recommendation: NEVER R1+U1 same session.** If lead must spend, pick at most one, and prefer mxbai over both.

---

## §8. Final scorecard

| Path | $ spend | Human-h | p(deploy) | EV vs (d) | Verdict |
|---|---:|---:|---:|---:|---|
| (d) ACCEPT-BASELINE + bank for P7 | $0 | 2 | 1.0 (process gain) | (ref) | **CHOOSE THIS** |
| Spend $1 on mxbai (single iter, eval-v3 grown to n=150 first) | $1 | 4–6 | 0.05 (deploy) | -0.50 | only if "must spend" |
| U1 alone | $0.30 | 1.5–3 | 0.03 (deploy) | -0.30 | KILL |
| R1 alone | $3.50–$5.00 | 10 | 0.07 (deploy) | -3.50 | KILL |
| R1 + U1 | $3.80–$5.30 | 12 | 0.07 (deploy) | -3.80 | NEVER |
| R3 / R5 alone | $2.50–$4.00 | 8 | 0.05 (deploy) | -2.50 | KILL |

**Default verdict for synthesis (task #4): GO option (d).** No FT, no monkey-patch. Close P6, ship final-report process-gains, bank $13.30 for P7 with eval-v3 hardened to n=150+ as Iteration 1.

If the lead absolutely insists on spending, **cap at $1 on mxbai with eval-v3 grown first**. NOT R1, NOT U1.

---

## Summary table for synthesis (paste-ready)

```
Skeptic verdict:
- KEEP:  (d) ACCEPT-BASELINE + bank $13.30 for P7    p(win) = 1.0 (process)
- KILL:  R1 (TSDAE→CoSENT)                            honest p(win) = 0.07
- KILL:  R2 (CachedMNRL+HN)                           honest p(win) = 0.05
- KILL:  R3 (MarginMSE distill)                       honest p(win) = 0.06
- KILL:  R4 (doc-internal InfoNCE)                    honest p(win) = 0.03
- KILL:  R5 (MLM pre-train + MNRL)                    honest p(win) = 0.05
- KILL:  U1 (gte-large monkey-patch)                  honest p(win) = 0.03
- IF-MUST-SPEND, cap $1: mxbai-embed-large-v1 single iter, eval-v3 grown to n=150 first.
- NEVER: R1+U1 same session.
```

Lead: spending $13.30 because it was allocated is **not** strategy. Banking it for P7 with eval-v3 hardened **is**. Take option (d).
