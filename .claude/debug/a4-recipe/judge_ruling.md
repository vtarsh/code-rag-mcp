# A4 architecture debate — Judge ruling

## Ranking (best first)
1. **V3 Off-the-shelf swap** — score 7.6 / confidence HIGH
2. **V2 Contrastive** — score 6.4 / confidence MEDIUM
3. **V1 Distillation** — score 4.8 / confidence MEDIUM

## Recommended execution order
**V3 first** ($0, ~1.5 h Mac, 3 candidates). If any candidate clears AND-gate (Δ R@10 ≥ +1pp, no stratum < −2pp on n≥8, p95 ≤ 2× A2 = ≤682 ms) → SHIP, end. If best Δ ∈ [+0.0, +1.0pp] (signal but sub-threshold) → run **V2** ($1–2, 2 h pod) using V3's best base as the starting checkpoint. If V3's best Δ ≤ 0 OR V2 also misses → **skip V1** entirely (its capped-at-teacher ceiling cannot exceed A2 by ≥+1pp on the docs↔code axis G1+G2 named). Re-evaluate after V2 fails: corpus-side fixes (content-tower content_boost on the 5 REAL ON-win queries from G1) are the next sane lever, not more reranker spend.

## Reasoning
V3 dominates cost-per-pp-of-expected-lift. Its midpoint Δ R@10 is +1.0–2.5 pp at $0; expected value ≈ +1.5 pp / $0 ≈ infinite ratio. V2 claims +0.10 R@10 (10 pp) at $1 with p(win)=0.16, giving ~$0.625/pp expected. V1 quotes +1.5 pp midpoint at $2.40 with p(win)=0.16, giving ~$1.60/pp expected — and crucially V1 is **structurally capped at the teacher** (`variant1_distillation.md:9,18`), the very `cross-encoder/ms-marco-MiniLM-L-6-v2` that G1+G2 caught injecting cross-provider noise (`p10-llm-judge-report.md:14, p10-llm-judge-report.md:131-148`). Distilling that bias into a smaller student inherits the failure mode.

V3's mechanism strength is mid: stronger generic models (bge-reranker-v2-m3, mxbai-base) have seen 100+ datasets vs prod's MS-MARCO-only (`variant3_swap.md:13, :22`) and may amplify the ON-positive direct-rate signal G1 found (+18 pp direct-rate, +0.67 DCG; `p10-llm-judge-report.md:55-58`) without retraining. ms-marco-L-12-v2 is the safe-floor, +0.0–1.0 pp at $0. V3's biggest exposure is bge-m3 latency: 568 M params × MPS × 50 pairs likely exceeds the 2× A2 p95 gate (~682 ms) per V3's own honest admission (`variant3_swap.md:18`). Mitigation already in proposal: top-30 pool + reject-if-over.

V2 is the right second move IF V3 misses. V2 trains a **separate** model (zero blast radius, `variant2_contrastive.md:70`), has explicit anchor-eval guards from the 210 G1+G2 LLM-judged pairs, and can EXCEED the teacher ceiling V1 cannot. Its p(win)=0.16 is honest given P5/P7 priors. V1's only edge over V2 is lower divergence risk on noisy labels — but V2's `--score-margin-min=0.4` filter (a 10-LoC add) closes most of that gap, and V1's $2.40/9 h burns more budget for a ceiling V2 already exceeds.

## Verified claims
1. **V1's `--loss=lambdaloss` flag exists** in `scripts/finetune_reranker.py:189,407,520-523` — TRUE. Listwise pipeline real (lines 503, 819, 834).
2. **V3's `E2E_RERANKER_MODEL` is hard-coded at line 82** of `scripts/benchmark_doc_intent.py` — TRUE (line 82 confirmed). One-line env-var swap is accurate.
3. **V1's `--keep-teacher-scores` and V2's `--score-margin-min` flags exist in `build_train_pairs_v2.py`** — FALSE. Both flags are NEW work (V1 admits "~5 lines" addition `:90`; V2 admits "~10 LoC" `:108`). Neither blocks execution but adds ~15 min of pre-work each.
4. **A2 baseline R@10 = 0.2427** — TRUE. Confirmed in `RECALL-TRACKER.md:32` (`recall@10  0.2427`) and `falsifier1_split_half.md:11`.
5. **A2 covers 28.7% of doc-intent prod traffic; 55.9% unknown stratum** — TRUE. `falsifier2_token_coverage.md:5,8`. Effective prod lift ≈ 1.20 pp (`:23`). V1 correctly cites this as the "unaltered prod CE on 55.9%" target (`variant1_distillation.md:16`).
6. **G1 +18 pp direct-rate, +0.67 DCG** — TRUE. `p10-llm-judge-report.md:55-58`.
7. **V2 p(win)=0.16, V1 p(win)=0.16, V3 p(any beats by +1pp)=0.40** — claims as written. V3's higher p() reflects 3 shots at the gate vs single-candidate runs.

## Combined gate
After running selected variants, SHIP criterion (any single variant): Δ macro R@10 ≥ +0.01 vs A2 (0.2427) AND no per-stratum (n≥8) Δ < −0.02 AND hit@10 ≥ A2 − 0.005 AND p95 ≤ 682 ms (2× A2). Tie-break preference if multiple pass: lower p95 first (latency is unconditional win), then larger Δ on `provider`+`interac` (the strata where A2 keeps reranker-on, so any new model must not regress them).

## Risks for the winning variant (V3)
- **bge-m3 p95 breach** — 6× MPS forward-pass latency may break the 2× A2 gate; mitigate via top-30 candidate pool + fp16 + reject-if-over.
- **DeBERTa-v3 fp16 NaN on MPS for mxbai** (`variant3_swap.md:97-98`) — known sentence-transformers <3.0 quirk; force fp32 or CPU fallback at n=192.
- **Fintech-jargon OOD on bge-m3 / mxbai** — provider tokens (`addUPOAPM`, `merchantSiteId`) fragment under XLM-R / DeBERTa-v3 vocab; per-stratum check on `nuvei`/`interac`/`provider` is mandatory before SHIP.

## Edge case: if V3 wins
If V3's winner clears the AND-gate, **skip V1 and V2 entirely**. V1 only made sense as a "rescue if zero-cost shelf models can't beat MS-MARCO" path; once V3 establishes a stronger reranker is reachable at $0, the FT cost is no longer justified. Bank the ~$5 budget for the next axis (corpus-side `content_boost` on the 5 G1 REAL-ON-win queries, or P11 stratum expansion). V2 stays in reserve only if a future regression on prod telemetry reopens the docs↔code axis.