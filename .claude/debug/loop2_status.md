# Session2 Loop Status — 2026-04-27 14:35 EEST

> Auto-updated by team-lead. Tail this file for live status.

## Working tree (NOT pushed)
| file | md5 | status |
|---|---|---|
| src/search/fts.py | (modified) | FTS5 sanitize fix — strips `: , [ ] \` ' /` |
| src/search/hybrid.py | cb3b5912 | D1: line 540 `apply_penalties = True` always |
| profiles/pay-com/conventions.yaml | 0a6f6d67 | D1: gotchas/reference/dictionary boost = 1.0 |
| scripts/bench_routing_e2e.py | (modified) | W2: wire `expand_query()` before `hybrid_search` |
| profiles/pay-com/glossary.yaml | (modified) | W2: 67→119 entries (+54 payment-domain pairs) |
| .claude/debug/current/agent-d1-report.md | NEW | D1 revert values |
| .claude/debug/current/agent-w2-report.md | NEW | W2 details |

## Pytest state
- tests/test_fts.py: **21/21 green** (W2 didn't break)
- tests/test_hybrid.py::TestRerankPenalties: **6/52 fail** (D1 expected — tests assert OLD penalty bypass behavior). NOT fixed.

## Bench plan (sequential, single Python process at a time)

| # | candidate | eval | status | output JSON |
|---|---|---|---|---|
| 1 | FTS5-fix only (no W2/D1) | jira (n=908) | ✅ DONE — **+11.89pp h@10 POSITIVE** | `bench_runs/jira_e2e_fts5fix_session2.json` (h@10=0.5352) |
| - | FTS5-fix only | v2 (n=161) | ✅ DONE — NOISE [-1.86, +0.00] | `bench_runs/v2_e2e_fts5_only_session2.json` (h@10=0.6025) |
| - | FTS5+W2+D1 combined | v2 (n=161) | ❌ -26.68pp v2 REGRESSION | `bench_runs/v2_e2e_full_session2.json` (h@10=0.3416) |
| - | FTS5+W2 (D1 reverted) | v2 (n=161) | ❌ -19.83pp v2 REGRESSION → W2 main culprit | `bench_runs/v2_e2e_fts5_w2_session2.json` (h@10=0.4099) |
| **PUSHED** | **FTS5 fix → remote main** | — | **commit 2fc8c9b9** | `src/search/fts.py` |
| **DROPPED** | D1 (zero boosts) | — | regresses v2 | working tree reverted |
| **DROPPED** | W2 (glossary+bench parity) | — | regresses v2 (root cause: over-expansion + wrong synonyms; **needs intent-gate**) | bench script reverted; glossary stays untracked |
| W1 v2 (unified B/P normalization) | v2 (n=161) | ✅ DONE — hit@10 0.6149 (+0.61pp **NOISE**, no regression) | `bench_runs/v2_e2e_w1_session2.json` |
| W1 jira | jira (n=908) | ⏳ running | `bench_runs/jira_e2e_w1_session2.json` (queued) |
| 2 | FTS5+W2+D1 (everything in tree) | jira (n=908) | ⏸ queued after #1 | `bench_runs/jira_e2e_full_session2.json` |
| 3 | FTS5+W2+D1 | v2 docs (n=161) | ⏸ queued after #2 | `bench_runs/v2_e2e_full_session2.json` |
| 4 | If v2 regresses #3 → revert D1, re-bench v2 | v2 (n=161) | conditional | `bench_runs/v2_e2e_fts5_w2_session2.json` |
| 5 | If v2 regresses #4 → revert D1, re-bench jira | jira (n=908) | conditional | `bench_runs/jira_e2e_fts5_w2_session2.json` |
| 6 | Bootstrap CI all variants vs wide-OFF baseline | — | conditional | (computed in-memory) |

**Wallclock estimate**: bench #1 ~25 min remaining + bench #2 ~60 min + bench #3 ~10 min = ~95 min minimum. Conditional reverts add ~70 min if needed.

## Decision tree post-benches

- if `FTS5+W2+D1` jira hit@10 ≥ 55% AND v2 hit@10 ≥ 0.6087 (no regression) → push all 3 fixes
- if jira lifts but v2 regresses → revert D1, push FTS5+W2 only
- if FTS5+W2 alone gives jira ≥ 55% → push that, skip D1
- if jira < 50% even with all 3 → escalate to W1 (B/P unified normalization, ~50 LOC) or W4 (equiv-class index)

## Open backlog (not started this turn)
- W1: B/P unified normalization (~50 LOC hybrid.py refactor) — kills boost/penalty asymmetry CLASS, not just instance. Would obviate D1.
- W4: equivalence-class index column — synonym graph at index time. User's grep+synonym proposal #2.
- D2: re-index backoffice-web — addresses Дірка 3 (42% files not indexed), recall ceiling unlock.
- Wide-OFF revert proposal — original session2 candidate, suspended after debate redirected to root cause.

## Roadmap progress (toward jira hit@10 ≥ 60%)

| stage | target hit@10 | status |
|---|---|---|
| baseline (remote narrow OFF) | 42.3% | shipped |
| FTS5-fix alone | ~46-49% | bench in progress |
| + W2 (bench parity + glossary) | ~50-54% | bench queued |
| + D1 (boost zero + penalty force) | ~54-58% | bench queued |
| + W1 (B/P unified) | ~57-62% | not started |
| + W4 (equiv-class index) | ~58-64% | not started |
| + D2 (re-index backoffice-web) | +R@10 ceiling unlock | not started |
