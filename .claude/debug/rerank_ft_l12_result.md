# Reranker FT — ms-marco-MiniLM-L-12-v2 on docs pairs

**Date**: 2026-04-26
**Verdict**: **REJECTED** (-11.5pp R@10 vs baseline)
**Pod cost**: ~$0.27 ($0.69/hr × 23 min)
**Pod**: `l75l42jwygi7bo` (rtx4090 secure-cloud, EUR-IS-1 NL) — STOPPED (EXITED)

## Setup

- **Base model**: `cross-encoder/ms-marco-MiniLM-L-12-v2` (33M params, 12-layer)
- **Train data**: 5362 docs triplets from `/tmp/r1_cosent_triplets_v3.jsonl`
  → 10,724 binary (query, doc, label) examples (positive label=1.0, negative label=0.0)
- **Code FT data NOT used**: `finetune_data_v8/manifest.jsonl` not present locally; trained docs-only
- **Hyperparams**: 3 epochs, batch_size=8, lr=2e-5, warmup_steps=100
- **Train time**: 5.5 min on RTX 4090
- **Output**: `/workspace/docs_rerank_l12_ft/` (129 MB, 8 files)
- **Loss curve**: 0.7479 → 0.5513 → 0.4751 → 0.3657 → 0.3379 → 0.2657 → 0.2503 → 0.2122 (steady decrease)

## Eval

Eval set: `profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl` (n=161 scoreable rows)
Bench: `scripts/benchmark_doc_intent.py --rerank-on --rerank-model-path=...`
(pod-side patch added `--rerank-model-path` flag; not pushed to repo per CONSTRAINTS)

## Results (apples-to-apples on same pod)

| Reranker | R@10 | NDCG@10 | Hit@5 | Hit@10 | p95 ms |
|---|---|---|---|---|---|
| **Baseline off-the-shelf MiniLM-L-6** | **0.7249** | **0.9681** | **0.9068** | — | 484 |
| **L-12 FT on 5362 docs pairs** (this run) | **0.6095** | 0.7787 | 0.8385 | 0.8882 | 552 |
| Δ vs baseline | **-11.5pp** | -19.0pp | -6.8pp | — | +14% |

Baseline R@10=0.7249 matches the prompt-stated production value (`reranker_ft_gte_v8`) — sanity confirms eval set is the right one. NDCG dropped most (-19pp) — the L-12 FT model not only loses recall but ranks much worse within top-10.

## Why it regressed

Same root cause as the doc-tower payfin/nomic-v2-moe rejections (memory `project_loop_2026_04_25.md`):

1. **Single-stratum overfit**: 5362 doc pairs are heavily provider/refund/webhook biased. The model learned to score those highly but lost the calibration that makes the off-the-shelf MiniLM a useful general-purpose reranker on the wider eval distribution.
2. **Binary label noise**: hard-negatives in `r1_cosent_triplets_v3.jsonl` are mined automatically — some "negatives" are likely related-but-not-canonical paths (the labeler-pool top-3 score 2 for the gold; second-best at 2 too). Training on label=0 for these signal-rich negatives teaches the wrong gradient.
3. **No code FT data**: workflow asked for combined code+docs but `finetune_data_v8/manifest.jsonl` was not present locally. Docs-only made the regression worse — the production winner is FT'd on code (904 pairs).
4. **L-12 ≠ better than L-6 here**: bigger model = more capacity to memorize noisy labels.

## Action items

- **DO NOT deploy** L-12 FT; production stays on `reranker_ft_gte_v8` (FT'd MiniLM-L-6 on code).
- Pattern reinforced: docs-only FT regresses on doc-intent eval. Five rejected candidates in a row (payfin-v0/v1-fixed, nomic-v2-moe, gte-large blocked, L-12 FT).
- If a reranker FT is attempted again, requirements:
  - Combined code (904) + docs (5362) pairs — not docs-only.
  - Validate hard-negatives have label-source != labeler_top_score≥2 (avoid signal noise).
  - Use the smaller L-6 baseline as the FT target (parity with production).

## Files

- `/tmp/bench_rerank_l12_ft.json` — full L-12 FT bench manifest
- `/tmp/bench_rerank_baseline_L6_off_the_shelf.json` — baseline same-pod control
- Pod `/workspace/train_l12.log` — training log (pod stopped, content lost on terminate)

## Pod lifecycle

- Started: 2026-04-26 12:11 UTC
- Stopped: 2026-04-26 12:34 UTC
- Duration: ~23 min
- Cost: ~$0.27 (cap was $2 own; well under)
- Cumulative RunPod spend (project): ~$1.97 of $15 budget
