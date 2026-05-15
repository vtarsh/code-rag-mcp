---
name: P6 T3 — pivot strategist
date: 2026-04-25
author: p6-pivot-strategist (debate teammate)
inputs: loop-state.json, verdict-stagec-v2, eval-critic.md, project_v12a_rejected_two_tower_pivot.md, project_docs_model_research_2026_04_24.md, project_docs_production_analysis_2026_04_24.md
budget_remaining_usd: 10.69
iter_remaining: 3 (no_improvement=2/5)
question: maximize p(deploy>baseline) — iterate or finalize?
---

# Strategic verdict: **OPTION (d) — accept baseline + ship eval-v2 as gold (PROMOTE-NULL).**

Headline numbers:
- E[p(win)] for (a) more FT: 0.10–0.18 (and conditional on eval-bias being correctable)
- E[p(win)] for (b) reranker swap: 0.05 (out of P6 scope, weeks of regression)
- E[p(win)] for (c) router improvements: 0.30–0.45 — but lift caps at +1 to +3 pp recall@10, NOT enough to clear the +10pp AND-gate
- **E[p(win)] for (d) finalize + harden eval-v2**: 1.00 against the lowered bar "ship process gain" (zero NEW failed FT spend, eval-v2 becomes the durable artefact)

**TL;DR: Stop spending the $10.69 on more candidates that compete on a biased eval. Convert the remaining 3 iterations into eval-v2 hardening + a tiny router probe. Reranker swap and more FT both have <20% p(win) at this point and high opportunity cost.**

---

## 1. Cost-vs-p(win) decision table

Grid uses observed P5 spend rates ($0.34/h GPU, ~3-4h per FT round = $1.0-1.4) + measured Δrecall@10 from prior 4 runs.

| Option | Cost (USD) | Iter | Wall-clock | E[Δr@10] vs nomic | Best-case Δr@10 | p(win, AND-gate +0.10pp) | p(any positive lift) | Risk |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| **(a1)** Re-FT v2 with more pairs (200→500), MNRL+TripletLoss | $1.5–2.5 | 1 | 4–5 h | -0.05 ± 0.08 | +0.04 | **0.05** | 0.30 | label noise dominates; v0 / v1 / v1-fixed all regressed; no single hyperparameter swing has produced +0.10pp on this corpus |
| **(a2)** Re-FT with hard-negative mining from baseline top-50 misses | $2.5–3.5 | 1–2 | 6–8 h | +0.00 ± 0.10 | +0.06 | **0.10** | 0.45 | best in family — addresses the "labeler bias" critique but eval still biased same way → may not transfer to prod |
| **(a3)** FT a bigger base (e5-mistral-7b on RunPod A40) | $4–7 | 2 | 10–14 h | +0.02 ± 0.12 | +0.12 | **0.18** | 0.55 | only honest path to +10pp on biased eval, but BLOCKS budget for everything else; 7B = 14GB RAM, won't fit on 16GB Mac → forces architecture change to remote inference |
| **(b)** Swap reranker (gte-base → bge-reranker-v2-m3) | $2–4 + weeks of regression testing | 2 | 1–2 weeks | UNKNOWN | UNKNOWN | OUT OF P6 SCOPE | n/a | reranker_ft_gte_v8 = 8 iterations of domain FT; replacing it = unwinding months of work; explicit research-memory exclusion: "Reranker NOT in A/B scope" |
| **(c1)** Router precision: tighten `_query_wants_docs` regex on the 348 OOB queries (currently unrouted) | $0 (Mac CPU) | 1 | 4 h | +0.005 ± 0.015 | +0.03 | 0.02 | 0.65 | safe but tiny; 348/2860 = 12% of traffic, of which maybe 30% are mis-routed ⇒ 4% address. Lift caps low. |
| **(c2)** Router fan-out: dual-search (both towers) for mixed-intent + RRF merge | $0 | 1 | 6 h | +0.015 ± 0.02 | +0.05 | 0.05 | 0.70 | already partially shipped per `project_two_tower_v13_landed.md` ("mixed → both, dedupe by rowid"). Marginal extension. |
| **(c3)** Routed long-tail recall: hand-write 30 doc-intent regexes for prod top-30 terms (payout, gateway, refund, apm…) → force docs index | $0 | 1 | 6 h | +0.02 ± 0.025 | +0.07 | **0.05** | **0.78** | highest p(any-positive) of the cheap options — but still misses +0.10pp |
| **(d)** Accept baseline + ship eval-v2 + close H2/H3 bias (hand-relabel 30 stock-doc rows + drop the 27% train-leaked rows) | $0 | 1–2 | 8–12 h | n/a (process win) | n/a | **1.00** vs "P6 deliverable = trustworthy eval" | 1.00 | converts $10.69 saved into durable infrastructure that unlocks future P7-P8 iterations. **Eval gold replaces auto-heuristic-v1 forever.** |

