# Significance Critic — T3 Verdict

**Date:** 2026-04-24
**Owner:** significance-critic
**Eval set:** `profiles/pay-com/doc_intent_eval_v1.jsonl`, **n=44**, all `gold=False`
**Method:** Wilson 95% CI on binomial; paired McNemar simulation (2 000 trials per cell, seed=20260424); Bonferroni / BH-FDR on 10 pairwise tests.

---

## TL;DR

> **44 queries cannot reliably distinguish the 5 candidate models.**
> Single-arm 95% CI half-width ≈ **±13–14 pp**. Paired McNemar MDE on n=44 ≈ **18 pp** at α=0.05/power=0.80. Under 10-way Bonferroni, MDE blows up to **≈25 pp**. None of the realistic candidate gains (nomic-v2-MoE blog +3-5 pp, gte-large +5-10 pp, fine-tunes +2-8 pp) survive that threshold.
>
> **Recommendation: FIX-EVAL-FIRST. Expand to ≥200 labeled queries before any A/B that asks "is candidate X better than baseline?".**

---

## S0 — Eval-set bug found before measurement (critical)

While instrumenting S1, discovered a **schema mismatch between `build_doc_intent_eval.py` and `benchmark_doc_intent.py`** that makes the bench unable to score the current eval file:

| File | Field used |
|---|---|
| `build_doc_intent_eval.py:344` | writes `expected_paths` and sets `"gold": False` for all 44 rows (`labeler="auto-heuristic-v1"`) |
| `benchmark_doc_intent.py:196,199` | reads `row.get("gold")` and `row["expected_files"]` |

Effect: `n_gold_queries = 0`, `recall_at_10 = None` for **every** model run today. Any "baseline" claim from the unmodified script is vacuous. This is a P0 blocker independent of statistics — escalate to team-lead before claiming any A/B result.

The S2-S6 numbers below treat the auto-heuristic `expected_paths` as **pseudo-gold** (acknowledging the labels are not independent of the BM25/path-overlap heuristic that generated them — see eval-critic for the bias argument). Pseudo-gold gives an *upper* bound on apparent recall and a *lower* bound on signal cleanliness; the statistical limits below hold regardless of label quality.

---

## S1 — Baseline Recall@10 (incumbent `docs` = nomic-embed-text-v1.5)

**Status: NOT MEASURED THIS RUN.** Pre-flight gate `PREFLIGHT_AVAIL_HARD_GB=5.0` (`benchmark_doc_intent.py:52`) failed (`avail=2.90 GB`). Same gate will block the user on a 16 GB Mac mid-day; reproducible only after RAM-pressure abates or daemon is unloaded (`/admin/unload`, see project_daemon_p0_landed).

**Workaround for the analysis below:** evaluate three plausible baseline scenarios so the conclusions don't depend on the unknown `p_base`:

| Scenario | p_base (Recall@10) | rationale |
|---|---|---|
| low | 0.30 | docs tower mostly misses (BM25 already in pipeline) |
| mid | 0.50 | nomic-v1.5 with docs prefix gets half right |
| high | 0.70 | strong incumbent; A/B chasing diminishing returns |

When the user re-runs after `kill -9 $(lsof -ti:8742); sleep 2`, drop the actual `p_hat` into the **mid** row above. **MDE conclusions do not change** — see S3.

---

## S2 — Bootstrap / Wilson 95% CI on n=44

Bootstrap on 44 binomial outcomes is a thin layer over the analytic Wilson interval; both give the same answer to within Monte-Carlo noise. Reporting Wilson because it is reproducible and closed-form:

| Scenario | k/n | Wilson 95% CI | half-width |
|---|---|---|---|
| low (p=0.30)  | 13/44 | [0.182, 0.442] | **±13.0 pp** |
| mid (p=0.50)  | 22/44 | [0.358, 0.642] | **±14.2 pp** |
| high (p=0.70) | 31/44 | [0.558, 0.818] | **±13.0 pp** |

**Reading**: a single-arm reading of "model X scored 0.55" on this eval is consistent with **anything from 0.41 to 0.69** at 95% confidence. **No reading is precise enough to deploy on its own.** This is the case BEFORE comparing two models.

The bootstrap script (`/tmp/significance_baseline.py:bootstrap_ci`) is wired and ready; rerun once baseline data exists. CI from 10 000 resamples will land within ~0.5 pp of the Wilson values above.

---

## S3 — MDE for paired comparison on n=44

Paired McNemar test on the same 44 queries (incumbent vs candidate). Simulation: A_i ~ Bern(p_base), B_i strictly dominates A (optimistic — gives MDE *lower bound*; realistic MDE is somewhat worse).

| p_base | MDE @ α=0.05, power=0.80 |
|---|---|
| 0.30 | **18 pp** |
| 0.50 | **18 pp** |
| 0.70 | **18 pp** |

**Verdict: ANY claimed lift below ~18 pp on n=44 is statistically indistinguishable from sampling noise.**

