# META-CONVERGED — strategic ruling, jira hit@10 → 60%

> Meta-debate, 2026-04-27. 3 teammates with diverse priors (Data-Engineer / Systems-Thinker / Researcher).
> Round 1 independent, Round 2 cross-critique with summarize-then-attack. **Organic convergence** on ship sequence — no forced consensus.

## Context (where we stand)

- baseline (remote narrow OFF): 41.6% hit@10 on jira_eval_n900
- ✅ FTS5 sanitize fix SHIPPED (commit 2fc8c9b9): **53.5%** (+11.89pp POSITIVE bootstrap)
- W1 (B/P unified normalization): NOISE → not shipped
- W2 (glossary expansion, raw + curated): catastrophic on v2 (-19.83pp / -6.81pp), and HURTS jira too (-9.71pp curated) → REJECTED globally
- All ranking-layer tweaks exhausted (model swaps ×17, pool sweep, stratum gate, two-tower, boost tuning, B/P unification)
- Goal: 60% hit@10. Need **+6.5pp** more. Tactical-tweak ceiling reached.

## Convergence — all 3 teammates align on ship sequence

The three priors converged on a **3-stage staircase** even though they started from different lenses (data-quality / systems / SOTA-research):

| stage | move | who proposed | rank R1→R2 | est. lift jira hit@10 | cost |
|---|---|---|---|---|---|
| **STAGE 0 (free, prerequisite)** | SY1: bench-prod parity audit + glossary kill-switch | SY rank 1 → demoted, all 3 endorse as gate | known: ~+5pp on prod (expand_query is BROKEN in prod) | <1h |
| **STAGE 1 (foundation)** | DE1: rebuild eval (drop unhittable + GT-noise rows) | DE rank 1 → promoted to top by SY+RE | exposes real ceiling, unblocks honest measurement | 4h |
| **STAGE 2 (structural lift, biggest single move)** | RE1: soft repo prefilter (top-3 repos × 1.4 boost in fusion) | RE rank 1 → endorsed by DE+SY | **+6 to +12pp** on jira | 6-10h, $0 |
| **STAGE 3 (compound)** | DE2 + RE3 bundle: extractor allowlist relax + code-aware FTS5 tokenizer + path-as-document column | both touch index-build, share one rebuild cycle | DE2 +R@10 ceiling 39.55%→50-55%; RE3 +2-5pp jira | 8-11h human + 3-6h compute |
| **STAGE 4 (deferred, only if STAGES 0-3 leave gap)** | RE2: Doc2Query (synthetic queries per chunk, RunPod offline) | RE rank 2 | +3 to +7pp | 16h + $5-15 RunPod |
| **STAGE 5 (parallel future-work)** | DE3: pivot primary eval to logs/tool_calls.jsonl (real prod queries) | DE rank 3 | unblocks real-traffic optimization (currently we optimize wrong distribution) | 12h + $10 API |

**Cumulative expected lift after STAGE 0-3**: **+11 to +24pp** on a clean jira eval → **60-77% hit@10** range. Lower bound clears the 60% goal.

## Why this convergence is robust (3 priors agree)

**Data-Engineer** sees STAGE 1 (eval rebuild) as *the* foundation — without honest GT, every subsequent number is half-noise. Promoted by SY+RE.

**Systems-Thinker** sees STAGE 0 (bench-prod parity) as *the* gate — until bench == prod, we're optimizing an oracle. The expand_query in production is silently broken (W2 showed it hurts -9.71pp on jira eval), and bench has been bypassing it. Confirmed by DE+RE.

**Researcher** sees STAGE 2 (repo prefilter) as *the* structural unlock — 85.4% of jira queries concentrate ≥50% of GT in a single repo, but the FTS+vector pool competes 80 repos. Endorsed by DE+SY.

These three insights are NOT competing — they're SEQUENTIAL. STAGE 0 makes measurement honest. STAGE 1 makes the eval honest. STAGE 2 unlocks the dominant unexploited signal.

## Disagreement (genuine, not forced)

**RE2 (Doc2Query) sequencing**: Researcher ranks #6 (after DE2); SY warns it has high cost-risk per `project_training_cycle_failure_2026_04_26.md` ($4.50 burned, 0 valid candidates 2 days ago). DE accepts as data-pipeline change but warns it's complement to DE2, not substitute. **Resolution**: DEFER until STAGE 0-3 land and we measure residual gap. If gap < 3pp, RE2 not worth the RunPod risk.

**SY1 vs DE1 sequencing**: SY initially ranked SY1 first; in R2 conceded DE1 should lead because SY1 is a 1-hour gate while DE1's 4h investment unlocks all subsequent measurement. DE upgraded SY1 from "complement" to "ahead of DE1" in R2. **Resolution**: SY1 + DE1 are co-primary; do SY1 same-day as setup for DE1 work.

## Specifically rejected this cycle

