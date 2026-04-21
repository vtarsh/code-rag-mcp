# P5 Reranker — Roadmap

**Status (2026-04-21 overnight):** Production = `ms-marco-MiniLM-L-6-v2` (unchanged). 8 FT iterations + Phase 1 design fixes done. v9/v10/v11 overnight run done.
Runtime benchmarks on v6.2 + v8 + v10 completed (5-critic validation). See §"🌙 2026-04-21 overnight" + "2026-04-20 night: Runtime validation" below.

---

## 🌙 2026-04-21 overnight: Phase 1 design fixes + v9/v10/v11 iterations

Trigger: user said "trainuj поки не отримаєш гарні результати" (iterate until quality achieved).

### Phase 1 (tooling, commit 9787a6f + ec52972 + 75316e3)
- `eval_finetune.py` added `--eval-query-mode {summary,enriched}`.
- `prepare_finetune_data.py` added `--test-ratio` for real stratified-per-project holdout.
- `finetune_reranker.py` added `--early-stopping-patience N`.
- `eval_parallel.sh` added `EVAL_QUERY_MODE` + `BASELINE=skip`.
- **FTS5 safety fix**: broadened `_FTS_PRECLEAN` regex to strip reserved punctuation (`Alias:`, `payment!`, `/path`) that crashed `sqlite3 MATCH`.
- New: `tests/test_fts_preclean.py` (9 tests), `tests/test_compare_query_modes.py` (3 tests). Total 328 tests.
- `.pre-commit-config.yaml`: pytest hook → `python3.12` (was `python3` = system 3.9).

### Phase 1 hypothesis test: query-parity on PI subset
Test: re-score v8 checkpoint on 44 PI tickets with `--eval-query-mode=enriched`.

| Pass | summary r@10 | enriched r@10 | Δ |
|---|---:|---:|---:|
| Baseline (L6) | 0.4782 | 0.3270 | **−0.1512** |
| v8 FT | 0.4534 | 0.3174 | −0.1360 |
| **Δ-of-Δ (FT−baseline)** | — | — | **+0.0152** |

**Interpretation:** query mismatch is real (FT +1.5pp) but DOMINATED by FTS candidate drift (both models lose ~15pp when query 80 chars → 500+ chars triggers 49 OR terms). Enriched-mode eval not viable.

### v9/v10/v11 iterations (real-holdout data, 772 train / 137 test)

All on same v6.2-style data with new `--test-ratio 0.15`. Differ only in lr.

| Model | lr | batch | val_loss | ALL Δr@10 (full) | ALL Δr@10 (holdout) | CORE (holdout) | PI (holdout, n=7) | net_r10 (holdout) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v6.2 (in-train for these 137) | 8e-5 | 32 | 0.035 | +4.30pp | +3.11pp | +0.99pp | +13.95pp | +10 |
| v9 | 2e-5 | 16 | 0.083 | +2.35pp | +1.52pp | +2.50pp | +9.18pp | +6 |
| **v10** | **5e-5** | **16** | **0.042** | **+3.51pp** | **+3.58pp** | **+3.12pp** | **+13.95pp** | **+14** |
| v11 | 8e-5 | 16 | 0.035 | +3.50pp | +3.02pp | +1.59pp | +13.95pp | +8 |
| v8 (legacy data) | 8e-5 listwise | 32 | n/a | +3.92pp | +2.36pp | — | — | +7 |

**v10 wins real-holdout.** BEATS v6.2 (+3.58 vs +3.11 on identical 137 tickets). **v6.2's +4.30pp full-eval was inflated** (trained on 904/909; +1.19pp of the win was memorization, not skill).

### Runtime benchmarks — v10 matches baseline (tie)

| Model | queries avg | q PASS | realworld avg | rw PASS/PART |
|---|---:|---:|---:|:---:|
| baseline (L6) | 0.850 | 3/4 | 0.8222 | 4/2 |
| v6.2 | 0.842 | 3/4 | 0.8222 | 4/2 |
| **v8** | **0.933** | **4/4** | **0.8430** | 4/2 |
| **v10** | 0.842 | 3/4 | 0.8222 | 4/2 |

