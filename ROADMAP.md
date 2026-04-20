# P5 Reranker — Roadmap

**Status (2026-04-20 evening):** Production = `ms-marco-MiniLM-L-6-v2` (unchanged). 7+ FT iterations exhausted. Audit wave concluded; first P0 ("fix eval methodology") DONE — see §"Verdict gate fix" below. Next: retrieval-stage (graph POC) + real-query eval + single-change v8 FT. Source of truth for what's been tried, what's still worth trying, and what's a dead end.

---

## ✅ DONE 2026-04-20: Verdict gate fix (P0)

Post-validation wave (5 critics + 3 cross-validators) confirmed the verdict gate was broken, but also refuted one critic's recommendation (MRR as primary). What changed:

- **Old gate** (`merge_eval_shards.py`): `max_regressions ≤ 3` on 909 tickets — mathematically unworkable since 46% of tickets have n_gt=1 (r@10 binary, single flip = ±100pp). Also a second conflicting `decide_verdict` in `eval_finetune.py` produced different verdicts on the same data.
- **New gate** (`scripts/eval_verdict.py` — single source of truth): `Δr@10 ≥ +0.02 AND ΔHit@5 ≥ +0.02 AND net_improved ≥ 20` on the FULL 909-ticket eval (not the 5-ticket test split, which was statistically fragile).
- **MRR is diagnostic only, NOT in gate.** Cross-val found MRR ranks v7 (rejected) as #1 above v6.2. Switching primary to MRR would have whitewashed a known-bad model.
- **Hit@5 added as co-primary** — on our data, 0/909 tickets where Hit@5↑ and r@10↓ (clean pairing; MRR disagrees on 24).
- Merge-shard bugs fixed: None-handling in `build_delta` (was collapsing "both passes missed GT" to delta=0), shard overlap detection (raise on duplicate tickets), stricter `--reuse-baseline-from` eval_config check (now compares batch_size/max_length/seed too, not just base_model+fts_limit).

Re-scored all historical snapshots with the new gate (dry run via `scripts/rescore_snapshots.py`; markdown at `profiles/pay-com/finetune_history/rescore_2026-04-20.md`):

| run | old verdict | new verdict | Δr@10 | ΔHit@5 | net |
|---|---|---|---|---|---|
| gte_v1 | REJECT | PROMOTE | +0.020 | +0.028 | +42 |
| gte_v4 | HOLD | PROMOTE | +0.041 | +0.050 | +74 |
| **gte_v6_2** | HOLD | **PROMOTE** | +0.043 | +0.057 | +89 |
| gte_v7 | HOLD | PROMOTE | +0.034 | +0.056 | +73 |

**Interpretation:** All four models look like Jira-eval wins under a sensible gate. The gate is a *necessary* filter on FT iteration cycles. It is NOT sufficient for production swap — runtime benchmarks (latency, realworld/queries) and per-project parity remain separate gates. v7's passage here is precisely why the gate alone is insufficient: v7 has known 2× latency and mixed runtime benchmarks from prior audits.

Tests: 310 pass (27 new in `tests/test_eval_verdict.py`). Single-process and sharded eval now share one verdict function. See `scripts/eval_verdict.py` module docstring for rationale.

---

## Next steps (post-gate-fix)

Validated by the critic+cross-val wave; ordered by ROI/cost:

