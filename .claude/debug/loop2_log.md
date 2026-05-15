# Loop 2 — Karpathy autonomous research (e2e ONLY) — 2026-04-27

## Mandate
- Every hypothesis verified end-to-end via `scripts/bench_routing_e2e.py` (real `hybrid_search()`).
- NEVER use cached vector→rerank benches for routing/stratum hypotheses (Tick 55 lesson).
- Push to remote main only after e2e POSITIVE bootstrap CI on v2 + jira.

## State at start
- hybrid.py md5: `c2e1b2a7bcd7c849ac4f33069e8d45d7` (baseline pre-loop1)
- pytest 1023/1023 (verified previous session)
- Local main HEAD: `9c0a263` (3 latest commits visible)
- Remote main HEAD: `92f8c989` (Tick 0 push from loop1 — completes routing wiring)
- **Divergence**: 25 ahead / 61 behind origin/main — investigate before any push
- Daemon: DOWN (intentionally — bench loads its own models in-process)
- Disk free: ~92 GB

## Stop conditions
- 8h wallclock OR
- Disk free <10 GB OR
- 3 consecutive bench errors

---

## Tick 0 (2026-04-27 — session2 start) — Calibrate e2e baseline

**Hypothesis**: current LOCAL hybrid.py (md5 c2e1b2a7) e2e baseline on v2 calibrated docs eval (n=161) matches loop1 reported baseline `hit@10=0.6087`. If yes → safe to bench candidate variations against this number. If no → investigate before any further work.

**Action**: run `scripts/bench_routing_e2e.py --eval=profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl --out=bench_runs/v2_e2e_baseline_session2.json --label=session2_baseline` in background. ETA ~5-10 min.

**Result**: PASS — exact match to loop1 baseline.

| Metric | loop1 | session2 Tick 0 | Δ |
|---|---|---|---|
| hit@5 | 0.4783 | 0.4783 | 0.0pp |
| hit@10 | 0.6087 | 0.6087 | 0.0pp |
| R@10 | 0.2716 | 0.2716 | 0.0pp |
| nDCG@10 | 0.3417 | 0.3417 | 0.0pp |
| n | 161 | 161 | match |
| p95 latency | n/a | 5062ms | — |

**Verdict**: baseline calibrated. Safe to bench candidate variants vs this number.

**Audit finding**: local hybrid.py (md5 c2e1b2a7) differs from remote main (origin/main = 92f8c99) in OFF/KEEP set composition only — routing intent line is IDENTICAL. Specifically:
- LOCAL OFF = {webhook, trustly, method, payout} (4)
- REMOTE OFF = {webhook, trustly} (2) — narrowing landed in `c3f7aab fix(rerank): narrow A2 OFF set to webhook+trustly only`

So the "Tick 0 push fate" question = which OFF set wins e2e? Routing wiring itself is no-op (both sides have it).

---

## Tick 1 (2026-04-27 12:30 EEST) — e2e bench remote narrow OFF on v2

**Hypothesis**: narrowing OFF from {webhook,trustly,method,payout} → {webhook,trustly} (= remote main) helps OR hurts e2e on v2 calibrated. If hit@10 > 0.6087 → keep remote (no need to push wide-OFF revert). If hit@10 < 0.6087 → suggests local wide-OFF is better, propose patching remote.

**Action**: `git checkout origin/main -- src/search/hybrid.py` (md5 d9c857e0) → run e2e bench → revert local. Output: `bench_runs/v2_e2e_narrow_off_session2.json`. ETA ~8 min.

**Result**: REGRESSION — narrow OFF -6.83pp hit@10 vs wide OFF baseline.

| Metric | LOCAL wide OFF (Tick 0) | REMOTE narrow OFF (Tick 1) | Δ |
|---|---|---|---|
| hit@5 | 0.4783 | 0.4037 | **-7.46pp** |
| hit@10 | 0.6087 | 0.5404 | **-6.83pp** |
| R@10 | 0.2716 | 0.2324 | **-3.92pp** |
| nDCG@10 | 0.3417 | 0.2752 | **-6.65pp** |

