# Phase 6 root-cause synthesis — 2026-04-25 ~05:10 EEST by team-lead

## Verdict: **REBUILD-EVAL-FIRST**, all Phase 5/5b results SUSPECT

3 critics конвергують на новій правді: eval-v2 — *structurally rigged for baseline*. Phase 5 + 5b results не можуть бути використані для deploy/reject decisions.

---

## Bombshell finding (eval-defender T2)

**90/100 queries have ≥1 expected_path в vanilla nomic-v1.5 top-10.** Threshold "rigged if >70%" — перевищено на 20pp.

**Root cause:** labeler script (`/tmp/label_v2_batch_*.py`) використовує `vec_pool(q, model_key="docs")` — **ту саму модель яку ми A/B'ємо.** Labels літералью drawn from baseline's retrievals.

**Implications:**
- Baseline отримує free 0.328 recall@10 floor (knows the answers)
- Candidates penalized для finding **better-but-different** docs
- All 4 "rejections" в Phase 5/5b — **invalid signal**
- v0/v1-fixed/v2-moe можуть бути actually better, але eval can't tell

---

## Per-candidate root cause (failure-analyst T1)

| Candidate | Recall | Δ | Real cause? |
|---|---|---|---|
| baseline (nomic-v1.5) | 0.328 | (ref) | reads its own homework |
| payfin-v0 | 0.199 | -0.129 | likely real overfit (10 pairs too few) + eval bias amplifies |
| payfin-v1-fixed | 0.257 | -0.071 | mix of real overfit + eval bias; recovers 34/63 v0 losses |
| nomic-v2-moe | 0.272 | -0.056 | mostly eval bias (model genuinely competent per Nomic blog) |
| gte-large | BLOCKED | — | upstream HF modeling.py NTK-rope bug, separate problem |

3 of 4 "losses" partly attributable to eval bias. Cannot say whether ANY of v0/v1/v2-moe would actually beat baseline on fair eval.

---

## Strategic decision (pivot-strategist T3)

T3 recommended Option (d) — **finalize + ship eval-v2 as gold + bank $10.69**. T3 made cost-vs-p(win) table assuming current eval is honest. But T2 just proved it isn't, so option (d) needs revision.

**Updated recommendation: REBUILD eval-v3 + RE-BENCH (no extra RunPod cost).**

---

## Phase 5c plan (next agent cycle, $0 spent)

**Phase 5c.1 — Build eval-v3 with model-agnostic labeler** (~45 min agent):

- Replace `vec_pool` with **multi-model rotation** OR drop entirely
- Best path: keep candidate pool = `fts5_top15 ⊕ path_overlap_top15 ⊕ glossary_match`. NO vector signal in label generation.
- For 14 expected_paths that came from FTS only / overlap only — keep
- For 261 expected_paths that touched nomic vec — re-evaluate via snippet judge over expanded FTS+overlap pool
- Add 5 hand-crafted "anti-baseline" queries — pull paths that nomic vec MISSES but a stronger semantic model would find (mine from `git log` recent docs)
- Output `profiles/pay-com/doc_intent_eval_v3.jsonl` (n=100)
- Push to private repo
- Smoke baseline on v3 — expect baseline recall@10 to DROP from 0.328 (no longer reading its homework) → maybe 0.15-0.20

**Phase 5c.2 — Re-bench all candidates on eval-v3** (~2-3h local, $0):

Build LanceDB indices locally for:
- docs (baseline, already indexed)
- docs-payfin-v0 (download + build)
- docs-payfin-v1-fixed (download + build)
- docs-nomic-v2-moe (download + build)

Skip gte-large (intrinsic bug, separate session).

Run benchmark on each on eval-v3. Compare with AND-gate.

**Phase 5c.3 — Re-judge** (~15 min):

If any candidate beats baseline by ≥+10pp on eval-v3 + ≥+5pp nDCG + no per-stratum drop > 15pp → **DEPLOY**.
If all candidates lose on v3 too → genuine ceiling, finalize baseline, document for next session.

---

## Updated stop conditions

- Phase 5/5b results — **invalidated, do not use** for decisions
- Iterations_no_improvement counter — **reset** (since Phase 5 was bad signal)
- Stop if Phase 5c also shows no winner (then we have HONEST signal of ceiling)

---

## Remaining budget

- $10.69 RunPod cap remaining
- Phase 5c is $0 (all local)
- After 5c, $10.69 still available for either: (a) Phase 6b — try one more FT with hard-negatives if 5c reveals signal direction; (b) bank for next session

---

## Open questions for autonomous loop next iteration

1. **Should v1 strata ALSO be relabeled?** failure-analyst flagged all 100 rows have `strata=[]` (data-pipeline bug, builder used `stratum` singular not `strata` plural). Phase 5c should fix this for per-stratum diagnosis.
2. **gte-large fix**: defer to next session. Document in NEXT_SESSION_PROMPT as known blocker (HF upstream modeling.py).
3. **Update memory `project_docs_model_research_2026_04_24.md`**: retract "nomic-v2-moe drop-in" claim — measured -6pp at minimum bias, real number unknown until v3.

---

## Final verdict sequence

1. Phase 6 → REBUILD-EVAL-FIRST (this verdict)
2. Phase 5c → eval-v3 + re-bench (next iteration)
3. Phase 6 again → analyze v3 results
4. Phase 7 → optional: hard-neg mining FT if v3 shows direction
5. Phase 8 → finalize: deploy winner OR ship baseline + reset roadmap
