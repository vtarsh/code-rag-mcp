---
variant: 2 — Contrastive (CrossEncoder, no teacher)
date: 2026-04-26
target: AND-gate ≥ +0.10 R@10 vs A2 baseline 0.2427 on doc_intent_eval_v3_n200.jsonl
budget: ≤ $5 RunPod
---

## TL;DR

Train `BAAI/bge-reranker-v2-m3` from public weights on ~6k pay-com docs (q, pos, neg) triplets. Loss = pairwise `MarginRankingLoss` margin=0.2, 1 epoch on A40 (~$1). Pos = G1+G2 LLM-judged anchors (held out for early-stop) + reranker-pseudo-pos with margin-guard. Neg = HN-A (rerank ranks 15-30) + HN-B (random docs). p(win AND-gate) = **0.16**.

## Mechanism vs V1

V1 distills ms-marco-MiniLM scores → capped at teacher; inherits the docs bias G1 named ("production cross-encoder IS being misled on docs"). V2 trains pos > neg directly on docs labels → can EXCEED teacher; risk = label noise → divergence. Mitigation: anchor-eval guard.

## Pipeline

**1. Pair mining ($0, ~30 min Mac)**

- **Anchors (gold, n≈210):** G1 (14×20) + G2 (15×10) Opus bundles at `/tmp/p10_judge_g{1,2}_*.json`. Score≥2 → pos, =0 → hard-neg. **Leakage:** 29 queries came from eval-v3-n200 → use ONLY as validation/early-stop.
- **Silver pairs (n≈6k):** `scripts/build_train_pairs_v2.py` (already query+path-disjoint vs eval-v3). New flag `--score-margin-min=0.4` (~10 LoC) drops weak pairs (P5/P7 antidote).
- **Random HN-B (n≈3k):** 0.5× random docs outside top-50.

Total ~12k triplets.

**2. Pre-flight ($0)**

```
python3.12 -m pytest tests/ -q   # 857/857
python3.12 scripts/runpod/cost_guard.py --check 2.0
python3.12 scripts/build_train_pairs_v2.py \
  --queries=logs/tool_calls.jsonl --filter=doc-intent \
  --eval-disjoint=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
  --eval-disjoint=profiles/pay-com/doc_intent_eval_v3.jsonl \
  --reranker=cross-encoder/ms-marco-MiniLM-L-6-v2 \
  --positives-rank=1-2 --hard-neg-rank=15-30 \
  --score-margin-min=0.4 --max-pairs=6000 --seed=42 \
  --out=/tmp/train_v2_xenc.jsonl
```

**3. Train ($1, ~2h A40)**

New `scripts/runpod/train_docs_reranker.py` (~120 LoC):

```python
from sentence_transformers import CrossEncoder
from sentence_transformers.cross_encoder.losses import MarginRankingLoss
m = CrossEncoder("BAAI/bge-reranker-v2-m3", num_labels=1, max_length=512)
loss = MarginRankingLoss(m, margin=0.2)
m.fit(train_dataloader=loader, loss_fct=loss, epochs=1,
      warmup_steps=int(0.1*total), optimizer_params={"lr": 1e-5},
      batch_size=16, evaluator=AnchorMarginEvaluator(anchor_pairs),
      evaluation_steps=100, save_best_model=True)
```

Hyperparams: lr=1e-5, bs=16, max_seq=512, warmup=10%, linear scheduler, ~750 steps.

**4. Bench + decide ($0.20)**

```
python3.12 scripts/benchmark_doc_intent.py \
  --eval=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
  --reranker-path=./reranker_a4_v2 --stratum-gated
```

AND-gate: macro Δ ≥ +0.10 R@10 AND no stratum (n≥8) Δ < -0.02.

## Base — `BAAI/bge-reranker-v2-m3`

568M, 8192 ctx, multilingual, beat ms-marco-L-6 ~3pp BEIR. **Rejected:** ms-marco-L-12 (same teacher bias V1 inherits); resume-FT prod L-6-v2 (catastrophic-forgetting on code). V2 ships SEPARATE model — prod stays on `reranker_ft_gte_v8` for code, new model only at doc-intent gate. Zero blast radius.

## Loss — `MarginRankingLoss`

Pairwise (`s(q,pos) − s(q,neg) ≥ 0.2`) is **relative** → tolerant of silver noise. BCE rejected (absolute target needs clean labels). RankNet/listwise overkill at n=6k.

## Why V2 won't repeat P5/P7

| P5/P7 fail | V2 |
|---|---|
| MNRL pos-only on 22 queries / 91 pairs | Pairwise margin on ~6k queries with explicit hard-neg per row |
| In-batch quasi-random neg → anisotropy | No in-batch coupling; cross-encoder scalar output can't anisotropize |
| No early-stop | Anchor-eval/100 steps on 210 LLM-judged pairs |
| Same recipe family 4× | New family (cross-encoder + margin), first project attempt |

## Kill criteria

1. Step-100 anchor spearman drop ≥0.05 vs step-0 → abort (~$0.30 sunk).
2. Final macro Δ R@10 < +0.02 → reject.
3. Any stratum n≥8 Δ < -0.02 → reject.

## Honest V1 vs V2

| Axis | V2 | V1 |
|---|---|---|
| Ceiling | EXCEEDS teacher | Capped at teacher |
| Bias inherited | None | Yes (G1-flagged) |
| Label quality required | Higher | Lower |
| Cost | ~$1 | ~$2–3 |
| Divergence risk | Higher (needs guard) | Lower |
| Better when | Teacher misses docs signal | Silver labels too noisy |

V2 wins if you trust G1's teacher-bias finding. V1 wins if you fear pseudo-label noise more than the teacher ceiling.

## Dependencies

- `sentence_transformers ≥ 4.0` (already in `setup_env.sh`)
- New `scripts/runpod/train_docs_reranker.py` (~120 LoC)
- New flag `build_train_pairs_v2.py --score-margin-min` (~10 LoC)
- New `AnchorMarginEvaluator` reading `/tmp/p10_judge_g*_scores.json`

## Cost ceiling: $2 of $5

Mining $0; A40 2h × $0.50/h = $1; bench $0.20; buffer $0.80.

---

**5-line summary**

1. Mining: 210 G1+G2 LLM anchors (validation) + 6k query-disjoint prod pairs via `build_train_pairs_v2.py --score-margin-min=0.4` + 3k random HN-B.
2. Base: `BAAI/bge-reranker-v2-m3` (separate model; prod reranker untouched).
3. Loss: `MarginRankingLoss` margin=0.2, lr=1e-5, bs=16, 1 epoch ~750 steps, anchor-eval/100.
4. Cost: ~$1 train + $0.20 bench (cap $2 of $5).
5. p(win): **0.16** — Jeffreys 0.11 × 1.45 (fresh class + zero blast radius + G1 confirms teacher-bias lever).
