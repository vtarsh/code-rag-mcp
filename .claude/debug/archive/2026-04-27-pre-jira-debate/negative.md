# NEGATIVE — keep ONE single reranker

**Position:** Reject the docs/code reranker split. Keep the single combined-data
reranker (production L6 baseline, or one re-FT'd on better-balanced combined
data). The split is wrong for THIS repo's constraints.

---

## Arg 1 — The router is a fragile regex pile, not a classifier; mis-routing destroys the per-axis gain

`src/search/hybrid.py:298 _query_wants_docs()` is a **7-tier regex cascade**
(`_DOC_QUERY_RE`, `_REPO_OVERVIEW_RE`, `_PROVIDER_ONLY_RE`, `_CONCEPT_DOC_RE`,
`_STRICT_CODE_RE` with hard-coded tokens like `doNotExpire`, `signalWithStart`,
`FF3Cipher`, `WITHDRAW_REQUEST`…), with a fall-through "2..15 tokens → docs"
heuristic. Already-shipped V4 churn evidence (`hybrid.py:310`):
**+394 OOB queries flipped routing, +12.9pp shift on prod traffic** when one
tier was edited. That is the magnitude of *one tweak*; held-out smoke caught
"11 OUT→IN flips" — the classifier is **continuously drifting** as we add
provider tokens. `positions_prior` line 67 records "12% of prod calls miss docs
tower entirely."

If a 2-reranker world routes 10–13% of code queries to the docs reranker (and
vice versa), the **expected gain dilutes proportionally**. Run 1 deltas: docs
+1pp, code +1pp. A 12% mis-route eats ~0.2pp on each axis, leaving the split
within noise. **The router is NOT a free oracle** — it is a maintenance burden
the affirmative side under-counts.

## Arg 2 — 904 code pairs alone is below the FT viability floor

`finetune_data_combined_v1/train.jsonl` has **904 code pairs** (constraints
doc, line 8). Run 1 confirmed CrossEncoder FT on **~10k pairs** (mxbai 10,971,
l12 10,724) was already noisy — bge-reranker-v2-m3 had `val_loss=null` on
**791 pairs** (RECALL-TRACKER line 17). A code-only reranker would train on a
~900-pair shard. Result: **overfit to the 80-query eval** (the same set we
benchmark on — leakage by structural similarity), or underfit and lose to L6
baseline on real prod traffic. The combined reranker absorbs both signals; a
split reranker has no such fallback.

## Arg 3 — 2× operational surface for solo developer; Run 1 already cost $4.50 to validate ONE axis

`memory/project_training_cycle_failure_2026_04_26` and RECALL-TRACKER line 8:
**6/6 candidates failed in cycle 1, $4.50 burned**, 5 distinct infra bugs
(missing CLI flag, ephemeral volume wipe, SSH key drift, eval too small to
discriminate, agent abandonment). Splitting the reranker DOUBLES every line of
that pain: 2 HF Hub repos, 2 retraining schedules, 2 deploy paths, 2
monitoring dashboards, 2 eval pipelines (we already saw `code_intent_eval_v1`
needs separate plumbing). Solo developer + ~$5.20 banked budget → cannot
sustain 2× ops debt for a marginal split-axis bet.

**Specific alternative:** *Single combined reranker, rebalanced training.*
Up-weight code pairs in the loss (e.g. 5× sample weight on the 904 code rows
to match docs cardinality). This addresses the same "code under-served"
diagnosis WITHOUT routing risk, WITHOUT 2× ops, WITHOUT 2× HF repos. Costs
**one** training run, ~$0.50.

---

## Acknowledged counter (the strongest)

Affirmative: *"Run 1 dual-axis empirically proved no single combined model
wins both axes — mxbai +1/-5, l12 -6/+1. Therefore split is the only path."*

## Pre-rebuttal

Three holes:

1. **n=80 code, n=100 docs**: paired SE math from `positions_prior:49` says
   n=143 is ±6.9pp 95% CI. n=80 is **±9.3pp**. The +1pp / -5pp deltas are
   **inside the CI** — Run 1 has not falsified single-model viability; it has
   only ranked two FT recipes that BOTH lost net.
2. **Trade-off isn't fundamental, it's recipe-specific.** Both Run 1 candidates
   used 1 epoch / lr=2e-5 / no per-axis loss weighting. Affirmative is
   conflating "this recipe couldn't win both" with "no single model can." The
   loss-weighting alternative above wasn't tried.
3. **The production L6 baseline already beats both FT'd splits on code**
   (RECALL-TRACKER + run1_status code table). If neither FT'd model dominates,
   we should diagnose the FT recipe, not double the surface area.

(594 words)
