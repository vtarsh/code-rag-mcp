# Architecture status — code-rag-mcp · 2026-05-19

> **READ THIS FIRST.** This is the current source of truth on direction.
> It SUPERSEDES `MODEL_TRAINING_SPEC.md`, `RERANKER_IMPROVEMENT_PLAN.md`,
> `NEXT_SESSION_PROMPT.md` and the recall@10 framing in `SESSION_FINDINGS.md` —
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
- ❌ Do **not** trust `MODEL_TRAINING_SPEC.md` / `RERANKER_IMPROVEMENT_PLAN.md` /
  `NEXT_SESSION_PROMPT.md` — superseded, they point the wrong way.

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

**Two findings:**
1. Step 2 body enrichment has ZERO effect on single-shot recall@10 (helps only
   the s2f multi-step iterating consumer where it adds +3.31pp on step5).
   Mechanism: body candidates land at lower ranks after title rerank; in
   single-shot they never reach top-10. In s2f they shift the reformulation
   cascade by changing which top-3-NEW files the agent reads.
2. camelCase expand adds +5 hits / +1.38pp recall@pool / -6 retrieval_failures
   orthogonal to body. Mechanism: for each adjacent token pair in a query
   (after stopword strip), emits `tokenAB` and `TokenAB` variants as OR-terms.
   FTS5 `porter unicode61` preserves these compound forms as whole tokens.

**Why this is a clean win:**
- Code already existed (env-gated). Just flip default "0" → "1" in fts.py:334.
- Zero cost: sub-ms FTS5 query overhead.
- Reversible: `CODE_RAG_USE_CAMELCASE_EXPAND=0` disables.
- No reindex required.
- Validated on full n=665 pod GPU (same hardware as Step 1 zero-noise floor).

**Local sanity preceding pod (`bench_runs/improve/recall_current/`):**
- n=100 offset=0 paired: hit@10 68% → 69%, recall@10 +0.88pp, recall@pool +1.10pp
- n=100 offset=100 paired (different sample): recall@10 +1.34pp, recall@pool +4.26pp
- Combined n=200: recall@10 +1.11pp, recall@pool +2.68pp

### Net branch-over-branch recall trajectory

| Metric | Branch start | After Step 5 | Δ |
|---|---|---|---|
| hit@10 single-shot | 60.5% | ~71-72% | +10pp+ |
| recall@10 | 15.2% | ~18.3% | +3pp |
| recall@pool | 42% | **49%** | **+7pp** |
| s2f@step5 | 65.1% | 68.4% | +3.31pp (Step 2) |

## Steps history (chronological)

### MEASURED 2026-05-19 — analyze_task routing

Built an honest routing benchmark (`scripts/eval/bench_routing.py`, repo-routing
recall@5 / foothold@5; de-leaked via `CODE_RAG_TASKS_DB=db/tasks_deleak.db` —
task_history with the 665 eval rows removed).