**Per-stratum diff** (n in v2 eval, wide vs narrow OFF):

| stratum | n | wide h@10 | narrow h@10 | Δ | mech |
|---|---|---|---|---|---|
| method | 16 | 0.75 | 0.25 | **-50.00pp** | OFF→KEEP, rerank pushes correct files out of top-10 |
| interac | 9 | 0.78 | 0.56 | -22.22pp | order shift: queries hit interac (KEEP) instead of method (OFF) |
| provider | 20 | 0.50 | 0.40 | -10.00pp | KEEP in both; ranker dynamics shift |
| aircash | 8 | 0.50 | 0.625 | +12.50pp | mild gain (only 8 queries) |
| webhook,nuvei,trustly,refund,payout,tail | n/a | unchanged | unchanged | 0 | OFF preserved or rerank result unchanged |

**Verdict**: NARROW OFF (= remote main) is materially **WORSE** than LOCAL wide OFF on v2 calibrated docs eval. Implies `c3f7aab` (narrow A2 OFF) was a bad change — and remote main (incl. Tick 0 push 92f8c99 which inherits it) is regressed -6.83pp on doc-intent traffic vs local.

Local hybrid.py restored to baseline c2e1b2a7 ✅.

**Caveat**: v2 docs eval might not represent prod traffic distribution (50% 'tail' stratum per loop1 prod analysis). Need jira (n=908) confirmation before recommending push.

---

## Tick 2 (2026-04-27 12:38 EEST) — jira (n=908) bench narrow OFF

**Hypothesis**: if jira also regresses with narrow OFF (or stays flat), recommend pushing wide-OFF revert to remote main. If jira improves with narrow OFF → trade-off needs weighted analysis.

**Action**: bench narrow OFF (remote hybrid.py) on jira_eval_n900 (n=908). ETA ~60 min. Output: `bench_runs/jira_e2e_narrow_off_session2.json`.

**Result**: jira narrow OFF hit@10 = 0.4229. Need wide OFF jira for comparison → ran Tick 2b.

---

## Tick 2b (2026-04-27 13:38 EEST) — jira (n=908) bench wide OFF (apples-to-apples)

**Action**: restore local hybrid.py → bench jira → compare. Output: `bench_runs/jira_e2e_wide_off_session2.json`.

**Result + bootstrap CI (n=10000 paired)**:

### v2 calibrated (n=161) — narrow OFF vs wide OFF
| metric | Δ (narrow-wide) | 95% CI | verdict |
|---|---|---|---|
| hit@5 | -7.48pp | [-12.42, -3.11] | **NEGATIVE** |
| hit@10 | -6.85pp | [-11.80, -1.86] | **NEGATIVE** |
| R@10 | -3.93pp | [-6.98, -0.82] | **NEGATIVE** |
| ndcg@10 | -6.68pp | [-12.10, -2.22] | **NEGATIVE** |

### jira (n=908) — narrow OFF vs wide OFF
| metric | Δ (narrow-wide) | 95% CI | verdict |
|---|---|---|---|
| hit@5 | +0.77pp | [+0.22, +1.43] | **POSITIVE** |
| hit@10 | +0.66pp | [+0.22, +1.21] | **POSITIVE** |
| R@10 | +0.18pp | [+0.02, +0.44] | **POSITIVE** |
| ndcg@10 | +0.41pp | [+0.09, +0.78] | **POSITIVE** |

### Weighted prod (47% docs / 53% jira per loop1 prod analysis)
| metric | weighted Δ |
|---|---|
| hit@10 | **-2.87pp** (wide wins) |
| hit@5 | **-3.11pp** (wide wins) |
| R@10 | -1.75pp |
| ndcg@10 | -2.92pp |

**VERDICT**: REMOTE main (narrow OFF inherited from `c3f7aab`) is materially WORSE than LOCAL on weighted prod traffic. 

