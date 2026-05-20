# steps-to-find — design doc

> **Status:** v1 design, defaults chosen per session brief 2026-05-20. Subject to
> redirect by user before/after first sanity run.
>
> **Goal:** measure agent-iteration efficiency, not single-shot ranking. Resolve
> the architecture-debate residual question: does the reranker's −14.1pp
> single-shot hit@10 transfer to a multi-shot iterating consumer, or does the
> agent's reformulation recover those files anyway?

## Why this metric

Single-shot `recall@10` is task-size-capped at ~0.77 (large JIRA tasks change
20-180 files). `recall@pool` is a structural ceiling at ~0.48 and partly
GT-noise-biased. Neither models the real consumer: an iterating Claude-agent
that issues multiple searches, narrows by extracted identifiers, and reads
top files between searches.

`steps-to-find` measures: **how many MCP tool-calls does a deterministic agent
need to put each GT file in its read-set?**

Lower = better for the real consumer.

## Design forks — defaults chosen

| Fork | Default | Alternatives considered | Rationale |
|------|---------|------------------------|-----------|
| **What counts as a "step"** | one MCP-tool-call (a search-call collapses search + read top-K of that search into one step in v1) | (a) search + read tracked separately; (b) only search calls counted; (c) include grep | v1 simplification: each search-call adds K=3 new files to read-set. Avoids exponential branching on per-result read decisions. v2 can split. |
| **What counts as a "find"** | `(repo_name, file_path)` in agent's read-set | (a) in retrieval pool; (b) in top-10 | Read-set = the realistic consumer model. "Was returned at rank 30" doesn't help an agent that only reads top-3. Read-set strictness matches actual behavior. |
| **Agent simulator** | hard-coded deterministic loop: search → take top-K_READ new files → extract identifiers from top-1 new file's path → reformulate next query | (a) real Claude-loop in sandbox | **Forced by `feedback_no_external_llm_apis`** — local-only stack, no LLM eval. Also: real Claude is non-deterministic and expensive. Deterministic is reproducible and the primary keep criterion is **determinism**. |
| **N cap (steps)** | 5 turns | (a) 3 (too tight); (b) 10/20 (slow + diminishing returns observed in real PI-* sessions ≈ 3-7 calls) | Real PI-* audits this session averaged ~4-6 search calls per finding phase. 5 covers the realistic median. |
| **K_READ per step** | 3 | (a) 1 (too narrow); (b) 10 (collapses to recall@10) | K=3 mirrors observed real-agent behavior (read top-3, skim, decide). Discriminating between arms requires K small enough that ranking matters. |
| **Arms compared** | v1: `rerank ON` (baseline) vs `rerank OFF`. v2 follow-up: `with analyze_task` (use returned repo-hints + keywords as query expansion) | (a) `+ ast-grep`; (b) `+ scaffolding` | Resolves the debate's open question directly. v2 arm gated on v1 baseline passing sanity. ast-grep is Step 4-ish work, not in scope here. |

## Reformulation policy (deterministic)

Between steps, the agent extracts new query terms. The policy is fixed:

1. Take the top-1 **new** file (not already in read-set) from the last search.
2. From its `file_path`, extract identifier tokens:
   - basename without extension (e.g. `use-merchant-pricing-logic` → `use merchant pricing logic`)
   - last 2 path segments (parent dir + basename), split on `/`, `-`, `_`, camelCase boundaries
   - drop stop-words from `_STOP_WORDS` (a, an, the, of, for, to, get, set, …)
   - dedup against original-query tokens
   - keep at most 2 new tokens
3. Next step query = `original_query + " " + " ".join(new_tokens)`.
4. If no new file was returned (search collapsed) → break, no progress, terminal early.

