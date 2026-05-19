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
  earns +8pp hit@10. It is not baggage.
- ❌ Do **not** trust `MODEL_TRAINING_SPEC.md` / `RERANKER_IMPROVEMENT_PLAN.md` /
  `NEXT_SESSION_PROMPT.md` — superseded, they point the wrong way.

## What was measured (this session)

| Test | Result |
|------|--------|
| Code fixes shipped (commits `22a996b`, `3eebeda`) | hit@10 0.605→0.714, recall@10 0.152→0.182. Env-gated, default ON. |
| Head-to-head, 15 tasks: MCP hybrid (single-shot) vs plain grep-agent (full loop) | ≈ tied. file-recall 0.19 vs 0.18; foothold 0.63 vs 0.51 (hybrid slightly ahead). |
| FTS-only (vector OFF) | hit@10 −8.3pp, recall@pool −7.3pp, retrieval_failures ×2 → **vector earns its keep**. |
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

### Prioritized next steps

1. **analyze_task routing is genuinely hard for CORE** — no cheap template win.
   Remaining real option: P1 (specificity-weight keyword matches — ranking-only,
   low-risk, modest). The structural answer for CORE is agent iteration, not a
   static prior.
2. **`ast-grep` structural-search tool** — additive, no risk, separate feature
   (`bench_runs/improve/ast_search_design.md`). ~2.5 days.
3. **Embedding-model SWAP bench** (not FT) — the one defensible RunPod spend;
   recall@pool 0.48 is the reach ceiling. Low priority.
4. GT: optionally drop the 122 noise paths. Trivial, not blocking.

Honest framing: the consumer is an **iterating agent**. "Good results" = the
agent gets a foothold + navigates well, not a perfect single-shot top-10. The
session shipped the biggest jump (+10.4pp hit@10) + the coverage hint; the
remaining gains are modest. The headline routing fix (CORE template) was tested
and does not hold — analyze_task routing for CORE is intrinsically hard.

## Source data

- `bench_runs/diagnose/fixI/` — current hybrid baseline (all fixes, vector+reranker ON)
- `bench_runs/diagnose/ftsonly/` — vector OFF
- `bench_runs/diagnose/norerank/` — reranker OFF
- `bench_runs/headtohead/` — MCP hybrid vs plain grep-agent
- `DEEPRESEARCH_PROMPT.md` — the deep-research brief
- `.claude/autonomous/PROGRESS.md` — full chronological log
