# Ruling: debate-arch-ft-closure

## Verdict

**WINNER: Position A — confidence MEDIUM.**

Topic: "Doc-tower FT/swap axis is exhausted on this corpus + eval; pivot to router/reranker (P8/P9) is the correct next-axis decision."

A wins on the *refined* claim made in Round 2 (axis closed for D1+D2; D3 deferred to P7 with preconditions, not P6). A wins NOT on the original Round 1 framing.

## Scoring summary

| Criterion | A | B |
|---|---|---|
| Evidence quality | strong (table + SE math + v12a 12-iter prior + MTEB headroom audit) | strong (same table decomposed by D1/D2/D3 + power calculation + family-wise α + bench JSON keys verified) |
| Logical consistency | wobbled in R1 (R1-stage2 used as load-bearing for D3); R2 explicitly conceded and refined | consistent across rounds |
| Counter-argument handling | R2 made TWO explicit factual concessions (R1≠R1-as-designed; trustly n=3); engaged reference-class critique by adding v12a; reframed "axis closure" as "axis deferral with preconditions" | identified A's strongest claim correctly; rebutted via distribution decomposition (3 distinct expected means); conceded r1-stage2 collapse but defended D3 |
| Fit to constraints ($11.42 / 5/13 / ±7.5pp SE) | argues sequencing: P6 closes, P7 builds preconditions (eval-v3 grow + CM4 + end-to-end), then D3 if budget allows | argues parallel spend: $3.50 R1-as-designed + $0 grow + $0 end-to-end now |

## Why A wins (medium, not high)

A's R2 refinement makes B's central catch (R1-stage2 ≠ R1-as-designed) **work for A's verdict** rather than against it. Restated: A claims "D1+D2 closed, D3 deferred to P7 with preconditions"; B claims "D3 untested, run R1-as-designed in P6 with $3.50." The disagreement collapses to **sequencing**, and on sequencing A's case is stronger:

1. **End-to-end harness is missing.** B verified `rerank_off=True` in the bench JSON and admits "axis exhausted" is a bi-encoder claim. But running R1-as-designed in P6 measures it on the same wrong layer B critiques. Sequencing harness BEFORE the next FT roll (A's R2 §3) is the Bayesian-correct order — both sides lose information from running D3 on the wrong gate.

2. **Eval-v3-n143 power for +5pp signal is weak per BOTH sides.** B's own ~18% power calculation argues against running D3 on n=143 — yet B proposes exactly that (n=200 grow listed as parallel, not prerequisite). A's "grow eval first" sequencing is more internally consistent with B's own power critique than B's plan is.

3. **v12a's 12-iter same-corpus prior** (introduced in A R2) is a stronger reference class than B's "D3 = n=0 → prior 0.5." 12 reranker FT iterations regressed; corpus-difficulty inheritance dampens D3's untested prior to ~0.10–0.15, not 0.30–0.40.

## Why MEDIUM, not HIGH

- A's R1 contained a real conflation (R1-stage2 = D3 evidence), which B caught cleanly. Confidence cannot be high when the affirmative had to retreat mid-debate.
- B's distribution-decomposition argument is mechanistically sound; A's rebuttal works only because of v12a's existence and the harness-sequencing point. Without v12a, A's case would be weaker.
- mxbai outcome grounding file (`/tmp/phase2_mxbai_outcome.txt`) was unavailable to me; I could not independently verify the -0.6pp delta or trustly -22.2pp violation that both sides cite. Both sides treat this as factual but I cannot re-check.
- B's correct point that the strategist's own §7 lists FT at p(win) 0.30–0.40 means A's "axis closure" verdict is genuinely conditional, not definitive. A's R2 owned this by reframing as deferral.

## Key risks for the winning side (Position A)

1. **v12a precedent over-extends**: 12 reranker FT iterations on this corpus is informative about reranker FT capability, but is a *different layer* than docs-tower FT. Treating it as in-class for D3's prior is a defensible-but-not-airtight inference.
2. **"Defer to P7" is a soft commitment**: if P7 preconditions (eval-v3-n200, CM4, end-to-end harness) drift past 1-2 sessions, the practical effect is "axis abandoned," not "axis deferred." Watch for backlog rot.
3. **Router c3's p(any positive lift)=0.78 is a cited number, not an independently verified one**. A's pivot-EV argument depends on it; if c3 lands at p(win)=0.3 instead, the relative attractiveness of D3 reopens.
4. **The +10pp AND-gate may itself be too strict**. B's family-wise α = 26% point is real and unaddressed by A. Keeping the gate at +10pp on n=143 means even genuinely useful +5–7pp recipes get rejected. A future rebuild of the gate (per-stratum quotas, lower headline threshold paired with significance) is implied debt.
5. **mxbai's -0.6pp delta is the closest-to-baseline measurement and the one most sensitive to power**. If a re-run with grown eval shifts mxbai to +1pp, "D2 closed" breaks. The current verdict treats the trustly n=3 violation as decisive but B is right that 1-sample swings can produce that.

## Reasoning (≤200 words)

Both positions argue the same factual record (6 rejections; eval n=143; SE ±6.9pp; r1-stage2 trained on the 10-pair set per r1_outcome.txt:80-81 verified). The disagreement is interpretive: A says the record is sufficient to close the axis for the P6 timeline; B says three of the six are vanilla-base-swap (different mechanism), three are tiny-data FT (different distribution), and the well-resourced D3 distribution is unmeasured. B's distribution decomposition is correct in principle and forced A to concede in Round 2 that R1-stage2 is not a D3 draw. However, A's Round 2 refinement absorbs the concession by reframing the claim as "axis deferred to P7 with preconditions (eval-v3-n200, CM4 query-disjointness, end-to-end reranker harness), not axis closed forever." Under this refined framing, A's sequencing argument is stronger than B's parallel-spend proposal: B's own ~18% power calculation and own `rerank_off=True` verification both argue for building preconditions BEFORE running D3, not in parallel with it. v12a's 12-iter same-corpus precedent (introduced by A R2) further dampens D3's untested prior below B's 0.30–0.40 estimate. Confidence medium because A's R1 conflation was real and the verdict hinges on the R2 reframing.
