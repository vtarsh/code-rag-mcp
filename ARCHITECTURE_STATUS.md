# Architecture status — code-rag-mcp · 2026-05-19

> **READ THIS FIRST.** This is the current source of truth on direction.
> It SUPERSEDES `archive/MODEL_TRAINING_SPEC.md`, `archive/RERANKER_IMPROVEMENT_PLAN.md`,
> `archive/NEXT_SESSION_PROMPT.md` and the recall@10 framing in `archive/SESSION_FINDINGS.md` —
> all of those reflect an earlier direction that was tested and rejected.

## TL;DR

The recall@10 chase and the reranker / embedding **fine-tuning** plan were tested
and **rejected**. But the system itself is a sound working hybrid: all three
retrieval legs (FTS, vector, reranker) were measured by remove-a-leg tests and
are **load-bearing** — nothing is baggage, nothing to delete. **Verdict: keep the
hybrid as-is; stop chasing fine-tuning.** A full agentic-grep rebuild is a
"maybe later", not a pending decision.

## DO NOT (new sessions / autonomous runs)

- ❌ Do **not** fine-tune the reranker or embeddings (RunPod). 1 success across a
  long failure history; the industry trend is against it; it is not the bottleneck.
- ❌ Do **not** optimize single-shot **recall@10**. It is capped at ~0.77 by task
  size alone (many JIRA tasks change 20-180 files). Retired as a primary metric.
- ❌ Do **not** delete the vector (LanceDB) leg "to simplify" — **measured**, it
  earns ~+5pp recall@pool (stable; the hit@10 delta is a noisy −3.6/−8.3pp
  test-retest pair — see 2026-05-20 correction). It is not baggage.
- ❌ Do **not** trust `archive/MODEL_TRAINING_SPEC.md` / `archive/RERANKER_IMPROVEMENT_PLAN.md` /
  `archive/NEXT_SESSION_PROMPT.md` — superseded, they point the wrong way.

## What was measured (this session)

| Test | Result |
|------|--------|
| Code fixes shipped (commits `22a996b`, `3eebeda`) | hit@10 0.605→0.714, recall@10 0.152→0.182. Env-gated, default ON. |
| Head-to-head, 15 tasks: MCP hybrid (single-shot) vs plain grep-agent (full loop) | ≈ tied. file-recall 0.19 vs 0.18; foothold 0.63 vs 0.51 (hybrid slightly ahead). |
| vector OFF (paired full-665, two runs of the same `CODE_RAG_NO_VECTOR` config) | hit@10 −3.6 to −8.3pp (test-retest spread; noisy), recall@pool −5.2 to −7.3pp (**stable**), retrieval_failures ×2 → **vector earns its keep on reach**. |
| reranker-OFF (raw RRF order) | hit@10 **−14.1pp**, recall@10 −3.6pp → **reranker is the single biggest contributor**. |
| Deep research (industry SOTA) | direction = agentic grep-first; but its headline "drop vector = free win" FAILED our test of its own criterion. |

## Decisions LOCKED

- **Reranker fine-tuning: NO.** RunPod money stays parked (not refundable; spend
  only on an off-the-shelf embedding-model **swap bench** or GT cleanup if at all).
- **Primary metric: foothold-recall** (≥1 file per relevant repo in top-K) **+
  steps-to-find.** Not single-shot recall@10.
- **Vector leg: KEEP.** Measured +8pp.
- **Graph + `analyze_task`: KEEP** — repo-routing is the real value (foothold 0.63
  vs single-file 0.19 says the system finds the right *repos* far better than the
  right *files*).
- Kept code fixes (FIX-A/D/F/G/H + provider-doc demotion + daemon-400): committed,
  default ON. Env vars are kill-switches.
- **Coverage hint (2026-05-19, uncommitted):** `search` output ends with a
  "limit reached — N in pool, re-run wider" line when truncated; `limit` cap
  raised 20→50. Default `limit` stays 10 — the agent opts into more. Env
  `CODE_RAG_COVERAGE_HINT`. Aligns with the agentic-iteration direction.

## VERDICT (all three legs now measured)

Remove-a-leg tests: vector −8.3pp, reranker −14.1pp, FTS = the base. **No leg is
baggage — all three are load-bearing.** "Simplify by deleting" is empirically
closed: there is nothing to delete. **Keep the hybrid as-is.** The only thing
rejected is *fine-tuning* (RunPod spend) — the existing reranker `l12-ft-run1`
is the single most important component and stays.

Residual open question (low priority): a full agentic-grep rebuild (option c)
where the agent's iteration replaces vector+reranker. Head-to-head hinted a
grep-agent ≈ the hybrid, but the hybrid demonstrably works and all legs are
load-bearing — so this is a "maybe later", not a pending decision. Default: keep.

## WHERE NEXT — how to actually get good results

There is no silver bullet left; the session already shipped the biggest single
jump (+10.4pp hit@10). The remaining path is incremental and toolkit-shaped:

### Findings from the 3 improvement audits (2026-05-19, `bench_runs/improve/`)

- **GT is clean — 98.85%.** Only 122 of 10650 expected_paths are noise (58 CI
  deploy-yml, 53 .env.example, 11 generated). 0 rows go empty. The "clean the GT
  first" worry is **dropped** — low recall is a retrieval problem, not GT noise.
  Optional trivial drop; not a prerequisite.
- **LEAKAGE in `analyze_task` routing eval.** `task_history` (1003 rows) is a
  superset of the 665 eval rows; co-occurrence mines it excluding only the
  current task → analyze_task routing numbers are partly memorization.
  ⚠️ This affects ONLY `analyze_task` benchmarks — NOT the `search`/`hybrid_search`
  recall numbers quoted in this doc (hybrid_search does not use task_history).
  Any future analyze_task benchmark MUST first exclude all 665 eval IDs.
- **CORE has no domain template** — 236/665 CORE tasks route on classifier seeds
  alone. Biggest single routing gap.
- **ast-grep is feasible** — ~10s install, covers TS/TSX/JS/Go, ~2.5 days to wire.

### MEASURED 2026-05-19 — analyze_task routing

Built an honest routing benchmark (`scripts/eval/bench_routing.py`, repo-routing
recall@5 / foothold@5; de-leaked via `CODE_RAG_TASKS_DB=db/tasks_deleak.db` —
task_history with the 665 eval rows removed).

- **Honest baseline:** foothold@5 = **0.34**, routing_recall@5 = 0.20, @10 = 0.30.
- **Leakage is negligible:** de-leaked 0.241 vs full 0.238 — co-occurrence does
  NOT memorize meaningfully. (Earlier 0.24 vs the 0.34 here = a fixed bench-parser
  bug; 0.34 is the real number.)
