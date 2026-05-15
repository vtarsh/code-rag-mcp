# A4 V2 status after V3 finding (2026-04-26 ~03:00 EEST)

## Current decision: V2 DEFERRED, requires re-design vs original proposal

The original `variant2_contrastive.md` proposal specified `BAAI/bge-reranker-v2-m3`
as the student base. **V3 swap empirically established that bge-m3 (568M
params) and mxbai-rerank-base (184M params) both breach the 2× A2 latency
gate** — mxbai measured 1098 ms p95 vs A2's 316 ms (3.5×); bge-m3 is 3×
larger than mxbai → predicted p95 ≥ 3000 ms (10× A2).

**V2's biggest risk listed in the proposal (`variant2_contrastive.md` §Risks
item 1) is now CONFIRMED via V3 measurement.** Training a base that's already
2-10× over the latency gate cannot produce a deployable artifact regardless
of recall.

## Three remaining V2 paths

| Path | Base | Cost | p(win, dedupe'd bench) | Latency-shippable? |
|---|---|---:|---:|---|
| V2-A: original bge-m3 | bge-m3 (568M) | $1-2 RunPod | ~0.10 | NO (latency breach) |
| V2-B: smaller base | ms-marco-MiniLM-L-12 (33M) | $1-2 RunPod | ~0.05 | YES |
| V2-C: same-family base | ms-marco-MiniLM-L-6 retrain | $1-2 RunPod | ~0.07 | YES |

**Tradeoff:** V2-A could give the strongest research signal (does docs-domain
training of a heavy base beat off-the-shelf?) but produces unshippable
artifact. V2-B/V2-C are deployment-feasible but predicted to win < +1pp on
the dedupe'd A2 baseline (R@10 = 0.2737), well below the AND-gate threshold.

## Why pause for user decision

The V3 swap empirically narrowed the design space. The original V2 proposal's
implicit assumption (bge-m3 latency would be acceptable) is now disproven on
this hardware. Continuing V2 requires picking V2-B or V2-C with adjusted
expectations, or running V2-A as research-only (no deploy possible without
a latency reduction technique like quantization or pruning, which adds 1-2
weeks of work outside this session).

## Recommended user choice

- **Skip V2** entirely. Prior dedupe analysis showed A2's real production
  win is **latency** (-158 ms p95) plus a **small recall lift** (+0.48pp on
  honest dedupe'd bench). The A4 path has effectively converged on
  "reranker is not the bottleneck for docs" — the bottleneck is bench
  artifacts (now fixed) + the 55.9% prod long-tail outside any current
  stratum gate.
- **Pivot to bench/eval improvements** instead: re-build eval-v3 with
  hand-graded labels (the heuristic R@10 ≠ LLM-rel correlation Pearson r=0.446
  per `p10-llm-judge-report.md` G2 means current eval has ±2pp inherent
  noise). With cleaner labels, future A/Bs become trustworthy.
- **Or pursue V2-B (MiniLM-L-12 contrastive)** as an honest small experiment
  ($1-2, 2h). p(win)=0.05 but the data point closes the recipe-class
  exhaustion question fully.

## What this session shipped

1. ✅ A2 stratum-gated rerank-skip (commits `7c0a16b4` + `9e8b2986`)
2. ✅ Bench dedup patch (commit `73ccb44a`) — closes the 83% bench artifact
3. ✅ Verified A2 via 6-track parallel: DERA + 2 falsifiers + canary + 2 LLM judges
4. ✅ V3 swap exhausted (3 candidates, no winner)
5. ✅ V1 distillation skipped per A4 judge ranking (caps at biased teacher)
6. ⏸ V2 contrastive deferred (latency-risk pivot needed)

## Open decisions for user

1. V2-B or V2-C run? ($1-2, 2h, p(win)=0.05-0.07 on dedupe'd bench)
2. Re-build eval-v3 with hand-graded labels? (no $$ but human time)
3. Expand A2 token map to capture prod long-tail? (Falsifier #2's recommendation;
   risk: per Falsifier #1 seed=1, adding `tail` to OFF causes -1.5pp collapse)

Each is non-blocking on the others. Pick one (or none) when waking.
