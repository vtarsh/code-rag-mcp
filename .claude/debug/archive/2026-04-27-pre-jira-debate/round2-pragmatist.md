# Round 2 — Pragmatist

## Systematist position summarized (in pragmatist terms)
S wants to stop the bleeding by making the *measurement* trustworthy before spending another dollar — eval-v4 (n≥540, query-disjoint, stratified) becomes a hard merge gate where the lower-95%-CI must beat baseline on every stratum with n≥30. They want training-time guards (joint code+docs val splits with hard floors) and a typed router contract that refuses to ship a specialist until the regex router is calibrated ≥95%. The shared theme: *make bad outcomes literally impossible in the artifact pipeline*, not "caught later in review".
**Strongest point:** The P10 A2 reversal (heuristic +2.89pp → calibrated -2.09pp) is concrete proof that single-axis aggregate gates have *already* nearly shipped a regression — institutionalizing the per-stratum lower-CI gate kills that exact failure mode for $0 + ~$3 Opus labels.
**Weakest point:** Building eval-v4 + val-code/val-docs + typed-router contract is 3 contracts going live simultaneously; pragmatist instinct says ship the cheapest one (the gate) and earn the right to add the other two only when an actual candidate beats baseline.

## Refactorist position summarized
R sees `oneshot_rerank.py` and `oneshot_docs.py` as 80%-duplicated forks where every infra bug (6L, 6n, `.hf-token`) gets paid twice and proposes unifying them into `oneshot_pod.py` + thin recipe modules + a `--smoke` flag that does pre-flight on a 50-row subset locally or on a 5-min pod. Their top-1 evidence action is doing this refactor *first* so Run 3-5 each cost <50% of Run 2, and their hard "don't" is spawning a 4th `oneshot_*.py` for any new variant. Underneath: today's surface area is the actual blocker, not model choice.
**Strongest point:** `--smoke` per-stage on a 50-row train + 20-row eval subset is exactly the gate that would have caught the $4.50 Run 1 burn (`project_training_cycle_failure_2026_04_26.md`) — it's measurable infra ROI, not speculation.
**Weakest point:** A "unify two files into orchestrator + recipes" refactor is itself a new abstraction with its own bug surface; doing it *before* we even know if Run 3 happens violates pragmatist YAGNI — extract on the 3rd duplication, not the 2nd.

## Updated ranked list
**KEEP:**
- R1 #1 (ship baseline L6, freeze FT 2 weeks) — Run 2 evidence + S's CI math reinforce this; baseline is the only artifact that has survived 6 candidates.
- R1 #2 (stratum-gated routing extending P10 A2 pattern) — S's per-stratum gate proposal *strengthens* the data we'd route on; same surgical idea, no new model.
- R1 evidence-first action (recompute CIs on existing Run 1 artifacts, $0/30 min) — strictly cheaper than S's eval-v4 build and answers the same question for free.

**ADOPT:**
- From S: the **per-stratum lower-95%-CI gate as the merge contract** (not the full eval-v4 build yet). Encode `if lower_CI_per_stratum < baseline_R@10: REJECT` into the existing bench script; no new eval data needed for the rule itself, just for tightening CIs later. Cost: ~1 hr code, $0.
- From R: the **`--smoke` flag on existing `oneshot_*.py` files (no refactor)**. Add the 50-row pre-flight as an inline arg to *both* current scripts. We get the $4.50-burn protection without paying for the abstraction. If Run 3 actually happens, *then* extract.

**DROP:**
- R1 #3 (mxbai-base + class-balanced loss) — Negative's own caveat (direction-not-magnitude conflict) plus S's evidence that we have no code val split means we'd burn $0.80 + risk re-shipping the same un-detected code regression. Kill the FT track until the gate from S is in place.