**Recommendation**: PROPOSE pushing wide-OFF revert to remote — **BUT** trade-off needs human review (jira loses +0.66pp if we push). Two paths:
- **Path A (full revert)**: push local hybrid.py wide OFF → recover -2.87pp weighted, accept jira -0.66pp
- **Path B (intermediate)**: keep only method in OFF (the -50pp loser), leave payout in KEEP — try to recover most of v2 loss without the small jira win

Going to bench Path B next for a more informed proposal.

---

## Tick 3 (2026-04-27 14:35 EEST) — method-only-OFF (intermediate)

**Hypothesis**: of method+payout, only method drives the v2 -50pp loss. payout shows 0pp delta on v2. Adding ONLY method back to OFF should recover most of v2 loss without losing the jira gain (since payout queries are responsible for most of the jira gain — 6 queries flipped GAIN_n on jira).

**Config**: OFF={webhook, trustly, method}, KEEP={nuvei, aircash, refund, interac, provider, payout}, ORDER=[trustly, webhook, method, nuvei, aircash, refund, interac, provider, payout]. Apply via Edit, bench, revert.

**Result**: ESSENTIALLY EQUIVALENT to wide on hit@10 (NOISE on most metrics, R@10 NEGATIVE).

| Metric | LOCAL wide (Tick 0) | method-only (Tick 3a) | Δ vs wide | bootstrap |
|---|---|---|---|---|
| hit@5 | 0.4783 | 0.4720 | -0.61pp | NOISE [-2.48, +1.24] |
| hit@10 | 0.6087 | 0.6087 | 0.00pp | NOISE [-1.86, +1.86] |
| R@10 | 0.2716 | 0.2571 | -1.45pp | **NEGATIVE** [-3.15, -0.23] |
| nDCG@10 | 0.3417 | 0.3217 | -1.99pp | NOISE [-5.87, +0.54] |

**Per-stratum v2 (3-way comparison wide / narrow / method-only):**

| stratum | n | wide | narrow | method-only | mech vs wide |
|---|---|---|---|---|---|
| method | 16 | 0.75 | 0.25 | 0.75 | recovered ✅ |
| provider | 20 | 0.50 | 0.40 | 0.50 | recovered ✅ |
| interac | 9 | 0.78 | 0.56 | 0.67 | partial recovery (-11pp) |
| aircash | 8 | 0.50 | 0.625 | 0.625 | narrow's gain captured |
| webhook,nuvei,payout,refund,trustly,tail | n/a | unchanged across all | 0 |

**jira (n=908, Tick 3b)**: hit@10=0.4174 (~tied wide 0.4163, doesn't capture narrow's +0.66pp gain).

**Bootstrap method-only vs current REMOTE narrow** (= push viability check):
- v2: hit@10/5 +6.85/+6.86pp **POSITIVE**, R@10 NOISE [-0.24, +5.09], nDCG +4.69pp **POSITIVE**
- jira: ALL **NEGATIVE** [-1.10 to -0.03] (loses the narrow gain)

**Verdict on Path B (method-only)**: weaker than Path A (full wide) — method-only has NEGATIVE R@10 vs wide on v2 AND doesn't preserve narrow's jira gain. Worst of both worlds.

**Path A (full wide OFF revert) is the recommended push.**

---

## TICK 3 OUTCOME — final session2 recommendation

**Push candidate**: LOCAL hybrid.py wide OFF + tests/test_rerank_skip.py wide-OFF assertions. Both already in local main HEAD.

**Bootstrap CI vs current prod (remote narrow OFF)**:
- v2 (n=161): all 4 metrics POSITIVE [+3.11pp to +7.48pp]
- jira (n=908): all 4 metrics NEGATIVE [-0.77pp to -0.18pp] (small)
- Weighted (47/53 prod): **+2.86pp hit@10 net win**

