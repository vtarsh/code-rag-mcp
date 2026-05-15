# Round 1 — Refactorist (R)

**Lens:** the second/third change must cost <50% of this one. Today's surface area is the bug.

## What I see in the repo (refactorist diagnostic)

`oneshot_rerank.py` (366 LOC) and `oneshot_docs.py` (374 LOC) are 80%+ identical: `_log`, `_ssh`, `_scp_to`, `_scp_back`, `_tar_overlay`, the Bug-6L `.hf-token` ritual, the HF push command, the provision sequence, the `try/finally stop_pod` are all **copy-pasted byte-for-byte**. The deltas are: train script name, `--max-seq-length` (docs only), `--rerank-on --rerank-model-path` vs `--model={tag}`, an extra `build_docs_vectors` step. That is **two divergent forks of one orchestrator** — every bug fix (6L, 6n, the `.hf-token` write) has to be applied twice. By Run 5 this becomes 4–6 forks.

Other duplications today: bench JSON cp-from-/tmp ritual (manual), no smoke-per-stage (root cause of the cycle's $4.50 burn — see `project_training_cycle_failure_2026_04_26.md`), NaN-guard logic split between `docs_vector_indexer.py` and any future loss-weighted rerank trainer.

## 1. Top 3 approaches IF Run 2 B+C don't dominate

**A. Loss-weighted single reranker (5x code) on `mxbai-rerank-base`.** Negative-team's untested alt; same model artifact, same router, same eval — *zero* new ops surface. **Fail mode:** if conflict is in update *direction* not magnitude (per ruling), weighting just amplifies docs gradient and code stays flat.

**B. Pairwise-distillation reranker — train mxbai-base to mimic baseline-L6 logits on code queries + ground-truth on docs queries.** Reuses the existing 20695-row train file with no resplit; preserves baseline behavior on the axis we're regressing on. **Fail mode:** student-teacher gap on code may be too small for distillation to add signal beyond just keeping baseline-L6 (i.e. degenerates into "use L6").

**C. Eval growth + frozen-recipe re-run with deterministic seeds — NOT new training.** If R@10 deltas live inside ±9.3pp CI (ruling §1), more candidates is noise. Grow `code_intent_eval_v1` 80→200, re-run Run 1 winners with seed=0/1/2 on identical recipe; compute mean±std. **Fail mode:** labeling cost (claude-judge or manual) on 120 new code queries can eat $20–50 in API or ~6 hr if manual.

## 2. Top 1 evidence-gathering action FIRST

**Refactor `oneshot_rerank.py` + `oneshot_docs.py` into `oneshot_pod.py` + 2 thin recipe modules (`recipe_rerank.py`, `recipe_docs.py`), then add `--smoke` flag that runs the train+bench loop on 50-row train + 20-row eval subsets locally OR on a 5-min pod.** Cost: 2-3 hrs local. Saves: every Run-2-onward candidate gets pre-flight validated for $0.05 instead of $0.69/hr × failure-discovery time. This is the lesson `project_training_cycle_failure_2026_04_26.md` already paid $4.50 to learn — banking it now means Run 3-5 each cost <50%.

Bundle with: `bench_runs/` auto-cp from `/tmp` in the `finally` block (kills the manual ritual flagged in anti-patterns), and a single `models/registry.yaml` that both orchestrators read so adding a candidate stops requiring a `src/models.py` edit.

## 3. ONE thing we should DEFINITELY not do

**Do not spawn a 4th oneshot_*.py for the loss-weighted variant or the distillation variant.** That is the moment the duplication becomes irreversible. If A or B above need a new flag, it goes into the unified `oneshot_pod.py` with a `--loss-weighting` / `--distill-from=L6` arg. Three forks of the same orchestrator is the single highest-leverage way to guarantee Run 3 costs >50% of Run 2.