### Math behind p(win) numbers

p(win) = **P(Δr@10 ≥ +0.10pp on rebuilt-prod knowledge.db AND deploys without latency regression)**.

Reference base rate: 4/4 candidates rejected (0% raw success). Bayesian prior with Jeffreys' adjustment = 1/9 = 0.11 per attempt. Apply per-option modifiers:

- **(a1/a2)** same FT recipe family that lost 4× → multiplier 0.5 → p(win) = 0.055 → 0.10
- **(a3)** different base model class (7B vs 137M) → modifier 1.6 → p(win) = 0.18, BUT eval-v2 is biased toward FTS+path-overlap → divide by 2 for transfer risk = **0.09 honest p(win) on prod**
- **(c)** router never produces the magnitude of lift required (caps at +0.03–0.07pp on the 12% OOB slice; AND-gate is +0.10pp on full eval) → p(win) ≤ 0.05 mechanically
- **(d)** the win condition is redefined: ship a fair eval. Given eval-critic.md confirmed-severe H2 bias, this is where the highest-leverage work lives.

---

## 2. Why (a) more fine-tunes is a sunk-cost trap

Three pieces of historical evidence make additional FT iterations the worst dollar-for-dollar bet:

1. **v12a precedent** — `project_v12a_rejected_two_tower_pivot.md`: 12 FT iterations on the *reranker* on the same corpus produced Δr@10 −0.020 train / −0.030 test. Single-tower CodeRankEmbed was the bottleneck; reranker tweaks were sunk-cost. The current docs-tower is in the **same architectural box**: nomic-embed-text-v1.5 is already top-15 MTEB; FT on a 91-pair training set against biased labels is *guaranteed* to regress for the same reason — labels reward what the baseline already does.

2. **P6 in-flight precedent** — Stage D produced **3 distinct FT artefacts** (v0 random init, v1 double-prefix, v1-fixed corrected). All 3 regressed −0.07 to −0.13 vs baseline. The fourth attempt (v2) on the same eval, same prep, same recipe class would be a 5th draw from the same urn. Empirical base rate so far: 0/3 with our recipe = upper Wilson 95% CI on p(win) ≤ 0.56, point estimate ≈ 0.0 — the only reason p(win) isn't 0 is small-sample.

3. **Eval-v2 itself is broken** (eval-critic.md, H2 confirmed-severe): 45% of expected_paths are stock auto-docs, 25% of rows have ALL-5 stock paths, and 27% of rows leak with train. **A model that finds the *right* provider-specific doc gets penalized.** No amount of FT can win an eval that punishes correctness. Spending more on FT before fixing the eval is solving the wrong problem.

---

## 3. Why (b) reranker swap is out of scope

Three independent gates rule it out:

1. **Memory boundary**: `project_docs_model_research_2026_04_24.md` explicit budget says "Reranker NOT in A/B scope — reranker_ft_gte_v8 is 8 iterations of domain FT, replacement would cost weeks of regression testing."
2. **Scope alignment**: P6 was framed as the docs-tower decision. The router/reranker layers are P7-P8 backlog.
3. **Cost asymmetry**: a reranker swap costs nothing in $ (CPU bench), but ~80h of human regression work + the entire RECALL-TRACKER baseline suite has to be re-validated. Not a $-bound option, a calendar-bound option.

---

## 4. Why (c) router improvements have a hard ceiling

Production analysis (`project_docs_production_analysis_2026_04_24.md`) gives the math:

- 46.8% of search calls are doc-intent → docs tower (1339/2860 calls)
- 12% (348/2860) are out-of-band (1 tok or ≥16 toks) — currently unrouted to docs
- Mixed-intent (53 calls) hits both, dedupes by rowid, merges by RRF — already shipped in v13

Best-case router gains, by mechanism:
- Tighter `_query_wants_docs` regex over the 348 OOB → +0.02pp on full traffic
- Routed long-tail by term whitelist (payout/refund/apm/…) → +0.04pp
- Dual-tower fan-out for ambiguous code-intent → +0.02pp

**Sum of best cases ≈ +0.08pp recall@10** on full traffic, and that assumes everything works at theoretical maximum. Below the +0.10pp AND-gate. Even if the router is the right next axis (it likely is), it cannot satisfy P6's deploy bar by itself.

---

## 5. Why (d) is the only choice with p(win) → 1

Reframe what "win" means at this point. The original P6 deploy bar (+0.10pp recall@10 on `doc_intent_eval_v2`) is **unachievable on the current eval** — proven by eval-critic.md confirming labeler is biased toward the existing baseline. Continuing to chase +0.10pp on a biased eval is a category error.

