# PLANNING DEBATE TASK

**Task:** How should we approach Run 2 for the **reranker pipeline** so we ship a reranker that beats baseline `cross-encoder/ms-marco-MiniLM-L-6-v2` on BOTH `doc_intent_eval_v3` (n=100) AND `code_intent_eval_v1` (n=80) R@10 metrics?

## What we have right now (Run 1 results, evidence-grounded)
- `rerank-l12` (33M FT): docs R@10 0.1891 (-6pp vs baseline 0.25), code 0.1869 (+1.1pp vs baseline 0.1756)
- `rerank-mxbai-base` (184M FT): docs **0.2609** (+1pp), code 0.1288 (-4.7pp)
- `rerank-bge-v2-m3` (568M FT, both bs=2 and bs=8): docs 0.196-0.198, code untested. Lost to both smaller candidates despite 3-15× params and 4-15× cost.
- Train data `finetune_data_combined_v1/train.jsonl` = 20695 rows (after expansion: 9839 docs / 10856 code = **0.91:1, near-balanced** — NOT 5.93:1 as Affirmative debate claimed; that was raw triplet count).

## Currently in flight (Run 2 architecture-debate verdict, $1.60)
- **B**: mxbai-base FT on **docs-only subset** (9839 rows). Hypothesis: docs specialist beats combined.
- **C**: mxbai-base FT on **code-only subset** (10856 rows). Hypothesis: code specialist beats combined.
- Pod bench docs only (vector index 88GB stays local); local code bench post-hoc.

## Already-rejected this session
- bge-reranker-v2-m3 (5x cost, lost both smaller candidates — see Run 1)
- oversampling code 5x (data already balanced, would over-correct)
- More epochs at default LR (no signal Run 1 tried this)
- mxbai-embed-large + ST.fit() (Bug 6o: gradient explosion, 389/391 params NaN locally — separate from rerankers)
- nomic FT (Bug 6p: ST + nomic-bert wrapper state_dict prefix mismatch)

## Constraints
- Solo developer, ~$5–10 budget per training cycle
- Mac 16GB; RunPod for compute (rtx4090 $0.69/hr, h100-sxm $2.99/hr; h100 only ~1.9× faster on bge per Run 1)
- Eval N small (100/80) → ±9.3pp 95% CI per axis (Negative debate observation)
- Routing classifier `is_doc_intent` exists but is a 7-tier regex pile (12% historical mis-route per `positions_prior`)
- Inference latency p95 must stay < 2s end-to-end

## What you must answer
1. **Top 3 approaches** to try next IF Run 2 B+C results don't dominate baseline on both axes
2. **Top 1 evidence-gathering action** to do FIRST before any more $ on training (e.g. grow eval, calibrate router, recompute CIs, profile FT recipe)
3. **One thing we should DEFINITELY not do** even if tempting

Use the diverse prior assigned to your teammate slot (pragmatist / systematist / refactorist). Cite repo paths/files when grounding claims. <500 words per round-1 file.
