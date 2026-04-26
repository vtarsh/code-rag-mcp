# Run 1 — status (2026-04-26 evening)

## Goal recap
First-ever successful end-to-end pod training cycle for 6 candidates.
3 docs-tower (nomic / mxbai-large / gte-base) + 3 reranker (L12 /
mxbai-rerank-base / bge-reranker-v2-m3). Defaults recipe (1 epoch,
lr=2e-5). Find candidates worth sweep-tuning in Run 2.

## Live results — docs eval (doc_intent_eval_v3.jsonl, n_eval=100)

| candidate          | kind     | base                              | R@10   | hit@5  | hit@10 | p50ms | p95ms | status |
|--------------------|----------|-----------------------------------|--------|--------|--------|-------|-------|--------|
| rerank-l12 (run1)  | reranker | ms-marco-MiniLM-L-12-v2           | 0.1891 | 0.3889 | 0.4222 | 596   | 964   | DONE (HF push fail; respawned) |
| rerank-mxbai (run1)| reranker | mxbai-rerank-base-v1              | **0.2609** | **0.4556** | **0.5778** | 632 | **851** | DONE (HF push fail; respawned) |
| rerank-bge         | reranker | bge-reranker-v2-m3                | …      | …      | …      | …     | …     | running (batch=4) |
| docs-nomic-ft      | docs     | nomic-embed-text-v1.5             | …      | …      | …      | …     | …     | retry (HF fix) |
| docs-mxbai-ft      | docs     | mxbai-embed-large-v1              | …      | …      | …      | …     | …     | retry (HF fix) |
| docs-gte-base-ft   | docs     | gte-base-en-v1.5                  | …      | …      | …      | …     | …     | retry (HF fix) |

Baseline reference (vanilla nomic + L6 reranker, eval-v3): R@10 = 0.2509.
Stratum-gated A2 (P10 deployed): R@10 = 0.2427.

## Code eval gap (KNOWN)
All Run 1 benches use `doc_intent_eval_v3` only. Reranker trained on
combined data (904 code + 5362 docs pairs); a docs win could mask code
regression. Pod can't host 88 GB `vectors.lance.coderank`, so code bench
runs locally on Mac via `scripts/local_code_bench.py` once a candidate's
FT'd model is on HF Hub.

## Bugs found + fixed live (Bug 6 series)
See `memory/project_run1_2026_04_26.md` for the table.

## Cost so far
- ~$0.30 burned on infra-bug failures (clean pod stops)
- ~$1.0-1.5 on successful train+bench runs
- Banked: ~$5.20 -> ~$3.5-4 left after current 6 retries land

## Decision points after all 6 land
1. Run code bench locally for top-2 reranker survivors + top-2 docs survivors (~20 min, $0).
2. debate-planning agent (3 opus): pick Run 2 sweep dimensions per surviving candidate (LR / epochs / batch / loss).
3. Run 2 spawns adjusted candidates only (no full 6 again).

## Files of record
- `bench_runs/run1_<tag>.json` — per-candidate raw bench (cp from /tmp before pod stop wipe)
- `scripts/runpod/oneshot_rerank.py` + `scripts/runpod/oneshot_docs.py` — orchestrators
- `scripts/local_code_bench.py` — post-hoc code-axis bench on Mac
- `memory/project_run1_2026_04_26.md` — Bug 6a-6n full table + lessons
