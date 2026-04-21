# P5 Reranker — Roadmap

**Status (2026-04-21 morning):** Production = **`reranker_ft_gte_v8`** (deployed, config.json swapped). Only v8 gave real runtime improvement (+8.3pp queries, +2.1pp realworld). v9/v10/v11 experiments purged (only ~3-5pp on Jira, zero practical impact on runtime).
Runtime benchmarks on v6.2 + v8 completed (5-critic validation). See §"2026-04-21 overnight" + "2026-04-20 night: Runtime validation" below.

---

## ✅ 2026-04-21 morning: v8 DEPLOYED to production

User decision after reviewing overnight results: **deploy v8**, purge rest.

Why v8 over v10:
- Only v8 gives real runtime improvement (+8.3pp queries vs baseline). v10/v6.2/v9/v11 all tie baseline on benchmarks.
- Jira r@10 gains (+3-5pp) don't transfer to real user queries — reranker learned Jira-label pattern, not RAG-query pattern.
- Reranker is polish, not recall fix. FTS+dense retrieval is the bottleneck.

Changes:
- `profiles/pay-com/config.json::reranker_model` → `/Users/.../profiles/pay-com/models/reranker_ft_gte_v8` (absolute path; relative broke because daemon cwd ≠ repo).
- Daemon restarted via `/admin/unload`. v8 loaded successfully.
- **Purged** v9, v10, v11 models (~6GB), `finetune_data_v9/` (70MB), all v9/v10/v11 eval snapshots, `gte_v8_enriched_pi.json`, `query_mode_compare_pi_2026-04-21.md`, `v10_{queries,realworld}.json`, all overnight training/eval logs, `scripts/prepare_v9.sh`, `scripts/compare_query_modes.py`, `tests/test_compare_query_modes.py`.
- **Kept:** Phase 1 tooling in scripts (eval_finetune.py `--eval-query-mode`, prepare_finetune_data.py `--test-ratio`, finetune_reranker.py `--early-stopping-patience`) + FTS5 preclean fix + `test_fts_preclean.py` (regression guard). These are reusable for future FT work.
- Tests: 325 pass (316 + 9 FTS preclean tests).

Overnight journey kept below for reference (v9/v10/v11 experiments, lr ablations, real-holdout analysis).

---

## 🌙 2026-04-21 overnight: Phase 1 design fixes + v9/v10/v11 iterations

Trigger: user said "trainuj поки не отримаєш гарні результати" (iterate until quality achieved).

### Phase 1 (code changes, commit 9787a6f)
- `eval_finetune.py` added `--eval-query-mode {summary,enriched}`. `enriched` composes query same as train (`build_query_text`).
- `prepare_finetune_data.py` added `--test-ratio` for real stratified-per-project holdout (replaces legacy 5-ticket auto).
- `finetune_reranker.py` added `--early-stopping-patience N` (enables `EarlyStoppingCallback` + `eval_strategy=steps` + `load_best_model_at_end`).
- `eval_parallel.sh` added `EVAL_QUERY_MODE` + `BASELINE=skip` for enriched runs.
- **FTS5 safety fix**: broadened `_FTS_PRECLEAN` regex in `prepare_finetune_data.py` to strip reserved punctuation (`Alias:`, `payment!`, `/path`) that crashed `sqlite3 MATCH`. Added `tests/test_fts_preclean.py` (9 tests).

### Phase 1 hypothesis test: query-parity on PI subset
Test: re-score v8 checkpoint on 44 PI tickets with `--eval-query-mode=enriched`.

| Pass | summary r@10 | enriched r@10 | Δ |
|---|---:|---:|---:|
| Baseline (L6) | 0.4782 | 0.3270 | **−0.1512** |
| v8 FT | 0.4534 | 0.3174 | −0.1360 |
| **Δ-of-Δ (FT−baseline)** | — | — | **+0.0152** |

**Interpretation:** query mismatch is real (FT benefits +1.5pp more than baseline from enrichment), but DOMINATED by FTS candidate drift (both models lose ~15pp when query goes from 80 chars → 500+ chars and pulls in 49 OR terms). Enriched-mode not viable.

### v9/v10/v11 iterations (purged, but documented for lessons)

Same v6.2-style data with new `--test-ratio 0.15`. Differ only in lr.