1. **Graph retrieval POC** (4h notebook, no prod change). Graph has 17k edges (8.7k real repo→repo); 1-hop reachability on v6.2 low-recall tickets ~87%. Added-candidate precision is low (~3.7%) so hub filter required. Ship criterion: `≥+2pp r@10 AND MRR not regressed` on 100 low-recall tickets. Calibrated lift: +1.5 to +3pp (not the roadmap's original +3-5pp). P(≥+3pp) = 25%, P(≥+1pp with neutral MRR) = 60%.
2. **Real-query eval** (1-2 days, LLM-assisted labeling). 1,174 unique queries in `logs/tool_calls.jsonl`. Stratify cap 10/session (top-3 sessions = 45% of queries — single-dev workflow-replay, NOT generalization signal). Note: "82% identifier-dense" claim was WRONG (actual token-level 26%). `search_feedback.jsonl` has no click signal (score=0 everywhere). Use as regression guard only.
3. **v8 FT — ONE surgical change, post-gate fix only.** 92.5% of v6.2 regressions are rank-reshuffle (fixable by reranker). Candidate levers, isolate one:
   - Pairwise/listwise loss (currently only pointwise MSE/BCE/Huber in `finetune_reranker.py:195`). Most targeted fix for reshuffle regressions.
   - Freeze bottom 6 ModernBERT layers (62k rows / 149M params is under-regularized).
   - Dense-neighbor hard negatives (currently ALL negatives come from FTS top-50 — orthogonal axis untried).

---

## Production state

- Reranker: `ms-marco-MiniLM-L-6-v2` (HuggingFace, 22M params).
- Config: `profiles/pay-com/config.json::reranker_model = "ms-marco-MiniLM-L-6-v2"`.
- Base model for FT experiments: `Alibaba-NLP/gte-reranker-modernbert-base` (149M params, ModernBERT).
- Hybrid retrieval: FTS5 (150) + dense CodeRankEmbed (50) + RRF → CrossEncoder rerank top-200 → top-K.

### Archive (kept for future iteration, NOT in prod)
| Artifact | Purpose | Path |
|---|---|---|
| `reranker_ft_gte_v4/` (2GB) | Best "simple" FT: +4.06pp aggregate, 41 regressions, val loss 0.0927 | `profiles/pay-com/models/reranker_ft_gte_v4/` |
| `reranker_ft_gte_v6_2/` (2GB) | Best "with filters" FT: +4.30pp, 40 regressions, val loss 0.0349 | `profiles/pay-com/models/reranker_ft_gte_v6_2/` |
| `finetune_data_v4/` (104M) | v4 training set (66,522 rows) | `profiles/pay-com/finetune_data_v4/` |
| `finetune_data_v6_2/` (97M) | v6.2 training set (61,250 rows) | `profiles/pay-com/finetune_data_v6_2/` |
| `gte_v1.json` | Baseline eval snapshot (needed for `--reuse-baseline-from`) | `profiles/pay-com/finetune_history/` |
| `gte_v4.json`, `gte_v6_2.json` | Best FT evals | `profiles/pay-com/finetune_history/` |
| `gte_v7.json` | v7 failure eval (learn-from-mistake artifact) | `profiles/pay-com/finetune_history/` |

Nothing else in `models/`, `finetune_data*/`, `finetune_history/` — prior cleanup freed ~8GB.

---

## Journey — All iterations (so we don't repeat)

| Ver | Data flags | Val loss | Δr@10 ALL | Reg | Outcome | Lesson |
|---|---|---|---|---|---|---|
| v1 | title-only query, chunk positives | — | — | — | REJECT | Jira title alone is too sparse |
| v2 | MiniLM-L6 FT, title-only | 0.14 | +1.7 test | 55 | REJECT | Small model, title data = weak |
| v3 | GTE, title-only + desc | — | +5 test | 80 | REJECT | Wrong: desc added but diff not |
| **v4** | **title+desc+diff positives** | **0.0927** | **+4.06** | **41** | **HOLD** | **First "good" FT. Proven baseline.** |
| v5 | v4 + `--dedupe-same-file` + more filters | 0.14 | **-16.67 test** | 176 | **CATASTROPHIC REJECT** | Dedupe (diff-over-chunk) broke train/eval distribution |
| v6 | revert dedupe, add basename + trivial + min-query-len 50 + PI oversample 3 | — | +3.65 | 51 | HOLD | Cap=120 destroyed CORE monster-PR signal |
| v6.1 | v6 | 0.0405 | +3.65 | 51 | HOLD | CORE -1.79pp vs v4 |
| **v6.2** | **v6.1 - skip-empty-desc + drop-generated + max-rows 300 + oversample PI=5 + min-query-len 30** | **0.0349** | **+4.30** | **40** | **HOLD (best FT)** | **Best to date. All runtime benchmarks mixed or worse. 2× latency.** |
| v7 | v6.2 + `--fe-hard-negatives 4` (inject from FE cluster) | 0.0354 | +3.39 | 52 | REJECT | FE neg injection was 99.7% graphql (first in cluster, FTS broke early). Model learned "avoid graphql" too broadly. Hypothesis (BO→FE leakage) was also likely wrong per audit. |

---

## Mistakes and wrong conclusions (review carefully before repeating)

These are things **we learned along the way** — but some of our explanations for failures may still be wrong. Next session should re-examine:

### 1. v5 failure: "dedupe-same-file broke distribution"
**Our post-mortem:** `--dedupe-same-file` with prefer-diff-over-chunk left 99.6% diff positives; eval uses chunks → mismatch → -16.67pp.
**Revisit:** This IS the right diagnosis — confirmed by sample check rule added. But related question: maybe pure diffs train reranker differently than we thought and the problem was not "mismatch" but "signal starvation." Different cure.

### 2. v6.1 failure: "max-rows=120 killed CORE monster-PRs"
**Our post-mortem:** cap=120 dropped 2,448 CORE positives, -1.79pp vs v4.
**Revisit:** Maybe CORE monster-PRs are LOW-signal (huge diffuse refactors). Dropping them might actually be correct — the regression could be from removing them combined with adding noisy `skip-empty-desc-multi-file`. Need to disentangle by changing one flag at a time.

### 3. v7 failure: "BO→FE vocabulary leakage"
**Our post-mortem:** Inject FE hard negs → expected CORE recovery.
**Audit revisited (agent 6):** Hypothesis **WRONG**. CORE regressions are multi-GT displacement noise (n_gt=8.5 for regressed tickets). Not leakage. Not CORE-specific either — BO and PI regressed same way in v7.
**Key insight:** our "BO→FE leakage" claim was plausible-sounding but unfalsified. We assumed it and built a cure. New rule: **don't act on a hypothesis without a falsification test first.**

### 4. Eval verdict threshold `reg ≤ 3` — ✅ FIXED 2026-04-20
**Original claim:** "noise floor of 40+ regressions from rank 8-12 reshuffling" → impossible threshold.
**Cross-validated:** direction confirmed (threshold IS unworkable) but *mechanism* was wrong. The real problem: 46% of tickets have n_gt=1 (r@10 binary); 5-ticket test split (one flip = 20pp); verdict gate ignored improvements entirely (v6.2: 129 improved, 40 regressed — net +89 invisible). Also a second divergent `decide_verdict` existed in `eval_finetune.py`. See §"DONE 2026-04-20" above for the fix.
**Refuted recommendation:** "switch primary to MRR" would have promoted v7 (rejected) over v6.2. MRR is now diagnostic only; Hit@5 is co-primary.

### 5. "FT helps — just need better data"
**Original claim:** v6.2 at 20.6% of theoretical ceiling, max +2pp remaining.
**Cross-validated:** ceiling math was wrong. Real addressable ceiling via r@25 is ~10pp baseline→oracle; v6.2 already closed ~47%. Remaining ~4pp is real AND addressable — but NOT by more data-filter tweaking. 92.5% of v6.2 regressions are pure rank-reshuffle (reranker-fixable via listwise/pairwise loss, which we've never tried — only pointwise MSE/BCE/Huber). Freeze bottom layers and dense-neighbor negatives are also untried levers. Lesson: **stop filter-tweaking, not FT as a whole.**

### 6. "Jira eval generalizes to runtime"
**Original claim:** Real runtime queries = 61 chars, 82% identifier-dense; zero overlap with train distribution.
**Cross-validated:** "61 chars" confirmed. "82% identifier-dense" WRONG — actual token-level identifier share is 26% (char-level 38%). Queries are lowercase prose+keyword hybrids, not identifier bags. Also: 1,174 queries from 43 sessions, top-3 sessions = 45% — this is one-dev workflow-replay, not a generalization benchmark. `search_feedback.jsonl` has no click signal (score=0 everywhere). Real-query eval is feasible as regression guard but should NOT be overweighted vs Jira eval until a second user profile exists.

---

## Where we think we should go next (WITH validation required)

Ordered by estimated ROI. **Every step requires its own critic/audit before execution.**

### P0. ✅ DONE 2026-04-20 — Fix evaluation methodology
See §"DONE 2026-04-20: Verdict gate fix" above.

### P0. Build real-query eval from `tool_calls.jsonl`
**Claim:** 1,194 real MCP queries are in the log. We can label ~100 for ground-truth relevance and use as held-out eval.
**Validation to run:**
- Agent: "Are these queries actually representative, or skewed toward our own self-testing? How many unique users/sessions? What's the duplicate rate?"
- Agent: "What's the cost of manual labeling? Is there a cheaper way (e.g., treat top-returned chunks as weak positives, or use LLM-as-judge)?"
**Effort:** 1-2 days if manual; could be faster with LLM-assisted labeling.

### P1. Graph-boosted retrieval
**Claim:** `graph_edges` table has 11k+ typed edges, unused by `hybrid_search`. ~50 LOC addition could give +3-5pp.
**Validation to run:**
- Agent: "Which edge types are most useful? How often do they actually connect seed chunks to GT chunks on our eval corpus? What's the expected recall lift before implementation?"
- Agent (code review): "Proposed implementation in `src/search/hybrid.py::_graph_boost()` — any risks (hub pollution, latency, scoring instability)?"
- POC: prototype boost on 100 tickets, measure delta without deploying.
**Effort:** 2h code + 4h POC validation. Low-risk.

### P1. Query rewriting / identifier extraction
**Claim:** Zero experiments. Reranker can't fix queries missed by recall.
**Validation to run:**
- Agent: "What specific rewrites help code search? Naive synonym expansion, AST-aware identifier extraction, LLM rewrite? Which are cheapest to ROI?"
- Agent: "On our eval set, how many tickets are 'zero recall' (GT not in top-200 FTS output)? If small, query rewrite has low ceiling."
**Effort:** 1-2 days exploration + small implementation. Biggest untouched axis.

### P2. v8 FT (conditional on P0 done)
Only if we fix eval first AND have real-query corpus. Candidates from agent 8:
- Multi-epoch (2-3) + cosine LR decay
- Mined-hard negatives only (drop random), ratio 1:4
- Freeze bottom 6 ModernBERT layers (62k rows is small for 149M params)
- Listwise loss for multi-GT tickets (agent 6 recommendation)
- Mixed query length: 50% full + 25% title-only + 25% identifier-extracted (agent 7)

**Validation required BEFORE training:**
- Sample check distribution alignment on new data
- Fit small subset first (HS project, ~3 min) to verify flag behavior
- Don't combine 5 changes at once — isolate each (lesson from v5).

### SKIP. Reranker model swap
v4-v7 already explored this space. GTE-modernbert is at ~95% of achievable FT performance. Bigger model = 2-4× latency, marginal gain. Smaller = quality loss.

---

## Proven settings (if FT resumed)

Train:
```bash
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8 PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4 \
python3.12 scripts/finetune_reranker.py \
  --train profiles/pay-com/finetune_data_vN/train.jsonl \
  --test profiles/pay-com/finetune_data_vN/test.jsonl \
  --base-model Alibaba-NLP/gte-reranker-modernbert-base \
  --out profiles/pay-com/models/reranker_ft_gte_vN \
  --epochs 1 --batch-size 32 --lr 8e-5 --warmup 350 --max-length 192 \
  --bf16 --optim adamw_torch_fused --loss mse \
  --save-steps 500 --val-ratio 0.10 --resume-from-checkpoint none
```
Throughput ~10 rows/s. 60-100k rows = 75-100 min on 16GB Mac.

Eval (parallel 3 shards, ~35-45 min):
```bash
SLUG=gte_vN MODEL=profiles/pay-com/models/reranker_ft_gte_vN DATA=profiles/pay-com/finetune_data_vN \
  bash scripts/eval_parallel.sh
```

Data prep (reference):
```bash
python3.12 scripts/prepare_finetune_data.py \
  --projects PI,BO,CORE,HS --min-files 1 --seed 42 \
  --out profiles/pay-com/finetune_data_vN/ \
  --use-description --use-diff-positives --diff-snippet-max-chars 1500 \
  --drop-noisy-basenames --drop-generated --drop-trivial-positives \
  --min-query-len 30 --oversample PI=5 \
  --drop-popular-files 25 --max-rows-per-ticket 300
```
All flags opt-in (default False). Don't use `--dedupe-same-file` (v5 catastrophe).

---

## Critical pitfalls (do NOT repeat)

1. **No `--dedupe-same-file`** — v5 catastrophe (-16.67pp).
2. **MANDATORY sample check** — 5 train + 5 test positive rows, visual compare. 10 min gate prevents 6h train waste.
3. **No `--max-rows-per-ticket` below 300** — v6.1 killed CORE at cap=120.
4. **No wholesale `--skip-empty-desc-multi-file`** — drops 13 CORE monster-PRs. Prefer query augmentation instead.
5. **Don't combine 5 new flags at once** — v5 lesson. Isolate each change. Attribution matters.
6. **Both MPS env vars** — `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8` AND `PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4` together.
7. **`--history-out` no shard suffix** — `eval_finetune.py` appends `.shardNofN.json` automatically.
8. **Env vars for DB paths** — `CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp`, `ACTIVE_PROFILE=pay-com`.
9. **Checkpoint resume requires same batch size** — HF Trainer bug.
10. **Eval metric is repo-level, not file-level** — if you see r@10 numbers, they're over ~70 repos, not 909 files. Post-2026-04-20 gate: r@10 primary + Hit@5 co-primary + net_improved counts, on full 909 set. MRR is diagnostic only (misleads on our data — see eval_verdict.py docstring).
11. **v7 lesson: don't iterate FE clusters sequentially** — single repo dominates. Round-robin or score-merge.
12. **Run critics BEFORE implementation** — v7 hypothesis was wrong; a falsification test would've saved a 3h cycle.

---

## Known infrastructure

- `db/tasks.db` (70MB) — `task_history` with 909 Jira tickets (ground truth = `files_changed`, but eval scores repos).
- `db/knowledge.db` (160MB) — FTS5 + chunk metadata + **`graph_edges` table with 11k+ typed edges (UNUSED by retrieval)**.
- `db/vectors.lance.coderank/` (11GB LanceDB) — CodeRankEmbed embeddings for hybrid search.
- `logs/tool_calls.jsonl` — **1,194 real MCP search queries, ~4,262 total tool calls. Unused as training/eval signal.**
- `logs/search_feedback.jsonl` (17.9M) — may contain click-through data for real positives.
- `scripts/eval_parallel.sh` — parallel 3-shard eval template (saves ~50% time vs sequential).
- `scripts/prepare_finetune_data.py` has all v1-v7 flags (all opt-in, default False).
- `scripts/finetune_reranker.py` — train pipeline with bf16, checkpointing.
- `scripts/eval_finetune.py` — eval with `--shard-index`, `--reuse-baseline-from`.

---

## Context for new session

- 16GB M-series Mac. MPS acceleration. One-epoch FT = ~75-100 min on 60k rows.
- Daemon on :8742 manages ML models in production. Unload before training (avoids MPS contention).
- `caffeinate -is -t 86400` to prevent sleep during overnight runs.
- User is the only dev; commits via `mcp__github__*` tools (gh deny-listed).
- Test suite (310 tests after 2026-04-20 P0 gate fix) must pass before any changes land: `python3.12 -m pytest tests/ -q`.
