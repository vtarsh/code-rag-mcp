# P5 Reranker — Roadmap

**Status (2026-04-20 night):** Production = `ms-marco-MiniLM-L-6-v2` (unchanged). 8 FT iterations done. Audit wave concluded; P0 (eval gate fix) + v8 (listwise LambdaLoss) done.
Runtime benchmarks on v6.2 + v8 completed (5-critic validation). See §"2026-04-20 night: Runtime validation + critic synthesis" below.

**User priority update 2026-04-20:** quality matters MORE than latency. Previous iterations held back v6.2/v8 from prod citing "2× latency" — that's no longer the dominant consideration. Main open question: do v6.2 or v8 win on RUNTIME query distribution (not just Jira eval) and per-project breakdown? If yes, one of them should ship.

---

## ✅ DONE 2026-04-20 night: Runtime validation + critic synthesis

Five parallel critics (general-purpose / opus) checked 5 claims before any prod swap. Results below.

### 1. Runtime benchmarks (fresh) — 6 JSON snapshots in `profiles/pay-com/benchmarks/`

Setup: `CODERANK_RERANK_MODEL=<path>` env var (short-circuits config). `hybrid_search` runs in-process; daemon on :8742 did NOT restart.

| Model | `benchmark_queries` avg | q PASS | `benchmark_realworld` avg | rw PASS/PART/FAIL | feature cat |
|---|---:|---:|---:|:---:|---:|
| baseline (L6) | 0.850 | 3/4 | 0.8222 | 4/2/0 | 0.854 |
| v6.2 | 0.842 | 3/4 | 0.8222 | 4/2/0 | 0.854 |
| **v8** | **0.933** (+0.083) | **4/4** | **0.8430** (+0.021) | 4/2/0 | **0.917** (+0.063) |

Per-query v8 wins over baseline: Q1-concept +0.167 (0.833→1.00), Q4-concept +0.167 (0.667→0.833), Q1-rw +0.125 (0.875→1.00). No PASS→FAIL or PASS→PARTIAL regressions. No `benchmark_flows.py` run yet (script exists; 10-query sample limits statistical power regardless).

**Verdict:** v8 clearly wins both runtime benchmarks. v6.2 is indistinguishable from baseline on this sample (10 queries — small, take with salt).

### 2. Per-project parity on Jira eval (breakdown of `gte_v1/v6_2/v8.json`)

| Project | n | v6.2 Δr@10 | v6.2 ΔH@5 | v6.2 net_H5 | v8 Δr@10 | v8 ΔH@5 | v8 net_H5 |
|---|---:|---:|---:|---:|---:|---:|---:|
| BO | 524 | +0.0711 | +0.0977 | +46 | +0.0630 | +0.1040 | +49 |
| CORE | 308 | +0.0154 | +0.0211 | +6 | +0.0249 | +0.0493 | +14 |
| **PI** | 44 | **−0.0055** | **−0.0227** | **−1** | **−0.0248** | **−0.0227** | **−1** |
| HS | 33 | +0.0455 | +0.0303 | +1 | 0.0000 | +0.0303 | +1 |
| ALL | 909 | +0.0470 | +0.0625 | +52 | +0.0429 | +0.0757 | +63 |