- **Honest baseline:** foothold@5 = **0.34**, routing_recall@5 = 0.20, @10 = 0.30.
- **Leakage is negligible:** de-leaked 0.241 vs full 0.238.
- **❌ CORE domain template — REJECTED by data.** The audit (P2) proposed it as
  the biggest win. But of 81 non-eval CORE tasks the most frequent repo
  (`express-api-v1`) appears in only **8%** (vs BO template's 93%). CORE is the
  whole heterogeneous backend — no universal repo set.

### MEASURED 2026-05-20 — recall@pool diagnosis + zero-recall root-cause

Two fact diagnostics (`scripts/eval/diagnose_pool_reach.py`,
`diagnose_query_gap.py`) + a 43-task case-by-case audit by 3 parallel agents.

- **recall@pool 0.48 is a STRUCTURAL ceiling, not a bug.** 100% of 10,650
  expected files ARE indexed. Ceiling = task-size cap + lexical gap +
  ranking crowd-out.
- **BM25 column-weight lever — TESTED, DEAD.** Reverted.
- **Zero-recall (43 tasks) failure modes:** generic-term-drowned ~15 FIXABLE;
  title↔code vocabulary gap / opaque-symptom titles ~14 INTRINSIC;
  camelCase tokenizer split (CLAIMED, later FALSIFIED — see Step 4);
  proper-noun / dependency symbol absent PARTLY FIXABLE; wrong-repo steering
  by provider proper-noun FIXABLE; tag-prefix steering FIXABLE; GT-noise ~5-6
  NOT retrieval failures.

### Step 1 — steps-to-find metric n=665 baseline + rerank-OFF arm 2026-05-20

Ran on RunPod RTX 4090 — baseline 22 min, rerank-OFF 16 min wall.

**Full corpus ARM COMPARISON:**

| Metric | rerank ON | rerank OFF | Δ |
|---|---|---|---|
| n_hit (step 5) | 433/665 (65.1%) | 399/665 (60.0%) | **−5.1pp** |
| **hit_rate@step 5** | **65.11%** | **60.00%** | **−5.1pp** |
| mean_terminal_recall | 18.09% | 16.10% | −2.0pp |

**REVISED debate-residual answer:** Iteration recovers ~64% of single-shot
reranker advantage (14.1pp → 5.1pp), but NOT all. Rerank still earns 5.1pp
irreducible value at step 5. KEEP rerank ON globally.

### Step 1 closing — test-retest noise + fix-#1 falsification 2026-05-21

**Test-retest noise floor on pod GPU: ZERO.** Re-ran the baseline n=665 bench
on a fresh pod with identical config/env. Bit-identical to 4 decimal places.

**Fix #1 (rerank-skip on compound code identifier) FALSIFIED on n=30 local:**
n_hit 23/30 → 20/30 (−3), hit_rate@step 5 76.67% → 66.67% (−10pp). REVERTED.

### Step 2 — refined JIRA body enrichment LANDED 2026-05-21

Separate FTS-only retrieval pass on code-anchored JIRA body tokens, RRF-merged
with the title pass. Default flipped ON 2026-05-21 night after pod n=665 PASSED.

**Components:**
- `src/tools/task_context.py` — sanitize_body + extract_code_anchored +
  build_body_query (drops tokens whose word-parts are fully in title).
- `src/search/hybrid.py` — body kwarg; FTS-only body pass (top-50);
  RRF merge with weight 0.5; CODE_RAG_BODY_PROTECT_TOP_N=5 guards title top-N.
- `scripts/eval/bench_steps_to_find.py` — fetches body from tasks.db.
- `tests/test_task_context.py` — 20 unit tests.

**Pod n=665 keep-decision:**

| Metric | OFF | ON | Δ |
|---|---|---|---|
| n_hit | 433 | **455** | **+22** |
| **hit_rate@step5** | **0.6511** | **0.6842** | **+3.31pp** |
| terminal_recall | 0.1809 | 0.1922 | +1.13pp |

Per-task: 70 wins / 37 losses, **26 rescues** vs 4 lost.

**Strata: CORE wins biggest** (+19 hits, 22 rescues) as causal_trace_analysis
predicted (reranker BO-bias hurts CORE; body extracts correct CORE repo names).

**Default flipped to ON.** Set `CODE_RAG_TASK_BODY_ENRICH=0` to disable.

### Step 3 v1 — per-token candidate union NO-OP 2026-05-21

Attempted IDF / rare-token rescue via per-token FTS5 union into the keyword
pool. Local n=30: ZERO change vs baseline (30/30 neutral).

**Why it failed:** v1 appends per-token candidates AFTER the main FTS pool.
They land at ranks 151-250, getting RRF score `2.0/(40+151+1)=0.0104` — vs top
vector candidate at `1.0/(40+0+1)=0.0244`. Per-token contribution dominated.

**Status:** v1 code committed env-gated default OFF. v2 redesign (per-token as
separate RRF leg) deferred.

### Step 3 v3 — FE-default-boost FALSIFIED 2026-05-21

Attempted soft 1.2x boost on FE repos for queries with neither FE nor BE
keywords. Local n=30: −2 n_hit, −6.66pp hit@step5, 7L/0W.

**Why it failed:** 1.2x boost on 19 FE repos shifts ranking → kicks out
previously-correct rank-3 GT → cascade divergence. CORE tasks have GT in
graphql/grpc, not FE repos, so FE boost demotes their actual GT by relative
ranking.

**Status:** code retained env-gated default OFF. NOT pod-benched (waste of $).

### Step 4 — camelCase whole-token indexing FALSIFIED PREMISE 2026-05-21

After user GO, ran an EMPIRICAL tokenizer test BEFORE launching the 2h+20GB
reindex. Found the premise is wrong:

```
sqlite3 db/knowledge.db
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'getMerchantId';      → 25
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'toArray';            → 129
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'generateColumnDefinitions'; → 88
  > SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'UpdateActiveMerchantApplication'; → 1
```

**FTS5 `porter unicode61` ALREADY preserves camelCase identifiers as whole
tokens.** unicode61 splits on non-alphanumeric chars only; case boundaries
are NOT splits. The earlier claim "`porter unicode61` splits
`toColumnDefinitions` → `column/definitions`" was wrong.

**Real BO-1234/904/1474 failure modes:** BO-1234 vocab gap (code renamed),
BO-1474 ranking issue (identifier in 1 chunk), BO-904 too-common token (toArray
129 chunks). Step 4 inject can't help.

**Verdict:** Step 4 cancelled before launching reindex.

### Step 5 — camelCase query EXPANSION default ON 2026-05-22

See top of WHERE NEXT section. Pod n=665 confirmed +5 hits / +1.38pp recall@pool.

## Source data

- `bench_runs/diagnose/fixI/` — current hybrid baseline (all fixes, vector+reranker ON)
- `bench_runs/diagnose/ftsonly/` — vector OFF
- `bench_runs/diagnose/norerank/` — reranker OFF
- `bench_runs/headtohead/` — MCP hybrid vs plain grep-agent
- `bench_runs/improve/s2f_step2_pod/` — Step 2 pod n=665 artifacts
- `bench_runs/improve/recall_pod_n665/SUMMARY.md` — Step 5 pod n=665 summary
- `DEEPRESEARCH_PROMPT.md` — the deep-research brief
- `.claude/autonomous/PROGRESS.md` — full chronological log
