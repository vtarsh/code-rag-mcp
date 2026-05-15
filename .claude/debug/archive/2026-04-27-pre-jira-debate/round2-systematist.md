# ROUND 2 — TEAMMATE S (Systematist)

**Lens unchanged:** invariants, contracts, illegal-states-unrepresentable. R1 thesis stands: ±9.3pp on n=100/80 is not an instrument; every R2 verdict gets read through that filter.

---

## Cross-read of P (Pragmatist)

**Translation (S-words):** P proposes a *negative invariant* — "no FT artifact promotes without proof" — implemented by freezing reranker work, hardcoding a per-stratum routing table from already-measured deltas, and refusing to build new orchestration abstractions until N≥3 cycles justify them. P's "evidence first" action is the contract-enforcement step I called for in R1, executed at $0.

- **Strongest:** "Recompute 95% CIs and per-stratum deltas on existing Run 1 artifacts ($0, 30 min)" — same gate I demanded in R1-A, with zero new spend. This is the cheapest path to a signed measurement contract.
- **Weakest (S-lens):** P's approach #2 (hardcoded route table from v3 strata) lacks a *staleness contract*. Eval-v2 calibrated already proved strata flip sign between label generations (`project_eval_v2_calibrated_2026_04_26.md`); a frozen route table is a constant pretending to be a measurement. No invariant says "route table must be re-derived when eval is re-labeled."

## Cross-read of R (Refactorist)

**Translation (S-words):** R identifies a *duplication-as-illegal-state* contract leak — `oneshot_rerank.py` and `oneshot_docs.py` are two unsynchronized copies of the same orchestrator, so every bug fix has 2× write-amplification and Run-N candidates each pay the missed-fix tax. R's evidence-first action is to collapse them under one `oneshot_pod.py` + per-recipe modules + a `--smoke` flag that makes "ship a candidate without pre-flight" unrepresentable.

- **Strongest:** `--smoke` flag enforced at the orchestrator level is *exactly* the "make Run-1's $4.50 burn impossible by construction" contract. Single highest-value invariant introduced this round on the engineering side.
- **Weakest (S-lens):** R's approach C (eval growth + seed re-runs) is the only one that addresses *measurement* contract — but it's ranked third behind two new training proposals (loss-weighted, distillation). Both A and B still ship a checkpoint judged on the same broken n=100/80 instrument; refactoring the orchestrator does not fix the eval contract leak.

---

## Updated ranked list (S, post-R2)

**KEEP** (from my R1):
1. **Eval-v4 stratified + per-stratum CI gate** — still #1. Both P and R independently land on "measurement before training"; this is the precondition both confirm.
2. **Joint-loss reranker with per-axis val floor** — still #2 *contingent on* eval-v4 existing first.
3. **Typed RouteDecision + audit log** — still #3; not regressed by either teammate.

**ADOPT:**
- From **P:** "Recompute CIs on existing Run 1 bench artifacts FIRST ($0)" — promote *above* my own eval-v4 build as the zeroth step. If existing deltas are already inside CI, eval-v4 design parameters change (we need bigger n, not just stratification).
- From **R:** `--smoke` flag + unified `oneshot_pod.py` as a *prerequisite* for Run 3 — encode as "no pod-spend cycle without smoke-pass" invariant. This is the per-cycle analog of my val-floor invariant.
- From **R:** `models/registry.yaml` as single source of truth — kills the "edit `src/models.py` twice" leak; aligns with my "make divergence unrepresentable" principle.

**DROP:**
- P's #1 (freeze 2 weeks) as a standalone — without the CI recompute it's premature; *with* the CI recompute it becomes a *consequence*, not a proposal.
- R's A (loss-weighted) and B (distillation) until eval-v4 + smoke gate exist — otherwise they reproduce the Run 1 failure mode at higher confidence theatre.

**Net delta vs R1:** ordering of evidence steps tightened (P's $0 recompute → my eval-v4 → R's smoke gate → any training). No invariant retracted.
