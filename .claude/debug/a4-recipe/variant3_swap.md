# A4 Variant 3 — Off-the-shelf reranker swap

## TL;DR
- Top-3: `BAAI/bge-reranker-v2-m3`, `mixedbread-ai/mxbai-rerank-base-v1`, `cross-encoder/ms-marco-MiniLM-L-12-v2`
- Cost: **$0** (Mac MPS local; pod only if 16 GB blows up on m3)
- Wall-clock: **~1.5 h** (3 × ~25-30 min on eval-v3-n200)
- Expected Δr@10 vs A2 0.2427: **+0.5 to +2.5 pp** (range −1 to +3.5)
- p(any beats A2 by ≥+1 pp): **~0.40**; p(top by ≥+3 pp): **~0.10**

## Per-candidate analysis

### 1. `BAAI/bge-reranker-v2-m3` (568 M, ~2.2 GB fp32)
- XLM-RoBERTa-large, distilled from m3 retriever; 100+ datasets incl. MS-MARCO/MIRACL/code-doc; long-ctx ≤8 K.
- 16 GB Mac: ~3 GB peak fp16 — fits if daemon `/admin/unload`-ed.
- Latency MPS: ~80-110 ms/pair × 50 ≈ 6× ms-marco-L6, p95 ~700-900 ms — **may breach 2× A2 latency gate**.
- Strengths: top of `mteb/Reranking`; only candidate trained on long doc passages.
- Risks: large; latency; needs `sentencepiece`.
- Predicted Δr@10: **+1.0 to +2.5 pp**, balanced.

### 2. `mixedbread-ai/mxbai-rerank-base-v1` (184 M, ~700 MB)
- DeBERTa-v3-base, distilled from mxbai-large + GPT-4 hard negatives (2024); beats bge-base on BEIR.
- Mac: ~1.2 GB peak — comfortable.
- Latency MPS: ~2× L-6, p50 ~250-350, p95 ~400-500 — within gate.
- Strengths: SOTA-class for size; DeBERTa-v3 strong on technical text (StackExchange in train).
- Risks: web-Q&A pretraining → provider-doc jargon (`addUPOAPM`, `merchantSiteId`) is OOD; possible weaker on `interac/provider`.
- Predicted Δr@10: **+0.5 to +2.0 pp**; risk ≤−5 pp on `interac/provider`. Best cost/upside.

### 3. `cross-encoder/ms-marco-MiniLM-L-12-v2` (33 M, ~130 MB)
- Same MS-MARCO pretraining as prod L-6, 12 layers (~2× depth). Same data, vocab, normalization.
- Mac: trivial (~250 MB).
- Latency: ~1.5-1.8× L-6, p50 ~250, p95 ~500 — within gate.
- Strengths: same-family drop-in; guaranteed no tokenizer regression; cheapest A/B; one-line swap if it wins.
- Risks: same English-Wiki domain bias; depth alone doesn't fix domain.
- Predicted Δr@10: **+0.0 to +1.0 pp**; safe-bet floor.

## Concrete pipeline

### Step 1: Pre-flight (5 min)
```bash
curl -s -X POST http://127.0.0.1:8742/admin/unload
python3 -c "import psutil; print(psutil.virtual_memory().available/1024**3)"
python3 -c "from sentence_transformers import CrossEncoder; \
  CrossEncoder('cross-encoder/ms-marco-MiniLM-L-12-v2'); \
  CrossEncoder('mixedbread-ai/mxbai-rerank-base-v1'); \
  CrossEncoder('BAAI/bge-reranker-v2-m3')"
cd ~/.code-rag-mcp && python3.12 -m pytest tests/test_rerank_skip.py -q
```

### Step 2: A/B benchmark (~25-30 min × 3)

Patch `scripts/benchmark_doc_intent.py:82` (currently hard-codes `E2E_RERANKER_MODEL`):

```python
E2E_RERANKER_MODEL = os.getenv(
    "CODE_RAG_BENCH_RERANKER", "cross-encoder/ms-marco-MiniLM-L-6-v2"
)
```

`load_reranker(model_name)` already accepts the arg.

```bash
for M in cross-encoder/ms-marco-MiniLM-L-12-v2 \
         mixedbread-ai/mxbai-rerank-base-v1 \
         BAAI/bge-reranker-v2-m3; do
  TAG=$(echo "$M" | tr '/:' '__')
  CODE_RAG_BENCH_RERANKER=$M python3.12 scripts/benchmark_doc_intent.py \
    --eval=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
    --model=docs --no-pre-flight --rerank-on \
    --out=/tmp/p10_a4_swap_${TAG}.json
done
```

### Step 3: Apply A2 stratum gate to winner (10 min)

```bash
M=<winner>; TAG=$(echo "$M" | tr '/:' '__')
CODE_RAG_BENCH_RERANKER=$M python3.12 scripts/benchmark_doc_intent.py \
  --eval=profiles/pay-com/doc_intent_eval_v3_n200.jsonl \
  --model=docs --no-pre-flight --rerank-on --stratum-gated \
  --out=/tmp/p10_a4_swap_${TAG}_gated.json
python3.12 scripts/benchmark_doc_intent.py \
  --compare /tmp/p10_a2_stratum_gated.json /tmp/p10_a4_swap_${TAG}_gated.json
```

### Step 4: AND-gate

Ship if all: Δ R@10 ≥ +0.01 vs A2; per-stratum (n≥5) drop ≥ −0.10; hit@5 ≥ A2−0.05; p95 ≤ 2× A2 (≤ 682 ms); KEEP strata (`interac`/`provider`) do not regress >5 pp.

## Implementation notes

- `benchmark_doc_intent.py:240` `load_reranker(model_name)` already parameterized; only line 82 needs env-var.
- Prod swap: single config in `src/embedding_provider.py::LocalRerankerProvider` ctor.
- Bench manifest already writes `rerank_model` so `--compare` is self-describing.

## Risks

1. **bge-m3 latency breach.** 568 M × MPS × 50 pairs > 2× A2 p95 likely. Mitigate: top-30 pool; fp16. If still over, reject for prod even if R@10 wins.
2. **DeBERTa-v3 MPS NaN (mxbai).** Known `sentence-transformers<3.0` fp16 quirk. Mitigate: force fp32; CPU fallback OK at n=192.
3. **Fintech-jargon OOD.** Tokens fragment. MS-MARCO-L12 keeps prod vocab (safest); bge-m3 widest XLM. Mitigate: per-stratum nuvei/interac/provider check.
4. **HF download.** bge-m3 = 2.2 GB; warm cache under wifi.

## Why this might beat both fine-tuning variants

V1/V2 spend $5-10 + a pod-day on a model trained on our 192-query distribution. Web-scale rerankers have seen orders of magnitude more supervision (m3: 100+ datasets; mxbai: GPT-4 hard negatives). With the heuristic labeler under-crediting relevance by 46 pp (G2 in `p10-llm-judge-report.md`), a stronger generic model may be the cleanest +1 pp at $0. If V3 wins, V1/V2 must clear that bar again to justify spend.

Counterpoint: prod ms-marco already wins +18 pp direct-rate on hard queries (G1). A larger generic model amplifies what ms-marco does well; it does not fix the docs↔code axis. If V3 nets ≤ +0.5 pp, the bottleneck is data not depth → V2 fine-tune mandatory.

## Total cost: $0
## Wall-clock: ~1.5 h