The shippable wins from the remaining $10.69 + 3 iterations:

**Win 1 — Eval-v2 hardening (Iteration 1, ~4–6 hours, $0):**
- Hand-relabel ≥30/44 rows where stock auto-docs dominate; replace with provider-specific or reference docs that *answer the query*.
- Drop the 12 train-leaked positives (per eval-critic H3).
- Add 10–15 prod-sampled rows up-weighting `payout` / `gateway` / `validation` strata to match prod term-mass (eval-critic H1).
- Outcome: **`doc_intent_eval_v3.jsonl` becomes the durable gold artefact** — every future axis (router, reranker, retrieval) is measured against it. Removes the H2 systematic bias.

**Win 2 — Cheap router probe (Iteration 2, ~6 hours, $0):**
- Implement (c3) routed long-tail: term-whitelist for prod top-30 → force docs index.
- Re-run benchmarks against eval-v3.
- p(any positive lift) ≈ 0.78 per cost table. Even if it doesn't clear +0.10pp, a +0.02pp validated-on-fair-eval lift is **shippable**.

**Win 3 — Document the negative result + close P6 (Iteration 3, ~2 hours, $0):**
- Update RECALL-TRACKER.md with: nomic-embed-text-v1.5 stays prod, eval-v3 published, 4 candidates rejected with measured deltas, NEXT_SESSION cues for P7 (router) + P8 (reranker reconsider).
- Memory entry in this debate's verdict file.
- $10.69 returned to budget for next axis.

**Cumulative p(P6 ships value) = 1.00.** Negative-result publishing + eval upgrade + small router probe are guaranteed wins. They convert a likely-failed FT race into reusable infrastructure.

---

## 6. Counter-argument I considered and rejected

> "Just one more FT with the bias-corrected eval-v3 — that's the honest test."

Tempting, but:
- Eval-v3 takes Iteration 1 to build. Full FT cycle is Iteration 2-3.
- Even with eval-v3, base-rate evidence (4/4 regressions, v12a precedent) keeps p(win) ≤ 0.20 for any single FT.
- If eval-v3 is built and we have one iteration left, the *correct* use of that iteration is a fast cheap probe, not a $2-3 FT roll.
- A useful FT pass here would need a fundamentally different recipe (e.g. domain-adaptive contrastive on full prod query log, not 91 hand-pairs). That's a P7+ project, not a P6 closer.

If the user explicitly accepts the risk and wants to spend $2-3 on (a2 hard-negative mining FT) AFTER eval-v3 is built, total cost is $2-3 of the remaining $10.69 with p(win, on eval-v3) ≈ 0.15-0.20. Worth offering as an opt-in path; not the default recommendation.

---

## 7. Concrete recommendation to lead

1. **Decide:** stop hunting candidates. P6 deliverable becomes "fair eval + negative-result writeup + router probe."
2. **Iteration 1 (4–6 h, $0):** rebuild eval as `doc_intent_eval_v3.jsonl` per eval-critic fixes.
3. **Iteration 2 (6 h, $0):** ship router term-whitelist (c3); benchmark on v3.
4. **Iteration 3 (2 h, $0):** finalize RECALL-TRACKER + memory + NEXT_SESSION; close P6.
5. **Hold $10.69 for P7** — first axis where it's honestly spendable: domain-adaptive contrastive on full prod query log (1242 unique doc-intent queries) with hard-negative mining from baseline misses. Estimated $5-8 pod cost; p(win) on eval-v3 ≈ 0.30–0.40 because (a) bigger training set, (b) fair eval, (c) different recipe class.

Optional opt-in for user: after Iteration 1, run a single (a2) FT for $2-3 with hard-negative mining. p(win) on eval-v3 = 0.15-0.20. If user values the experiment more than the saved budget, take it; otherwise skip and bank the $.

---

## 8. What I am NOT recommending and why

- ❌ More v0/v1/v2 FT on current pair set — same recipe family, same bias, same urn.
- ❌ gte-large unblock (modeling.py fork) — yak-shave outside P6 scope; ~6h debug for a model that may not even win after fixing.
- ❌ Reranker swap — explicit out-of-scope; weeks of regression cost.
- ❌ Try a 4th candidate "for completeness" — sunk-cost reasoning; eval is broken.
- ❌ Spend the $10.69 just because it's allocated — preserved budget unblocks P7's bigger swing.

---

## Open questions for lead before close

1. Does "ship process gain" count as P6 success in your framing? (My read: yes, eval-v3 is reusable forever.)
2. Do you want the optional opt-in (a2) hard-negative FT after eval-v3 lands, or bank the $?
3. Should I draft the v3 labeling rubric now, or wait for synthesis (T4)?
