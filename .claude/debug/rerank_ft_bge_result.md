# pod-rerank-ft-bge Result — BAAI/bge-reranker-v2-m3 fine-tune

**Date:** 2026-04-26
**Pod:** pl5oordxmjv0zm (code-rag-rerank-bge), A40 48GB, $0.44/hr
**Runtime:** ~34 min total, $0.25 spent (well under $3 hard cap)
**Status:** TRAINING SUCCEEDED, BENCHMARKED, MODEL DESTROYED (no persistent volume — pod restart wiped /workspace).

---

## TL;DR

| metric                   | baseline (untrained bge-v2-m3) | fine-tuned bge-v2-m3 ft  | delta       |
|--------------------------|-------------------------------:|-------------------------:|-------------|
| R@10 (train, overfit)    |                         0.8985 |                   0.9961 | +0.0976     |
| Hit@5  (train, overfit)  |                         0.9625 |                   1.0000 | +0.0375     |
| Hit@10 (train, overfit)  |                         0.9989 |                   1.0000 | +0.0011     |
| latency p50 (A40 GPU)    |                       277.4 ms |                 279.3 ms |    +1.9 ms  |
| latency p95 (A40 GPU)    |                       285.0 ms |                 286.7 ms |    +1.7 ms  |
| latency mean (A40 GPU)   |                       243.5 ms |                 245.0 ms |    +1.5 ms  |
| latency max (A40 GPU)    |                       290.6 ms |                 292.2 ms |    +1.6 ms  |
| load time (cold start)   |                         4.76 s |                   2.88 s | -1.88 s     |

**LATENCY VERDICT: A40 GPU latency p95 = 287 ms — comfortably under the 1500 ms cutoff.**

**On a Mac (CPU/MPS)** the model would be **substantially slower** than these GPU numbers (estimated 5-10x = ~1.5-3 s p50 per query). CPU inference test could not be executed (pod auto-stopped at the 33-minute mark before that test ran). Without a persistent volume, the FT model was destroyed on pod restart, so no cross-platform validation possible.

**Quality verdict:** R@10 / Hit@5 numbers are on TRAIN data — they show the model overfit to the training corpus (R@10 = 0.996 is a red flag). True production-quality verdict requires running the FT model against eval-v3-n200 with the full retrieval pipeline (hybrid + bge as reranker), which was not possible since model files were destroyed. **Recommend re-run with persistent volume to capture eval-v3 numbers.**

---

## Workflow executed (per task spec)

| step | what | result |
|---|---|---|
| 1 | Add A40 to GPU presets in `pod_lifecycle.py` | DONE (preset = NVIDIA A40, 48GB, est. $0.39/hr; actual $0.44/hr in EUR-NO-1) |
| 2 | Launch A40 pod with HF_TOKEN, time_limit=180m, spending_cap=$3 | DONE (pod_id `pl5oordxmjv0zm`, ssh `194.68.245.198:22074`, costPerHr $0.44) |
| 3 | Save pod info to `/tmp/r1_pod_rerank_bge.json` | DONE |
| 4 | Install deps on pod (`sentence-transformers>=4.0`, `transformers>=4.38`, etc.) | DONE — st 5.4.1, transformers 5.6.2, torch 2.4.1+cu124 |
| 5 | scp `scripts/finetune_reranker.py` + `finetune_data_v8/{train,test}.jsonl` to pod | DONE |
| 6 | Smoke test (1 epoch, batch=2, fp16, lambdaloss) | DONE — ran ~5 min, killed before 396 steps to save time |
| 7 | Real training (`--base-model=BAAI/bge-reranker-v2-m3`, `--epochs=2`, `--batch-size=4`, `--max-length=384`, `--loss=lambdaloss`, `--fp16`, `--gradient-checkpointing`) | DONE — completed in 733.4s (~12.2 min), reload smoke score 0.998 (positive sample) |
| 8 | Bench FT model on test.jsonl (101 rows → 4 unique queries after bucketing) | DONE — too few queries for stable p95 / R@10 |
| 9 | Bench FT model on train.jsonl (879 rows → 879 query batches, 12,190 pairs) | DONE — p95=287ms, R@10=0.996 (overfit, expected) |
| 10 | Bench BASELINE on same train.jsonl | DONE — p95=285ms, R@10=0.899, Hit@5=0.963 |
| 11 | Bench FT on CPU to estimate Mac latency | FAILED — pod auto-stopped during this run (cause unknown, "Exited by user" in API log; possibly the parent agent stopped it) |
| 12 | Restart pod + retrieve files | DONE — pod resumed but `/workspace` was empty (no persistent volume); FT model permanently lost |
| 13 | Terminate pod | DONE |

---

## Bench raw output

### Fine-tuned model on train.jsonl (879 queries, 12,190 pairs)
```json
{
  "model": "/workspace/docs_rerank_bge_ft",
  "device": "cuda",
  "n_test_queries": 879,
  "n_pairs": 12190,
  "max_length": 384,
  "load_seconds": 2.877,
  "per_query_latency_ms": {
    "min": 37.91,
    "p50": 279.29,
    "p95": 286.72,
    "mean": 245.01,
    "max": 292.18,
    "n": 879
  },
  "quality": {
    "n_queries_with_positive": 879,
    "recall_at_10": 0.9961,
    "hit_at_10": 1.0,
    "hit_at_5": 1.0
  },
  "gpu": "A40",
  "timestamp": "2026-04-26T12:39:24Z"
}
```

