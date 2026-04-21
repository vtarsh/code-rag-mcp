# P5 Reranker — Roadmap

**Status (2026-04-21 evening):** Production = `reranker_ft_gte_v8` (deployed morning). Canonical baseline r@10 = 0.7112 / Hit@5 = 0.8339 (with `--fts-fallback-enrich`). v8 full-eval: r@10 = 0.7622 / Hit@5 = 0.9131 — Δr@10 = +0.051, ΔHit@5 = +0.079, net = +100. PROMOTE confirmed. 5-agent post-breakthrough re-audit surfaced: P0 BUG (eval pipeline ≠ production retrieval), free lunch (code_facts_fts + env_vars tables built but unused at query time), one mini bug, null_rank rescue opportunity. Next lever = retrieval parity + wire unused tables + null_rank rescue BEFORE any further FT.

---

## 🎯 2026-04-21 evening: Conditional enriched FTS fallback — CONFIRMED +5.09pp v8 Δr@10 full-eval

**Full eval completed** (`gte_v8_fallback.json`, 909 tickets, fresh baseline + v8 both with `--fts-fallback-enrich`). Verdict: **PROMOTE** (Δr@10=+0.051, ΔHit@5=+0.079, net=+100, 146 improved / 46 regressed). All gate thresholds cleared with margin.

### Measured numbers (vs pre-session estimates)

| Metric | Old (no fallback) | New (with fallback) | Δ absolute | Estimated | Delta-estimate |
|---|---:|---:|---:|---:|---:|
| baseline r@10 | 0.6527 | **0.7112** | **+5.85pp** | +6.33pp | −0.48pp |
| v8 r@10 | ~0.6955 | **0.7622** | **+6.67pp** | +7.21pp | −0.54pp |
| baseline Hit@5 | 0.7668 | ~0.8339 | +6.71pp | +6.93pp | −0.22pp |
| v8 Hit@5 | ~0.8425 | **0.9131** | **+7.06pp** | +7.92pp | −0.86pp |

Estimates were ~0.5pp high across the board — expected noise from reranker behaviour on rescued candidates (candidate pool from enriched query differs from the candidates the diagnostic scored). Net directionally correct.

### Relative v8 vs baseline (with fallback on both)

| Metric | Old (no fallback) | New (with fallback) | Gap preserved? |
|---|---:|---:|:---:|
| Δr@10 (v8 − baseline) | +0.0429 | **+0.0509** | ✅ WIDER (+0.8pp) |
| ΔHit@5 | +0.0757 | **+0.0792** | ✅ same |
| net_improved | +63 | **+100** | ✅ better |
| MRR diag | — | +0.1038 | new |

**v8 advantage over baseline is PRESERVED and slightly amplified with fallback enabled.** Fallback helps baseline too (symmetric), but v8's rerank FT continues to pay on the rescued tickets.

### Implementation notes

- `scripts/eval_finetune.py`: new `--fts-fallback-enrich` flag. When `query_mode=summary` AND `fetch_fts_candidates` returns empty, retry with `build_query_text(task, use_description=True)` preclean'd via `preclean_for_fts`. On rescue, use the enriched query for rerank too (matches training distribution). Per-ticket `fallback_used` recorded.
- `scripts/eval_parallel.sh`: new `FTS_FALLBACK_ENRICH=1` env var passthrough.
- `scripts/sample_real_queries.py` + `tests/test_sample_real_queries.py`: foundation for real-query eval (400 stratified MCP queries sampled from `logs/tool_calls.jsonl`). Not yet labeled.
- Trigger: 77/909 eval tickets had 0 FTS candidates on raw summary. Reranker couldn't reach them. Blanket enriched mode breaks other tickets (−15pp on PI per Phase 1 test); conditional rescue can only help.
- **EVAL-TIME ONLY.** Runtime queries lack Jira description; transfer requires separate lever (query expansion, identifier injection).

### Post-breakthrough re-audit (5 agents: 2 context-aware + 2 blind + 1 runtime-reality)