v10 not a runtime improvement, not a regression. v8 still best runtime.

### Overnight conclusion

- **v10 beats v6.2 on real generalization** (+3.58pp vs +3.11pp on identical 137 held-out tickets).
- **v10 beats v8 on Jira r@10** (+3.58 vs +2.36 on holdout) and net (+14 vs +7).
- **v8 still owns runtime benchmarks + Hit@5**.
- **No single model dominates on all axes.** v10 for r@10-aligned use-cases; v8 for top-5 + runtime.

**Recommended recipe (next FT):**
```bash
# Data: --test-ratio 0.15 for real holdout
# Train: lr=5e-5, batch=16, max_length=256, --early-stopping-patience 2, MSE loss, 1 epoch
```

**Next session decisions (not autonomous):**
1. Deploy v10 (best Jira r@10 on honest eval) — swap `profiles/pay-com/config.json::reranker_model`.
2. Deploy v8 (best runtime + Hit@5) — candidate from night one.
3. Ensemble of v10 + v8 — untried lever, uncorrelated errors.
4. Next untried lever: BCE + proper `mine_hard_negatives()` (critic E).

Local-only artifacts (next session pushes these along with deploy decision):
- Models: v9, v10, v11 in `profiles/pay-com/models/` (~850MB each, gitignored).
- Data: `profiles/pay-com/finetune_data_v9/` (real 772/137 holdout).
- Large scripts: `eval_finetune.py`, `prepare_finetune_data.py`, `finetune_reranker.py` — local commits 9787a6f, ec52972, 099b42e, 75316e3, 93d7ab9, 4744cab.

---

**User priority update 2026-04-20:** quality matters MORE than latency. Previous iterations held back v6.2/v8 from prod citing "2× latency" — that's no longer the dominant consideration.

---

## ✅ DONE 2026-04-20 night: Runtime validation + critic synthesis

Five parallel critics (general-purpose / opus) checked 5 claims before any prod swap.

### 1. Runtime benchmarks (fresh) — 6 JSON snapshots in `profiles/pay-com/benchmarks/`

| Model | `benchmark_queries` avg | q PASS | `benchmark_realworld` avg | rw PASS/PART/FAIL |
|---|---:|---:|---:|:---:|
| baseline (L6) | 0.850 | 3/4 | 0.8222 | 4/2/0 |
| v6.2 | 0.842 | 3/4 | 0.8222 | 4/2/0 |
| **v8** | **0.933** (+0.083) | **4/4** | **0.8430** (+0.021) | 4/2/0 |

**Verdict:** v8 wins both runtime benchmarks.

### 2. Per-project parity on Jira eval (full-eval, now known to be inflated)

| Project | n | v6.2 Δr@10 | v8 Δr@10 |
|---|---:|---:|---:|
| BO | 524 | +0.0711 | +0.0630 |
| CORE | 308 | +0.0154 | +0.0249 |
| **PI** | 44 | −0.0055 | **−0.0248** |
| HS | 33 | +0.0455 | 0.0000 |

### 3. MCP top-K usage

median K = 10, p90 = 15, mean = 9.0. 88% calls want K≤10 (r@10 aligns); 24% want K≤5 (Hit@5 aligns).

### 4. Gate adversarial audit

Gate SUFFICIENT for FT-iteration filter. **INSUFFICIENT as single prod-decision gate.** No holdout (train=904, test=5 in v8 manifest); per-project blind; v7 (known-bad) passes gate.

### 5. Untried levers (at time of 2026-04-20 night audit)

- Query-parity mismatch (CONFIRMED real at +1.5pp but FTS drift dominates) — REFUTED 2026-04-21 as practical lever.
- v6.2+v8 ensemble (UNTRIED).
- Dense-neighbor hard negatives (UNTRIED).
- Freeze bottom layers (UNTRIED).

---

## ✅ DONE 2026-04-20: Verdict gate fix (P0)

