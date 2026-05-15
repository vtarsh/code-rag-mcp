# A4 Variant 1 — Distillation (cross-encoder teacher → student)

## TL;DR
- Recipe: `cross-encoder/ms-marco-MiniLM-L-6-v2` (prod CE) teacher → student CE via **listwise LambdaLoss** on teacher logits.
- Student base: **`cross-encoder/ms-marco-TinyBERT-L-2-v2`** (4L/14M, ~2× faster). Goal = domain-conditioned, faster reranker. DistilBERT (66M) regresses latency, no recall gain.
- Loss: **LambdaLoss listwise** (`scripts/finetune_reranker.py:194,407,503-506`); MarginMSE = fallback. Listwise > MSE: G1 signal is rank-position (DCG +0.67), not score magnitude.
- Data: 1,423 doc-intent prod queries × FTS5 top-50 × teacher score → ~30k listwise rows post eval-disjoint.
- Cost: **$2.40** (RTX 4090 @ $0.34/h × ~7 h). Wall-clock ~9 h.
- Δr@10 vs A2 baseline 0.2427: **+0.8 to +2.5 pp** (point +1.5 pp).
- p(win, AND-gate ≥+1pp R@10, no stratum −2pp): **0.16**.

## Mechanism

G1 (`p10-llm-judge-report.md:55-60`): prod CE has REAL signal on hard queries (direct-rate +18pp, DCG +0.67) but loses macro R@10 on broad eval. G2 (`:131-148`): CE was MS-MARCO-trained, **never adapted to provider markdown** (nuvei/aircash/trustly conventions, payment-flow cross-refs). On `tail`/`payout`/`provider` it injects cross-provider noise (Q7 paysafe→trustly). A2 mitigated by **disabling** rerank on 5 strata; V1 **fixes** it.

Distillation works because (a) no human labels for 1,423 queries, but teacher 0–1 sigmoid carries soft rank info — richer than (q, pos, neg) MNRL — and `finetune_reranker.py` already supports MSE+LambdaLoss on CE logits. (b) Student inherits MS-MARCO ranking prior from TinyBERT init, specializes on pay-com via listwise loss — the gap G2 named. (c) `falsifier2_token_coverage.md:23`: A2 covers only 28.7% of doc-intent prod traffic; V1 replaces unaltered prod CE on the other 55.9% (`unknown`).

Different from `debate-recipes.md` R3 (CE→bi-encoder for recall): here we keep the rerank role, make it cheaper AND domain-tuned.

## Concrete pipeline

**Step 1 — Mine candidates + teacher labels (Mac, ~60 min, $0)**
```bash
cd ~/.code-rag-mcp && python3.12 -m pytest tests/ -q
python3.12 scripts/runpod/cost_guard.py --check 3.0
python3.12 scripts/build_train_pairs_v2.py \
  --queries=logs/tool_calls.jsonl --filter=doc-intent \
  --eval-disjoint=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
  --eval-disjoint=profiles/pay-com/doc_intent_eval_v3_n150.jsonl \
  --reranker=cross-encoder/ms-marco-MiniLM-L-6-v2 \
  --positives-rank=1-3 --hard-neg-rank=4-30 \
  --candidate-pool=50 --max-pairs=50000 --seed=42 \
  --keep-teacher-scores --out=/tmp/a4v1_pairs.jsonl
python3.12 scripts/convert_to_listwise.py \
  --in=/tmp/a4v1_pairs.jsonl --out=/tmp/a4v1_listwise.jsonl --use-input-scores
```
Pre-flight: head 5 train rows + head 5 eval rows; verify NO query overlap.

**Step 2 — Pod (5 min, $0.05)**
```bash
source ~/.runpod/credentials
python3.12 scripts/runpod/pod_lifecycle.py --start \
  --gpu=rtx4090 --secure-cloud --time-limit=6h --spending-cap=3 --hold &
POD_ID=$(python3.12 scripts/runpod/pod_lifecycle.py --list | head -1 | awk '{print $1}')
scp /tmp/a4v1_listwise.jsonl pod:/workspace/
ssh pod 'pip install -q sentence-transformers==5.* datasets accelerate'
```

**Step 3 — Train (~4 h, $1.40)**
```bash
ssh pod 'cd /workspace/code-rag-mcp && python3 scripts/finetune_reranker.py \
  --base-model=cross-encoder/ms-marco-TinyBERT-L-2-v2 \
  --train=/workspace/a4v1_listwise.jsonl \
  --loss=lambdaloss --batch-size=16 --max-length=256 \
  --epochs=2 --lr=3e-5 --warmup-ratio=0.1 \
  --bf16 --grad-ckpt --val-split=0.1 \
  --early-stop-patience=2 --save-steps=500 \
  --out=/workspace/a4v1_student'
```
lr 3e-5 standard; 2 epochs × ~150 steps = ~300 steps; bf16 saves 30% VRAM on 24 GB. All flags exist (`finetune_reranker.py:188-246`).

**Step 4 — Pull + bench (~30 min, $0)**
```bash
scp -r pod:/workspace/a4v1_student ./models/reranker_a4v1
python3.12 scripts/runpod/pod_lifecycle.py --terminate $POD_ID
CODE_RAG_RERANKER=./models/reranker_a4v1 python3.12 scripts/benchmark_doc_intent.py \
  --eval=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
  --model=docs --no-pre-flight --rerank-on --out=/tmp/a4v1_bench.json
python3.12 scripts/bench_v2_gate.py \
  --baseline=/tmp/p10_a2_stratum_gated.json --candidate=/tmp/a4v1_bench.json
```
**AND-gate SHIP**: macro R@10 ≥ baseline +1.0pp AND hit@10 ≥ baseline AND no stratum drop > −2pp on n≥8 AND p50 ≤ baseline +20 ms.

## Risks

1. **Self-distillation collapse** — student same family as teacher; may converge to teacher ranks incl. cross-provider noise. Falsifier: held-out 100 prod queries; student top-3 == teacher top-3 on >85% → abort, pivot to MarginMSE + resampled hard negatives.
2. **Listwise leakage** — 30 cands/row, one leaked path bankrupts the list. Post-build assert via `prepare_train_data.py:159-200` semantics; REFUSE on (repo, file_path) collision with eval expected_paths.
3. **A2-gate conflict** — V1 may obsolete A2 OR lose vs A2. Bench both A2-gate-ON and A2-gate-OFF with new student; ship winner.
4. **TinyBERT-L-2 capacity floor** — if R@10 drops > −2pp, pivot to ms-marco-MiniLM-L-4-v2 (+$1.20).

## Kill criteria

- Step 1: <800 queries OR <20k (q,doc) rows → abort, $0.
- Step 3 mid-train (~75 steps): val LambdaLoss > 0.95× initial → abort, $0.70 saved.
- Step 3 final: val-loss > 0.85× initial → ship+manual-review.
- Step 4: macro R@10 < baseline −0.5pp OR any stratum > −2pp on n≥8 → NO DEPLOY.

## Dependencies

- `build_train_pairs_v2.py:208`: add `--keep-teacher-scores` (~5 lines).
- `convert_to_listwise.py`: add `--use-input-scores` (~10 lines).
- `tests/test_build_train_pairs.py`: 1 round-trip case.
- `embedding_provider.py:99-119` already handles `CODE_RAG_RERANKER` path override.

## Total cost ceiling: $2.40 (≤$5; $2.60 banked)
## Total wall-clock: ~9 h