| # | Finding | Severity | Source |
|---|---|---|---|
| 1 | **Eval pipeline ≠ production retrieval.** `scripts/eval_finetune.py` uses FTS5-only candidate pool; `src/search/hybrid.py` uses FTS+vector RRF + content-type boosts + 70/30 rerank:RRF mix. 11 prior FT iterations tuned to wrong candidate pool. | P0 BUG | Blind audit C |
| 2 | **Free lunch — unused tables.** `code_facts_fts` (1659 rows, built by `src/index/builders/code_facts.py`) and `env_vars` (4753 rows, built by `scripts/build_env_index.py`) exist in `db/knowledge.db` but NEVER read by `src/search/*.py` at query time. Pure untapped recall surface. | P0 | Blind audit D |
| 3 | **Mini bug.** `src/search/suggestions.py:72` — `WHERE node_type = 'repo'` but actual column is `type`. Swallowed by bare `except Exception`. 629 zero-result queries affected. | bug | Blind audit C |
| 4 | **Null_rank headroom.** 42 tickets have GT outside top-200 even with fallback → +4.62pp of locked headroom. Most GTs are mega-repo names (`backoffice-web`, `graphql`) not present in summary. Fix: FTS query expansion via `conventions.yaml` / `glossary.yaml` identifier injection, or Haiku 3.5 LLM rewrite (~$0.0002/query). | P1 | Context audit A |
| 5 | **Runtime transfer signal = top-K churn replay.** Cheapest high-signal measurement: replay 2308 real queries through baseline vs v8+fallback, compare top-10 ranks. $0 cost, 4h daemon time. Real queries are structurally close to fallback bucket (short, identifier-heavy) — transfer likely positive but needs measurement. | P1 | Runtime audit |
| 6 | **v12 FT verdict — mixed.** Agent A says NO (fix retrieval first). Agent B says YES with specific recipe (listwise + lr=5e-5 + freeze bottom 6 ModernBERT layers + dense-neg hybrid + fallback-enriched training + real-holdout gate). **Consensus: defer v12 until P0a retrieval parity lands** — else we tune to wrong pool again. | decision | Agents A/B |

### New next-lever ranking (supersedes all prior)

| # | Action | Effort | Expected |
|---|---|---|---|
| P0a | Fix `eval_finetune.py` to use `hybrid.py` retrieval | 4-6h | Validates future FT decisions |
| P0b | Fix `suggestions.py` `type` column typo | 5 min | Pure bug, 629 queries |
| P0c | Wire `code_facts_fts` + `env_vars` readers into `src/search/*.py` | 1-2d | Unused recall surface |
| P1a | Null_rank rescue (glossary/LLM query expand) | 1-2d | +2-3pp r@10 est |
| P1b | Top-K churn replay on 2308 real queries | 4h | Runtime transfer signal, $0 |
| P2 | v12 FT — ONLY AFTER P0a (else tuning to wrong pool) | 2-3d | +1.5-2.5pp est |
| ❌ | v6.2+v8 ensemble | — | Jaccard 0.918, skip |
| ❌ | LLM-as-judge full labeling | $5+4h | Churn replay gives 80% signal |
| ⏸ | Dense embedding FT | 12-24h re-embed | Defer |

---

## ✅ 2026-04-21 morning: v8 DEPLOYED to production

User decision: deploy v8, purge rest. Only v8 gave real runtime improvement (+8.3pp queries, +2.1pp realworld vs baseline); v10/v6.2/v9/v11 tied baseline on benchmarks. Jira r@10 gains didn't transfer to real queries — reranker learned Jira-label pattern, not RAG-query pattern.

- `profiles/pay-com/config.json::reranker_model` → absolute path to `reranker_ft_gte_v8` (relative broke: daemon cwd ≠ repo).
- Daemon restarted via `/admin/unload`. v8 loaded successfully.
- Purged v9, v10, v11 models (~6GB), `finetune_data_v9/` (70MB), all v9/v10/v11 eval snapshots, overnight experiment logs, `scripts/prepare_v9.sh`, `scripts/compare_query_modes.py`, `tests/test_compare_query_modes.py`.
- Kept: Phase 1 tooling (`--eval-query-mode`, `--test-ratio`, `--early-stopping-patience`) + FTS5 preclean fix + regression tests.

