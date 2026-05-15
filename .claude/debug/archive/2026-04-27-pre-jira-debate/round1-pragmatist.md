# Round 1 — Pragmatist

Run 1 evidence: baseline L6 ties or beats every FT candidate on at least one axis. n=100/80 means ±9.3pp CI per Negative debate — most "wins" are noise. Stop spending money to chase ghosts inside the CI band; spend $0 on evidence first, then minimal $ on the smallest viable change.

## Top 3 approaches (if Run 2 B+C don't dominate)

### 1. Ship baseline L6 as-is, freeze reranker work for 2 weeks
Run 1 + flat-leaning Run 2 already prove the prod L6 is a strong floor; no FT in 6 attempts has cleanly beaten it on both axes. Stop the bleed: declare reranker quality plateaued at current eval resolution, redirect $5–10/cycle to retrieval (vector index, hybrid weighting) where deltas have historically been larger (see `profiles/pay-com/RECALL-TRACKER.md`).
**Failure mode:** A future business need (a new doc strata flagged by ops) makes "freeze" untenable; we have to thaw with stale recipe muscle.

### 2. Stratum-gated routing (extend the P10 A2 pattern)
We already proved per-stratum on/off gating works for reranker-skip (P10 A2 verification: webhook +3pp, refund +4pp, payout +3pp on calibrated v2). Same surgical idea: run baseline L6 on code-intent + on stable strata, route only `{webhook, trustly, payout, method}` to mxbai-FT — hardcode the route table from measured per-stratum deltas, no ML required.
**Failure mode:** Eval-v2 calibrated finding (`project_eval_v2_calibrated_2026_04_26.md`) showed strata flip sign between v1-heuristic and v2-calibrated labels — the gate table built on v3 may not transfer when eval grows.

### 3. mxbai-base + class-balanced loss (Run 2 option A from ruling, NOT yet run)
Negative's loss-weighting alternative was ranked but never executed; it's the cheapest single-model fix (~$0.80) and avoids the 2x-ops penalty of split-models. If it doesn't beat baseline on both axes within one cycle, kill the FT track entirely and go to approach 1.
**Failure mode:** Update-direction conflict (not magnitude) is the real issue — weighting won't help and we burn another $0.80 confirming Negative's caveat.

## Top 1 evidence-gathering action FIRST
**Recompute 95% CIs and per-stratum deltas on existing Run 1 bench artifacts.** Cost: $0, ~30 min Mac. Reuse `bench_runs/run1_*.json` + bootstrap resampling (already exists in eval scripts per `project_eval_v2_calibrated_2026_04_26.md`). Tells us: (a) which Run 1 deltas are real vs noise, (b) which strata each candidate genuinely wins/loses, (c) whether Run 2 B+C even has measurable signal at n=100. If every delta sits inside ±9.3pp, we have our answer for free: stop training, grow eval, OR ship baseline.

## ONE definitely-don't-do
**Do not build a new "training framework" / "candidate orchestrator" / "sweep manager" abstraction.** The current ad-hoc `oneshot_rerank.py` + `oneshot_docs.py` flow shipped 6 candidates in Run 1 with 11 caught bugs — it works. Building infra for "Run 3-Run N" assumes we'll have many more cycles; the evidence (4 sessions, 0 deployed wins) suggests we should be questioning whether to *do* Run 3, not making it pretty. Inline the next 1–2 recipes; if a 3rd cycle is genuinely needed, *then* extract the common 5 lines.
