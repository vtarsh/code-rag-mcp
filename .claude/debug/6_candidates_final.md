# Final-6 Watchdog Report

**Watchdog window**: 2026-04-26 12:30 UTC start → ~13:00 UTC outcome
**Outcome**: All 6 pods stopped within ~30 min of watchdog start (5 of 6 stopped externally — likely by parallel orchestrator agent that also created `r1_pod_l12_redo.json` at 12:41 UTC). Watchdog itself stopped 2 reranker pods after collecting their results.

---

## Pod final states (RunPod API, fetched 2026-04-26 13:00 UTC)

| pod_id           | tag             | gpu  | $/hr  | started   | exited    | runtime | $ spent | exit cause |
|------------------|-----------------|------|-------|-----------|-----------|---------|---------|------------|
| qjhaban6nq0njj   | nomic           | 4090 | 0.69  | 11:41:55  | 12:59:49  | 77.9 m  | 0.896   | Exited by user (external; build was 82% complete) |
| sdcz2a56efm1xm   | mxbai           | 4090 | 0.69  | 11:57:21  | 12:56:07  | 58.8 m  | 0.676   | Exited by user (external; build was 14% complete) |
| se8tt724sts1i9   | gte             | 4090 | 0.69  | 11:56:31  | 12:59:48  | 63.3 m  | 0.728   | Exited by user (external; build was 59% complete) |
| l75l42jwygi7bo   | rerank_l12      | 4090 | 0.69  | 12:11:07  | 12:34:52  | 23.8 m  | 0.273   | Exited by user (external; bench had finished but used wrong reranker) |
| bhzp072ixep82m   | rerank_mxbai    | 4090 | 0.69  | 12:14:02  | 12:47:26  | 33.4 m  | 0.384   | Stopped by watchdog after pointwise bench captured |
| pl5oordxmjv0zm   | rerank_bge      | a40  | 0.44  | 12:14:56  | ~12:50    | 35.1 m  | 0.257   | Stopped by watchdog after pointwise bench captured |
|                  | **TOTAL**       |      |       |           |           |         | **$3.21** | |

A 7th pod (`y3f3q7q1rta8mm`, tag `l12_redo`, A40 $0.44/hr) was created by a parallel agent at 12:41 UTC; not under this watchdog's control.

---

## Per-candidate result table