Overnight v9/v10/v11 lessons: lr=5e-5 > lr=2e-5 (undertrain) ≈ lr=8e-5 (mild overfit); `--test-ratio 0.15` real holdout confirmed v6.2's "+4.30pp" inflated by memorization by ~+1.19pp; honest held-out gain for best model was +3.58pp.

---

## Critical pitfalls — do NOT repeat (граблі)

1. **No `--dedupe-same-file`** — v5 catastrophe (−16.67pp).
2. **MANDATORY sample check** — 5 train + 5 test positive rows, visual compare. 10 min gate prevents 6h train waste.
3. **No `--max-rows-per-ticket` below 300** — v6.1 killed CORE at cap=120.
4. **No wholesale `--skip-empty-desc-multi-file`** — drops 13 CORE monster-PRs. Prefer query augmentation instead.
5. **Don't combine 5 new flags at once** — v5 lesson. Isolate each change. Attribution matters.
6. **Both MPS env vars** — `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8` AND `PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4` together.
7. **`--history-out` no shard suffix** — `eval_finetune.py` appends `.shardNofN.json` automatically.
8. **Env vars for DB paths** — `CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp`, `ACTIVE_PROFILE=pay-com`.
9. **Checkpoint resume requires same batch size** — HF Trainer bug.
10. **Eval metric is repo-level, not file-level** — if you see r@10 numbers, they're over ~70 repos, not 909 files. Post-2026-04-20 gate: r@10 primary + Hit@5 co-primary + net_improved counts, on full 909 set. MRR is diagnostic only (misleads on our data).
11. **v7 lesson: don't iterate FE clusters sequentially** — single repo dominates. Round-robin or score-merge.
12. **Run critics BEFORE implementation** — v7 hypothesis was wrong; a falsification test would've saved a 3h cycle.
13. **Eval pipeline ≠ production.** `eval_finetune.py` uses FTS-only; `hybrid.py` uses FTS+vector RRF+boosts. Always verify eval candidate pool matches serving pool before concluding "reranker is the bottleneck".
14. **Pre-commit pytest flakes under MPS contention during FT training.** If it fails with "11 errors" while training is running, verify manually — do NOT `--no-verify` by default. Under normal load, hook passes (tests don't import torch directly).
15. **MCP push_files requires FULL file content** per commit. Previous session's "push" was partial — file-by-file instead of diff-based. This reconciled today across 9 commits. Never assume origin has the same content as local HEAD.
16. **Blanket enriched query mode LAMEs FTS candidates** (−15pp on PI in Phase 1 test). Only CONDITIONAL fallback (retry enriched ONLY when FTS=0) is safe.
17. **The "rerank ceiling" claim after 11 iterations was WRONG.** 8.5% of tickets (77) had 0 FTS candidates — reranker couldn't reach them. Always verify retrieval reaches all eval tickets before concluding rerank saturated.

---

## Mistakes and wrong conclusions

### 1. v5 failure: "dedupe-same-file broke distribution"
Dedupe prefer-diff-over-chunk left 99.6% diff positives; eval uses chunks → mismatch → −16.67pp. Confirmed by sample-check rule. Related question: maybe pure diffs train reranker differently than assumed — different cure.

### 2. v6.1 failure: "max-rows=120 killed CORE monster-PRs"
cap=120 dropped 2,448 CORE positives → −1.79pp vs v4. Maybe CORE monster-PRs are LOW-signal; dropping them might be correct. Disentangle by changing one flag at a time.

### 3. v7 failure: "BO→FE vocabulary leakage"
Hypothesis WRONG per audit. CORE regressions are multi-GT displacement noise (n_gt=8.5 for regressed tickets), not leakage. Rule: **don't act on a hypothesis without a falsification test first.**

### 4. Eval verdict threshold `reg ≤ 3` — ✅ FIXED 2026-04-20
Direction confirmed (unworkable) but mechanism wrong. Real problem: 46% of tickets have n_gt=1 (r@10 binary), 5-ticket test split fragile, verdict ignored improvements. New gate: `Δr@10 ≥ +0.02 AND ΔHit@5 ≥ +0.02 AND net_improved ≥ 20`. MRR is diagnostic only (would have promoted rejected v7 over v6.2).

### 5. "FT helps — just need better data"
Claimed max +2pp remaining via filter tweaks. Refuted: real addressable ceiling via r@25 is ~10pp; v6.2 already closed ~47%. Remaining ~4pp is real but NOT addressable by filter tweaking. Listwise/pairwise loss, freeze-bottom-layers, dense-neighbor negatives are untried. Lesson: **stop filter-tweaking, not FT as a whole.**

### 6. "Jira eval generalizes to runtime"
Real queries = 61 chars (confirmed), but "82% identifier-dense" WRONG — actual char-level = 38%, token-level = 26%. 1174 queries from 43 sessions, top-3 = 45% → single-dev workflow-replay, not generalization benchmark. `search_feedback.jsonl` has no click signal (score=0 everywhere).

### 7. "Rerank is at ceiling" (2026-04-21 morning) — WRONG
Claim: 11 FT iterations capped at +3-5pp, rerank exhausted. Revisit: 8.5% of eval tickets scored 0 FTS candidates → reranker couldn't help. Conditional enriched fallback unlocks baseline +5.85pp AND v8 +6.67pp. The ceiling was a RETRIEVAL blocker, not a rerank one. **Always audit "which tickets can retrieval even reach?" before concluding reranker saturated.**

### 8. "Eval pipeline accurately reflects production" (2026-04-21 evening) — UNCLEAR
Claim: `eval_finetune.py` is the canonical measurement. Revisit: Critic C blind audit found eval uses FTS-only + direct rerank, production uses FTS+vector RRF + content-type boosts + 70/30 rerank:RRF mix. v8's measured +5.09pp may not transfer 1:1 to production. P0a priority: align eval to `hybrid.py` before any further FT.

---

## Journey — Key iterations (lesson-bearing only)

| Ver | Key change | Δr@10 ALL | Lesson |
|---|---|---|---|
| v4 | title+desc+diff positives | +4.06pp | First "good" FT. Proven baseline. |
| v5 | added `--dedupe-same-file` | **−16.67** | Distribution mismatch. **Never dedupe.** |
| v6.2 | reverted dedupe, +oversample PI, max-rows 300 | **+4.30pp** | Best Jira r@10 but inflated by memorization. |
| v7 | `--fe-hard-negatives 4` from FE cluster | +3.39pp | 99.7% graphql in first cluster. Hypothesis unfalsified. |
| v8 | v6.2 data reformatted listwise + LambdaLoss | +3.92pp | Hit@5 champion (+6.9pp), runtime winner. **In prod.** |
| fallback (today) | conditional enriched FTS retry on FTS=0 | +6.67pp (v8 absolute) | Rerank ceiling was a retrieval blocker. +5.09pp net v8 over baseline with fallback on both. |

---

## Production state

- Reranker: `reranker_ft_gte_v8` (listwise LambdaLoss, 285MB bf16) — absolute path in `profiles/pay-com/config.json`.
- Hybrid retrieval: FTS5 (150) + CodeRankEmbed dense (50) → RRF → rerank top-200 → top-K.
- **Canonical baseline**: r@10 = 0.7112 / Hit@5 = 0.8339 (with `--fts-fallback-enrich`).
- **v8 eval**: r@10 = 0.7622 / Hit@5 = 0.9131 (with `--fts-fallback-enrich`).
- Base model for FT: `Alibaba-NLP/gte-reranker-modernbert-base` (149M params).

### Archive (kept for future iteration)

| Artifact | Purpose | Path |
|---|---|---|
| `reranker_ft_gte_v4/` (2GB) | First good FT baseline | `profiles/pay-com/models/reranker_ft_gte_v4/` |
| `reranker_ft_gte_v6_2/` (2GB) | Best Jira r@10 (inflated +1.19pp by memorization) | `profiles/pay-com/models/reranker_ft_gte_v6_2/` |
| `reranker_ft_gte_v8/` (285MB bf16) | **PROD.** Best Hit@5 + runtime + listwise LambdaLoss | `profiles/pay-com/models/reranker_ft_gte_v8/` |
| `finetune_data_v6_2/` (97M) | v6.2 training set (61,250 pointwise rows) | `profiles/pay-com/finetune_data_v6_2/` |
| `finetune_data_v8/` (16M) | v8 listwise (879 groups, derived via `convert_to_listwise.py`) | `profiles/pay-com/finetune_data_v8/` |
| `gte_v1.json`, `gte_v4.json`, `gte_v6_2.json`, `gte_v7.json`, `gte_v8.json`, `gte_v8_fallback.json` | Eval snapshots (reuse via `--reuse-baseline-from`) | `profiles/pay-com/finetune_history/` |

---

## Proven FT recipe (if v12 happens — AFTER P0a)

**Update:** canonical baseline is now 0.7112 r@10 (not 0.6527). Prefer listwise LambdaLoss (v8). Consider freeze bottom 6 ModernBERT layers + dense-neg hybrid (both untried). Train with `--fts-fallback-enrich` enabled in eval gate.

**Train:**
```bash
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8 PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4 \
python3.12 scripts/finetune_reranker.py \
  --train profiles/pay-com/finetune_data_vN/train.jsonl \
  --test profiles/pay-com/finetune_data_vN/test.jsonl \
  --base-model Alibaba-NLP/gte-reranker-modernbert-base \
  --out profiles/pay-com/models/reranker_ft_gte_vN \
  --epochs 1 --batch-size 16 --lr 5e-5 --warmup 200 --max-length 256 \
  --bf16 --optim adamw_torch_fused --loss lambdaloss \
  --save-steps 500 --val-ratio 0.10 --early-stopping-patience 2 \
  --resume-from-checkpoint none
```

**Data prep (real holdout — mandatory):**
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

**Eval (parallel 3 shards, ~35-45 min):**
```bash
FTS_FALLBACK_ENRICH=1 SLUG=gte_vN \
MODEL=profiles/pay-com/models/reranker_ft_gte_vN \
DATA=profiles/pay-com/finetune_data_vN \
  bash scripts/eval_parallel.sh
```

Don't use `--dedupe-same-file` (v5 catastrophe).

---

## Known infrastructure

- `db/tasks.db` (70MB) — `task_history` with 909 Jira tickets (ground truth = `files_changed`, eval scores repos).
- `db/knowledge.db` (160MB) — FTS5 + chunk metadata. Tables: `chunks`, `chunks_fts`, `code_facts`, **`code_facts_fts` (1659 rows — UNUSED at query time)**, **`env_vars` (4753 rows — UNUSED)**, `graph_edges` (11k+ typed edges, UNUSED by retrieval), `graph_nodes`.
- `db/vectors.lance.coderank/` (11GB LanceDB) — CodeRankEmbed embeddings for hybrid search.
- **UNUSED-BUT-BUILT artifacts** (see P0c): `code_facts_fts` (built by `src/index/builders/code_facts.py`), `env_vars` (built by `scripts/build_env_index.py`), `graph_edges` (never read at query time).
- `logs/tool_calls.jsonl` — 2308+ real MCP search queries (post-breakthrough count updated from 1194). Source of truth for P1b churn replay.
- `logs/search_feedback.jsonl` (17.9M) — no click-through signal (score=0 everywhere).
- `profiles/pay-com/traces/raw/*.summary.json` — Jaeger runtime traces, not ingested.
- `scripts/eval_parallel.sh` — parallel 3-shard eval template (saves ~50% time).
- `scripts/prepare_finetune_data.py` — all v1-v8 flags (all opt-in, default False).
- `scripts/finetune_reranker.py` — train pipeline with bf16, checkpointing, early-stopping.
- `scripts/eval_finetune.py` — eval with `--shard-index`, `--reuse-baseline-from`, `--fts-fallback-enrich`. **NOTE: currently uses FTS-only retrieval — diverges from `src/search/hybrid.py` (P0a).**

---

## Context for new session

- 16GB M-series Mac. MPS acceleration. One-epoch FT = ~75-100 min on 60k rows.
- Daemon on :8742 manages ML models in production. Unload before training (avoids MPS contention).
- `caffeinate -is -t 86400` to prevent sleep during overnight runs.
- User is the only dev; commits via `mcp__github__*` tools (gh deny-listed).
- Test suite (337 tests) must pass before any changes land: `python3.12 -m pytest tests/ -q`.