### Baseline (untrained BAAI/bge-reranker-v2-m3) on same train.jsonl
```json
{
  "model": "BAAI/bge-reranker-v2-m3",
  "device": "cuda",
  "n_test_queries": 879,
  "n_pairs": 12190,
  "max_length": 384,
  "load_seconds": 4.762,
  "per_query_latency_ms": {
    "min": 36.9,
    "p50": 277.44,
    "p95": 285.04,
    "mean": 243.45,
    "max": 290.58,
    "n": 879
  },
  "quality": {
    "n_queries_with_positive": 879,
    "recall_at_10": 0.8985,
    "hit_at_10": 0.9989,
    "hit_at_5": 0.9625
  },
  "gpu": "A40",
  "timestamp": "2026-04-26T12:43:21Z"
}
```

### Training summary (from `/workspace/docs_rerank_bge_ft/training_summary.json`, captured before wipe)
- duration_seconds: 733.4 (~12.2 min)
- final_val_loss: null (lambdaloss-friendly — no val loss reported)
- reload_smoke_score: 0.9976 (model reloads cleanly; positive sample scored ~1.0)
- training rows: 791 train + 88 val (val_ratio=0.1)
- hyperparams: epochs=2, batch_size=4, lr=2e-5, max_length=384, fp16=True, grad_ckpt=True, optim=adamw_torch, warmup=50, loss=lambdaloss
- model size: 2.27 GB FP32 safetensors

---

## Key findings

1. **Latency on A40 GPU is fine** — p95 = 287 ms, well under 1500 ms threshold. **NOT TOO SLOW for production on GPU.**
2. **Latency comparison vs ms-marco-MiniLM-L-6-v2 (current production):** the current production reranker has p95 = 710 ms on Mac MPS (per `/tmp/bench_v3_n200_docs_e2e.json`); bge-v2-m3 on A40 GPU is **2.5x faster than current Mac MPS production**, but production currently runs on Mac (no GPU). **On Mac CPU/MPS, bge-v2-m3 will likely be 5-10x slower than ms-marco-L-6** (568M vs 23M params). **Production-truth latency assessment requires Mac/MPS bench, not pod GPU.**
3. **Quality numbers are unreliable** — measured on TRAIN data (the 879 rows the model trained on). R@10=0.996 is overfitting, not a real signal. Cannot disprove or confirm a quality lift over baseline without eval-v3-n200 run, which requires the full retrieval pipeline + a persistent FT model artifact.
4. **Pod ephemerality bit us** — without a persistent volume mount, restart wiped `/workspace`. For future reranker FT runs, attach a network volume (`volumeInGb` > 0) OR push model to HF Hub immediately after training.
5. **Cost contained:** $0.25 of the $3 cap (~8% of budget consumed). $2.75 banked.

---

## Recommended next steps (gated on user GO)

### Option A — re-run with persistent storage + HF Hub push ($0.50 est, ~20 min)
- Launch fresh A40 with `volumeInGb=20` mount at `/workspace`
- Repeat training (12 min)
- Add `--out=hf:vtarsh/pay-com-rerank-bge-v0` to push to HF on completion
- Then bench against eval-v3-n200 ON POD (per coordinator directive — never on Mac)

### Option B — accept current data + decline deploy
- The latency hypothesis ("too slow for production") was the original rejection reason. A40 GPU p95 = 287 ms refutes that on GPU.
- But Mac CPU latency (the actual production environment) was never measured. Without that number we cannot recommend deploy.
- If P10 quickwin (rerank-skip on doc-intent) holds, the cross-encoder choice (bge vs ms-marco) becomes moot for doc-intent queries anyway.

### Option C — pivot to a Mac-friendly distillation
- Use bge-v2-m3 as a teacher to distill into a small student on the same 879-row corpus
- Student size ~80M params (bge-small range)
- Latency on Mac CPU ≈ 50-100 ms p95 (vs ms-marco-L-6 ~250-400 ms)
- Out of scope for this task; queue under P11 if recall lift on eval-v3 from B is sustained.

---

## Files of interest (now-existing on this Mac)

- `/Users/vaceslavtarsevskij/.code-rag-mcp/scripts/runpod/pod_lifecycle.py` — A40 preset added (one-line addition at L57-60)
- `/Users/vaceslavtarsevskij/.code-rag-mcp/.claude/debug/rerank_ft_bge_result.md` — this file
- `/tmp/r1_pod_rerank_bge.json` — terminal pod info (pod terminated)
- `/tmp/bench_rerank_bge_ft.py` — bench script (also was on pod, now wiped)

## Files that no longer exist (pod was terminated)
- `/workspace/docs_rerank_bge_ft/` — fine-tuned model (2.27 GB)
- `/workspace/rerank_bge/` — training script + data + bench script
- `/tmp/bench_rerank_bge_ft.json` — final bench JSON (numbers captured in this report)
- `/tmp/bench_rerank_bge_baseline.json` — baseline bench JSON (numbers captured in this report)

---

## Latency summary against task spec

> "If LATENCY p95 > 1500ms: flag as **too slow for production** but report numbers anyway."

**A40 GPU p95 = 287 ms — NOT too slow on GPU.** But the task statement implies production = Mac. The honest answer requires Mac CPU/MPS bench, which was prevented by pod termination. **Recommendation: do not promote to production without Mac-side latency measurement first.**
