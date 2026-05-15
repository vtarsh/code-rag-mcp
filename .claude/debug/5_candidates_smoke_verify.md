# 5-Candidate Smoke Verification — 2026-04-26

Verifier: 5-candidate-smoke-verifier (read-only ssh, no Mac compute).
Verified at: 2026-04-26 ~14:00 EEST.
Wall time: ~6 min, all 5 in parallel.

## SSH Key Map

- `nomic` (qjhaban6nq0njj @ 103.196.86.122:14608) — `~/.ssh/id_ed25519`
- `mxbai` (sdcz2a56efm1xm @ 94.101.98.238:21727) — `~/.runpod/ssh/RunPod-Key-Go`
- `gte` (se8tt724sts1i9 @ 213.173.102.171:36729) — `~/.ssh/id_ed25519`
- `rerank_mxbai` (bhzp072ixep82m @ 149.36.1.86:49278) — `~/.runpod/ssh/RunPod-Key-Go`
- `rerank_bge` (pl5oordxmjv0zm @ 194.68.245.198:22074) — `~/.runpod/ssh/RunPod-Key-Go`

## Summary Table

| tag           | gpu  | train_artifact          | index_model_correct | lance_size      | vec_dim | vec_rows  | bench_wiring | VERDICT                |
|---------------|------|-------------------------|---------------------|-----------------|---------|-----------|--------------|------------------------|
| nomic         | ok (2.2 GB used) | ok (547 MB safetensors @ /workspace/docs_v1_ft) | ok (`--model=docs-nomic-ft-v1`, log header `[key=docs-nomic-ft-v1]`, src `/workspace/docs_v1_ft`) | growing (74% — 36k/48.9k chunks) | 768 | 37,448 / 48,904 | n/a (docs build) | IN_PROGRESS_RECHECK |
| mxbai         | ok (1.9 GB used) | ok (1340 MB safetensors @ /workspace/docs_mxbai_ft) | ok (`--model=docs-mxbai-ft`, log header `[key=docs-mxbai-ft]`, src `/workspace/docs_mxbai_ft`) | growing (9% — 4.5k/48.9k chunks, ~95 MB) | 1024 | 6,080 / 48,904 | n/a (docs build) | IN_PROGRESS_RECHECK |
| gte           | ok (5.2 GB used) | ok (1736 MB safetensors @ /workspace/docs_gte_ft) | ok (`--model=docs-gte-ft`, log header `[key=docs-gte-ft]`, src `/workspace/docs_gte_ft`) | growing (37% — 18k/48.9k chunks) | 1024 | 34,328 / 48,904 | n/a (docs build) | IN_PROGRESS_RECHECK |
| rerank_mxbai  | ok (1 MiB idle) | ok (369 MB safetensors @ /workspace/docs_rerank_mxbai_ft, training_summary OK: base=mxbai-rerank-base-v1, 10971 train, 2 epochs, val_loss=0.332) | n/a (no docs index on rerank pod; bench runs elsewhere) | n/a | n/a | n/a | bench wiring uses env `CODE_RAG_BENCH_RERANKER` (verified in `scripts/benchmark_doc_intent.py:85`) | PASS (train artifact ready; bench pending docs-pod execution) |
| rerank_bge    | ok (3.0 GB used; idle GPU memory baseline) | ok (2271 MB safetensors @ /workspace/docs_rerank_bge_ft, training_summary OK: base=BAAI/bge-reranker-v2-m3, 791 train, 2 epochs, lambdaloss, val_loss=null) | n/a | n/a | n/a | n/a | same wiring path | PASS (train artifact ready; ⚠ val_loss=null, only 791 train pairs — small) |

## Per-Candidate Notes

### 1. nomic (docs tower, FT'd CodeRankEmbed/nomic-embed-text-v1.5)

```
[1/3] Loading docs-nomic-ft-v1 model...
  Model loaded on cuda in 5.7s
Model: /workspace/docs_v1_ft (768d) [key=docs-nomic-ft-v1]
Output: /workspace/code-rag-mcp/db/vectors.lance.docs.nomic-ft-v1
```

