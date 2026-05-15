# `.claude/debug/` — what lives where

Updated 2026-04-25 after the autonomous A/B loop converged. Contents grouped by current relevance.

## Active artifacts (read these first in any new session)

| File | Purpose |
|---|---|
| `final-report.md` | One-page TL;DR of the loop (outcome, A/B table, process gains, recommendations). Start here. |
| `loop-log.md` | Full 18-iteration journal of the autonomous A/B loop (Phases 1–8). |
| `loop-state.json` | Machine-readable final state: phase, candidates_tested dict, budget, commits, handoff fields. |
| `p6-verdict.md` | Phase 6 root-cause synthesis — why eval-v2 was rigged → eval-v3 mandate. |
| `eval-methodology-verdict.md` | Pre-loop labeling-methodology consensus (3 critics). Foundation for eval-v3 design. |
| `eval_v3_bias_report.json` | Quantified bias delta (eval-v2 vs eval-v3). |
| `next-session-prompt.md` | **Copy-paste prompt for the next session.** Starts agent debate on recipe-improvement + gte-large unblock. |

## Phase-6 input artifacts (kept; feed `p6-verdict.md`)

| File | Role in Phase 6 |
|---|---|
| `p6-eval-defender.md` | Detected eval-v2 was 90% rigged (labeler used `vec_pool` of baseline). |
| `p6-failure-analyst.md` | Per-candidate root-cause attribution. |
| `p6-pivot-strategist.md` | Strategic options (a..d), recommended option (d) before T2 invalidated it. |

## Subdirectories

- `labeled_batches/` — 10 × per-batch label JSONLs from Phase 2b (v2_batch_01..10.jsonl). Source for `doc_intent_eval_v2.jsonl` (now superseded by v3).
- `archive/` — superseded debate artifacts (see below). Kept for history; nothing references them in active flow.

## `archive/` contents (superseded, do not read for current state)

- Stage-C debate (pre-loop docs-tower 89% stall investigation, 2026-04-24): `verdict-stagec.md`, `verdict-stagec-v2.md`, `verify-stagec.md`, `regressions-stagec.md`, `e2e-stagec.md`, `hypotheses-stagec.md`, `investigator-stagec.md`. All findings consolidated into `final-report.md` and the memguard fix already shipped (commits `cf5a852..e429776`).
- Pre-loop debate set (2026-04-24, debug-89-stall + eval-critic round): `hypotheses.md`, `hypotheses.solved.md`, `verdict.md`, `eval-critic.md`, `independent.md`, `metric-critic.md`, `significance-critic.md`. Outputs absorbed into `eval-methodology-verdict.md` and the eval-v3 design.

## Reading order for a fresh session

1. `final-report.md` (5 min)
2. `next-session-prompt.md` (3 min, then copy-paste into new session)
3. Skim `loop-log.md` last 200 lines for context (5 min)
4. Optional deep-dive: `p6-verdict.md` if curious why eval-v2 failed
