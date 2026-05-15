# EXP1 — Verdict (clean jira eval)

**Date:** 2026-04-28
**Stage:** STAGE 1 (DE1) of meta-converged ladder
**Owner:** EXP1 (eval rebuilder)

---

## Headline

**hit@10 on clean eval (FTS5-fix candidate) = 63.97%** (n=619).

This **clears the 60% goal** on the cleaned metric.

→ Per meta-converged.md decision rule (`if cleaned hit@10 between 60-70%`):
> **ship as primary metric, schedule STAGE 2 as compounding lift**

---

## Drop ratios (eval cleaning)

- queries dropped (GT < 3 after cleaning) = **289 / 908 (31.83%)**
- final n_queries = **619**
- GT pairs dropped = **13,001 / 22,459 = 57.89%**
  - mechanical PR-noise = 5,506 (24.52% of original)
  - unhittable (not in chunks) = 7,495 (33.37% of original)
- mean GT/query: 24.73 → 14.83
- top repos by GT-pair count: backoffice-web (3,333), graphql (1,021), express-api-v1 (434), hosted-fields (297), grpc-payment-gateway (236)

This matches DE1's pre-flight estimate (25.4% noise + 33% unhittable) almost exactly.

---

## Bench results (n=619, paired)

| metric | rescored baseline (wide-OFF, n=908 → re-evaluated on clean GT) | FTS5-fix (clean eval) | Δ | 95% CI | verdict |
|---|---|---|---|---|---|
| **hit@5**    | 0.4475 | 0.5541 | **+0.1064** | [+0.0808, +0.1341] | POSITIVE |
| **hit@10**   | 0.5153 | **0.6397** | **+0.1242** | [+0.0953, +0.1535] | POSITIVE |
| **R@10**     | 0.1153 | 0.1549 | **+0.0394** | [+0.0293, +0.0503] | POSITIVE |
| **nDCG@10**  | 0.1825 | 0.2382 | **+0.0556** | [+0.0421, +0.0699] | POSITIVE |

Bootstrap: 10,000 resamples, paired by query, 619/619 common.

p95 latency = 5,563 ms (no degradation expected — same pipeline as wide-OFF, only the eval changed).

---

## Cross-check vs raw n=908 narratives

| eval | wide-OFF baseline | FTS5-fix |
|---|---|---|
| `jira_eval_n900.jsonl` (raw, noisy) | 41.63% hit@10 | 53.5% (per session2 narrative) |
| `jira_eval_clean.jsonl` (cleaned) | **51.53% hit@10** (rescored from baseline top_files) | **63.97%** |

The 9.9 pp uplift on the baseline alone confirms the noise/unhittable thesis: 1/4 of every "miss" on the noisy eval was a measurement artefact, not a retrieval failure. Once the artefact is removed, the FTS5-fix lands at 63.97% — within the 60-70% band that meta-converged predicted as "real ceiling, STAGE 2 still useful as compounding".

---

## Decision (per meta-converged STAGE 1 rule)

> if cleaned hit@10 ≥ 70% → STAGE 2 optional (bonus)
> if cleaned hit@10 < 60% → continue to STAGE 2 with adjusted expectations
> **if cleaned hit@10 between 60-70% → ship as primary metric, schedule STAGE 2 as compounding**

**Outcome: 63.97% → SHIP `jira_eval_clean.jsonl` as primary going forward, schedule STAGE 2 (RE1 repo prefilter) as compounding lift.**

Recommended follow-on:
1. Promote `profiles/pay-com/jira_eval_clean.jsonl` to canonical primary jira eval (n=619).
2. Keep `jira_eval_n900.jsonl` as frozen historical regression eval (do not delete; track BOTH per DE1 mitigation note).
3. Proceed to STAGE 2 (RE1 soft repo prefilter, +6 to +12pp expected on clean eval). Decision gate: ship if Δ ≥+6pp paired bootstrap POSITIVE on clean eval.

---

## Artifacts

- `scripts/build_clean_jira_eval.py` (new, ≤180 LOC, committable)
- `scripts/rescore_against_clean.py` (new, helper for fair baseline comparison)
- `profiles/pay-com/jira_eval_clean.jsonl` (n=619)
- `bench_runs/jira_clean_fts5fix_session2.json` (FTS5-fix candidate on clean eval)
- `bench_runs/jira_clean_baseline_rescored.json` (wide-OFF rescored on clean GT)
- `.claude/debug/current/exp1-clean-eval-stats.md` (counts + repo distribution)
- `.claude/debug/current/exp1-bootstrap.log` (bootstrap CI table)
- `.claude/debug/current/exp1-bench.log` (bench progress trace)

No production code modified (constraint respected). Nothing pushed remote (constraint respected). Only `bench_routing_e2e.py` ran (constraint respected).
