# Round 2 — Refactorist (R)

**Lens unchanged:** the second/third change must cost <50% of this one. Every "one-off" tool we ship today is a duplication tax tomorrow.

---

## On Pragmatist (P)

**Translation (refactorist words):** P proposes we *stop investing* — freeze L6, decline new infra, decline a new orchestrator. The compounding bet is on **deletion** (no Run 3) and on **reuse of existing artifacts** (recompute CIs from `bench_runs/run1_*.json` for $0). Pattern that repeats: we keep paying training $ to confirm noise; pattern that compounds: bootstrap CI on artifacts we already paid for.

- **Strongest:** the $0 CI recomputation is the highest-leverage single action in the entire debate — it tells us whether *any* downstream investment (mine, S's, P's #3) has signal. It also reuses a tool that already exists (per `project_eval_v2_calibrated_2026_04_26.md`). Pure compounding.
- **Weakest (refactorist lens):** P's #3 ("class-balanced loss, then kill if it loses") *requires* a new oneshot script flag OR a 4th `oneshot_*.py` to run — and P explicitly forbids extracting the common code. So P's plan ships a duplicate fork to test a hypothesis P expects to fail. That is exactly the duplication-while-rejecting-abstraction trap. The "inline now, extract on Run 3" rule fails when Runs 1 + 2 + the proposed loss-weighted run = 3 forks already.

---

## On Systematist (S)

**Translation (refactorist words):** S proposes we **lift contracts up the call stack** — eval-v4 with per-stratum CI gate, val splits made disjoint by construction, router output as a typed enum with calibration gate. Every invariant is "encode the failure mode into a type/gate so it can't recur." Pattern that compounds: one labeled split, one gate function, one enum — reused by Run N+1, Run N+2, and any future axis (e.g. multilingual) for free.

- **Strongest:** the `min(val_docs_R@10, val_code_R@10)` early-stop hook is the smallest-possible refactor that prevents the *exact* Run 1 mxbai disaster (-4.7pp code, no in-loop signal). One ~20-LOC callback in the trainer, paid once, blocks negative transfer in every future cycle. That is textbook compounding investment.
- **Weakest (refactorist lens):** S's eval-v4 (n≥540 stratified, per-stratum CI gate, ~$3 Opus labeling) treats the eval *as the contract*, not the *eval-builder* as the contract. We've already rebuilt eval three times (v1 heuristic → v2 calibrated → v3 model-agnostic). Each rebuild required hand-rolled scripts (`build_doc_intent_eval_v3.py` etc.). S ships eval-v4 without extracting `EvalBuilder(strata, n_per, judge, disjoint_from)` — so eval-v5 will cost the same as eval-v4. The contract is in the wrong place: it's on the artifact, not the factory.

---

## Updated ranked list

**KEEP** (from R1):
1. **Unified `oneshot_pod.py` + `--smoke` flag + `bench_runs/` auto-cp** — still the single thing that makes every other approach (mine, P's #3, S's joint-loss) cost <50%. R1 verdict holds.

**ADOPT** (from R2 cross-read):
2. **P's $0 CI recomputation FIRST** — promote above my own refactor. If every Run 1 delta is inside ±9.3pp, the refactor's payback horizon collapses; we'd be optimizing a process we shouldn't run. Cheapest possible falsifier of the entire debate.
3. **S's `min(val_code, val_docs)` early-stop callback** — fold into the unified `oneshot_pod.py` as a default trainer hook. ~20 LOC, blocks negative transfer forever, zero new ops surface.

**DROP** (from R2):
4. **S's eval-v4 as a one-off artifact** — adopt only if wrapped in a reusable `EvalBuilder` (factory, not artifact). Otherwise it's the 4th hand-rolled eval script and we pay this cost again next cycle.
5. **P's #3 loss-weighted run before refactor** — sequence-dependent: do the refactor first, then this run costs $0.05 smoke + $0.80 real instead of another $0.69 mystery burn.

## ONE thing we should DEFINITELY not do (unchanged from R1)

Ship a 4th `oneshot_*.py`. The duplication becomes irreversible at fork count 3.