**Why path-token reformulation:** observed in real session traces — agents
frequently follow up "BO-1234 toColumnDefinitions" with a search containing
`toColumnDefinitions` (extracted from a returned file's basename). This is the
cheapest non-LLM imitation of that pattern.

**Limitations:** does NOT model the agent reading file CONTENT and grabbing
identifiers from inside the file. v1 uses file-path tokens only. Realistic
agents extract more (function names, imports, sibling-file names). This is the
biggest known fidelity gap and is explicit; v2 can add content-token extraction
if the metric proves useful but lacks discrimination.

## Metric formulas

Per task `t` with `expected = {gt_files}`:

```
read_set = {}      # set of (repo, path) tuples
found_at = {}      # gt_key -> first step that put it in read_set
for step in 1..N:
    query = step_query(t, prev_results)
    ranked = hybrid_search(query, pool=200)
    new = first K_READ unseen ranked files
    read_set ∪= new
    for f in new ∩ expected: found_at[f] = step (if not already)
    if not new: break          # collapsed search
    if found_at == expected:   # full recall reached
        break

steps_to_first_hit(t) = min(found_at.values()) if found_at else None
steps_to_full_recall(t) = max(found_at.values()) if len(found_at) == len(expected) else None
terminal_recall(t) = len(found_at) / max(1, len(expected))
```

Aggregate (across `T` tasks):

```
mean_steps_to_first_hit = mean(steps_to_first_hit(t) for t with hit)
hit_rate_at_step_K = #{t: steps_to_first_hit(t) ≤ K} / T   for K = 1..5
mean_terminal_recall = mean(terminal_recall(t))
full_recall_rate = #{t: steps_to_full_recall(t) not None} / T
```

**Primary readout for the reranker question:**
- `hit_rate_at_step_5` (rerank ON) vs `hit_rate_at_step_5` (rerank OFF)
- If gap shrinks vs single-shot −14.1pp → reranker is partly iteration-redundant
- If gap stays ≈ −14.1pp → reranker contribution is irreducible by iteration

## Keep criteria (gating)

A `steps_to_find` measurement is **kept** as a primary metric iff:

1. **Honest:** no obvious bugs (sanity-checked top-1/top-3 read counts match
   reading top-K of `hybrid_search` output exactly).
2. **Reproducible:** running the bench twice on the same task subset with the
   same env config produces **byte-identical** per-task JSON output. (Determinism
   is non-negotiable. Vector search has minor numerical noise — if non-determinism
   shows, fix by seeding or disable vector for the metric.)
3. **Sanity:** baseline (rerank ON, n=20 first cut) shows discriminating numbers
   — mean `steps_to_first_hit` in `(1, 5)` (i.e., NOT all hits at step 1, NOT
   all misses). If the baseline collapses to "all step 1" then K_READ is too
   large and we need to drop to K=1; if "all 5" we need to relax K_READ or N.

If 1+2+3 pass on n=20 → run full n=665 baseline + rerank-OFF arm.

## Implementation outline

- **File:** `scripts/eval/bench_steps_to_find.py`
- **Interface:** subprocess-batched like `diagnose_recall.py` (50 tasks per
  fresh python, dodges macOS sentence-transformers semaphore leak).
- **Args:** `--eval`, `--out`, `--offset`, `--count`, `--n-steps=5`,
  `--k-read=3`, `--pool-limit=200`.
- **Env arms:** stub `rerank` / `vector_search` via `CODE_RAG_NO_RERANK=1` /
  `CODE_RAG_NO_VECTOR=1` (already supported by `hybrid` module).
- **Reproducibility check:** an `--assert-deterministic` flag re-runs each
  task twice and asserts identical results. Catches numerical noise sources
  early.

## Out-of-scope for v1 (deferred or off-by-design)

- LLM-driven reformulation (forbidden by `no_external_llm_apis`).
- Content-token extraction (file body) — biggest fidelity gap; add only if v1
  proves discriminating.
- ast-grep / grep as separate step types — Step 4 work in the plan; not here.
- Real wall-clock latency dimension — `p50_step_ms` is logged but not part of
  the keep criterion; latency is for ARCHITECTURE_STATUS report after Step 1.
- Cost-per-finding (vs token budget) — only meaningful with real LLM, skip.

## Risks (acknowledged, monitored)

1. **Path-token reformulation is artificial.** If the metric is dominated by
   "what tokens the path happens to contain" rather than retrieval-pipeline
   behavior, arms may show identical numbers (both arms get same path-tokens →
   same downstream queries). Mitigation: report both `hit_rate_at_step_1`
   (which IS pure single-shot) and `hit_rate_at_step_5`; if the gap between
   them is large the iteration policy is discriminating.

2. **Reformulation can overfit pool composition.** Reformulation tokens come
   from `hybrid_search` results. If those tokens steer the next query right back
   to the same files (over-anchor), iteration does no work. Mitigation: log
   per-step **unique-files-added** count; if it collapses to ~0 by step 3, the
   policy is broken.

3. **K_READ choice affects everything.** K=3 is a guess. If sanity check shows
   collapse, retune; document the retune.

4. **Lateral-iteration agents (the real PI workflow) involve grep + analyze_task
   + re-search; v1 only does re-search.** Real-agent steps-to-find probably ≤
   v1 steps-to-find. The metric is an upper bound on real-consumer cost. That is
   fine: arm-to-arm DELTA is still meaningful. Absolute numbers will be
   pessimistic.

## Expected next steps after this design lands

1. Implement `bench_steps_to_find.py` (Task #2).
2. Run n=20 baseline + assert-deterministic. Gate-check the 3 criteria. (Task #3.)
3. If green: full n=665 baseline + rerank-OFF arm. ~2 × 15 min = ~30 min.
4. If gap < single-shot −14.1pp gap: reranker partly redundant for iteration →
   ARCHITECTURE_STATUS update, decision to disable reranker becomes a real option.
5. If gap ≈ single-shot −14.1pp: reranker contribution is irreducible → keep ON
   as baseline, focus moves to Step 2 (JIRA-body enrichment).
