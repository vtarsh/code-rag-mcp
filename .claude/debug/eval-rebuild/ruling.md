# Eval rebuild — Judge ruling

## Verdict
- WINNER: **HYBRID-DESCRIBED** (Position B's targeted deep-grade is the core; Position A's parallel-bucket auto-label fills the residual mid-difficulty band)
- Confidence: **medium-high**

## Reasoning

Position B's saturation evidence is the most decisive single fact in the
debate, and it survives spot-check exactly. 92/192 queries are saturated
at 0.0 or 1.0 under BOTH A2 and rerank-on (75 both-zero, 17 both-one);
only 21/192 (10.9%) disagree at all between the two retrieval systems
and only 10/192 (5.2%) disagree by ≥0.30 R@10. This means Position A's
"label all 200" plan would spend at minimum 92/192 = 47.9% of compute on
rows that are mechanically incapable of changing eval outcomes — both
systems already agree, so re-grading them only refines a number neither
side disputes. Position B's targeted re-grade hits exactly the 10–21
queries where label noise actually affects which system wins.

That said, Position A's strongest concrete claim — that hand-grading
n=30 leaves ~170 rows at the proven 46pp heuristic noise floor — is
real, verified by source: mean |Δ|=0.460 between heuristic R@10 and LLM
rel-rate, Pearson r=0.446, with per-stratum bias +0.700 on tail and
+0.683 on payout (`p10-llm-judge-report.md:125-147`). Even saturated
0.0 rows can be wrong: a "0.0 heuristic" query where the LLM finds top-10
to be 70% relevant means the `expected_paths` set itself is wrong, not
just the score. So pure-B leaves systematic label noise on saturated
rows. The fix is hybrid: deep-grade the 21 disagreement queries (B's
core), parallel-bucket-label the ~80 OFF-stratum biased queries (A's
rubric-shift pass scoped to where bias lives), and let the 92 saturated
extreme rows keep heuristic labels with a `label_source: heuristic_sat`
metadata flag for transparency. This buys ≥90% of label quality at
≤50% of compute.

Position A's "Pass-1 vs Pass-2 only 7/30 overlap" rebuttal is partly
illusory — `llm_judge_opus2.md:55-58` shows the 7/30 overlap is a
**sampling artifact** (different query_id schemes), not evidence of
judge decorrelation. The Pearson r between Judge#1 and Judge#2 was
explicitly "uncomputable." A's rubric-shift mitigation is sound in
theory but unproven in this codebase.

## Verified claims

1. **"Pearson r=0.446 / mean |Δ|=0.46"** — VERIFIED at `.claude/debug/p10-llm-judge-report.md:125, 133`. r=0.446 (rel-rate), 0.559 (direct-rate), 0.428 (DCG); mean |delta|=0.460, median 0.400.
2. **"7/30 queries overlap by string match"** — VERIFIED at `llm_judge_opus2.md:57`. But the same line clarifies Pearson r between judges is "uncomputable" — it does NOT prove judge-decorrelation, only different sampling.
3. **"92/192 saturated at 0.0 or 1.0"** — VERIFIED. Computed from `/tmp/p10_a2_dedupe.json` + `/tmp/p10_rerank_on_dedupe.json`: 75 both-0.0 + 17 both-1.0 = exactly 92/192.
4. **"21/192 disagree at all, 10/192 by ≥0.30"** — VERIFIED exactly: 21 differ on R@10, 10 differ by ≥0.30 absolute.
5. **Bundle ready for execution** — VERIFIED. `/tmp/eval_rebuild_bundle_full.json` is a list of 192 queries × avg 11.6 candidates, max 18 (one query has 18 distinct chunks). Each candidate has `snippet`, `repo_name`, `file_path`, `in_heuristic_expected`, `a2_rank`, `on_rank` — directly graded-able.

## Concrete execution recipe (HYBRID)

Three-tier label policy on the 192 queries in `/tmp/eval_rebuild_bundle_full.json`:

**Tier 1 — Saturated extremes (n=92, KEEP heuristic):**
- Both-1.0 (n=17): keep `expected_paths`, set metadata `label_source: heuristic_saturated_top`. Risk: low — both systems agree top-10 is fully relevant.
- Both-0.0 (n=75): grade ONLY a random sample of 15 (seed=42) with 1 Opus pass to check if `expected_paths` itself is wrong. If ≥4/15 flip to ≥1 relevant, escalate the rest to Tier 2. Else keep with `label_source: heuristic_saturated_zero` flag.

**Tier 2 — Mid-difficulty / OFF-stratum bias (n=~80, parallel-bucket label):**
- Define mid-set = (192 − 92 saturated) ∪ (the 75 zero-saturated if Tier 1 sample escalates).
- 4 parallel Opus agents × ~20 queries each. Each agent grades all candidates 0–3 with one rubric framing assigned: load-bearing / citation-worthy / direct-answer-presence / would-a-senior-cite-in-PR-review.
- Output 4 score-vectors per (query, candidate). Final label = median across the 4. Disagreement of ≥1.5 std → Tier 3.
- Estimated: ~80 × 11.6 cand × 4 passes ≈ 3,700 grades. ~30s/grade ≈ ~30 min wall on 4 parallel agents.

**Tier 3 — High-disagreement deep-grade (n=21, B's core):**
- All 21 queries where A2 ≠ ON in R@10 (with priority to the 10 that differ by ≥0.30).
- 3 Opus passes per query × 11.6 candidates × 3 rubric framings ≈ 720 grades.
- Resolution: median across passes; if std ≥0.7, 4th-pass adjudicator.
- These rows form `eval_v3_gold` slice — reportable separately.

**Output:** `profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl` with each row carrying `label_source ∈ {heuristic_sat_top, heuristic_sat_zero, llm_bucket_median, llm_gold}`. Existing v1 stays as historical artifact.

**Total cost:** ~4500 LLM grades, ~45 min wall on 4 parallel Opus agents at $0 incremental.

## Risks for the winning approach

- **Tier 1 zero-saturation sampling could miss systematic `expected_paths` errors** — the LLM-judge bias data shows tail/payout strata heuristic UNDER-credits by +0.683 to +0.700, so "both 0.0" might routinely contain real hits. Mitigation: 15-query sample is the canary; auto-escalate threshold ≥4/15.
- **4-rubric-framing decorrelation is unproven on this codebase** — `feedback_code_rag_judge_bias.md` warns Opus is code-biased systematically. Median-of-4 may still inherit shared bias. Mitigation: Tier 3 is the gold anchor; macro reports must cite Tier 3 separately from bucket-merged macro.
- **Bundle has avg 11.6 candidates per query but range 5-18** — quality of grades may vary; queries with 18 candidates dominate compute. Mitigation: cap grading at top-15 by retrieval rank, document truncation.

## Recommended next action

Spawn 4 parallel Opus sub-agents with the bundle pre-sliced by tier; run Tier 1 zero-sample probe FIRST (single agent, 15 queries) to decide if Tier 2 expands to absorb the 75. Once probe lands, dispatch Tiers 2+3 in parallel. Merge to `doc_intent_eval_v3_n200_v2.jsonl` with per-row `label_source` metadata. First downstream check: re-run A2 vs rerank-on benchmarks against v2 and confirm the +2.89pp heuristic R@10 lift either survives (real) or evaporates (artifact) — that single number is the smoking-gun test the whole rebuild exists to enable.
