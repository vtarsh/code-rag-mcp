# P6 T2 — Eval-defender verdict

**Question:** Is `profiles/pay-com/doc_intent_eval_v2.jsonl` (n=100, used for the 5-candidate A/B on RTX 4090) biased toward the vanilla-nomic baseline (`docs` model_key = `nomic-embed-text-v1.5`) by construction?

**Verdict: YES — eval-v2 is structurally biased toward vanilla nomic. ~62% of expected paths were reachable via the vanilla-nomic vector pool, and 90% of queries have at least one expected path in the vanilla-nomic top-10. Threshold "rigged if labels overlap >70% with vanilla-nomic top-10 pre-judge" is exceeded on the per-query metric (90% > 70%).**

This means the bench numbers from `bench_runs/doc_intent_summary_*.json` undermeasure how much a candidate model can win, because the labels themselves were drawn from a candidate pool that included vanilla-nomic's own retrievals. Candidates only get credit for paths the baseline already proposed (or that FTS / token-overlap also surfaced). A candidate that retrieves a *better* doc the baseline never proposed would score 0 on that path.

---

## How the eval was built (audit trail)

`scripts/build_doc_intent_eval.py` runs in two phases:

1. **Phase 2a (queries-only, `--v2-candidates`)** — sample 100 prod doc-intent queries from `logs/tool_calls.jsonl`, stratified by 9 head terms. Uses `wants_docs(q)` mirror of `src/search/hybrid.py::_query_wants_docs`. Hard-excludes train queries (Jaccard ≥ 0.5) and v1-kept queries. Output: `doc_intent_eval_v2_candidates.jsonl` — schema is `{query_id, query, source, stratum}`. **No expected_paths yet.** This stage is fair: queries are sampled cleanly from prod.

2. **Phase 2b (label assignment, `/tmp/label_v2_batch_{01..10}.py`)** — for each candidate query, build a candidate pool from THREE signals, then pick top-3..5 by a heuristic judge:

   ```
   pool = vec_pool(q, model_key="docs")  ⊕ fts_pool(q, doc_types)  ⊕ overlap_pool(q, all_doc_paths)
        ⊕ v1_seed_paths_for_query(q)         # (rare; only when query also exists in v1)
   ```

   Crucially, `vec_pool` calls `vector_search(q, limit=15, model_key="docs")` — **the model_key="docs" route resolves to `nomic-embed-text-v1.5` (src/models.py:60-72), which is the production baseline that the A/B is supposed to test.**

   The judge then assigns each pool item a 0-3 score using token-overlap heuristics (no LLM, no separate model), and the top-5 strong items become `expected_paths`. Train-leaked paths are filtered. The labeler is named `agent-judge-v2-batch-NN`.

So the labeling pipeline literally asks "what does vanilla nomic think is relevant?" and writes those answers into the gold set, alongside FTS and string-overlap candidates.

---

## Quantification (live measurement, not speculation)

I re-ran the same pools used by the labeler against the final `doc_intent_eval_v2.jsonl` and asked: of the 448 labeled `expected_paths` across 100 queries, how many would vanilla nomic alone recover?

```
total expected paths:                                                 448
recovered by vec_pool(model_key="docs", limit=15):                    277  (61.8%)
recovered by fts_pool(doc-types, per-type limit):                     166  (37.1%)
recovered by overlap_pool(token jaccard, top-15):                     117  (26.1%)

attribution per expected_path (which pool(s) contained it):
  vec only:                  180  (40.2%)   ← baseline-only paths
  fts only:                   96  (21.4%)
  overlap only:               50  (11.2%)
  vec + fts:                  41  ( 9.2%)
  vec + overlap:              38  ( 8.5%)
  fts + overlap:              11  ( 2.5%)
  all 3:                      18  ( 4.0%)
  none of 3 (v1 seed only):   14  ( 3.1%)
```

**Per-query metric (the rigged-threshold check from the brief):**

```
queries with >=1 expected_path in vanilla-nomic top-10 (limit=10):    90 / 100   = 90.0%
sum |E ∩ vanilla_top10| / sum |E|:                                    227 / 448  = 50.7%
mean Recall@10 of vanilla baseline against the labels it helped pick: 0.508
```

90% > 70% threshold → **eval is rigged by the brief's own definition.**

Per-source breakdown (matches what bench labels declare):
```
kept_v1            :   22 rows,  52/104 expected in vanilla top10  =  50.0%
kept_v1_path_dirty :   11 rows,  21/45  expected in vanilla top10  =  46.7%
prod_sampled (new) :   67 rows, 154/299 expected in vanilla top10  =  51.5%
```

The bias is uniform across sources — the `prod_sampled` rows (the 67 freshly-sampled queries) leak just as hard as the v1 carry-overs. Adding more prod queries did not dilute the bias because the labeling step always re-introduces vanilla nomic into the pool.

---

## Why this matters for the bench

