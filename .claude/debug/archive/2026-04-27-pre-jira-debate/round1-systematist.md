# ROUND 1 — TEAMMATE S (Systematist)

**Lens:** Invariants, contracts, and making illegal states unrepresentable. Eval N=100/80 with ±9.3pp CI is *not* a measurement instrument — it's a coin-flip dressed as evidence. Every approach below is judged by: *which contract does it harden so the failure mode becomes impossible, not just detectable?*

---

## 1. Top 3 approaches IF Run 2 B+C don't dominate

### A. **Freeze a stratified, query-disjoint eval-v4 (n≥300 docs + n≥240 code) with per-stratum CIs as the merge gate**
- **Invariant:** "No reranker ships unless lower-95%-CI ≥ baseline R@10 on BOTH axes AND on every stratum with n≥30." Encodes the dual-axis + small-n risk into the gate itself, not into reviewer vigilance.
- **Contract leak it closes:** Today the train↔eval boundary leaks — `finetune_data_combined_v1` query hashes are not provably disjoint from eval-v3 queries (only assert added in P7 phase 1, eval-v3 was rebuilt later — verify in `scripts/build_doc_intent_eval_v3.py`). Stratified CI gate also closes the "single number masks per-stratum harm" leak (P10 calibrated A2 case: `nuvei -7.58pp / aircash -8.78pp / refund -14.51pp` hidden behind a +0.48pp aggregate).
- **Failure mode:** Stratum-imbalance fakery — if some strata have n=8 the per-stratum CI is uselessly wide and the gate trivially passes. Mitigation: refuse to compute per-stratum gate for n<30; explicitly mark "untested stratum" rather than infer.

### B. **Joint-loss reranker with hard per-axis floor enforced as a training-time constraint, not a hopeful side-effect**
- **Invariant:** Every checkpoint emitted to HF Hub has been validated against held-out *code* val + held-out *docs* val, and is rejected if either drops > 0.5pp vs init. Make negative transfer literally unrepresentable in the artifact pipeline.
- **Contract leak it closes:** Run 1 mxbai shipped a checkpoint that lost -4.7pp on code with no in-loop signal — there was no code val split (`memory/project_run1_2026_04_26.md`). Add `val_code.jsonl` + `val_docs.jsonl` produced by the same disjoint-query splitter as eval-v4; early-stop on `min(val_docs_R@10, val_code_R@10)`.
- **Failure mode:** Val splits drift from prod distribution (P10 calibrated discovered eval-v3 was biased — same risk for val). Mitigation: regenerate val/test together each cycle from the same prod-sampled head terms; hash and pin in `bench_runs/`.

### C. **Single reranker + hard router contract (typed `RouteDecision` + audit log + rollback)**
- **Invariant:** Router output is a typed enum `{DOCS, CODE, UNCERTAIN}`, every prod query writes `(query_hash, decision, confidence, downstream_R@10_proxy)` to a contract log. Promotion of any per-axis specialist is *gated* on router calibration ≥ 95% on a labeled sample. No router calibration → no specialist ships.
- **Contract leak it closes:** Today's `is_doc_intent` is a 7-tier regex (12% historical mis-route per `positions_prior`) — the split-reranker plan treats it as oracle. Affirmative debate's 2-model architecture is invariant-violating until this contract is signed.
- **Failure mode:** UNCERTAIN bucket grows monotonically (regex can't classify → everything routes to fallback) and the system silently degenerates to single-reranker mode. Mitigation: alert when UNCERTAIN > 5% rolling; force human label.

---

## 2. Top 1 evidence-gathering action FIRST

**Build eval-v4 (stratified, query-disjoint, n≥540) and recompute per-stratum CIs for Run 1 + Run 2 B/C *before* any new $ on training.** Cost: ~$0 + Opus labeling budget (~$3 per P10's 3-tier recipe). Without this, every Run-N delta is noise indistinguishable from signal — we are literally fitting to a 100-row coin.

## 3. ONE thing we should DEFINITELY not do

**Do not promote any reranker on a single-axis win or a sub-±9.3pp aggregate delta — even if both Run 2 B and C "look great."** That's the exact failure mode that nearly shipped P10 A2 (heuristic +2.89pp → calibrated -2.09pp reversal). Aggregate-delta promotion on n=100/80 is the contract leak that has cost us two cycles already.

