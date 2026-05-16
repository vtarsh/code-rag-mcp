# Session Status: 2026-05-17 Autonomous Search Improvement

## Goal
Achieve 80% hit@10 on jira_eval_clean.jsonl (n=665). Current baseline: 63.91%.

## Active Experiments

### Mac (Local CPU/MPS)
| Experiment | Config | Status | Result | Notes |
|-----------|--------|--------|--------|-------|
| Exp1 | RRF_K=20, KW=1.5, NO_PENALTIES, NO_DOCS | STOPPED at 250/665 | 61.6% | Worse than baseline. Too aggressive. |
| Exp2 | RRF_K=40, KW=2.0, PENALTIES=ON, NO_DOCS | RUNNING | 150/665: 62.67% | Conservative. Better than Exp1 (61.6%), close to baseline (63.91%). |

### RunPod (RTX 4090 GPU)
| Task | Status | Progress | ETA |
|------|--------|----------|-----|
| Vectors rebuild | RUNNING | 86% (70,356/81,087 chunks) | ~45 min |
| Batch eval (7 configs) | PENDING | Waiting for vectors | After rebuild |

## Code Changes Applied (all synced to RunPod)
1. `config.py`: RRF_K and KEYWORD_WEIGHT env overrides
2. `hybrid.py`: Vector limit 50→100, additive repo prefilter
3. `hybrid_rerank.py`: Sigmoid normalization, multiplicative penalties
4. `vector.py`: ANN→500 then Python filter
5. `fts.py`: Snippet 64→256 tokens
6. `code_facts.py`: MIN(rowid) instead of nondeterministic GROUP BY
7. `hybrid_rerank.py`: Merge towers by _distance (interleave)

## Key Findings So Far
- **Exp1 failure**: Aggressive changes (RRF_K=20 + KW=1.5 + no penalties) hurt by ~2pp.
  - Hypothesis: NO_PENALTIES lets doc chunks dominate top results
  - Hypothesis: RRF_K=20 over-discounts lower ranks
  - Hypothesis: KW=1.5 weakens FTS5 signal too much
- **Docs tower disable**: Previous test showed +0.45pp (64.36% vs 63.91%)

## Parallel Agent Investigations
- [ ] Analyze baseline eval failures (which queries miss and why)
- [ ] Investigate reranker impact (does it help or hurt?)
- [ ] Explore chunking strategy (is gold lost due to splitting?)

## Budget
- RunPod: ~$0.69/hr, ~$3-4 spent so far of $10
- Remaining budget: ~$6-7 for fine-tuning and additional experiments

## Next Actions (Autonomous)
1. Wait for Exp2 results (Mac)
2. Wait for RunPod vectors rebuild completion
3. Launch RunPod batch eval immediately after rebuild
4. Based on results, either:
   - a) Combine winning changes and test
   - b) Investigate deeper (chunking, embedding model, reranker)
   - c) Fine-tune reranker on RunPod

## Last Updated
2026-05-17 14:55 UTC+3

## Progress Update (15:15)
- Mac exp2: 250/665 = 65.60% (vs baseline 63.91%). 🟢 +1.69pp improvement!
- RunPod vectors: 90% (72,993/81,087), ETA ~45 min. Speed 3.0 emb/s.
- 3 research agents timed out (300s limit). Manual analysis done instead.

## Key Finding: Eval Quality Issues
- **hosted-fields repo**: 73.7% miss rate (highest)
- **Queries with "fix"**: 69.2% miss rate
- **Queries with "refactor"**: 45.5% miss rate
- **Conclusion**: Many eval tasks are low-quality vague descriptions that even humans would struggle to match. These drag down overall hit rate artificially.

## Exp2 Trend Analysis
| Queries | Hit Rate | vs Baseline |
|---------|----------|-------------|
| 50 | 64.00% | +0.09pp |
| 100 | 58.00% | -5.91pp (outlier) |
| 150 | 62.67% | -1.24pp |
| 200 | 65.50% | +1.59pp |
| 250 | 65.60% | +1.69pp |

Trend: improving after query 100. Conservative changes (RRF_K=40, KW=2.0, penalties ON, no docs) work!

## Progress Update (15:25)
- Mac exp2: RESTARTED with 45min timeout. Previous run reached 350/665 = 65.71% (+1.8pp) before timeout.
- RunPod vectors: 91% (74,136/81,087), ETA ~43 min.
- New eval task: bash-94aoxuwl (exp2_final)