New gate: `Δr@10 ≥ +0.02 AND ΔHit@5 ≥ +0.02 AND net_improved ≥ 20` on full 909-ticket eval. Single source of truth: `scripts/eval_verdict.py`.

Re-scored snapshots:

| run | old verdict | new verdict | Δr@10 | ΔHit@5 | net |
|---|---|---|---|---|---|
| gte_v1 | REJECT | PROMOTE | +0.020 | +0.028 | +42 |
| gte_v4 | HOLD | PROMOTE | +0.041 | +0.050 | +74 |
| **gte_v6_2** | HOLD | **PROMOTE** | +0.043 | +0.057 | +89 |
| gte_v7 | HOLD | PROMOTE | +0.034 | +0.056 | +73 |
| **gte_v8** | — | **PROMOTE** | +0.039 | **+0.069** | +78 |

---

## Production state

- Reranker: `ms-marco-MiniLM-L-6-v2` (HuggingFace, 22M params).
- Config: `profiles/pay-com/config.json::reranker_model = "ms-marco-MiniLM-L-6-v2"`.
- Base model for FT: `Alibaba-NLP/gte-reranker-modernbert-base` (149M).
- Hybrid retrieval: FTS5 (150) + dense CodeRankEmbed (50) + RRF → CrossEncoder rerank top-200 → top-K.

### Archive (kept for future iteration, NOT in prod)
| Artifact | Purpose |
|---|---|
| `reranker_ft_gte_v4/` | Best "simple" FT: +4.06pp aggregate |
| `reranker_ft_gte_v6_2/` | Best r@10 inflated-full: +4.30pp; real-holdout +3.11pp |
| `reranker_ft_gte_v8/` (285MB) | Best Hit@5 + runtime |
| **`reranker_ft_gte_v10/` (285MB)** | **NEW: best real-holdout Jira r@10** |
| `reranker_ft_gte_v9/`, `v11/` | lr ablations |

---

## Journey — All iterations

| Ver | Data flags | Val loss | Δr@10 full | Real-holdout Δr@10 | Outcome |
|---|---|---|---|---|---|
| v1 | title-only query | — | — | — | REJECT |
| v2 | MiniLM-L6 FT, title-only | 0.14 | +1.7 test | — | REJECT |
| v3 | GTE, title+desc | — | +5 test | — | REJECT |
| **v4** | title+desc+diff positives | 0.0927 | +4.06 | — | HOLD (proven baseline) |
| v5 | v4 + dedupe-same-file | 0.14 | **-16.67 test** | — | CATASTROPHIC REJECT |
| v6.1 | v6 cap=120 | 0.0405 | +3.65 | — | HOLD |
| **v6.2** | v6.1 tuned, cap=300 | **0.0349** | **+4.30** (inflated) | +3.11pp | HOLD |
| v7 | v6.2 + FE-hard-negs | 0.0354 | +3.39 | — | REJECT (graphql bias) |
| **v8** | v6.2 listwise LambdaLoss | n/a | +3.92 | +2.36 | PROMOTE sidegrade |
| v9 | v6.2 + holdout + lr=2e-5 batch=16 | 0.083 | +2.35 | +1.52 | PROMOTE (undertrained) |
| **v10** | v9 + **lr=5e-5** | **0.042** | **+3.51** | **+3.58** | **PROMOTE (new best)** |
| v11 | v9 + lr=8e-5 | 0.035 | +3.50 | +3.02 | PROMOTE (CORE overfit) |

---

## Mistakes and wrong conclusions

### NEW 2026-04-21: "Community lr=2e-5 is the right default"
**Wrong on our data.** lr=2e-5 (HF blog) → undertrained. lr=5e-5 (tomaarsen) → sweet spot. lr=8e-5 at batch=16 → CORE overfits. Data-scale matters.

### NEW 2026-04-21: "Query-parity mismatch was THE root cause"
**Half-wrong.** Real but small effect (+1.5pp); FTS candidate drift dominates with long enriched queries. Enriched-mode eval not viable.