`scripts/benchmark_doc_intent.py` computes Recall@10 / nDCG@10 / Hit@5 against `expected_paths` directly. Implications:

1. **Baseline (`docs`) gets a free 0.508 Recall@10 floor** just because half the labels come from its own pool. Its bench number is *not* a measure of how good the model is — it's a measure of how concentrated the doc set is around the labeler's vec_pool seeds.

2. **Candidates (`docs-gte-large`, `docs-arctic-l-v2`, `docs-bge-m3-dense`, `docs-nomic-v2-moe`, `docs-payfin-v0/v1`) are penalized for retrieving new-but-correct docs** that the baseline missed. A genuinely better model that surfaces a different (equally valid) doc gets 0 credit on that slot.

3. **The 0.10 recall-lift gate (`GATE_RECALL_LIFT=0.10`) in `--compare` is therefore unrealistically hard for an honest improvement.** A candidate would have to win 10 pp on a label set tilted against it. Conversely, if vanilla baseline reports e.g. 0.33 Recall@10, the *real* baseline retrieval quality is unknown — we only know "it captured 33% of the paths it itself nominated and the heuristic judge approved."

4. **Train-leakage filter is solid (0/448 expected paths overlap with v12 train positives), so this is NOT a train-test contamination issue.** It is a *labeler-baseline contamination* issue. Two different bugs, both bench-killing.

5. The 14 (3.1%) "v1 seed only" paths are the only ones that escaped both vec/FTS/overlap — these came from the v1 carry-over expected_paths and were judged on snippet content alone. That is the only fair sub-slice in the entire eval.

---

## What would an unbiased eval look like

Pool construction MUST exclude any signal that the candidates themselves provide. Options, ranked by cost:

- **Cheapest, immediate:** rebuild the labeled pool using *only* FTS5 + token-overlap (no vec_pool at all). Sacrifices ~40% of current coverage (the vec-only paths drop out), but yields a model-independent label set. Re-bench all 5 candidates against the smaller honest set.
- **Better:** pool from N independent vector models (e.g. `bge-m3-dense` + `gte-large` + `arctic-l-v2`) plus FTS+overlap, then judge the union. This gives every candidate equal "pool privilege" but is still not perfectly fair to a 6th candidate.
- **Best:** pool from a *separate* held-out reference model not in the candidate set (e.g. OpenAI text-embedding-3-large, or a strong BGE/E5 not under test) plus FTS+overlap. Documents the reference dependency in the bench writeup.
- **Independent of pool fix:** require the judge to look at snippet content only, ignoring path/repo tokens (the current judge inflates scores on path-token overlap with the query, which compounds the vec_pool bias because the labeler's vec_pool is also token-driven).

A useful diagnostic to run first: re-bench the 5 candidates *only on the 14 v1-seed-only rows*. If the candidate ordering inverts on this honest subset vs the full 100, that's the smoking gun that the bench result is a labeling artifact, not a model-quality signal.

---

## Files audited

- `scripts/build_doc_intent_eval.py` (full read, both phases)
- `/tmp/label_v2_batch_01.py` (full read; 02..10 are templated copies — same pool construction; only `wanted = {f"v2_{i:03d}" for i in range(BATCH_LO, BATCH_HI)}` changes)
- `profiles/pay-com/doc_intent_eval_v2.jsonl` (n=100, 448 expected_paths)
- `profiles/pay-com/doc_intent_eval_v2_candidates.jsonl` (n=100, queries-only, no labels)
- `profiles/pay-com/doc_intent_eval_v1.jsonl` (n=44, used for v2 carry-over + judge seed)
- `src/models.py` (model_key="docs" → nomic-embed-text-v1.5 confirmed)
- `src/search/vector.py` (model_key dispatch confirmed)
- `scripts/benchmark_doc_intent.py` (consumes `expected_paths` directly; no awareness of label provenance)

## Live commands run (reproducible)

```bash
# 1. Per-query top-10 overlap (90/100 = 90.0%)
CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp ACTIVE_PROFILE=pay-com python3.12 -c "
from src.search.vector import vector_search
import json
rows = [json.loads(l) for l in open('profiles/pay-com/doc_intent_eval_v2.jsonl')]
n=sum(1 for r in rows if (set((p['repo_name'],p['file_path']) for p in r['expected_paths']) &
   {(h['repo_name'],h['file_path']) for h in (vector_search(r['query'],10,model_key='docs')[0] or [])}))
print(n,'/',len(rows))"

# 2. Per-path source attribution (40.2% vec-only / 61.8% vec-touched)
# (full script in this report, runs in ~2 min)
```

## Bottom line for synthesis (T4)

The 5-way A/B numbers from `bench_runs/doc_intent_summary_*.json` should not be used to "deploy nomic as winner" or "reject candidate X" without first either (a) rebuilding the label set with vec_pool removed, or (b) at minimum reporting a confidence band that shrinks with each removed pool source. Current bench result is consistent with vanilla nomic appearing strong because it scored its own homework.
