# DEBATE: Split reranker into TWO models (docs + code)

## Decision
PROPOSAL: Replace single combined-data CrossEncoder reranker with TWO specialized rerankers — one trained on docs-only subset, one on code-only subset. Route at inference time by query intent classifier.

## Constraints
- Current production: 1 reranker (ms-marco-MiniLM-L-6-v2 baseline)
- Train data we have: profiles/pay-com/finetune_data_combined_v1/train.jsonl = 20695 pairs (904 code intent + 5362 docs intent base, expanded to triplets+pairs)
- Run 1 evidence: combined-trained mxbai R@10 +1pp on docs / -5pp on code; combined-trained l12 -6pp docs / +1pp code → trade-off, no winner on both axes
- Eval: doc_intent_eval_v3 (n=100), code_intent_eval_v1 (n=80) — distinct
- Routing: `is_doc_intent()` heuristic already in src/search/router_eval_keep.py
- Inference latency target: p95 < 2s end-to-end
- Cost budget: $5-10 per training cycle on RunPod
- Team: solo developer

## Already-rejected alternatives
- bge-reranker-v2-m3 (568M): 5x cost, lost R@10 to 184M mxbai
- Train one big model with twice the data: bandwidth limited, harder to deploy

## Round 1