**Both models regress PI on Hit@5** (net_H5 = −1 each, i.e. ~1 ticket falls out of top-5 that was there in baseline).
**v8 regresses PI harder on r@10** (−2.48pp ≈ ~1 ticket vs v6.2's ~0.25 ticket).
BO (58% of eval) dominates aggregate — dropping BO alone fails v8 gate.

### 3. MCP top-K usage (from `logs/tool_calls.jsonl` — 1222 MCP search calls)

- Default limit = 10 (`mcp_server.py:118`, `src/search/service.py:23,43`).
- median K = 10, p90 = 15, mean = 9.0. Distribution: ≤3: 2.8%, 4–5: 21.0%, 6–10: 64.2%, 11–20: 12.0%.
- 88% of calls want K≤10 (r@10 aligns) — but 24% want K≤5 (Hit@5 aligns).
- Caveat: top-5 ordering inside K=10 response is still judged by Hit@5; users who read top-down benefit from Hit@5 wins even at K=10.

### 4. Gate adversarial audit — `scripts/eval_verdict.py`

**Verdict:** SUFFICIENT for FT-iteration filter. INSUFFICIENT as single prod-decision gate. Critical findings:

1. **No holdout.** `profiles/pay-com/finetune_data_v8/manifest.json` has `train=904, test=5`. "Full 909 eval" = 904 train + 5 test. Gate cannot detect overfitting. On the 5-ticket test split, v8 Δr@10 = +0.0000.
2. **Per-project blind.** Dropping BO alone → v8 fails gate.
3. **v7 (known-bad) passes gate** with margin — test `test_v7_promotes_on_jira_eval_but_is_known_bad_on_runtime` already captures this.
4. **Noise floor close to threshold.** Bootstrap 95% CI on v8 Δr@10 ≈ [+0.026, +0.054] — half-width ~1.4pp vs 2pp threshold. Single seed.

Recommended additional guards (ranked): (a) real ≥90/10 holdout fold, (b) per-project floor `Δr@10 ≥ -0.01 ∀ project n≥30`, (c) multi-seed variance check, (d) explicit runtime-benchmark co-gate (now tractable — file snapshots in `profiles/pay-com/benchmarks/`).

### 5. Untried levers (fresh perspective, NOT in ROADMAP)

**STRONG_CANDIDATE_EXISTS — query-parity mismatch never addressed across 8 iterations:**

- `prepare_finetune_data.py::build_query_text` → `summary + description + comments` (v6.2 avg 517 chars).
- `eval_finetune.py:176` → raw `task["summary"]` (~80 chars).
- Runtime queries (memory file) → ~61 chars.

Same class of distributional bug that killed v5 (train-diff vs eval-chunk), just on the query side. Not in ROADMAP §Mistakes. Effort 2–3h, HIGH probability.

Other viable (lower ROI): v6.2+v8 ensemble score-average (MED-HIGH), listwise group-size expansion (v8 avg 13.9 docs/group vs cap 32), knowledge distillation GTE→MiniLM (fixes latency directly).

### Decision matrix synthesized

| Criterion | v6.2 | v8 |
|---|---|---|
| Runtime queries | tie | **WIN +0.083** |
| Runtime realworld | tie | **WIN +0.021** |
| Runtime regressions | none | none |
| Jira Δr@10 aggregate | +4.30pp | +3.92pp |
| Jira ΔHit@5 aggregate | +5.7pp | **+6.9pp** |
| **PI project (n=44)** | −0.55pp r@10, −2.27pp H@5 | **−2.48pp r@10**, −2.27pp H@5 |
| MCP top-K alignment | 76% (K≥6, r@10) | 24% (K≤5, Hit@5); top-5 ordering at K=10 still benefits |
| Model size / latency | 149M (≥2× p95) | 285MB bf16 (unverified, faster in this benchmark) |
| Gate status (new) | PROMOTE | PROMOTE |

### Blockers for immediate deploy

1. **PI regression in BOTH candidates.** NEXT_SESSION_PROMPT rule was "per-project parity OK" → literal reading = block. Magnitude ~1 ticket (n=44 small), but the rule is explicit.
2. **Gate has no holdout** (test=5, train=904). All PROMOTE claims are in-training-distribution. Critical — needs fix before future FT cycles, but does NOT block current deploy decision since runtime numbers are independent.
3. **Runtime sample = 10 queries.** Directional but low statistical power.

### What is NOT blocking (resolved by runtime run)

- v6.2/v8 "mixed or worse" runtime claim (from pre-gate-fix audit) → REFUTED for v8 (wins both). For v6.2 → tie not worse.
- "2× latency" claim → not reproduced in this benchmark (v8 actually finished faster). Dedicated latency micro-bench would need `benchmark_rerank_ab.py` if it exists; not run here.

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
| **gte_v8** | — | **PROMOTE** | +0.039 | **+0.069** | +78 |

**Interpretation:** All four models look like Jira-eval wins under a sensible gate. The gate is a *necessary* filter on FT iteration cycles. It is NOT sufficient for production swap — runtime benchmarks (latency, realworld/queries) and per-project parity remain separate gates. v7's passage here is precisely why the gate alone is insufficient: v7 has known 2× latency and mixed runtime benchmarks from prior audits.

Tests: 316 pass (27 new in `tests/test_eval_verdict.py`, 6 new in `tests/test_listwise_conversion.py` for v8 data converter). Single-process and sharded eval now share one verdict function. See `scripts/eval_verdict.py` module docstring for rationale.

---

## Next steps (post-gate-fix)

Ordered by ROI given the **quality-over-latency** priority update:

1. **[TOP PRIORITY] Runtime benchmark + per-project parity for v6.2 and v8.**
   Prior audits warned v6.2 has "mixed or worse" runtime numbers, but that was captured before we fixed the eval gate. Need fresh runs:
   - `python scripts/benchmark_queries.py` — curated synthetic queries
   - `python scripts/benchmark_realworld.py` — real-world distribution
   - Per-project breakdown on `gte_v6_2.json` / `gte_v8.json` (do CORE/BO/PI/HS each net-win, or does one project hide the rest?)
   If one of v6.2 / v8 passes both, **ship it** — latency is no longer blocking.

2. **Model selection if both pass:** pick v6.2 (best r@10) or v8 (best Hit@5) based on which metric aligns with downstream RAG use-case. Current pipe takes top-10 → v6.2 is the default; but MCP surfaces top-5 to the user often → v8 might be better ergonomically.

3. **❌ Graph retrieval POC — DONE 2026-04-20, does not ship.** +2.85pp r@10 on low-recall 100 tickets but collapses to +0.33pp with Hit@5/MRR regression on full 832. See `graph_boost_poc_2026-04-20.md`.

4. **Real-query eval from `tool_calls.jsonl`** (1-2 days, LLM-assisted labeling). 1,174 unique queries. Stratify cap 10/session (top-3 sessions = 45% of queries — single-dev workflow-replay). Use as regression guard, not primary metric.

5. **If v6.2/v8 do NOT pass runtime benchmarks** (same mixed/negative pattern as prior audit), next untried levers:
   - Freeze bottom 6 ModernBERT layers (62k rows / 149M params is under-regularized).
   - Dense-neighbor hard negatives (currently ALL negatives from FTS top-50 — orthogonal axis untried).
   - v8 + longer max-length (128 forced by MPS OOM; on GPU could try 192-256 for listwise).

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
| `reranker_ft_gte_v6_2/` (2GB) | Best r@10 FT: +4.30pp, 40 regressions, val loss 0.0349 | `profiles/pay-com/models/reranker_ft_gte_v6_2/` |
| `reranker_ft_gte_v8/` (285MB bf16) | Best Hit@5 FT: Δr@10 +3.9pp, ΔHit@5 +6.9pp, 45 regressions, listwise LambdaLoss | `profiles/pay-com/models/reranker_ft_gte_v8/` |
| `finetune_data_v4/` (104M) | v4 training set (66,522 pointwise rows) | `profiles/pay-com/finetune_data_v4/` |
| `finetune_data_v6_2/` (97M) | v6.2 training set (61,250 pointwise rows) | `profiles/pay-com/finetune_data_v6_2/` |
| `finetune_data_v8/` (16M) | v8 training set (879 listwise groups, derived from v6.2 via convert_to_listwise.py) | `profiles/pay-com/finetune_data_v8/` |
| `gte_v1.json` | Baseline eval snapshot (needed for `--reuse-baseline-from`) | `profiles/pay-com/finetune_history/` |
| `gte_v4.json`, `gte_v6_2.json`, `gte_v8.json` | Best FT evals | `profiles/pay-com/finetune_history/` |
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
| **v6.2** | **v6.1 - skip-empty-desc + drop-generated + max-rows 300 + oversample PI=5 + min-query-len 30** | **0.0349** | **+4.30** | **40** | **HOLD (best r@10 FT)** | **Best r@10. All runtime benchmarks mixed or worse. 2× latency.** |
| v7 | v6.2 + `--fe-hard-negatives 4` (inject from FE cluster) | 0.0354 | +3.39 | 52 | REJECT | FE neg injection was 99.7% graphql (first in cluster, FTS broke early). Model learned "avoid graphql" too broadly. Hypothesis (BO→FE leakage) was also likely wrong per audit. |
| **v8** | **v6.2 data reformatted listwise + `--loss lambdaloss` (NDCG-optim)** | n/a | **+3.92** | **45** | **PROMOTE (sidegrade)** | **Listwise worked as designed on Hit@5: ΔHit@5=+6.9pp (vs v6.2's +5.7pp, best top-5 ever). Δr@10 slightly below v6.2 (-0.4pp). Same 2× latency. Can replace v6.2 if top-5 browsing matters more than top-10 recall.** |

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

### P1. ~~Graph-boosted retrieval~~ — ❌ POC FAILED 2026-04-20
Tested blanket 1-hop neighbor boost on FTS top-200. Result: +2.85pp r@10 on low-recall 100 tickets but on full 832 only +0.33pp with Hit@5/MRR regression. Shelved. See `graph_boost_poc_2026-04-20.md`. Conditional boost (apply only when baseline confidence low) remains untried but is a bigger project.

### P1. Query rewriting / identifier extraction
**Claim:** Zero experiments. Reranker can't fix queries missed by recall.
**Validation to run:**
- Agent: "What specific rewrites help code search? Naive synonym expansion, AST-aware identifier extraction, LLM rewrite? Which are cheapest to ROI?"
- Agent: "On our eval set, how many tickets are 'zero recall' (GT not in top-200 FTS output)? If small, query rewrite has low ceiling."
**Effort:** 1-2 days exploration + small implementation. Biggest untouched axis.

### P2. ✅ v8 FT (listwise LambdaLoss) — DONE 2026-04-20, sidegrade
See §"Next steps" #3 above for results. Remaining untried levers from the original v8 candidate list: freeze bottom layers, dense-neighbor hard negatives, longer max-length on GPU.

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
- Test suite (316 tests after 2026-04-20 v8 work) must pass before any changes land: `python3.12 -m pytest tests/ -q`.