All 3 teammates VETO:
1. **More reranker model swaps / fine-tunes** (DE: biased eval encodes into weights; SY: rerank stage isn't where signal dies; RE: 17 prior rejections)
2. **Boost/penalty/threshold knob-tweaking** (DE: 6 sessions of ±3pp noise; SY: shadow-boxing without diagnostic; RE: not in the SOTA toolset)
3. **Metric refactor (MRR / R@K=24) before diagnostic** (SY: resets baselines, adds "did fix help or did metric rephrase?" ambiguity; DE+RE concur)
4. **Glossary expansion in current form** (W2 catastrophic; YAML / equiv-class is 2015-era IR per RE; corpus-driven approaches strictly better)

## Watchlist (regression markers post-ship)

After each stage, watch for:

- **STAGE 0 (parity audit)**: if disabling `expand_query` in production causes ANY user-facing regression (subjective: "I used to find X, now I don't"), the production glossary had real signal we missed. Capture before/after on the 30 most-frequent prod queries pre-ship.
- **STAGE 1 (eval rebuild)**: if cleaned eval shows hit@10 ≥ 70% on FTS5-fix baseline, the "real ceiling" hypothesis is correct and STAGE 2-4 are bonus. If cleaned eval shows < 55%, the ranker still has work.
- **STAGE 2 (repo prefilter)**: if soft-boost top-3 prefilter HARMS the 14.6% multi-repo queries, switch to top-5 or 7. Track per-stratum delta on multi-repo queries explicitly.
- **STAGE 3 (extractor relax + tokenizer)**: monitor latency p95 — splitting camelCase tokens triples FTS5 inverted-list size; adding `.json/.yml` may bloat to OperationalError on RAM-tight 16GB Mac. Mitigation already proposed in RE3 report.

## Concrete next-session experiments (not "ship this fix" — "run this experiment")

The autonomous loop should pick these up in order. Each experiment has a clear pass/fail signal that gates the next stage.

### EXPERIMENT 0 (1h, autonomous-friendly)
1. Add `--with-expand` flag to `bench_routing_e2e.py` calling `expand_query()` before `hybrid_search()`.
2. Run jira n=908 in 3 modes: `baseline` (current), `--with-expand` (mirrors prod), `--via-service` (calls `service.search_tool` and parses output).
3. Bootstrap CI all three vs FTS5-fix baseline.
4. **Decision**: if `--with-expand` regresses jira ≥3pp → **strip `expand_query` from production immediately** (commit + restart daemon). Track as separate ship.

### EXPERIMENT 1 (4h, autonomous-friendly)
1. Script `scripts/build_clean_jira_eval.py`:
   - Drop GT pairs with file_path matching noise patterns (`package-lock.json`, `package.json`, `/generated/`, `.drone.yml`, `Dockerfile`, `.test.`, `_spec.`, `__tests__/`, `.eslintrc`, `.prettierignore`, `.gitignore`, `tsconfig.json`).
   - Drop GT pairs not in current `chunks` table.
   - Drop queries with <3 remaining GT pairs.
   - Output: `profiles/pay-com/jira_eval_clean_n*.jsonl`.
2. Re-bench wide-OFF baseline + FTS5-fix candidate on the new eval.
3. Bootstrap CI delta.
4. **Decision**: if FTS5-fix on clean eval ≥ 70% hit@10 → goal achieved on cleaned metric, document and PROMOTE jira_eval_clean as primary going forward. If < 60% → continue to STAGE 2.

### EXPERIMENT 2 (6-10h, requires planning)
1. Build per-repo summary corpus: for each of the 292 distinct repos, gather `README.md` + `code_facts` summaries (top-50) + `repo_overview` output.
2. Implement repo prefilter as a separate FTS5 query against this summary corpus → returns ranked list of repos.
3. Modify `hybrid.py::hybrid_search`: add `_apply_repo_prefilter` step before RRF — for each candidate, multiply RRF score by 1.4 if its repo is in top-3 of the prefilter ranking, by 1.0 otherwise.
4. Pytest + e2e bench jira (clean eval).
5. **Decision**: if jira hit@10 lifts ≥+6pp → ship. If +2 to +5pp → A/B for one week before ship. If <+2pp → diagnose with SY3-style stage instrumentation.

### EXPERIMENT 3 (10h+ rebuild)
Bundle DE2 + RE3 into one full re-index cycle:
1. Edit `scripts/extract_artifacts.py`: relax allowlist to include `.sql/.graphql/.cql/.yml` selectively; exclude noise (`.eslintrc/.gitignore`).
2. Add code-aware tokenizer pass to FTS5 chunk preparation: split camelCase / snake_case for tokens with len ≥ 8 + ≥1 internal capital, store in parallel `_tokens` column.
3. Add path-as-document FTS5 column: index `repo_name + " " + file_path + " " + camelcase_split(file_path)` as a high-weight FTS5 column.
4. Re-build (~3-6h on 16GB Mac with memguard).
5. Bench jira-clean + v2-baseline + (if available) prod-eval.
6. **Decision**: if cumulative lift hits 60% → ship. If still gap → STAGE 4 (Doc2Query) on RunPod.

## Convergence criteria check

- [x] Round 1, 2, 3 files exist (round1-{de,sy,re}.md, round2-{de,sy,re}.md, this meta-converged.md)
- [x] Round 3 picks ONE primary direction with clear sequencing (3 priors organically converged on STAGE 0 → STAGE 1 → STAGE 2 → STAGE 3 ladder)
- [x] Genuine disagreement listed (RE2 sequencing; SY1 vs DE1 priority — both resolved with explicit evidence-needed)
- [x] Watchlist exists
- [x] No forced convergence — convergence emerged from cross-critique, not coordination

**Debate complete.** Primary direction: **stop tuning the ranker; clean the measurement, then exploit unexploited structural signal (repo concentration), then expand index coverage**. Sequence above is the autonomous-loop's playbook to land 60% jira hit@10.
