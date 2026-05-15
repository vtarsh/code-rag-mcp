# Planning debate — CONVERGED (Run 2+ approach to reranker)

## Verdict: **One winner survives all rounds (with strong cross-prior endorsement)**

**WINNER (unanimous, R2 cross-adoption):**
> **STEP 1 — `$0` CI bootstrap on existing `bench_runs/run1_*.json` + `run2_B_*.json` (+ `run2_C` when lands).**

Test: are Run 1/2 deltas (mxbai-combined +1pp docs / -5pp code, l12 -6pp / +1pp, mxbai-docs-only = 0.2609 = combined) inside the 95% CI? If yes → all current "split helps" / "negative transfer" / "specialist wins" hypotheses collapse to noise.

This action is FREE, takes ~30 min local Python, and gates every other $ decision in the queue.

## Genuine disagreement (preserved per template rule §3)

After CI recompute, two paths fork:

### Branch A — deltas INSIDE CI (likely; n=80 has ±9.3pp)
- **Pragmatist:** ship baseline L6, freeze reranker work, redirect budget to retrieval (where deltas are larger per RECALL-TRACKER)
- **Systematist:** build eval-v4 (n≥540, stratified, query-disjoint) before any merge gate is meaningful
- **Refactorist:** wrap eval-v4 in `EvalBuilder` factory so we don't hand-roll v5 next cycle

**Resolution test:** what's the per-stratum CI for the WORST stratum? If even per-stratum is noise-dominated, P wins (ship baseline). If there's signal but only per-stratum, S+R win (eval-v4 + factory).

### Branch B — some deltas OUTSIDE CI
Sequenced consensus path:
1. **R's `--smoke` flag** inlined into `oneshot_rerank.py` (50-row train + 20-row eval, ~30 min). Banks the $4.50 lesson from `project_training_cycle_failure_2026_04_26.md`.
2. **S's `min(val_code, val_docs)` early-stop hook** in `train_reranker_ce.py` (~20 LOC). Makes Run-1-style negative-transfer literally unrepresentable: trains aborts before HF push if either axis val drops below the floor.
3. THEN run **single reranker + class-balanced sample weights** (the loss-weighting alternative from architecture-debate ruling, Variant A — never executed).
4. Refactor `oneshot_rerank.py` + `oneshot_docs.py` → unified `oneshot_pod.py` only AFTER one full Run 3 cycle uses the new `--smoke` flag and validates the contract surface. Premature unification is exactly what P warns against.

## Definitely-don't list (3/3 unanimous against)
- **Spawn a 4th `oneshot_*.py` script.** Both P and R explicitly veto. Extensions land as flags or modules in the existing scripts only.
- **Promote any reranker on a single-axis win or sub-CI aggregate.** Re-runs the P10 A2 reversal pattern (+2.89pp heuristic → -2.09pp calibrated, see `project_eval_v2_calibrated_2026_04_26.md`).
- **Build the full "training framework" abstraction** before evidence justifies it. R wants extraction, P wants delay, both veto building it speculatively.

## Concrete next 2 actions (executable today, $0)
1. **Write `scripts/bootstrap_eval_ci.py`**: takes a list of `bench_runs/run*_*.json` + matching ground truth, computes paired bootstrap 95% CI for every (recall@10, hit@5, hit@10, ndcg@10) delta-vs-baseline. Prints per-axis + per-stratum table. ~50 LOC.
2. **Run it on:** Run 1 (l12, mxbai, bge bs=2, bge bs=8) + Run 2 (B docs-only, C code-only when lands) + production baseline L6. Decide Branch A vs B from the output, not from theory.

## Failure modes to monitor
- **Bootstrap on n=80 over-estimates noise** when eval is biased — check stratum coverage match before trusting CIs
- **`--smoke` flag passes BUT real run still fails** — Bug 6o demonstrated mxbai-large NaN under MNRL, smoke caught it; reranker class doesn't have the same issue (l12/mxbai/bge all train clean), but smoke must include a deliberate-stress row to keep regression coverage
- **Refactor sneaks in via "small cleanups"** — guard with grep: every `oneshot_*.py` edit reviewed against "is this duplication being preserved or extracted?"
