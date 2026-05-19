# SESSION_FINDINGS — recall@10 autonomous analysis

> Session: 2026-05-19 (autonomous)
> Source: `bench_runs/diagnose/full_diagnose.json` (665 queries, deep top-200 pool)

---

## ★ FINAL RESULT — autonomous run complete

| Metric | Original baseline | After code fixes | Δ |
|--------|-------------------|------------------|------|
| hit@10 | 0.6045 | **0.6451** | **+4.06pp** |
| recall@10 | 0.1518 | **0.1670** | **+1.52pp** |
| recall@pool (top-200) | 0.4017 | 0.4428 | +4.11pp |
| retrieval_failures | 86 | 67 | −19 |
| reranker_failures | 177 | 169 | −8 |

**Kept code fixes** (all env-gated, default OFF — production must opt in):
- **FIX-A** `CODE_RAG_DEMOTE_DOC_NOISE` — `src/search/hybrid.py`. Keeps in-repo
  doc/markdown out of the candidate pool for code-intent queries.
- **FIX-D** `CODE_RAG_DEDUP_RESULTS` — `src/search/hybrid.py`. Collapses per-chunk
  duplicates of the same file before reranking. **Biggest win** (+3.01pp hit@10).
- **FIX-F** — 7 domain abbreviations in `profiles/pay-com/glossary.yaml`
  (gitignored — lives locally only).

**Reverted** (measured flat/negative): FIX-B (frontend keywords), FIX-C (bracket
strip). **Not viable:** FIX-E. → All three are retrieval/intent-side; consistent
with the prior finding that mild repo-boost mechanisms don't move recall.

**To activate in production:** set `CODE_RAG_DEMOTE_DOC_NOISE=1` and
`CODE_RAG_DEDUP_RESULTS=1`. See QUESTIONS.md — this is a deploy decision.

## The ceiling — why code fixes can't go further

recall@pool is **0.44**: 67% of expected files never reach the top-200 pool at
all. Even a perfect reranker caps recall@10 at ~0.44. Code/query-side fixes are
exhausted at +1.52pp recall@10. **The remaining ~0.28 gap is a model problem:**
- embeddings don't capture task-level semantics (file implements a concept
  without naming it) → 69 semantic_gap queries.
- reranker mis-orders the (now cleaner) pool → 169 reranker_failures.

**Training data is extracted and ready** — `bench_runs/training_data/`:
6686 reranker pairs (2521 pos + 4165 hard-neg) and 7145 retrieval/embedding
positives. Actual model training awaits explicit user GO (RunPod $, per project
rules). Targets + spec in `RERANKER_IMPROVEMENT_PLAN.md`.

---

## Honest baseline (Fixed config, full 665-query eval)

| Metric | Value |
|--------|-------|
| hit@10 | **60.45%** (402/665) |
| recall@10 | **0.152** |
| recall@pool (top-200) | **0.402** |
| **reranker headroom** (recall@pool − recall@10) | **0.250** |

> Note: this 60.45% / 0.152 is the honest full-set number. The prompt's
> "70-72%" came from offset 0-49 only — an easier slice.

## Query classification

| Class | Count | Share |
|-------|-------|-------|
| hit (≥1 expected in top-10) | 402 | 60.5% |
| reranker_failure (expected in pool, ranked >10) | 177 | 26.6% |
| retrieval_failure (no expected in top-200) | 86 | 12.9% |

## Expected-file rank distribution (all 10 650 expected files)

| Rank bucket | Files | Share |
|-------------|-------|-------|
| 1-10 | 984 | 9.2% |
| 11-50 | 1353 | 12.7% |
| 51-100 | 687 | 6.5% |
| 101-200 | 481 | 4.5% |
| **miss (>200)** | **7145** | **67.1%** |

## Two root causes, quantified

1. **Retrieval ceiling (dominant).** 67% of expected files never enter the
   top-200 pool. recall@pool caps at 0.40 — even a perfect reranker can't beat
   that. This is an embeddings / query-vocabulary problem.
   - **Not an indexing gap:** all 7145 missed files ARE in the index
     (`retrieval_positive_NOT_in_index: 0`). The data is there; retrieval
     doesn't surface it.
2. **Reranker headroom (0.25).** Of files that DO reach the pool, the reranker
   leaves 25 recall-points unplaced — 1353 expected files sit at rank 11-50,
   recoverable by a better reranker. 133 queries are "near-miss" (best expected
   file at rank 11-50).

## Training data extracted (`bench_runs/training_data/`)

| File | Rows | Purpose |
|------|------|---------|
| `reranker_pairs.jsonl` | 6686 (2521 pos + 4165 hard-neg) | CrossEncoder FT |
| `retrieval_pairs.jsonl` | 7145 positives | embedding FT |

## Pattern analysis — DONE (6 agents, 263 non-hit queries)

| Sub-type | Count | Fixable by code? |
|----------|-------|------------------|
| intent_routing | 87 | YES |
| semantic_gap | 69 | NO → embeddings training |
| huge_task | 50 | NO → eval-design / inherent |
| granularity | 39 | PARTLY |
| vocab_mismatch | 17 | YES |

**6 ranked code fixes** (FIX-A..F) in `bench_runs/diagnose/FIX_PLAN.md`:
- FIX-A demote in-repo `docs/*.md` for code queries (5/6 agents — top consensus)
- FIX-B frontend intent keywords for refactor/UI tasks (4/6)
- FIX-C strip bracket tags `[API]`/`[GW]`/`[3DS]` + route them
- FIX-D pool dedup of mirrored path variants
- FIX-E strip generic edit verbs before repo-name matching
- FIX-F glossary/expansion entries (abbreviations)

**Training-data targets:** `bench_runs/training_data/` — 6686 reranker pairs,
7145 retrieval pairs. semantic_gap (69) → embeddings FT; granularity residual
→ reranker FT.

Per-query detail: `bench_runs/diagnose/agent_findings/batch_*.md`.
Fix execution + before/after metrics tracked in `FIX_PLAN.md` and PROGRESS.md.