| Model | lr | batch | val_loss | ALL Δr@10 full | ALL Δr@10 real-holdout | net_r10 (holdout) |
|---|---:|---:|---:|---:|---:|---:|
| v6.2 (in-train for these 137) | 8e-5 | 32 | 0.035 | +4.30pp | +3.11pp | +10 |
| v9 | 2e-5 | 16 | 0.083 | +2.35pp | +1.52pp | +6 |
| v10 | 5e-5 | 16 | 0.042 | +3.51pp | +3.58pp | +14 |
| v11 | 8e-5 | 16 | 0.035 | +3.50pp | +3.02pp | +8 |
| v8 (prod) | 8e-5 listwise | 32 | n/a | +3.92pp | +2.36pp | +7 |

**v10 won Jira r@10 on real holdout** (beat v6.2 honestly, +3.58pp vs +3.11pp). BUT **v10 did not improve runtime benchmarks** (tie with baseline). Jira-r@10 gains don't transfer to runtime user queries — different distribution.

### Runtime benchmarks — v8 wins, v10 ties baseline

| Model | queries avg | q PASS | realworld avg | rw PASS/PART |
|---|---:|---:|---:|:---:|
| baseline (L6) | 0.850 | 3/4 | 0.8222 | 4/2 |
| v6.2 | 0.842 | 3/4 | 0.8222 | 4/2 |
| **v8** | **0.933** | **4/4** | **0.8430** | 4/2 |
| v10 | 0.842 | 3/4 | 0.8222 | 4/2 |

### Recipe for future FT (kept the tooling, purged the models)
```bash
# Data: --test-ratio 0.15 for real holdout
# Train: lr=5e-5, batch=16, max_length=256, --early-stopping-patience 2, MSE loss, 1 epoch
```

---

**User priority update 2026-04-20:** quality matters MORE than latency. Previous iterations held back v6.2/v8 from prod citing "2× latency" — that's no longer the dominant consideration.

---

## ✅ DONE 2026-04-20 night: Runtime validation + critic synthesis

Five parallel critics (general-purpose / opus) checked 5 claims before any prod swap.

### 1. Runtime benchmarks (6 JSON snapshots in `profiles/pay-com/benchmarks/`)

| Model | benchmark_queries | q PASS | benchmark_realworld | rw PASS/PART/FAIL |
|---|---:|---:|---:|:---:|
| baseline (L6) | 0.850 | 3/4 | 0.8222 | 4/2/0 |
| v6.2 | 0.842 | 3/4 | 0.8222 | 4/2/0 |
| **v8** | **0.933** (+0.083) | **4/4** | **0.8430** (+0.021) | 4/2/0 |

### 2. Per-project parity on Jira eval (full-eval, known inflated)

| Project | n | v6.2 Δr@10 | v8 Δr@10 |
|---|---:|---:|---:|
| BO | 524 | +0.0711 | +0.0630 |
| CORE | 308 | +0.0154 | +0.0249 |
| **PI** | 44 | −0.0055 | **−0.0248** |
| HS | 33 | +0.0455 | 0.0000 |

### 3. MCP top-K usage

median K = 10, p90 = 15. 88% calls want K≤10 (r@10 aligns); 24% want K≤5 (Hit@5 aligns).

### 4. Gate audit

Gate SUFFICIENT for FT-iteration filter. **INSUFFICIENT as single prod-decision gate.** No holdout, per-project blind.

### 5. Untried levers (2026-04-20 audit)

- Query-parity mismatch — REFUTED 2026-04-21.
- v8+v10 ensemble (UNTRIED).
- Dense-neighbor hard negatives (UNTRIED).
- Freeze bottom layers (UNTRIED).
- BCE + `mine_hard_negatives()` community recipe (UNTRIED).

---

## ✅ DONE 2026-04-20: Verdict gate fix (P0)

New gate: `Δr@10 ≥ +0.02 AND ΔHit@5 ≥ +0.02 AND net_improved ≥ 20` on full 909-ticket eval. Source of truth: `scripts/eval_verdict.py`.

---

## Production state

- Reranker: **`reranker_ft_gte_v8`** (PRODUCTION — deployed 2026-04-21).
- Base model for FT: `Alibaba-NLP/gte-reranker-modernbert-base` (149M, ModernBERT).
- Hybrid retrieval: FTS5 (150) + dense CodeRankEmbed (50) + RRF → CrossEncoder rerank top-200 → top-K.