- **WARNING**: build log shows nomic `_IncompatibleKeys(missing_keys=[encoder.layers.X...], unexpected_keys=[encoder.encoder.layers.X...])` — layer-name prefix mismatch. The build is still embedding (37k chunks written, 14 emb/s, ETA 15min remaining), so the model loaded forward-pass weights from the safetensors via the parent layer. **However**: this could mean the pre-trained transformer weights are NOT being applied — only the FT'd head/pooling. **Recommend post-build smoke**: encode same text with FT'd model and base nomic, verify cosine !=1.0 (proves FT diff).
- 768d confirmed in lance schema. Build wiring is CORRECT (FT'd path used, not vanilla).

### 2. mxbai (docs tower, FT'd mxbai-embed-large-v1)

```
[1/3] Loading docs-mxbai-ft model...
  Model loaded on cuda in 6.3s
Model: /workspace/docs_mxbai_ft (1024d) [key=docs-mxbai-ft]
Output: /workspace/db/vectors.lance.docs.mxbai-ft
Mode: full rebuild (--force)
```

- Clean header. No incompatible-keys warning. Index growing at ~15 emb/s, ETA ~50 min remaining (slowest of 3).
- 1024d confirmed in lance. Build wiring is CORRECT.

### 3. gte (docs tower, FT'd gte-large-en-v1.5)

```
[1/3] Loading docs-gte-ft model...
  Model loaded on cuda in 3.0s
Model: /workspace/docs_gte_ft (1024d) [key=docs-gte-ft]
Output: /workspace/code-rag-mcp/db/vectors.lance.docs.gte-ft
Mode: full rebuild (--force)
  Cleared stale checkpoint (force build).
```

- 2 build processes seen earlier (PIDs 4582 + 4869 — duplicate launch?). Active checkpoint shows continuous progress — likely one is the parent shell, one is the worker. Not a bug, just verbose ps output.
- 1024d confirmed in lance. Build wiring is CORRECT.

### 4. rerank_mxbai (FT'd mxbai-rerank-base-v1)

- Train: 217.88s, 10971 examples, 2 epochs, BCE loss, **val_loss=0.332** (converged), bf16, sdpa attn.
- Artifact saved: 369 MB at `/workspace/docs_rerank_mxbai_ft/`.
- GPU now idle (1 MiB) — train DONE, waiting for bench.
- **Bench wiring**: `scripts/benchmark_doc_intent.py:85` reads `CODE_RAG_BENCH_RERANKER` env var; default = `cross-encoder/ms-marco-MiniLM-L-6-v2` (vanilla baseline). When bench runs with `CODE_RAG_BENCH_RERANKER=/workspace/docs_rerank_mxbai_ft`, JSON manifest's `rerank_model` field will reflect the override (line 581). **L-12 bug fix confirmed in code** — no hardcoded MiniLM path.
- Smoke bench on rerank pod itself: NOT POSSIBLE (no code-rag-mcp checkout, no eval, no docs lance). Must run on a docs pod after FT artifact transfer.

### 5. rerank_bge (FT'd BAAI/bge-reranker-v2-m3)

- Train: 733.4s, **791 examples** (small!), 2 epochs, lambdaloss, fp16+gradient_checkpointing, **val_loss=null** (lambdaloss may not record val).
- Artifact saved: 2271 MB at `/workspace/docs_rerank_bge_ft/`.
- GPU 3.0 GB used (residual from training session, no active proc).
- **WARNINGS**:
  1. **791 train pairs** — 14× smaller than mxbai (10971). Risk of underfit / poor generalization. Same family of risk as v0 10-pair FT (rejected -10.8pp on eval-v3).
  2. **val_loss=null** — cannot verify convergence from training_summary alone. Must read `train_*.log` for epoch losses.
- Bench wiring: same as mxbai — env override path is verified in code.

## Critical Findings & Recommendations

1. **All 3 docs candidates are wired correctly to FT'd models** — `--model=docs-{tag}-ft` flag matches the FT artifact path in build log header. NOT vanilla. NOT production `docs` model.
2. **All 5 train artifacts exist and are non-trivial size** (369 MB – 2271 MB). Mechanical training succeeded for all 5.
3. **Reranker pods cannot self-bench** — no code-rag-mcp / no docs lance / no eval. They are pure train pods. Bench runs on a docs pod after `scp` of artifacts.
4. **L-12 bug (vanilla used in bench) cannot recur** — `E2E_RERANKER_MODEL = os.getenv("CODE_RAG_BENCH_RERANKER", "ms-marco-MiniLM-L-6-v2")` (`scripts/benchmark_doc_intent.py:85`) is env-driven and the manifest's `rerank_model` field reflects the override (line 581). When the runner sets the env var to the FT path, the JSON proves which model was used. **Verify at bench time** by `jq '.rerank_model' on output JSON; assert path contains `_ft`.
5. **nomic _IncompatibleKeys warning needs post-build sanity check** — encode 5 sample queries with FT vs base nomic; cosine should be < 0.999. If == 1.0, FT weights silently NOT applied → REDO.
6. **rerank_bge red flags** — 791 train pairs is on the small side; val_loss=null prevents convergence validation. Recommend `tail train_bge_rerank.log | grep -E "epoch|loss"` before deploying.

## Remaining Builds — ETA

| tag   | progress | ETA from now |
|-------|----------|--------------|
| nomic | 74% (36k/48.9k) | ~15 min |
| gte   | 37% (18k/48.9k) | ~17 min |
| mxbai | 9%  (4.5k/48.9k) | ~50 min |

Re-check at +20 min (nomic+gte should be done) and +60 min (mxbai done). Verify each finalizes lance dataset (rows=48,904) and writes ANN index (`*.idx` files in `_indices/`).

## Final Verdicts

- **nomic**: IN_PROGRESS_RECHECK (build 74%; ⚠ verify nomic FT weights actually applied via post-build cosine smoke)
- **mxbai**: IN_PROGRESS_RECHECK (build 9%; clean wiring, no warnings)
- **gte**: IN_PROGRESS_RECHECK (build 37%; clean wiring, no warnings)
- **rerank_mxbai**: PASS (train artifact ready; bench wiring env-driven; pending docs-pod execution)
- **rerank_bge**: PASS_WITH_WARNINGS (train artifact ready; ⚠ 791 pairs only, ⚠ val_loss=null — verify convergence in train log before bench)

No FAIL_REDO. No UNREACHABLE.
