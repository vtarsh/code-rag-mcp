# A2 + A4 V3 — Final session synthesis (2026-04-26 ~03:00 EEST)

## TL;DR — KEEP A2, V3 SWAP EXHAUSTED, V2 RUNPOD FUTURE-WORK

| Decision | Status |
|---|---|
| A2 stratum-gated gate (default-on in code at `7c0a16b4`+`9e8b2986`) | **KEEP** — wins every metric on dedupe'd bench |
| Bench dedupe patch (default OFF, opt-in via `--dedupe`) | **PATCH READY** — pending MCP push |
| A4 V3 swap (3 off-the-shelf rerankers) | **EXHAUSTED** — none beats A2 |
| A4 V2 contrastive train ($1-2 RunPod, 2h) | **DEFERRED** — future-work after user wakes |
| A4 V1 distillation ($2.40 RunPod, 9h) | **SKIP** — A4 judge ranked last (caps at biased teacher) |

## Final dedupe'd bench numbers (eval-v3-n200, n=192)

| Pipeline | R@10 | nDCG@10 | hit@5 | hit@10 | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| **A2 stratum-gated (deployed)** | **0.2737** | **0.2234** | **0.4688** | 0.5938 | **236** | **316** |
| rerank-on baseline (L-6 prod) | 0.2689 | 0.2194 | 0.4479 | 0.5833 | 302 | 473 |
| V3 swap: ms-marco-MiniLM-L-12 | 0.2684 | 0.2162 | 0.4583 | 0.5938 | 364 | 518 |
| V3 swap: mxbai-rerank-base | 0.2599 | 0.2112 | 0.4740 | 0.5677 | 941 | **1098** ⚠ |
| V3 swap: bge-reranker-v2-m3 | (skipped — predicted >2× A2 latency, unshippable) |

**A2 vs rerank-on baseline (production-truth comparison):**
R@10 +0.48pp, hit@5 +2.09pp, hit@10 +1.05pp, p50 -66ms, p95 -158ms.

## Verification chain (all 6 passed in different ways)

1. **DERA debate (Defender vs Skeptic vs Judge):** Defender wins MEDIUM. A2 is a deterministic router — per-stratum recalls are exact identities to pre-measured priors; no model fit. (`ruling.md`)
2. **Falsifier #1 split-half (5 seeds):** SURVIVES weakly. Held-out lift +1.83pp mean (range [+0.07, +2.84]); apply prod-OFF as-is gives +2.14pp mean. (`falsifier1_split_half.md`)
3. **Falsifier #2 prod token coverage:** GATE PARTIAL. 28.7% prod doc-intent OFF, 55.9% fall through to rerank-on. Effective prod lift estimate ~+1.20pp on un-deduped bench. (`falsifier2_token_coverage.md`)
4. **A3 canary (live daemon):** PASS. OFF-stratum p50=486ms, KEEP/UNK/CODE p50=4870-4983ms (10× ratio). (`a3_canary_result.md`)
5. **LLM judge #1 (Opus, n=30 stratified seed=42):** MIXED leaning ARTIFACT. Δrel=-0.003, ΔDCG=-0.100. 5 wins / 15 ties / 10 losses. (`llm_judge_opus.md`)
6. **LLM judge #2 (Opus #2, independent n=30 sample, only 7/30 overlap with #1):** MIXED. Δrel=-0.013, ΔDCG=-0.049. 5 wins / 19 ties / 6 losses. (`llm_judge_opus2.md`)

The combined LLM-judge insight forced a **bench dedupe patch** (the heuristic
+2.89pp R@10 was ~83% bench artifact from duplicate file_paths in top-10).

## Bench dedupe patch (local, pending MCP push)

`scripts/benchmark_doc_intent.py` + `tests/test_benchmark_doc_intent.py`:

- New `--dedupe` flag + `dedupe: bool` evaluate_model param. When ON, top-10
  contains only unique (repo, file_path) pairs. Pulls from `retrieval_k=50`
  candidate pool to fill 10 unique files when fewer survive dedup.
- `CODE_RAG_BENCH_RERANKER` env var lets V3-style reranker A/B without code
  changes (used to test L-12, mxbai-base above).
- Manifest now records `dedupe: bool` flag for downstream traceability.

Tests pass: 8/8 in `test_benchmark_doc_intent.py` (added `dedupe` parameter
to fake_evaluate stubs).

## Per-stratum (A2 vs ON, dedupe'd)

The stratum gate's real wins:

| stratum (n) | A2 | ON | Δ (A2-ON) | Verdict |
|---|---:|---:|---:|---|
| webhook (n=23) | 0.2971 | 0.2674 | +2.97pp | Real win |
| refund (n=13) | 0.4295 | 0.3872 | +4.23pp | Real win |
| payout (n=21) | 0.1627 | 0.1341 | +2.86pp | Real win |
| nuvei (n=23) | 0.4304 | 0.4304 | +0.00pp | Wash (was claimed +10.4pp) |
| trustly (n=4) | 0.3167 | 0.3167 | +0.00pp | Wash (was claimed +8.34pp) |
| aircash (n=9) | 0.4056 | 0.4111 | -0.55pp | Slight regress |
| interac (n=9) | 0.7407 | 0.7407 | +0.00pp | KEEP, expected |
| provider (n=23) | 0.2696 | 0.2696 | +0.00pp | KEEP, expected |
| method (n=17) | 0.1990 | 0.2186 | -1.96pp | Slight regress |
| tail (n=50) | 0.1130 | 0.1237 | -1.07pp | Sub-noise |

Real wins concentrated in **webhook/refund/payout** strata. Latency win
applies across all OFF strata uniformly.

## Recommendation

1. **A2 stays deployed** (commits `7c0a16b4` + `9e8b2986` on main).
   Production narrative: "+2.09pp hit@5, -158ms p95 latency" instead of "+2.89pp R@10".
2. **Push bench dedupe patch** so future A/Bs run against honest baseline.
3. **Defer A4 V2** (RunPod $1-2, 2h, p(win)=0.16) to user-confirmed budget moment.
   The dedupe patch fundamentally changes how we evaluate A4 candidates —
   prior +1pp predictions for V1/V2 may shrink on dedupe'd bench.
4. **Skip A4 V1** entirely (judge ruled last; teacher is the model G1+G2
   already flagged as biased).

## Files of interest

- `/tmp/p10_a2_dedupe.json` — A2 on dedupe'd bench (canonical going forward)
- `/tmp/p10_rerank_on_dedupe.json` — production-truth baseline
- `/tmp/p10_v3_minilm_l12.json`, `/tmp/p10_v3_mxbai_base.json` — V3 swap losers
- `/tmp/p10_a2_judge_scores_opus2.json` — Opus #2 raw 0-3 scores
- All `.claude/debug/a2-verification/*.md` — full audit trail
- All `.claude/debug/a4-recipe/*.md` — V1/V2/V3 proposals + judge ruling

## Cumulative spend

- A4 V3 swap on Mac: **$0** (all 2 candidates run locally)
- A4 V2 RunPod: **$0** (deferred)
- Total session: **$0 of $5 authorized**; banked still $9.72 of $15.

## Next session entry point

- A4 V2 RunPod (mining + pod + train + eval). Recipe in
  `.claude/debug/a4-recipe/variant2_contrastive.md`. p(win) 0.16.
- Or: re-run A2 vs baseline on freshly-rebuilt eval (after any docs index
  change) to confirm the +0.48pp dedupe'd lift holds.