Cross-check against expected candidate gains (from `project_docs_model_research_2026_04_24.md`):

| Candidate | Expected lift vs nomic-v1.5 | Detectable? |
|---|---|---|
| nomic-v2-moe (drop-in) | +3-5 pp (per nomic blog) | NO — buried in noise |
| gte-large-en-v1.5 | +5-10 pp (MTEB-extrapolated) | borderline NO at upper end |
| arctic-l-v2 | +5-8 pp (estimate) | NO |
| bge-m3-dense | +3-7 pp (estimate) | NO |
| Tarshevskiy/v0 (10 FT pairs) | +1-3 pp (small data) | NO |
| Tarshevskiy/v1 (103 FT, Stage D) | +5-15 pp (data-scaled est.) | only at the very top |

**None of the realistic candidate uplifts clear the n=44 noise floor.**

---

## S4 — Per-comparison MDE for the 5 intended matchups

All five vs-baseline contrasts share the same n and same MDE budget. **MDE = 18 pp** per pair (from S3). Pairwise among 5 models = C(5,2) = 10 tests; see S5 for correction.

---

## S5 — Multiple-comparisons correction (10 pairwise tests)

If we run 10 pairs at α=0.05 each, family-wise error rate = 1 − 0.95¹⁰ ≈ **40%** — i.e. ~40% chance of at least one false "candidate beats baseline" call by chance alone. This is the dominant risk when shopping for a winner across 5 models.

| Procedure | Per-test α | Notes |
|---|---|---|
| Bonferroni | **0.005** | Strict FWER control |
| BH-FDR (rank 1) | 0.005 | Same as Bonferroni for the smallest p |
| BH-FDR (rank 10) | 0.050 | Original α at the largest rank |

**MDE recomputed under Bonferroni (α=0.005, power=0.80, p_base=0.5, n=44): ≈ 25 pp.**

A 25-percentage-point improvement is **larger than any plausible candidate model can deliver**. Under proper correction, n=44 cannot select a winner among the 5 candidates at conventional significance — period.

Pragmatic alternative: pre-register the **single most promising candidate** (currently nomic-v2-moe per the 2026-04-24 update) as the only test → α=0.05 → MDE returns to 18 pp. Still below the +3-5 pp that nomic-v2-moe is expected to deliver, so even the single-test path fails on n=44.

---

## S6 — Sample sizes required to make this eval honest

McNemar power simulation, target power=0.80, two-sided α=0.05, p_base=0.5:

| Target MDE | n required (single test) | n required under 10-way Bonferroni |
|---|---|---|
| **+10 pp** | **~100** | ~150 (extrapolated) |
| **+5 pp** | **~200** | ~400 (extrapolated) |
| **+3 pp** (nomic-v2 floor) | ~500 | ~1 000 |

p_base sensitivity for +5 pp MDE: **n≈200 holds across p_base ∈ {0.3, 0.5, 0.7}** — the requirement is robust to the unknown baseline.

### What this maps to in user effort

- **44 → 200 = +156 queries to manually triage.** With auto-heuristic-v1 already producing candidate paths, human relabeling cost ≈ 30 s/query → **~80 min one-shot work**.
- **Auto-heuristic-v2** path: extend `build_doc_intent_eval.py` to pull from a wider sample of `tool_calls.jsonl` (3 000+ rows) and apply stronger validation (e.g. cross-reranker-vote agreement). Cheaper but inherits the same independence-from-system bias eval-critic flags.
- **Hybrid:** auto-heuristic-v1 for n=200, then sample 50 for human spot-check; calibrate auto labels against the 50 to weigh down systematic bias. ~40 min spot-check, recovers most of the statistical signal.

---

## Concrete recommendation to team-lead (T4 input)

**FIX-EVAL-FIRST.** Two non-negotiables before re-running A/B:

1. **Patch the schema bug** (`benchmark_doc_intent.py` reads `expected_paths` not `expected_files`; treat `gold=False` as pseudo-gold OR re-label some rows as gold). One-line fix; without it every A/B claim is `recall=None`.
2. **Expand eval to n≥200** (human-spot-checked) before claiming any candidate beats incumbent. Without this, the 5-way A/B has a ~40% chance of crowning a noise-driven winner.

If user wants to proceed despite n=44, the only defensible posture is:
- Single pre-registered candidate (nomic-v2-moe) — not a 5-way bake-off.
- Report effect with CI, not point estimate. Say "Δ = +X pp [−13, +13]" and refuse to deploy on overlapping CIs.
- Treat any reported lift <+18 pp as "no decision".

---

## Reproducibility

Script: `/tmp/significance_baseline.py` (not committed; one-off analysis; rerun w/ `CODE_RAG_HOME=~/.code-rag-mcp python3.12 /tmp/significance_baseline.py` after RAM frees).
Seed: 20260424 across bootstrap, McNemar simulation, sample-size search.
Env at run time: avail=2.90 GB (baseline run skipped per pre-flight gate at `benchmark_doc_intent.py:52`).