**Risk**: jira NEGATIVE is real and stat-significant. Mitigation: weighted analysis dominates; v2 magnitude (+6.85pp) >> jira magnitude (-0.66pp).

**pre-push state**:
- pytest local: 58/58 green (test_hybrid + test_hybrid_doc_intent + test_rerank_skip)
- pytest local extended: 118/118 green (incl router_whitelist)
- hybrid.py md5 c2e1b2a7 verified
- test_rerank_skip.py md5 0f4a5728 verified
- File sizes: hybrid 42.4KB / 1016 lines, test 14.8KB / 327 lines

**Push command** (awaiting user "yes"):
```
mcp__github__push_files(
  owner="vtarsh",
  repo="code-rag-mcp",
  branch="main",
  files=[
    {path: "src/search/hybrid.py", content: <local file content>},
    {path: "tests/test_rerank_skip.py", content: <local file content>},
  ],
  message="revert(rerank): restore wide A2 OFF set (webhook/trustly/method/payout)\n\nReverts c3f7aab. E2E validation through real hybrid_search() shows wide OFF wins +6.85pp hit@10 on v2 calibrated docs (POSITIVE), loses -0.66pp on jira (NEGATIVE). Weighted prod (47/53) = +2.86pp net.\n\nFull rationale + bench artifacts in .claude/debug/wide_off_revert_proposal.md."
)
```

Post-push: get_file_contents + md5 verify both files.

Proposal artifact: `.claude/debug/wide_off_revert_proposal.md`.

---

## Tick 4 (2026-04-27 13:50 EEST) — IR2 FTS5 sanitize fix LANDED

**Action**: extended `_sanitize_fts_input` in `src/search/fts.py:60` to strip `: , [ ] \` '` and replace `/` with space. Verified: 28.4% → 0.2% OperationalError on jira eval. pytest 956/956 (excluding runpod_lifecycle). e2e bench in progress: `bench_runs/jira_e2e_fts5fix_session2.json`. At 200/908 running h@10=0.5050 (vs wide-OFF baseline 0.4550 in same point = +5pp lift on running average).

## Tick 5 (2026-04-27 14:15 EEST) — Planning debate verdict (debate-jira-strategy)

3-agent debate (Pragmatist/Systematist/Refactorist) converged. Primary winner W1 (two-stage retrieval contract, downscoped form). User's grep+synonym proposal accepted as W2 (glossary YAML extension) + W4 (equivalence-class index column), rejected as framework form. Disagreement D1 (P-A2 zero boosts + drop penalty gate) requires A/B evidence on v2 + jira.

Full ruling: `.claude/debug/current/converged.md`.

## Tick 6 (2026-04-27 14:18 EEST) — DELEGATION (autonomous)

User authorized parallel delegation. Two implementation agents spawned:

**Agent A — W2**: bench harness `expand_query()` parity + glossary YAML growth (~50 entries from jira miss tokens). Files: `scripts/bench_routing_e2e.py`, `profiles/pay-com/glossary.yaml`. Does NOT touch hybrid.py.

**Agent B — D1 A/B prep**: apply P-A2 changes locally (force `apply_penalties=True` always at hybrid.py:540 + zero out `gotchas_boost / reference_boost / dictionary_boost` in `profiles/pay-com/conventions.yaml`). Run pytest. Does NOT bench (lead runs benches sequentially after current jira FTS5-fix bench finishes).

**Sequencing**:
1. Current jira FTS5-fix bench finishes (~30 min remaining as of 14:20)
2. Agents A+B finish edits
3. Lead bootstrap CI on FTS5-fix vs baseline
4. Lead applies D1 changes (Agent B's diff), runs v2 + jira bench
5. Lead reverts D1, applies W2 changes (Agent A's diff), runs jira bench
6. Compare all variants via bootstrap CI
7. Pick best combo, propose to user before push

**Open at this point**: W1 (B/P unified normalization — ~50 LOC hybrid.py refactor), W4 (equivalence-class index column — schema migration). Both heavier; defer to next session unless quick.