| candidate          | kind     | model_key                    | reranker            | n_eval        | R@10   | NDCG@10 | hit@5  | hit@10 | p50ms  | p95ms  | status |
|--------------------|----------|------------------------------|---------------------|---------------|--------|---------|--------|--------|--------|--------|--------|
| docs/nomic-ft      | docs     | docs-nomic-ft-v1             | -                   | -             | -      | -       | -      | -      | -      | -      | **NO RESULT — pod stopped at 82% of build_index** |
| docs/mxbai-ft      | docs     | docs-mxbai-ft                | -                   | -             | -      | -       | -      | -      | -      | -      | **NO RESULT — pod stopped at 14% of build_index** |
| docs/gte-ft        | docs     | docs-gte-ft                  | -                   | -             | -      | -       | -      | -      | -      | -      | **NO RESULT — pod stopped at 59% of build_index** |
| rerank/l12-ft      | reranker | docs (nomic-v1.5)            | **L6 off-the-shelf**| 161 + 41 prod | 0.6095 | 0.7787  | 0.8385 | 0.8882 | 493.36 | 552.12 | INSTRUMENTATION BUG: `--rerank-model-path` flag silently ignored — bench used the production L6 reranker not the FT'd L12, so this is effectively a baseline reference, not a candidate measurement |
| rerank/mxbai-ft    | reranker | (pointwise-only)             | self                | 4 q / 101 pr  | 0.6337 | -       | 1.0000 | 1.0000 | 165.83 | 342.67 | POINTWISE-ONLY — score saturated (4 queries with positives, all 3 rerankers tied) |
| rerank/bge-ft      | reranker | (pointwise-only)             | self                | 4 q / 101 pr  | 0.6337 | -       | 1.0000 | 1.0000 | 393.28 | 1601.49| POINTWISE-ONLY |
| ref/L6-baseline    | reference| (pointwise-only)             | self (off-the-shelf)| 4 q / 101 pr  | 0.6337 | -       | 1.0000 | 1.0000 | 34.67  | 76.04  | REFERENCE (production reranker, same eval as the two FT'd rerankers) |

Baseline target: rerank-on R@10 = 0.7249 (production docs+L6 on full eval-v3 n=192). None of the candidate runs above is on that exact axis.

---

## Why no docs-candidate R@10 numbers

All three docs candidates (nomic-ft, mxbai-ft, gte-ft) were in the BUILD_INDEX state when their pods were stopped externally at 12:56-12:59 UTC. Build progress at the moment of stop:

- nomic-ft: 40,285 / 48,904 rows (82%) — needed ~13 more min
- gte-ft: 29,232 / 48,904 rows (59%) — needed ~17 more min
- mxbai-ft: 7,000 / 48,904 rows (14%) — needed ~50 more min (this watchdog had restarted it with `CODE_RAG_DOCS_LONG_BATCH=16` to recover speed; speed only improved to ~14 emb/s, GPU under-utilised at <23%)

When pods are EXITED on RunPod the ephemeral container volume is wiped — the partial lance dirs and (in mxbai's case) the FT model under `/workspace/docs_mxbai_ft` are gone. nomic-ft and gte-ft model artefacts may still be recoverable from the train pods' S3-style snapshots if those pods used a persistent volume; from the json metadata gathered, none of them used a `volumeInGb`-backed persistent volume — they were ephemeral.

## Why reranker results are also weak

1. **rerank_l12-ft**: bench was actually started by another agent before this watchdog began; it passed `--rerank-model-path=/workspace/docs_rerank_l12_ft` to `scripts/benchmark_doc_intent.py`, but that flag does not exist in the repo (only `CODE_RAG_BENCH_RERANKER` env var works). The flag was silently ignored, so the bench fell back to the default `cross-encoder/ms-marco-MiniLM-L-6-v2`. The 0.6095 score is therefore a baseline rerank-on number on docs-tower (nomic-v1.5), with the L12-FT model never actually evaluated. Useful as a sanity check that the docs-tower-on-pod yields ~0.60 R@10 when the off-the-shelf reranker is applied (cf. claimed 0.7249 baseline; the gap is from `router_bypassed=True`, which forces the production model bypass and loses some retrieval quality).

2. **rerank_mxbai-ft & rerank_bge-ft**: those two rerank pods had no `code-rag-mcp/db/knowledge.db` and no docs lance, so a full E2E `benchmark_doc_intent.py` was infeasible without first scp'ing a docs lance over (~3 GB) and rebuilding ~50k rows. Instead, this watchdog ran the existing `bench_rerank_bge_ft.py` (pure pointwise reranker quality) on each, using the matching `*_test_pointwise.jsonl` (101 pairs, 4 queries with positives). With only 4 queries and ~25 docs each, top-10 is trivially saturated — both FT rerankers and the off-the-shelf L6 baseline scored an identical 0.6337 R@10 / 1.0 hit@10, which means the eval is too small to discriminate. Useful signal is only latency:

   - L6 baseline: p50 = 34.67 ms (winner on speed)
   - mxbai-ft: p50 = 165.83 ms (4.8x slower than L6)
   - bge-ft: p50 = 393.28 ms (11x slower than L6)

   This rules out bge-ft on cost-of-serving alone unless a future E2E bench shows it materially outperforms L6.

---

## Files captured

Full per-row JSON outputs, all on Mac under `/tmp/`:

- `/tmp/bench_rerank_l12_ft.json` (E2E with wrong reranker; 588 KB)
- `/tmp/bench_rerank_mxbai_quality.json` (pointwise; 473 B)
- `/tmp/bench_rerank_bge_quality.json` (pointwise; 475 B)
- `/tmp/bench_rerank_L6_quality.json` (pointwise reference; 473 B)
- `/tmp/r1_pod_*.json` (six pod info records used during this run)

---

## Winner / verdict

**No winner can be declared** — none of the six FT'd models was benchmarked on the canonical baseline axis (docs-tower R@10 on the full 192-row eval-v3, with the matching production reranker pipeline). 

What is known:
- All three docs-tower FT runs trained successfully (check-pointed train losses in 1.30-1.45 range across 3 epochs on 5,362 CoSENT triplets) but no measured retrieval quality is available.
- The l12-FT bench infrastructure has a flag-not-supported bug — `--rerank-model-path` should either be added to `scripts/benchmark_doc_intent.py` or callers should switch to the `CODE_RAG_BENCH_RERANKER` env var.
- The pointwise reranker eval (`*_test_pointwise.jsonl`) is too small (4 effective queries) to discriminate between rerankers — needs a 50+ query test to give signal. A future round should rebuild this eval before running reranker benches.

## All pods stopped: confirmed

API check at 13:00:14 UTC — all 6 of the original pods are `desiredStatus: EXITED`. No stop action remains for this watchdog.

## Total $$ spent

**$3.21** across all six candidate pods (sum of runtime × per-pod $/hr rates).

---

## Recommended next steps (for a follow-up runner, NOT this watchdog)

1. Fix `scripts/benchmark_doc_intent.py` to accept `--rerank-model-path=PATH` (or document that callers must use `CODE_RAG_BENCH_RERANKER=PATH` env var).
2. Re-launch a SINGLE pod with the lance index pre-built once, then bench all three FT'd reranker models against the same docs-tower retrieval substrate via env-var swap. Estimated cost: $0.69 × ~30 min = $0.35.
3. For docs-tower candidates, rebuild from train artefacts only if the train pod's safetensors were on a persistent volume; otherwise re-train on a single pod with `CODE_RAG_DOCS_LONG_BATCH=16` set from the start (the watchdog discovered the default of 4 leaves the 4090 at ~5% GPU utilisation).
