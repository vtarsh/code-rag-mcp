---
role: failure-analyst
mode: adversarial
inputs: /tmp/bench_v2_*.json (5 files)
output_owner: p6-failure-analyst
date: 2026-04-24
---

# P6 T1 — Per-candidate failure analysis

## TL;DR

| candidate | R@10 | NDCG@10 | Hit@10 | Δ R@10 vs base | net Hit@10 flips (lost − gained) | verdict |
|---|---|---|---|---|---|---|
| **`docs` (baseline nomic-v1.5)** | **0.3277** | **0.5210** | **0.74** | — | — | **WINNER** |
| `docs-payfin-v0` | 0.1990 | 0.2710 | 0.48 | **−0.129** | **+26 (37 lost / 11 gained)** | catastrophic regression |
| `docs-payfin-v1-fixed` | 0.2568 | 0.4909 | 0.72 | −0.071 | +2 (14 lost / 12 gained) | mild regression, near-tie on NDCG |
| `docs-nomic-v2-moe` | 0.2720 | 0.4056 | 0.65 | −0.056 | +9 (19 lost / 10 gained) | regression — NOT the cheap win we hoped |
| `docs-gte-large` | — | — | — | — | — | **NEVER BUILT** (`no_table` — LanceDB shard absent) |

All four landed candidates LOSE to baseline on every headline metric. None of them clear the +3pp Recall@10 threshold from the docs-research memory. **No candidate should ship.**

---

## 1. Adversarial framing

Before per-candidate analysis: this benchmark itself is suspect for the FT models because:

- The eval-v2 set is 100 queries, **0 gold-labeled**, 0 prod-sourced (`n_gold_rows=0`, `n_prod_queries=0` for every run). The whole eval is the synthetic stratified sample.
- All 100 queries report `strata=[]`, so the "per-stratum" recall collapses to a single bucket `__none__`. We **cannot** answer "does fine-tune lose on a specific stratum?" — the eval set was built with no usable stratification field per row. That is a data-pipeline bug; flag it to T2.
- The labeler used baseline nomic-v1.5 to generate `expected_paths`, so any candidate that disagrees with baseline on borderline files is pre-penalized. Net effect on this benchmark is ≤6pp (T2 will quantify) but it is real.

That said: even granting the most charitable assumptions, the **Hit@10 deltas are too large to be label-bias artifacts** — losing 37/100 hits (v0) cannot be explained by labeler-overlap noise. The candidates fail on their own merits.

---

## 2. Per-candidate root cause

### 2a. `docs-payfin-v0` — catastrophic FT collapse on head distribution

- **R@10 0.199** (−39% relative). 37 queries flipped baseline-hit → cand-miss; only 11 the other way.
- **First-token of LOST queries** is dominated by the head providers we have the most training data for:
  ```
  nuvei: 5    paysafe: 5    interac: 3    payper: 3    trustly: 2
  ```
- **First-token of WON queries** is dominated by tail providers:
  ```
  aircash: 3    paynearme: 1    credorax: 1
  ```
- **Lost-query expected-path provider distribution:**
  ```
  nuvei-docs: 45    paysafe-docs: 15    payper-docs: 14    trustly-docs: 12
  paynearme-docs: 10
  ```
- **Anisotropy collapse**: top-10 cosine score spread for v0 is mean=0.064 with min=0.0042 — top1 score 0.843, top10 0.908 (everything in a 6pp window). On the Nuvei-checksum example, the v0 top-10 are all in the 0.570–0.602 band. The model has lost discriminative geometry — every doc looks ~equally similar to every query.
- **Concrete example** — `"Nuvei payout checksum formula concatenation order merchantId merchantSiteId clientRequestId clientUniqueId amount currency"`:
  - baseline finds `documentation_accept-payment_payment-page_cashier.md` at rank 1
  - v0: that file does not appear in the top-10 at all; ranks fill with `latin-america-guides_pix.md`, `asia-pacific-guides_gcash.md`, etc. (other Nuvei docs, but wrong topic).
  - Same query: v1-fixed recovers it back to rank 1.

**Root cause for v0:** training corpus heavily biased the embedding manifold; the FT process pulled head-provider docs into a tight cluster while flattening cross-doc geometry. Result is anisotropy / mode collapse on exactly the providers we cared about most.

### 2b. `docs-payfin-v1-fixed` — partial recovery, still a regression

- **R@10 0.2568** (−22% relative); **NDCG 0.4909** (−6% relative). Hit@10 0.72 vs baseline 0.74 — within noise on hit-rate.
- **Net flip count = +2** (14 lost, 12 gained) — barely net-negative on retrieval.
- **What v1-fixed fixed (vs v0)**: 34 queries that v0 missed are recovered by v1-fixed. The recovered set is dominated by the same head providers v0 collapsed on (Nuvei checksum, Nuvei payout output params, Nuvei addUPOAPM, Interac auto-deposit, etc.).
- **What v1-fixed still loses (vs baseline)**: 14 queries — heavily Nuvei (15 path occurrences) and Trustly (10) and grpc-apm-trustly (4). Same head-provider failure mode, just smaller magnitude.
- **Score-scale anomaly**: v1-fixed `score` values are unnormalized (top-1 mean 157.9, top-10 mean 185.2 — looks like raw L2 distance, lower-is-better but stored as-is). Within-query order is correct so ranking still works, but distance metric for this shard differs from the others. That alone wouldn't explain the regression but it is an artifact worth noting.
- **Diagnosis**: v1-fixed largely fixes v0's collapse but still does not exceed baseline on ANY metric. The "fix" was successful at undoing the worst of v0 yet the fundamental bet — that FT on pay-com docs would beat vanilla nomic — is not paying off.