### Archive (kept for future iteration, NOT in prod)
| Artifact | Purpose |
|---|---|
| `reranker_ft_gte_v4/` (2GB) | Best "simple" FT: +4.06pp aggregate |
| `reranker_ft_gte_v6_2/` (2GB) | Best r@10 FT (inflated): full +4.30pp, real-holdout +3.11pp |
| `reranker_ft_gte_v8/` (285MB bf16) | **CURRENT PRODUCTION** — best runtime + Hit@5 |
| `finetune_data_v4/`, `v6_2/`, `v8/` | Training sets |
| `gte_v1.json`, `gte_v4.json`, `gte_v6_2.json`, `gte_v7.json`, `gte_v8.json` | Eval snapshots |

---

## Journey — All iterations

| Ver | Data flags | Val loss | Δr@10 full | Δr@10 real-holdout | Outcome |
|---|---|---|---|---|---|
| v1 | title-only query | — | — | — | REJECT |
| v2 | MiniLM-L6 FT, title-only | 0.14 | +1.7 test | — | REJECT |
| v3 | GTE, title+desc | — | +5 test | — | REJECT |
| **v4** | title+desc+diff positives | 0.0927 | +4.06 | — | HOLD (proven baseline) |
| v5 | v4 + dedupe-same-file | 0.14 | **-16.67 test** | — | CATASTROPHIC REJECT |
| v6.1 | v6 cap=120 | 0.0405 | +3.65 | — | HOLD |
| v6.2 | v6.1 tuned, cap=300 | 0.0349 | +4.30 (inflated) | +3.11pp | HOLD |
| v7 | v6.2 + FE-hard-negs | 0.0354 | +3.39 | — | REJECT (graphql bias) |
| **v8** | **v6.2 listwise LambdaLoss** | n/a | **+3.92** | +2.36 | **PROMOTE (now PROD)** |
| v9 (purged) | v6.2 + holdout + lr=2e-5 batch=16 | 0.083 | +2.35 | +1.52 | undertrained |
| v10 (purged) | v9 + lr=5e-5 | 0.042 | +3.51 | +3.58 | Jira win but runtime tie |
| v11 (purged) | v9 + lr=8e-5 | 0.035 | +3.50 | +3.02 | CORE overfit |

---

## Mistakes and wrong conclusions

### NEW 2026-04-21: "Community lr=2e-5 is the right default"
**Wrong on our data.** lr=2e-5 (HF blog) → undertrained. lr=5e-5 (tomaarsen) → sweet spot for FT. lr=8e-5 at batch=16 → CORE overfits.

### NEW 2026-04-21: "Query-parity mismatch was THE root cause"
**Half-wrong.** Real but small effect (+1.5pp); FTS candidate drift dominates with long enriched queries. Enriched-mode eval not viable.

### NEW 2026-04-21: "If Jira r@10 improves, runtime search will improve"
**Wrong.** v10 improves Jira r@10 +3.58pp on real holdout → but runtime benchmarks = tie with baseline. Jira-label pattern ≠ real RAG-query pattern. Only listwise-NDCG v8 crossed the gap (+8.3pp runtime).

### 1-6 (prior audits)
Dedupe, cap=120, BO→FE leakage, verdict-threshold, ceiling-math, Jira-runtime-generalization — see prior ROADMAP versions in git history for full post-mortems.

---

## Where we think we should go next

### P0. Real breakthrough requires changing axis
Reranker is polished. The 11 iterations hit +3-5pp ceiling on Jira r@10 and near-zero on runtime (except v8). Further rerank FT will yield diminishing returns.

**Better bets:**
1. **Query rewriting / identifier extraction** — fix recall upstream of rerank. Biggest untouched axis.
2. **Improve dense retrieval (CodeRankEmbed FT?)** — reranker can only resort what's already in top-200.
3. **v8+v10 ensemble (score averaging)** — exploit uncorrelated errors.
4. **BCE + `mine_hard_negatives()`** — community standard we skipped; may unlock a different axis.

### SKIP. More rerank hyperparam tuning.
### SKIP. Enriched eval mode (FTS drift).
### SKIP. Larger reranker model (latency/quality Pareto).