### 1-6 (from prior audits)
Dedupe, cap=120, BO→FE leakage, verdict-threshold, ceiling-math, Jira-runtime-generalization — see prior ROADMAP versions in git history for full post-mortems.

---

## Where we think we should go next (WITH validation required)

### P0. Deploy decision: v10 vs v8 vs ensemble
- v10: best Jira r@10 honest eval, matches baseline runtime.
- v8: best runtime + Hit@5 (but trained on gate-broken data).
- Ensemble v10 + v8: untried.

### P1. BCE loss with `mine_hard_negatives()` (critic E)
Community baseline we never tested — might unlock ceiling together with v10 recipe.

### P1. Query rewriting / identifier extraction
Biggest untouched axis for query side.

### SKIP. Enriched eval mode (refuted 2026-04-21).
### SKIP. Reranker model swap (v4-v7 already explored).

---

## Proven settings (if FT resumed)

**Recommended (v10, new best):**
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

Don't use `--dedupe-same-file` (v5). Don't use `lr=8e-5` at batch=16 (v11 CORE overfit).

---

## Critical pitfalls (do NOT repeat)

1. **No `--dedupe-same-file`** — v5 catastrophe (-16.67pp).
2. **MANDATORY sample check** — 5 train + 5 test positive rows before every training.
3. **No `--max-rows-per-ticket` below 300** — v6.1 killed CORE at cap=120.
4. **No wholesale `--skip-empty-desc-multi-file`** — drops 13 CORE monster-PRs.
5. **Don't combine 5 new flags at once** — v5 lesson.
6. **Both MPS env vars** — HIGH=0.8 AND LOW=0.4 together.
7. **`--history-out` no shard suffix** — eval_finetune.py appends `.shardNofN.json`.
8. **Env vars for DB paths** — `CODE_RAG_HOME`, `ACTIVE_PROFILE=pay-com`.
9. **Checkpoint resume requires same batch size** — HF Trainer bug.
10. **Eval metric is repo-level, not file-level**.
11. **v7 lesson: don't iterate FE clusters sequentially**.
12. **Run critics BEFORE implementation**.
13. **NEW 2026-04-21: Real-holdout eval is MUST.** Full-eval on 909 with 904 in train = memorization inflation. Always `--test-ratio 0.15` on v9+.
14. **NEW 2026-04-21: `max_length=256 + batch=32` OOMs MPS.** Use batch=16 when max_len≥256.
15. **NEW 2026-04-21: FTS5 `_FTS_PRECLEAN` must strip all non-word/space/.-/ punctuation.** Jira descriptions have `Alias:`/`payment!` that crash sqlite3 MATCH.

---

## Known infrastructure

- `db/tasks.db` (70MB) — `task_history` with 909 Jira tickets.
- `db/knowledge.db` (160MB) — FTS5 + chunk metadata + `graph_edges` (11k+ edges, UNUSED).
- `db/vectors.lance.coderank/` (11GB LanceDB) — CodeRankEmbed embeddings.
- `logs/tool_calls.jsonl` — 1,194 real MCP queries (unused as training signal).
- `scripts/eval_parallel.sh` — parallel 3-shard eval template. NEW: `EVAL_QUERY_MODE`, `BASELINE=skip`.
- `scripts/prepare_finetune_data.py` — data prep. NEW: `--test-ratio` for real holdout.
- `scripts/finetune_reranker.py` — train pipeline. NEW: `--early-stopping-patience`.
- `scripts/eval_finetune.py` — eval. NEW: `--eval-query-mode`.
- `scripts/compare_query_modes.py` — NEW: Δ-of-Δ analysis tool.

---

## Context for new session

- 16GB M-series Mac. MPS. One-epoch FT = ~75-100 min on ~40k rows at batch=16.
- Daemon on :8742 manages ML models in production. Unload before training.
- `caffeinate -is -t 86400` for overnight runs.
- User is the only dev; commits via `mcp__github__*` (gh deny-listed).
- Tests: 328 pass (316 → +12 overnight). `python3.12 -m pytest tests/ -q`.
- Pre-commit pytest hook now uses `python3.12`.