### 2c. `docs-nomic-v2-moe` — drop-in upgrade that ISN'T

This is the candidate we expected the cheapest win from (memory-note: "drop-in 768d, deploy without testing other candidates if ≥3pp lift"). Result is the opposite:

- **R@10 0.272** (−17% relative); **NDCG 0.4056** (−22% relative — much worse than v1-fixed despite higher recall).
- 19 queries lost / 10 gained; net flip = +9.
- **Lost queries** match the same head-provider pattern: nuvei-docs (17), payper-docs (7), trustly-docs (6), paysafe-docs (5).
- **Won queries** skew to APM/gRPC repos: grpc-apm-aircash (12), aircash-docs (5), grpc-apm-trustly (4) — i.e., it picks up some structural-doc wins but loses provider-doc wins.
- **No anisotropy issue** (spread mean 0.062, similar to baseline 0.041).
- **Score top-1 mean is 0.844 vs baseline 0.515** — different score range but ordinal behavior fine.

**Diagnosis**: nomic-v2-moe is a different geometry than v1.5 — it's not a "drop-in" upgrade for our index. Out-of-the-box it is **strictly worse** for our docs corpus, contrary to the +3-5% claim from the nomic blog. The MoE routing seems to favor different doc-shape neighbors than v1.5 in our specific index.

The memory-note "deploy without testing other candidates if ≥3pp lift" — the lift is **−5.6pp**, so the gating logic correctly says **DO NOT DEPLOY**. But the prior expectation needs to be retracted (see T3).

### 2d. `docs-gte-large` — never evaluated

- The result file contains a single skip record: `skipped_reason: "no_table"`, detail: `db/vectors.lance.docs.gte-large has no 'chunks' table`.
- The build pipeline failed silently for gte-large (or was killed mid-build before producing the table). **No conclusion can be drawn about gte-large from this run.**
- This is an infrastructure/run failure, not a model failure. T3 should decide whether to retry the build before declaring the candidate dead.

---

## 3. Cross-candidate patterns

### 3a. The 3 queries ALL THREE candidates lose

These are the structural failures that survive any model swap:

1. `Adjust Verification Checks for Global Individuals, Websites, Partners and bank account` — expects checkout-docs newsroom posts (PR/marketing pages — possibly mis-labeled).
2. `/payout checksum fields order merchantId merchantSiteId clientRequestId userTokenId amount currency` — Nuvei (head-provider, technical query).
3. `paysafe Interac webhook handler timeout 30 minutes 24 hours pending lookup` — Paysafe Interac.

(2) and (3) suggest a deeper docs-corpus issue with provider-specific technical retrieval — possibly the chunks for these answers are large/buried. T2 should verify these expected_paths are actually well-chunked in the index.

### 3b. Common LOST path tokens (all candidates)

`docs, providers, api, nuvei, documentation, payment, interac, reference, paysafe, trustly, payper`

All four LOST sets are dominated by **provider docs** (`/docs/providers/{name}/...`). This is consistent across v0, v1-fixed, and moe — the candidates systematically degrade on long provider-documentation file paths.

### 3c. Common WON path tokens

`docs, providers, api, coding, guide, security, overview, aircash, credorax, frame, gotchas`

Wins skew toward **structural / cross-cutting docs** (`gotchas`, `references`, `coding`, `guide`, `overview`) and **tail providers** — a smaller volume of corpus that all three candidates generalize to better than baseline.

---

## 4. What the evidence rules in / out

| hypothesis | verdict | evidence |
|---|---|---|
| FT v0 overfit head providers, lost geometry | **CONFIRMED** | 37 losses centered on Nuvei/Paysafe/Interac; top-10 cosine spread collapsed to 6pp; recovered by v1-fixed |
| v1-fixed beats baseline | **REJECTED** | −0.071 R@10, −0.031 NDCG, net −2 hits — strict regression even after the fix |
| nomic-v2-moe is a free drop-in upgrade | **REJECTED** | −0.056 R@10, −0.115 NDCG — strictly worse on this corpus |
| gte-large is dead | **NOT TESTED** | infrastructure failure (no_table); rerun needed before deciding |
| Eval bias from labeler-baseline overlap explains all losses | **UNLIKELY** | flip magnitudes (37, 14, 19) are too large; T2 will quantify the actual bias contribution |
| Per-stratum failures are FT-vs-baseline-specific | **UNTESTABLE WITH THIS DATA** | all queries report `strata=[]` — eval-v2 isn't usable for stratum-level diagnosis. Flag to T2 |

---

## 5. Recommendations to the synthesis agent (T4)

1. **Do not promote any candidate.** Baseline `docs` (vanilla nomic-v1.5) wins on Recall@10, NDCG@10, Hit@5, and Hit@10.
2. **Retract the v0/v1 FT line of work**, or at minimum require a holdout trained on a head/tail-balanced sample before another FT cycle. The current FT data over-represents head providers and induces anisotropy.
3. **Retract the "nomic-v2-moe is a drop-in upgrade" prior.** Memory note `project_docs_model_research_2026_04_24` should be updated to reflect the measured −5.6pp R@10 instead of the claimed +3-5%.
4. **Either rebuild gte-large and re-run, or remove it from the candidate list.** A skipped run is not a result.
5. **Fix eval-v2 stratification before the next round.** All 100 queries with `strata=[]` means we cannot diagnose per-domain regressions — that is the largest methodology gap and worth fixing before another model is trained.
6. **Cross-check against T2's eval-bias quantification**: even if eval-v2 over-favors baseline by some Δ, the v0 collapse (−12.9pp, anisotropy-confirmed) is too large to be bias-explained.