---

## Proven FT recipe (if resumed)

```bash
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8 PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4 \
python3.12 scripts/finetune_reranker.py \
  --train profiles/pay-com/finetune_data_vN/train.jsonl \
  --test profiles/pay-com/finetune_data_vN/test.jsonl \
  --base-model Alibaba-NLP/gte-reranker-modernbert-base \
  --out profiles/pay-com/models/reranker_ft_gte_vN \
  --epochs 1 --batch-size 16 --lr 5e-5 --warmup 200 --max-length 256 \
  --bf16 --optim adamw_torch_fused --loss mse \
  --save-steps 500 --val-ratio 0.10 --early-stopping-patience 2 \
  --resume-from-checkpoint none
```

**Data prep (new, with real holdout):**
```bash
python3.12 scripts/prepare_finetune_data.py \
  --projects PI,BO,CORE,HS --min-files 1 --seed 42 \
  --out profiles/pay-com/finetune_data_vN/ \
  --use-description --use-diff-positives --diff-snippet-max-chars 1500 \
  --drop-noisy-basenames --drop-generated --drop-trivial-positives \
  --min-query-len 30 --oversample PI=5 \
  --drop-popular-files 25 --max-rows-per-ticket 300 \
  --test-ratio 0.15
```

Don't use `--dedupe-same-file` (v5). Don't use lr=8e-5 at batch=16 (v11).

---

## Critical pitfalls

1. **No `--dedupe-same-file`** — v5 catastrophe (-16.67pp).
2. **MANDATORY sample check** before training.
3. **No `--max-rows-per-ticket` below 300** — v6.1 killed CORE.
4. **No wholesale `--skip-empty-desc-multi-file`** — drops CORE monster-PRs.
5. **Don't combine 5 new flags at once** — v5 lesson.
6. **Both MPS env vars** — HIGH=0.8 AND LOW=0.4 together.
7. **`--history-out` no shard suffix** — eval_finetune.py appends `.shardNofN.json`.
8. **Env vars** — `CODE_RAG_HOME`, `ACTIVE_PROFILE=pay-com`.
9. **Checkpoint resume requires same batch size** — HF Trainer bug.
10. **Eval metric is repo-level, not file-level**.
11. **v7 lesson: don't iterate FE clusters sequentially**.
12. **Run critics BEFORE implementation**.
13. **NEW 2026-04-21: Real-holdout eval is MUST.** Always `--test-ratio 0.15` on v9+.
14. **NEW 2026-04-21: `max_length=256 + batch=32` OOMs MPS.** Use batch=16.
15. **NEW 2026-04-21: FTS5 `_FTS_PRECLEAN` must strip all non-word/space/.-/ punctuation.**
16. **NEW 2026-04-21: Jira r@10 gains don't imply runtime gains.** Check benchmarks separately.
17. **NEW 2026-04-21: Reranker model path in config.json must be ABSOLUTE** (daemon cwd ≠ repo).

---

## Known infrastructure

- `db/tasks.db` (70MB) — `task_history` with 909 Jira tickets.
- `db/knowledge.db` (160MB) — FTS5 + chunk metadata + `graph_edges` (11k+, UNUSED).
- `db/vectors.lance.coderank/` (11GB LanceDB) — CodeRankEmbed embeddings.
- `logs/tool_calls.jsonl` — 1,194 real MCP queries.
- `scripts/eval_parallel.sh` — parallel 3-shard eval. `EVAL_QUERY_MODE`, `BASELINE=skip` options.
- `scripts/prepare_finetune_data.py` — data prep. `--test-ratio` for real holdout.
- `scripts/finetune_reranker.py` — train. `--early-stopping-patience`.
- `scripts/eval_finetune.py` — eval. `--eval-query-mode`.

---

## Context for new session

- 16GB M-series Mac. MPS. One-epoch FT = ~75-100 min on ~40k rows at batch=16.
- Daemon on :8742. Unload before training.
- User commits via `mcp__github__*` (gh deny-listed).
- Tests: 325 pass. `python3.12 -m pytest tests/ -q`.
- Pre-commit pytest uses `python3.12`.
- **Production reranker path (absolute):** `/Users/vaceslavtarsevskij/.code-rag-mcp/profiles/pay-com/models/reranker_ft_gte_v8`
