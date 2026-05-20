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

## Source data

- `bench_runs/diagnose/fixI/` — current hybrid baseline (all fixes, vector+reranker ON)
- `bench_runs/diagnose/ftsonly/` — vector OFF
- `bench_runs/diagnose/norerank/` — reranker OFF
- `bench_runs/headtohead/` — MCP hybrid vs plain grep-agent
- `DEEPRESEARCH_PROMPT.md` — the deep-research brief
- `.claude/autonomous/PROGRESS.md` — full chronological log
