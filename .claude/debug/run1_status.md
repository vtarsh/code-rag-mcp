# Run 1 — status (2026-04-26 evening)

## Goal recap
First-ever successful end-to-end pod training cycle for 6 candidates.
3 docs-tower (nomic / mxbai-large / gte-base) + 3 reranker (L12 /
mxbai-rerank-base / bge-reranker-v2-m3). Defaults recipe (1 epoch,
lr=2e-5). Find candidates worth sweep-tuning in Run 2.

## Live results — DUAL-AXIS

### Docs eval (doc_intent_eval_v3.jsonl, n_eval=100)

| candidate          | R@10   | hit@5  | hit@10 | p50ms | p95ms |
|--------------------|--------|--------|--------|-------|-------|
| baseline L6 (prod) | ~0.25  | …      | …      | …     | …     |
| rerank-l12 (FT)    | 0.1891 | 0.3889 | 0.4222 | 596   | 964   |
| **rerank-mxbai (FT)** | **0.2609** | **0.4556** | **0.5778** | 632 | **851** |

### Code eval (code_intent_eval_v1.jsonl, n_eval=80) — LOCAL Mac MPS

| candidate          | R@10   | hit@5  | p95ms |
|--------------------|--------|--------|-------|
| baseline L6 (prod) | 0.1756 | 0.525  | **341** |
| **rerank-l12 (FT)** | **0.1869** | **0.55** | 500 |
| rerank-mxbai (FT)  | 0.1288 | 0.4875 | 1046  |

### Combined verdict
- **rerank-l12 (FT)**: docs **-6pp**, code **+1.1pp**, latency 1.5x. *Marginal code win, big docs regression.*
- **rerank-mxbai (FT)**: docs **+1pp**, code **-4.7pp**, latency 3x. *Docs win cancelled by code regression.*
- **Neither dominates the production L6.** Single-axis bench would have crowned mxbai falsely.

### Docs-tower candidates — all FAILED Run 1 (3 distinct bugs)
| candidate         | failure mode                                     | bug |
|-------------------|--------------------------------------------------|-----|
| docs-nomic-ft     | state_dict missing keys when loading from HF Hub | 6p  |
| docs-mxbai-ft     | NaN vectors in embeddings                        | 6o (FIXED, on_bad_vectors='drop' filter) |
| docs-gte-base-ft  | CUDA index out of bounds (tokenizer mismatch?)  | 6q  |

Defer docs candidates investigation to next iteration.

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