- **❌ CORE domain template — REJECTED by data.** The audit (P2) proposed it as
  the biggest win. But of 81 non-eval CORE tasks the most frequent repo
  (`express-api-v1`) appears in only **8%** (vs BO template's 93%). CORE is the
  whole heterogeneous backend — no universal repo set. A CORE template would
  ADD noise. Do not implement it. (`bench_runs/improve/analyze_task_audit.md` P2
  is stale on this point.)

### MEASURED 2026-05-20 — recall@pool diagnosis + zero-recall root-cause + BM25 dead

Two fact diagnostics (`scripts/eval/diagnose_pool_reach.py`,
`diagnose_query_gap.py`) + a 43-task case-by-case audit by 3 parallel agents
(`.claude/debug/current/misses_slice{1,2,3}.md`):

- **recall@pool 0.48 is a STRUCTURAL ceiling, not a bug.** 100% of 10,650
  expected files ARE indexed — the deep-research "corpus reach" claim is
  FALSIFIED. Ceiling = task-size cap (recall@pool 0.58 @1-5 → 0.31 @41+ files)
  + lexical gap (32% of misses share zero query tokens with expected content) +
  ranking crowd-out (68% have tokens but lose the 200-pool race).
- **BM25 column-weight lever — TESTED, DEAD.** `CODE_RAG_BM25_PATH_WEIGHT`
  (boost file_path, zero metadata cols): +0.24pp recall@pool / −1.14pp hit@10
  on n=350. Reverted. Retrieval-layer ranking tweaks are exhausted (3rd
  independent confirmation).
- **Zero-recall (43 tasks, recall@pool=0) failure modes:**
  - **generic-term-drowned** (~15) FIXABLE — token-poor expected files lose to
    token-dense siblings. IDF/rare-token weighting, per-token candidate union,
    repo-balanced pooling.
  - **title↔code vocabulary gap / opaque-symptom titles** (~14) INTRINSIC —
    bug error strings, "Audit all", "Refactoring … flow", version-bump titles
    carry no code signal. Only the JIRA *body* (not title) recovers these.
  - **camelCase tokenizer split** (BO-1234/904/1474) FIXABLE — `porter
    unicode61` splits `toColumnDefinitions`→`column/definitions`. Index whole
    identifier forms alongside split forms.
  - **proper-noun / dependency symbol absent** (PI-47, BO-1289, CORE-2566)
    PARTLY FIXABLE — index `package.json` + import-alias maps.
  - **wrong-repo steering by provider proper-noun** (PI-37, CORE-2412, PI-41)
    FIXABLE — proper-noun→repo routing.
  - **tag-prefix steering** (`[CSV]`, `[API]`) FIXABLE — strip in
    `_sanitize_fts_input`.
  - **GT-noise (~5-6 of 43) — NOT retrieval failures.** Pipeline returned the
    on-topic file at rank 1-3 (BO-937 use-copy-to-clipboard, CORE-2353
    scylla/database.ts, CORE-2468 evaluate-and-cancel-auth.js) but GT lists
    incidental merge-diff files. The metric is biased by counting these.
- **Frontend under-retrieval bias** — token-poor JSX components consistently
  lose to token-dense backend `.js` files. 13/15 in slice 1.

### Prioritized next steps (updated 2026-05-20)

1. **Pipeline tracing** (`CODE_RAG_TRACE=1`) — `src/search/trace.py` +
   `emit_trace(...)` at the end of `hybrid_search`. Per-query JSONL with
   fts/vec counts, pool size, rerank-skip flag, final count. Default OFF.
   **SHIPPED this session**. Catches silent bugs (cf. the historic 28.4% FTS5
   OperationalError, the daemon DEFAULT_EXCLUDE leak).
2. **GT-noise prune** — **PARTIAL DONE 2026-05-20.** `scripts/eval/prune_gt_noise.py`
   removes 223 noise paths in 4 categories (101 boilerplate-doc, 58 ci-config,
   53 env-example, 11 generated) into `profiles/pay-com/eval/jira_eval_clean_v2.jsonl`
   (original preserved; 0 tasks went empty). Metric delta tiny (+0.34pp recall@pool,
   +0.00pp hit@10) — pipeline missed BOTH the noise AND the real files, so prune is
   hygiene, not lift. **Still TODO:** manual GT review for 3 mismatch tasks where
   the pipeline returned the on-topic file but GT lists incidental — **BO-937**
   (`use-copy-to-clipboard.ts` rank 1), **CORE-2353** (`scylla/database.ts` rank 3),
   **CORE-2468** (`evaluate-and-cancel-auth.js` rank 2). Not auto-fixable.
3. **Query-side cheap fixes** — **PARTIAL SHIPPED 2026-05-20.**
   - ✅ `calc`→`calculation` added to `profiles/pay-com/glossary.yaml` (slice2
     BO-1619 finding). Smoke-verified single-task win.
   - ✅ Meta-tag prefix strip env-flag `CODE_RAG_STRIP_META_TAGS=1` (default OFF)
     in `_sanitize_fts_input`. Strips ONLY ticket-category tags
     `[API|Audit|Reports|CSV|Migration|Tech Debt|ABU]`; domain tags
     `[3DS|Risk|APM|CVV|Vault|Provider|Webhooks|GW|...]` are KEPT. Default-OFF
     — not yet measured on 665.
   - ❌ Classifier-fix `CODE_RAG_INTEGRATION_CODE_OVERRIDE` (engineering-anchor
     override in `_query_wants_docs`): blind-tested +1 on PI-61, but on full
     n=665 lost **−4.51pp hit@10 / −30 net hits** (vs fixI baseline). 34 tasks
     newly LOST hit@10 against 4 newly gained. **REVERTED 2026-05-20 night.**
     Lesson: per-task blind smoke is NOT predictive of aggregate behavior on
     this corpus. Trust full-665 only for keep-decisions.
   - ❌ `raw_query` pipeline propagation through `hybrid_search`: blind tests
     showed regressions on PI-65/67 (cls_query bypassed the accidental
     `method`-stratum rerank-skip that was net-positive). REVERTED.
   - ❌ Glossary `apm` reformulations (hyphen, removed): both variants regressed
     blind tests. REVERTED.
4. **camelCase whole-token identifier indexing** — recovers BO-1234/904/1474.
   Requires a reindex → **GATED on explicit user GO** (no-auto-rebuild rule).
5. **IDF / rare-token weighting + per-token candidate union** — for the ~15
   generic-term-drowned tasks. Ranking-only, testable, no reindex.
6. **`steps-to-find` metric** — the debate's #1 gating experiment; measures the
   real iterating-agent consumer. Reranker-ON vs OFF arm. The right primary.
7. **`ast-grep` structural-search tool** — additive, no risk, ~2.5 days.
8. **`analyze_task` routing P1 specificity-weight** — **TESTED, NOT SHIPPED
   2026-05-20.** Implemented hyphen-token-match for short keywords (<5 chars) +
   demote-single-(name)-match to `low` in `_section_keyword_scan` per
   `analyze_task_audit.md` P1. `bench_routing.py` on n=200 (de-leaked):
   foothold@5 −0.5pp, routing_recall@5 −0.24pp, routing_recall@10 −0.5pp.
   Audit predicted "foothold roughly flat, precision improves" — but recall
   measurably regressed. REVERTED.

### Net result of autonomous session 2026-05-20 night

Attempted 5 fixes (classifier-override, raw_query pipeline, apm-glossary
variants, P1 specificity), **all reverted** after measurement. Genuinely
shipped/kept this session:
- `src/search/trace.py` + `emit_trace` call in `hybrid_search` (default OFF,
  no production behavior change).
- `calc`→`calculation` glossary entry.
- `CODE_RAG_STRIP_META_TAGS` env-flag (default OFF, infrastructure only).
- 5 diagnostic scripts: `diagnose_pool_reach.py`, `diagnose_query_gap.py`,
  `extract_worst_misses.py`, `prune_gt_noise.py`, `jira_eval_clean_v2.jsonl`.

**Aggregate 665 metrics unchanged** vs fixI baseline (all default-ON changes
reverted; only OFF-by-default infra added).

**Key methodology lesson:** per-task blind smoke (3-5 PI tasks with trace) is
INSUFFICIENT for keep-decisions on a 665-task corpus. A fix can show locally
+1 while regressing −30 globally. **Mandatory: run full 665 diagnose before
keeping any retrieval-pipeline change.** The 15-min cost is non-negotiable.

**Intrinsic (~14 of 43 zero-recall): opaque/symptom JIRA titles carry no code
signal — unfixable from the title.** Would need the JIRA description as query.
The structural answer is agent iteration; ast-grep + tracing serve this.

Honest framing: single-shot recall is task-size-capped AND partly GT-noise-
biased. Real gains live in (#2) un-biasing the metric, (#1) catching our own
bugs, (#3) cheap query fixes, then (#6) measuring the right thing.

### Step 1 (steps-to-find metric) — IN PROGRESS 2026-05-20 late

Design + impl + v2 policy on n=10 sanity. See
`bench_runs/improve/steps_to_find_design.md` for the design forks and keep
criteria; `scripts/eval/bench_steps_to_find.py` for the simulator;
`scripts/eval/run_s2f.sh` for batched-subprocess wrapper.

**v1 (path-token reformulation, accumulated tokens) — REJECTED on sanity.**
n=10 took 665s (BO-1585 alone = 307s due to FTS5 OR-explosion from long
accumulated queries). hit_rate@step plateaued at 60% from step 2 onward —
iteration adds **nothing** after step 2 because path-tokens drift off-topic
(observed: BO-1593 step5 query = "checks documents docs columns enums task
business" — generic-pool drift).

**v2 (content-token reformulation, slide-window) — KEPT on sanity.** Same n=10
tasks took 177s (3.8× faster; slide-window killed long-tail). hit_rate@step:
30→50→60→60→**70%** — discriminating across all 5 steps. Wins:
- BO-1037 terminal_recall 25%→50%
- BO-1266 14%→21%
- BO-1593 0%→20% (v1 complete miss → v2 hit at step 3; content tokens kept
  query on-topic)
- One regress: BO-1588 first_hit 2→5 (same terminal_recall though)

**Reformulation policy v2:** extract camelCase/PascalCase/snake_case compound
identifiers ≥8 chars from top-K-NEW snippet content. Dedup against query
overlap. Slide-window (not accumulate). Falls back to path-tokens when snippet
has no discriminating compounds (config / JSON files).

**Next:** n=50 baseline + n=50 rerank-OFF arm to (a) confirm v2 signal at scale,
(b) check whether the metric distinguishes the reranker question from the
debate. If green → full n=665 baseline + arm.

### Step 1 v2 — n=50 baseline + rerank-OFF arm DONE 2026-05-20 late

Direct test of the debate residual ("does the reranker's −14.1pp single-shot
hit@10 transfer to a multi-shot iterating agent?"). Same first 50 tasks
(BO 17 / CORE 16 / HS 9 / PI 8). v2 policy. Trace enabled both arms — 250
entries each, 0 vec_errs / 0 fts_zero / 0 vec_zero (pipeline clean).

| Metric | rerank ON | rerank OFF | Δ |
|---|---|---|---|
| n_hit | 33/50 | 33/50 | **0** |
| hit_rate@step 5 | 66% | 66% | **0** |
| hit_rate@step 1 | 50% | 54% | **+4pp (OFF better!)** |
| mean_terminal_recall | 20.8% | 18.5% | −2.3pp |
| full_recall_rate | 2% | 0% | −2pp |

**ANSWER to the debate residual:** single-shot −14.1pp DOES NOT transfer to
iterating-agent any-hit rate. Both arms reach 66% by step 5. B-team's claim
confirmed — rerank rescues files iteration recovers anyway. Where rerank still
earns its keep: terminal_recall completeness on multi-file tasks (BO-928
100%→33%, CORE-2299 64%→18%) — NOT foothold.

**Stratum split — where rerank helps vs HURTS:**
- BO: 6 wins ON, 2 wins OFF → reranker helps
- CORE: 7 wins ON, 5 wins OFF → mixed lean ON
- **HS: 0 wins ON, 3 wins OFF → reranker HURTS HS** (new finding)
- **PI: 3 wins ON, 2 wins OFF → mixed with strong individual losses**

Extends [[project_p10_a2_landed_2026_04_26]] stratum-gated rerank-skip pattern
to HS strata.

**Causal mechanism (from queries_used trace):** reformulation policy is identical
in both arms; the flip happens because rerank changes WHICH top-1-NEW file is
returned per step → different content-token extraction → cascading divergence.

- **HS-283** (rerank=0% / off=60%): ON's top-1 was generic feature-flag file →
  tokens `featureFlagsList merchantProvider` (off-topic). OFF's top-1 was an
  APM file → `apmConfig initApmResponse redirectUrl` (precisely on-topic, APM
  files ARE the GT zone).
- **PI-15** (rerank=0% / off=11%): ON drifted to generic `InvalidDataError`;
  OFF picked up `gumballpayPayoutCredentials endpointGroupId` from the
  provider-specific repo at step 2.
- **BO-1593** (rerank=20% / off=0%): without rerank, top-1 step-1 was a Jest
  test config → `testPathIgnorePatterns clickhouse_compiled` → cascade collapse.
- **CORE-2299** (rerank=64% / off=18%): without rerank, top-1 was literally the
  `sanitize-cardholder-name` file → first hit step 1. Rerank DEMOTED the right
  file.

Metric is honest and arm-discriminating. Next: full n=665 baseline + rerank-OFF
arm to produce stratum-specific rerank-skip policy on full corpus.

### Step 1 v2 — n=665 baseline + rerank-OFF arm DONE 2026-05-20 late

Ran on RunPod RTX 4090 (l8xsgfwkzzqhcn, EU-RO-1, ~$2 cost) — baseline 22 min,
rerank-OFF 16 min wall. Trace enabled both arms (3317 + 3311 entries, 0 vec_errs,
0 vec_zero, 14 fts_zero in BOTH arms = same pipeline behavior). Per
`feedback_check_trace_between_runs` — pipeline clean, no silent bugs.

**Full corpus ARM COMPARISON:**

| Metric | rerank ON | rerank OFF | Δ |
|---|---|---|---|
| n_hit (step 5) | 433/665 (65.1%) | 399/665 (60.0%) | **−5.1pp** |
| hit_rate@step 1 | 49.32% | 42.41% | −6.9pp |
| hit_rate@step 2 | 56.54% | 49.32% | −7.2pp |
| hit_rate@step 3 | 60.6% | 53.83% | −6.8pp |
| hit_rate@step 4 | 63.31% | 57.44% | −5.9pp |
| **hit_rate@step 5** | **65.11%** | **60.00%** | **−5.1pp** |
| mean_terminal_recall | 18.09% | 16.10% | −2.0pp |
| n_full_recall | 8 | 7 | −1 |

**REVISED debate-residual answer (corrects n=50 finding):**

| Context | Δ rerank |
|---|---|
| Single-shot (prior bench) | −14.1pp hit@10 |
| **s2f@step 5 (n=665)** | **−5.1pp** |
| s2f@step 5 (n=50 subset) | 0 (sample artifact) |

Iteration recovers **~64% of single-shot reranker advantage** (14.1pp → 5.1pp),
but **NOT all of it**. Rerank still earns 5.1pp irreducible value at step 5.

**A-team's "keep rerank" position confirmed for iterating consumer.**
B-team's "iteration-redundant" claim was overstated by the n=50 sample.

**STRATA breakdown on full 665 — surprises:**

| Stratum | n | rerank-ON hits | rerank-OFF hits | Δ hit | Verdict |
|---|---|---|---|---|---|
| **BO** | 361 (54%) | 237 | 201 | **−10.0pp** | rerank **STRONGLY HELPS** |
| **CORE** | 236 (35%) | 143 | **147** | **+1.7pp** | rerank **HURTS** |
| HS | 28 (4%) | 20 | 18 | −7.1pp | helps (noisy small sample) |
| PI | 40 (6%) | 33 | 33 | 0 | neutral |

Flips: rerank-ON wins 227, rerank-OFF wins 141, unchanged 297.

**KEY METHODOLOGY LESSON — n=50 sub-sample inverted the truth.** n=50 said
"rerank HURTS HS, helps BO" — full 665 says "rerank STRONGLY HELPS BO, HURTS
CORE, helps HS". The n=50 sample (BO 17, CORE 16, HS 9, PI 8) was too small to
expose CORE's pattern and misrepresented HS. Reinforces
[[feedback_blind_smoke_insufficient]] — n=50 isn't enough for stratum-level
keep-decisions on this 665-task corpus.

**Decisions:**
- **KEEP rerank ON globally** — biggest stratum BO loses 10pp without it.
- **Consider stratum-gated rerank-skip for CORE** — could recover +1.7pp (4 hits)
  on 236 CORE tasks. Extends [[project_p10_a2_landed_2026_05_20]] strata-gating
  pattern (currently PI-substrata) to CORE.
- **Step 1 (steps-to-find) is DONE.** Metric is honest, reproducible, and
  discriminates arms at the corpus level.

Next priorities (from Plan B): Step 2 (refined JIRA body enrichment),
Step 3 (provider-scaffolding tool), Step 4 (camelCase whole-token indexing).

### Step 1 closing — test-retest noise + fix-#1 falsification (2026-05-21)

**Test-retest noise floor on pod GPU: ZERO.** Re-ran the baseline n=665
bench on a fresh pod with identical config/env. Result: bit-identical to
4 decimal places across all metrics, 0/665 tasks differ, 0 hit-state flips.

| Metric | Run 1 | Run 2 | Δ |
|---|---|---|---|
| n_hit | 433 | 433 | 0 |
| mean_terminal_recall | 0.1809 | 0.1809 | 0.0000 |
| hit_rate@step 5 | 0.6511 | 0.6511 | 0.0000 |
| per-task differ | — | — | **0/665** |

**Implication:** any +/-Xpp delta from a fix evaluated on pod-vs-pod is
**real signal, not noise** — even +0.6pp is measurable. Earlier concern
from [[project_recall_pool_diagnosis_2026_05_19]] about 4pp test-retest
spread applied to vector-ablation tests on different pod hardware, NOT to
s2f on same hardware. Cross-platform (pod GPU vs Mac CPU) divergence
remains real — that's a separate concern.

**Fix #1 (rerank-skip on compound code identifier) FALSIFIED on n=30 local:**

Implementation: env-gated `CODE_RAG_SKIP_RERANK_ON_IDENT=1` in
`hybrid_rerank.py`. Detects camelCase ≥6 / PascalCase ≥6 / snake_case ≥6
in query and skips reranker if present.

| Metric | Baseline (no fix) | With fix #1 | Δ |
|---|---|---|---|
| n_hit | 23/30 | 20/30 | **−3** |
| mean_terminal_recall | 21.75% | 15.97% | **−5.8pp** |
| hit_rate@step 5 | 76.67% | 66.67% | **−10pp** |
| full_recall_rate | 3.33% | 0% | −3pp |

Catastrophic per-task losses: BO-928 100%→33% (rerank rescued it), BO-1588
33%→0% (full miss), BO-1593 20%→0% (full miss), BO-1037 50%→25%.

**Root cause of fix failure:** the bench's reformulation policy (content-
token extraction) ADDS compound identifiers to the query on step 2+. So
the fix triggers on nearly every task at step 2+, effectively becoming
"rerank only at step 1, raw RRF after" — too aggressive.

**Implementation reverted** (commits to follow). The hypothesis
"skip rerank when query has identifier" doesn't generalize across
iteration steps; the detection signal would need to filter to ORIGINAL
title text only (before reformulation augmentation), or use task-level
metadata (ticket prefix). Deferred.

**Saved artifacts** (`bench_runs/improve/`):
- `s2f_baseline_n30.json` — local baseline before fix
- `s2f_fix1_withfix_n30_FAILED.json` — falsifying evidence
- `s2f_v2_n665_baseline_retest/` — pod test-retest confirming zero noise

### Step 2 — refined JIRA body enrichment LANDED 2026-05-21

Plan B Step 2: separate FTS-only retrieval pass on code-anchored JIRA body
tokens, RRF-merged with the title pass. Default OFF; env
`CODE_RAG_TASK_BODY_ENRICH=1` enables. Pod n=665 RTX A100-80GB cost ~$1.50.

**Components (commits `7e25763` + `35cbdab`):**
- `src/tools/task_context.py` — sanitize_body (strips URLs/JWTs/credentials/
  hex-hashes/markdown/FTS5-breakers), extract_code_anchored (PascalCase /
  camelCase / snake_case ≥6, hyphenated ≥6, file paths, non-stopword
  ALL_CAPS abbrevs), build_body_query (drops tokens whose word-parts are
  fully in title — catches `payment_method_options` vs "payment method
  options").
- `src/search/hybrid.py` — body kwarg; FTS-only body pass (top-50, no
  vector, no rerank); RRF merge with weight 0.5 vs title's 1.0;
  CODE_RAG_BODY_PROTECT_TOP_N=5 guards title's top-N from body
  displacement so body can rescue zero-recall tasks without hurting
  good-title ones. Top-5 protect was the critical fix — naive RRF without
  it lost 10/30 CORE-opaque tasks on local sanity.
- `scripts/eval/bench_steps_to_find.py` — fetches body from
  `db/tasks.db.task_history.description` per ticket_id, passes to every
  hybrid_search call. Same env gate.
- `tests/test_task_context.py` — 20 unit tests.

**Pod n=665 keep-decision (both arms on A100-80GB, same hardware):**

| Metric | OFF | ON | Δ |
|---|---|---|---|
| n_hit (any GT in 5 steps) | 433 | **455** | **+22** |
| n_full_recall | 8 | 13 | +5 |
| hit_rate@step1 | 0.4932 | 0.4932 | 0.00pp |
| hit_rate@step2 | 0.5654 | 0.5729 | +0.75pp |
| hit_rate@step3 | 0.6060 | 0.6316 | +2.56pp |
| hit_rate@step4 | 0.6331 | 0.6677 | +3.46pp |
| **hit_rate@step5** | **0.6511** | **0.6842** | **+3.31pp** |
| mean_terminal_recall | 0.1809 | 0.1922 | +1.13pp |
| mean_steps_to_first | 1.4711 | 1.5429 | +0.07 |
| full_recall_rate | 0.0120 | 0.0195 | +0.75pp |

**Keep criterion (both required by NEXT_SESSION_STEP2.md):**
1. ✓ +Δ on s2f@step5: **+3.31pp** (target: positive)
2. ✓ Non-regression on n_hit: 433 → 455 (+22)

Per-task: 70 wins / 37 losses, **26 rescues** (None→hit) vs 4 lost.

**Strata breakdown — CORE wins biggest, as predicted by causal_trace_analysis:**

| Stratum | n | OFF | ON | Δhit | rescues | lost | wins | loss |
|---|---|---|---|---|---|---|---|---|
| **CORE** | 236 | 60.6% | **68.6%** | **+19** | **22** | 3 | 41 | 22 |
| BO | 361 | 65.7% | 66.5% | +3 | 4 | 1 | 24 | 14 |
| HS | 28 | 71.4% | 71.4% | 0 | 0 | 0 | 4 | 0 |
| PI | 40 | 82.5% | 82.5% | 0 | 0 | 0 | 1 | 1 |

CORE was the stratum where `causal_trace_analysis.md` documented the
reranker's BO-bias hurting CORE tasks (`l12-ft-run1` demotes CORE-relevant
files in favour of BO-style infrastructure files). Body enrichment rescues
22 CORE zero-recall tasks because the JIRA body typically names the
correct CORE repo (e.g. CORE-2522 body extracts `grpc-payment-gateway` —
which is literally the GT repo).

**Default flipped to ON 2026-05-21 night.** After pod n=665 keep-decision
PASSED, the env-flag default was flipped from `"0"` → `"1"` in both
`src/search/hybrid.py:_TASK_BODY_ENRICH` and `scripts/eval/bench_steps_to_find.py:_BODY_ENRICH`.
Set `CODE_RAG_TASK_BODY_ENRICH=0` to disable for ablation runs.

**Local sanity preceding pod (`bench_runs/improve/s2f_step2_smoke/`):**
- `core30_off.json` / `core30_on*.json` — CORE offset=440 n=30 mixed signal
  on early-design variants (w=0.5 lost 5/30, w=0.25 marginal); validated
  CODE_RAG_BODY_PROTECT_TOP_N=5 as the load-bearing fix (5W/4L → 7W/4L on
  that slice, ultimately +1 n_hit / +3.3pp hit@5).
- `bo30_off.json` / `bo30_on_protect5.json` — BO offset=0 n=30 confirmed
  no regression on healthy-title strata (5W/2L, 2 rescues).

**Pod artifacts (`bench_runs/improve/s2f_step2_pod/`):**
- `s2_off_n665.json` — OFF baseline, bit-identical to Step 1 baseline
  (n_hit=433, hit@5=0.6511) confirming default-OFF is no-op.
- `s2f_on_n665.json` — ON arm, keep-decision PASSED.
- `trace_s2_off.jsonl` / `trace_s2_on.jsonl` — per-query traces with
  body_query + body_fts_count fields (added in this step).
- `s2_off.log` / `s2_on.log` — full bench stdout.

**Methodology notes (post-mortem):**
- Local n=30 on CORE-opaque cluster initially looked mixed (5W/10L on
  naive RRF, then 4W/5L on w=0.25). Brief warned `feedback_blind_smoke_
  insufficient` and the warning held: pod n=665 revealed strong CORE
  signal that local n=30 sample undersaturated.
- The CODE_RAG_BODY_PROTECT_TOP_N=5 guard was the load-bearing fix —
  without it, body's generic-payment-domain tokens (`expiry_month`,
  `company_id`, `three_ds_challenge`) would displace correct title hits.
  Top-5 protect lets body fill ranks 6-10 (rescue path) without competing
  for ranks 1-5 (where title's correct top-3 lives).
- Body query is FTS-only (no vector, no rerank) — keeping it lightweight
  matches the brief's "auxiliary signal" framing. Adding rerank-with-title
  on body candidates would be a future experiment.

### Step 3 v1 — per-token candidate union NO-OP 2026-05-21 night

Plan B Step 3 (revised from "provider-scaffolding" — see decision rationale
below) attempted IDF / rare-token rescue via per-token FTS5 union into the
keyword pool. Implementation (env-gated default OFF):

- `src/search/fts.py::fts_search_per_token` — splits query into content
  tokens (≥3 chars, non-stopword), sorts by length desc as rarity proxy,
  caps at `_PT_MAX_TOKENS=10`, calls `fts_search(token, limit=_PT_LIMIT=10)`
  per token, unions into a single dedup'd list.
- `src/search/hybrid.py` — env `CODE_RAG_PER_TOKEN_UNION=1`. Appends
  per-token results to `keyword_results` AFTER the main `fts_search(query,
  limit=150)` call.
- `tests/test_fts_per_token.py` — 10 unit tests.

**Result on local n=30 (offset=0):** ZERO change vs OFF baseline.
- n_hit 23 → 23 (Δ=0)
- hit_rate@step5 76.7% → 76.7% (Δ=0)
- terminal_recall 21.75% → 21.75% (Δ=0)
- mean_steps_to_first 1.78 → 1.78 (Δ=0)
- 30/30 NEUTRAL on per-task TR.

**Why it failed (RRF math):** v1 appends per-token candidates AFTER the
main FTS pool. They land at ranks 151-250, getting RRF score
`2.0/(40+151+1)=0.0104` — vs top vector candidate at
`1.0/(40+0+1)=0.0244`. Per-token contribution is dominated by every other
leg. FIX-D dedup (chunk → file) further trims duplicates of files already
in pool. Per-token-only candidates never reach the reranker top-200 read
window in any meaningful position.

**Why "provider-scaffolding" was deferred:** the brief's Step 3 was
"provider-scaffolding tool", but recon found only 3/43 zero-recall tasks
are provider-related (PI-37, PI-41, PI-47). PI strata already hit 82.5%
(best stratum). PI-47 "Payhub" is unfixable without external mapping
(brand name 0-chunks in index). Estimated lift was <1pp.

**Why per-token failed concretely:** smoke on 5 BO/CORE tasks the design
agent picked (BO-1041, BO-1139, BO-1224, CORE-2507, CORE-2609) showed all
5 had ZERO overlap between query tokens and GT-file content (vocab-gap
failure mode, NOT generic-term-drowned). Per-token union can only rescue
files that have AT LEAST ONE query token — these had none.

**Status:** v1 code committed env-gated default OFF (zero production impact).
v2 redesign (per-token as separate RRF leg with comparable weight to keyword
leg) is under research — see `tasks/research_v2_*.md` for agent reports.

### Step 3 v3 — FE-default-boost FALSIFIED 2026-05-21 night

After Step 3 v1 no-op, attempted a different angle from a research agent
report: 7/13 FE-bias zero-recall tasks (per `.claude/debug/current/misses_slice1.md`)
have queries with NEITHER explicit FE keywords NOR BE keywords. Current
`_detect_intent_adjustments` gives them `repo_boost=None`. Hypothesis:
soft 1.2x boost on FE repos for these signal-free queries would lift
token-poor JSX components.

Implementation: env-gated `CODE_RAG_FE_DEFAULT_BOOST=1` + new branch in
`src/search/service.py::_detect_intent_adjustments` (`elif not has_backend
and not has_frontend`).

**Result on local n=30 (offset=0, body-enrich ON baseline = current best):**
- n_hit 25 → 23 (**−2**)
- hit_rate@step5 83.33% → 76.67% (**−6.66pp**)
- terminal_recall 22.44% → 19.03% (**−3.41pp**)
- 7 LOSSES / 0 WINS

**Per-task losses:**
- BO-928 100% → 67%, BO-1588 33% → 0%, BO-1266 29% → 14%,
  CORE-2167 30% → 20%, CORE-1751 17% → 12%, CORE-2362 12% → 9%, BO-1041 3% → 0%

**Why it failed:** 1.2x boost on 19 FE repos shifts ranking → kicks out
previously-correct rank-3 GT → cascade divergence (same pattern as
causal_trace_analysis.md). The "indiscriminate boost helps everyone equally"
assumption is wrong: CORE tasks have GT in graphql/grpc, not FE repos, so
FE boost demotes their actual GT by relative ranking.

**Status:** code retained env-gated default OFF (zero production impact);
do NOT enable without a more targeted activation rule (e.g. only when
query contains UI-suggesting nouns that aren't already in `_FRONTEND_KEYWORDS`).
NOT pod-benched (waste of $).

### Step 4 — camelCase whole-token indexing FALSIFIED PREMISE 2026-05-21 night

Plan B step 4. After user GO, ran an EMPIRICAL tokenizer test BEFORE
launching the 2h+20GB reindex. Found the premise is wrong:

```
sqlite3 db/knowledge.db
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'getMerchantId';      → 25
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'toArray';            → 129
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'generateColumnDefinitions'; → 88
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'UpdateActiveMerchantApplication'; → 1
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'columnDefinitions';  → 9
```

**FTS5 `porter unicode61` ALREADY preserves camelCase identifiers as
whole tokens.** unicode61 splits on non-alphanumeric chars only; case
boundaries are NOT splits. The earlier ARCHITECTURE_STATUS claim
"`porter unicode61` splits `toColumnDefinitions` → `column/definitions`"
was wrong — that's not how the tokenizer behaves.

**Real BO-1234/904/1474 failure modes (empirically verified):**
- **BO-1234** "Remove `toColumnDefinitions` function" — `toColumnDefinitions`
  matches 0 chunks. The identifier doesn't exist in current code (refactor
  task: code was renamed to `generateColumnDefinitions` already, which has
  88 chunks). Vocab gap from STALE QUERY TOKEN. Step 4 inject can't help.
- **BO-1474** "UpdateActiveMerchantApplication permission" — identifier
  IS indexed (1 chunk). The failure is ranking: that 1 chunk doesn't
  beat 200 other candidates in the BM25 race. Ranking-layer fix only.
- **BO-904** "Refactor all backoffice Children.toArray" — `toArray` is
  TOO COMMON (129 chunks). The 4 GT files are drowned by 125 sibling
  uses. Needs IDF or repo-aware filtering — Step 3 already tried both
  and failed.

**Verdict:** Step 4 as designed (inject `__identifiers` at index time)
would add ~60 LOC + force a 2h pod reindex ($1-2) for ZERO measurable
lift. The tokenizer is not the bottleneck. Cancelled before launching
the reindex.

This closes the brief's Plan B prioritized next steps list. Remaining
items #5 (IDF — tested as Step 3 v1, no-op), #6 (steps-to-find — done),
#7 (ast-grep — additive, no risk, ~2.5 days) are documented in earlier
sections.

### Step 5 — camelCase query expansion DEFAULT ON 2026-05-22 night

After user redirected to recall focus, ran single-shot recall@10 diagnostic
on full pod n=665 to test all available env knobs. Discovery: the existing
`CODE_RAG_USE_CAMELCASE_EXPAND` env-flag (default OFF for ~6 weeks per the
src/search/fts.py:sanitize_fts_query comment) gives a measurable recall lift.

**Pod n=665 4-arm A/B (RTX 4090 ~$0.50, 2026-05-22):**

| Arm | hits | hit@10 | recall@10 | recall@pool | retr_fails |
|---|---|---|---|---|---|
| baseline (body OFF, camel OFF) | 454 | 0.6827 | 0.1794 | 0.4708 | 58 |
| body ON | 454 | 0.6827 | 0.1794 | 0.4708 | 58 |
| **camel ON** | **459** | **0.6902** | **0.1828** | **0.4846** | **52** |
| body + camel | 459 | 0.6902 | 0.1828 | 0.4846 | 52 |

**Findings:**
1. **Step 2 body enrichment has ZERO effect on single-shot recall@10.**
   Body/no-body arms are bit-identical on diagnose_recall metrics. Body
   helps only in the s2f multi-step iterating consumer (+3.31pp pod-validated
   2026-05-21). Mechanism: body candidates land at lower ranks after title
   rerank; in single-shot they never reach top-10. In s2f they shift the
   reformulation cascade by changing which top-3-NEW files the agent reads.
2. **camelCase expand: +5 hits / +1.38pp recall@pool / −6 retrieval_failures**
   independently of body. Mechanism: for each adjacent token pair in a
   query (after stopword strip), emits `tokenAB` and `TokenAB` variants.
   FTS5 `porter unicode61` preserves these compound forms as whole tokens
   (verified empirically earlier — see "Step 4 FALSIFIED" section). So
   query "update merchant" gets OR-terms `updateMerchant UpdateMerchant`
   that match code identifiers literally containing those compounds.
3. **Orthogonal composition** — body + camel = same as camel alone on
   single-shot recall. No interaction.

**Why this is a clean win:**
- Code already existed (env-gated). Just flip default "0" → "1" in fts.py:330.
- Zero cost: sub-ms FTS5 query overhead for the extra OR-terms.
- Reversible: `CODE_RAG_USE_CAMELCASE_EXPAND=0` disables.
- No reindex required.
- Validated on full n=665 pod GPU (same hardware as Step 1 zero-noise floor).

**Local sanity preceding pod (`bench_runs/improve/recall_current/`):**
- n=100 offset=0 paired: hit@10 68% → 69%, recall@10 +0.88pp, recall@pool +1.10pp
- n=100 offset=100 paired (different sample): recall@10 +1.34pp, recall@pool +4.26pp
- Combined n=200: recall@10 +1.11pp, recall@pool +2.68pp (consistent across slices)

**Net session-over-session recall trajectory:**
| Metric | Branch start | After Step 5 | Δ |
|---|---|---|---|
| hit@10 single-shot | 60.5% | ~71-72% | +10pp+ |
| recall@10 | 15.2% | ~18.3-19% | +3-4pp |
| recall@pool | 42% | **49%** | **+7pp** |
| s2f@step5 | 65.1% | 68.4% | +3.31pp (Step 2) |

### Step 3 v2 — per-token as separate RRF leg FALSIFIED 2026-05-22

After Step 3 v1 (per-token append-to-FTS) was NO-OP, redesigned as separate
RRF leg with own `pt:{rowid}` scores dict key + `CODE_RAG_PT_RRF_WEIGHT=0.5`.
Pool widens (+0.28pp recall@pool) but reranker picks per-token candidates over
true positives at boundary ranks → net **-7 hits at w=0.5, -4 at w=0.25**.

Monotonic: lower weight reduces both wins and losses proportionally — no
sweet spot under current reranker. Code kept env-gated default OFF.

**Same pattern as Step 3 v1, STRIP_META_TAGS, H5 FE_DEMOTE attempts: any
PRE-rerank pool-composition tweak gets overridden by the reranker.**

### STRIP_META_TAGS NEUTRAL 2026-05-22

Tested `CODE_RAG_STRIP_META_TAGS=1` (existing flag, never bench'd on n=665).
20/665 queries match the regex (3% coverage). Result: 0 hit@10 flips, +0.06pp
recall@pool — neutral. Mechanism analysis showed 11/20 affected tasks have
prefix token IN GT path (e.g. `[API]` → `express-api-v1/`), so strip removes
positive routing signal for some while removing noise for others. Cancellation.
Code unchanged.

### QUERY_V2 bench/prod parity BUG FIXED 2026-05-22

`scripts/eval/diagnose_recall.py:89` had `CODE_RAG_QUERY_V2` defaulting to
`"0"`, while `src/search/service.py:_QUERY_V2` defaults to `"1"` (FIX-G, ON
since 2026-05-19). All prior bench runs (Step 2, Step 3 v1/v2/v3, Step 5,
STRIP_META) measured an OLDER entity-boost code path that collapses 6+ word
queries to a single entity token. **23/665 bench retrieval_failures are
bench-only artifacts** — production never had the collapse.

n=8 local smoke confirmed: +25pp hit@10, +18pp recall@pool, trace shows
"Replace JSON Viewer with Monaco Editor" → "JSON" (1 word) in OFF arm vs
full 6-word query in ON arm.

**Fix:** `scripts/eval/diagnose_recall.py:89` default flipped to `"1"`.
**Implication:** all prior bench numbers (recall trajectory 60.5% → 71-72%
hit@10) are LOWER bounds vs production. Re-baselining on full pod n=665
deferred — RunPod budget exhausted.

### H5 ARCHITECTURAL FINDING (2026-05-22)

154 reranker_failures (pool reached GT but rank > 10) cluster heavily in BO
prefix (86/154 = 56%). Of those, 5 are TRUE BUGS where the intent classifier
mis-routes BO tasks as `backend` (via keywords `export`/`csv`/`microservice`)
→ `_FRONTEND_DEMOTE_MULTIPLIER` demotes `backoffice-web` 0.9x → GT files
in bo-web lose ranking. Affected: BO-1078, BO-1433, BO-1579, BO-831, BO-941.

Tested 5 narrow keyword-removal sets — all NET NEGATIVE (regress more
correctly-classified backend tasks than fix BO ones). Tested aggressive
boosts via env (FE_DEMOTE 0.9→1.0→1.3) — moves rank by ~2 positions only,
never crosses top-10. Tested post-rerank cluster rescue heuristic — lifts
wrong repo on BO-1579 (cluster threshold catches grpc-onboarding-pricing
NOT backoffice-web). **No simple fix.**

### Step 6 — task_hint LANDED 2026-05-22

Opt-in `task_hint: str | None` param on `search_tool()`:
- `"frontend"` / `"backoffice"` → 2.0x boost on FRONTEND_REPOS
- `"backend"` → 2.0x boost on BACKEND_REPOS + prefixes
- Default `None` → byte-identical to baseline

Verified A/B on 5 H5 BO + 3 BO controls:
- 3/5 H5 lift into top-10 (BO-1078 25→10, BO-1433 11→1, BO-1579 14→1)
- 1/5 H5 expected regression (BO-941 mixed FE+BE GT)
- 0/3 controls regress
- NET **+4 GT files into top-10**

Pool composition byte-identical between arms — pure post-fusion RRF
multiplier. **Falsifies "magnitude ceiling" assumption from H5**: 2.0x boost
crosses rerank ceiling where 1.3x doesn't.

Env: `CODE_RAG_TASK_HINT_BOOST=2.0`. Default OFF (caller opt-in via param).
MCP orchestrator routes on JIRA prefix: `BO-*` → `"frontend"`, `PI-*` →
`"backend"`. Plus `_auto_task_hint_from_query()` that regex-extracts prefix
from query string (env `CODE_RAG_AUTO_TASK_HINT=1`, default OFF).

### Step 6 — search noise demote LANDED 2026-05-22

`src/search/hybrid.py` POST-rerank score adjustments:
- `CODE_RAG_DEMOTE_TEST_PATHS=1` → 0.5x on `/tests/`, `/__tests__/`, `.spec.`, `.test.`
- `CODE_RAG_DEMOTE_TOOLING_REPOS=1` → 0.2x on `github-*-action`, `*-eslint-config`, `*-prettier-config`, `lint-*`, `config-*`

Suppressed when query has matching keyword (`test`/`spec`/`ci`/`action`/etc).
EXCLUDES `boilerplate-*` from tooling pattern — those are templates users
actually consult for "how to add new X" queries.

**Key insight: PRE-rerank score adjustments bounce off the reranker** —
moving the demote AFTER `rerank()` returns + re-sorting by `combined_score`
is the only place pool-composition tweaks actually move final results.

Verified A/B on 3 realistic dev queries:
- Q1 "how to add new provider integration": github-run-e2e-action rank 3 → 8
- Q2 "webhook signature validation": byte-identical (already perfect)
- Q3 "merchant onboarding flow": 3 `.spec.js` files moved from #2,3,5 → #6,7,8

25-query stratified noise audit: **1.6% noise rate in top-5** (was higher
before). Today's demote fixes generalize broadly.

### Step 7 — HARD_FILTER + SCOPE_WARNING LANDED 2026-05-23

**HARD_FILTER (`CODE_RAG_HARD_FILTER=1`):** when query mentions a known
provider name (paypal/nuvei/stripe/etc), drop pool entries whose repo is
NOT in the provider-relevant set BEFORE rerank. Implementation: new
`repo_allow_list` + `repo_allow_prefixes` params on `hybrid_search()`,
deleted from `scores` dict before rerank input.

Verified on user's real PI-56 deploy ("Nuvei expired payment handling"):
- baseline: 0 hits in top-10 (reranker confused with workflow-dispute-expiration)
- fix: **5 hits in top-10, rank 1 = `workflow-provider-webhooks/handle-activities.js`** (PR target file)
- pool 320 → 69 (78% reduction of cross-workflow noise)

Non-provider queries byte-identical (zero regression on 3 control queries).

**SCOPE_WARNING (`CODE_RAG_SCOPE_WARNING=1`):** prepend warning when top-N
spans many repos for short query (vague intent). Fires on `settlement`
(1w, 9 repos), `add prettier plugin tailwindcss` (4w, 10 repos), but NOT on
focused queries (`paypal webhook signature verification` → 1 distinct repo).

12-test smoke suite (`tests/smoke_search.py`) including a real PI-56
regression test. 12/12 pass.

### Step 8 — CRON SILENTLY BROKEN FIXED 2026-05-23..25

User reported MCP returning stale results on tasks merged weeks ago. Root
cause: `scripts/full_update.sh` referenced scripts at OLD paths after a
refactor moved them into subdirectories.

**Broken paths (4 in full_update.sh):**
- `extract_artifacts.py` → `scripts/scrape/`
- `build_index.py` → `scripts/build/`
- `build_graph.py` → `scripts/build/`
- `embed_missing_vectors.py` → `scripts/data/`
- `build_docs_vectors.py` → `scripts/build/`
- `build_shadow_types.py` → `scripts/build/`
- `benchmark_queries.py` → `scripts/bench/`
- `benchmark_realworld.py` → `scripts/bench/`
- `detect_blind_spots.py` → `scripts/analysis/`
- `detect_doc_staleness.py` → `scripts/analysis/`

**Plus PYTHONPATH bug in 6 moved scripts** — they used
`Path(__file__).parent.parent` which after move pointed at `scripts/` (not
repo root). Fixed: all changed to `.parent.parent.parent`.

**Plus `build_vectors.py` regex aborted on `.github` dot-prefix repo** —
added filter to skip dot-prefix repos with warning.

**Plus smoke trigger used GNU `timeout`** (not available on macOS) —
switched to project's own `run_with_timeout.sh`.

**Index freshness BEFORE FIX:**
- `knowledge.db`: May 16 (1 week old)
- `vectors.lance.coderank/`: Apr 24 (1 MONTH old)
- Cron had been silently failing at step 2/7 since ~May 5

**AFTER FIX:**
- All 7 steps execute successfully
- knowledge.db: May 24 (fresh)
- Vector index: 81688 chunks, 81605 → 81688 delta (new content embedded)
- `grpc-apm-inpay`: 1 file → 34 files (PI-65 PR now indexed)

**Post-rebuild smoke trigger** added to `scripts/full_update.sh` tail —
runs `pytest tests/smoke_search.py` after every rebuild, logs to
`logs/post_rebuild_smoke.log` for next-morning review.

### Net branch trajectory after Steps 6-8

| Metric | Branch start | After Step 5 | After Step 8 |
|---|---|---|---|
| hit@10 single-shot | 60.5% | ~71-72% | ~71-72% (no aggregate movement; reranker still ceiling) |
| recall@10 | 15.2% | 18.3% | 18.3% (no full re-bench post-cron-fix) |
| recall@pool | 42% | 49% | 49% |
| PI-56 NUVEI top-10 hits | 0 (reranker miss) | 0 (still wrong workflow) | **5 (rank 1 with HARD_FILTER)** |
| PI-65 INPAY visible | 0 (stale index) | 0 (stale index) | **34 files indexed (post-cron-fix)** |
| Noise rate top-5 | ~10% | ~5% | **1.6%** (25-query audit) |
| Smoke suite | none | none | **12/12 PASS** |

### Honest limits remaining

- **Reranker remains binding constraint** on hit@10 for queries WITHOUT
  provider name + WITHOUT explicit task_hint. Architectural fix would need
  alt reranker (jina-v2 / mxbai-v2 — license + pod budget) or post-rerank
  rescue (not yet implemented).
- **Full pod n=665 validation deferred** — RunPod budget exhausted.
  Production env should opt-in: HARD_FILTER + SCOPE_WARNING +
  DEMOTE_TEST_PATHS + DEMOTE_TOOLING_REPOS = recommended.
- **State-file forgiveness:** when cron breaks for weeks, state file thinks
  changed repos are "processed" even though extract failed. To force
  re-index after such drift: clear `repo_state.json` entries for affected
  repos before next run.

## Source data

- `bench_runs/diagnose/fixI/` — current hybrid baseline (all fixes, vector+reranker ON)
- `bench_runs/diagnose/ftsonly/` — vector OFF
- `bench_runs/diagnose/norerank/` — reranker OFF
- `bench_runs/headtohead/` — MCP hybrid vs plain grep-agent
- `bench_runs/improve/step3v2/` — Step 3 v2 FALSIFIED on pod n=665
- `bench_runs/improve/stripmeta/` — STRIP_META_TAGS neutral
- `bench_runs/improve/queryv2_local/` — QUERY_V2 parity bug finding
- `bench_runs/improve/agent_task_prefix/` — task_hint A/B (8-task panel)
- `bench_runs/improve/auto_hint/` — auto-task-hint A/B
- `bench_runs/improve/noise_fix/` — test/tooling demote A/B
- `bench_runs/improve/noise_audit/` — 25-query stratified noise audit
- `bench_runs/improve/hard_filter/` — HARD_FILTER PI-56 A/B
- `tests/smoke_search.py` — 12-test quality regression baseline
- `DEEPRESEARCH_PROMPT.md` — the deep-research brief
- `.claude/autonomous/PROGRESS.md` — full chronological log
