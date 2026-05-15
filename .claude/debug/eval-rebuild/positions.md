# Eval-rebuild Architecture Debate — positions.md

Decision: rebuild `profiles/pay-com/doc_intent_eval_v3_n200.jsonl` labels
via **dual-Opus auto-labeling all 200 queries + 3rd-pass Opus disagreement
resolver**, vs. targeted hand-grade-tier (Position N).

## Position A — Defender (Round 1)

**A1. Full coverage kills cherry-pick bias.** Position N's hand-grade-tier
labels a "important" subset (e.g. n=30 OFF or top-30 prod-traffic); the
other ~170 stay heuristic. The Pearson r=0.446 / mean |Δ|=0.46 finding in
`llm_judge_opus.md §1` and `llm_judge_opus2.md` macro table shows the
heuristic miscounts **systematically**, so every unlabeled row keeps a
46pp absolute label-noise floor. Re-grading all 200 makes every
`expected_paths` actually mean what an LLM says is relevant; n=200 fully
covered also matches the stratification in `build_doc_intent_eval_v3.py`
(kept_v1, kept_v1_path_dirty, gold-false), so no row is second-class.

**A2. Reproducible, cheap, parallel.** Two Opus passes × 200 queries × top-20
chunks ≈ 8000 graded snippets. With seed=42 sampling (already proven by
`.claude/debug/p10-judge/extract_candidates.py`) and a fixed rubric, the
labels replay deterministically. 5–7 sharded agent runs by `query_id`
finish in <30 min wall-clock at $0 incremental cost — the prior 30-query
P10 pass took ~12 min per `llm_judge_opus.md`. Hand-grading 200 × ~5
candidates = 1000 decisions at ~30s each ≈ **8+ hours** of focused human
labor that must be redone whenever the rubric shifts.

**A3. Dual + 3rd-pass resolver mitigates judge bias.**
`feedback_code_rag_judge_bias.md` warns Opus is code-biased; we mitigate
not by averaging two correlated runs but by giving Pass-1 and Pass-2
**different rubric framings**: Pass-1 = "is this snippet load-bearing
for answering the query?", Pass-2 = "would a senior dev cite this exact
file in a code review?". Disagreements (|Δscore| ≥ 1) get a Pass-3 prompt
showing both prior judgments and an explicit resolve-disagreement
instruction. Per `llm_judge_opus2.md` cross-check methodology, this is a
real DERA-style triangulation; the current eval has zero such cross-check
— upgrading to 2-of-3 LLM consensus is strictly more independent than
status quo.

**Acknowledged strongest counter.** Position N's sharpest attack:
*"Two Opus runs are not independent — same training data, same
instruction-tuning, same systematic biases per
`feedback_code_rag_judge_bias.md`. Auto-labeling 200 queries with two
correlated judges yields 2× higher-confidence-but-still-wrong labels.
A small hand-graded gold set of 30–50 queries is true ground-truth; the
rest can stay heuristic."*

## Position A — Pre-rebuttal of strongest counter

The "Opus-self-judge is correlated, not independent" objection is the
strongest attack and partially correct — but it under-weighs three
concrete mitigations and over-weights hand-grading's own bias.

**1. Rubric-shift breaks the correlation surface.** Identical-rubric
double runs only average noise. Position A's design uses **load-bearing**
(Pass-1) vs **citation-worthy** (Pass-2) framings — different decision
boundaries: load-bearing scores ≥2 if the answer can be reconstructed
from the snippet; citation-worthy scores ≥2 only if a reviewer would link
the file. In the 30-query P10 sample (`/tmp/p10_a2_judge_scores_opus.json`
vs `_opus2.json`) only 7/30 query_ids overlap by string match
(`llm_judge_opus2.md §cross-check`), so internal-judge correlation is
already empirically <1.0. Disagreements expose exactly the edge cases
hand-grading would also have to resolve; the 3rd-pass resolver, seeing
BOTH framings, mimics a human toggling between "useful" and "canonical".

**2. Hand-grading n=30 isn't a gold standard either.** A single human
grader has recency, fatigue, and prompt-priming biases. Per
`llm_judge_opus.md §5 confounds`, even the LLM judges flagged "score 2 is
permissive" and dup-chunk inflation — biases a single human would also
miss. Dual-Opus + resolver at n=200 gives an **auditable, replayable**
noise model (re-run with a new seed → measure drift); n=30 hand-graded
**cannot be cheaply replayed** when the eval grows to cover the 55.9%
"unknown" prod stratum from `falsifier2_token_coverage.md`.

**3. Calibration loop bounds the bias.** The 192-query A2 vs baseline in
`/tmp/p10_a2_dedupe.json` and `/tmp/p10_rerank_on_dedupe.json` already
supplies per-query heuristic-R@10. If new dual-Opus labels reproduce the
r=0.446 vs heuristic but tighten r(judge1, judge2) ≥ 0.75 with
disagreement <15%, that's quantifiable bounded bias — a calibration
footprint hand-graded n=30 literally cannot produce.

---

**3-line summary:**

- Strongest argument: full n=200 coverage + reproducible + $0 cost + parallel <30 min beats N's n=30 hand-grade that leaves ~170 rows at the proven 46pp heuristic noise floor.
- Biggest counter: two Opus runs are correlated (shared training/biases per `feedback_code_rag_judge_bias.md`), so dual-Opus is 2× same-error, not real cross-validation.
- Rebuttal: rubric-shift between Pass-1 (load-bearing) and Pass-2 (citation-worthy) decorrelates judges (only 7/30 overlap in P10 evidence); 3rd-pass resolver on |Δ|≥1 gives auditable bounded bias that a single human grader at n=30 cannot replay.


## Position B — Challenger (Round 1)

**Proposal:** rebuild eval-v3-n200 by deep-grading the 30-50 most-uncertain queries (where heuristic and LLM-judged relevance disagree most) via 3+ Opus passes per (query, candidate) pair with rotating rubric framings. Leave the other ~150 queries untouched — heuristic is good enough on the easy ones.

### Argument 1 — The eval is mostly saturated; uniform re-labeling spends 75% of compute on no-op rows.

Evidence from `/tmp/p10_a2_dedupe.json` + `/tmp/p10_rerank_on_dedupe.json`:
- 92/192 queries have **identical extreme R@10** under both A2 and rerank-on (saturated 0.0 or 1.0). These rows give zero discrimination signal — re-labeling them changes nothing about how the eval ranks two systems.
- Only **21/192** queries (10.9%) disagree at all between A2 and ON. Only **10/192** (5.2%) disagree by ≥0.30 R@10.
- Position A's "label all 200" budget is therefore ≥75% spent on rows whose label is already determined by retrieval saturation.

### Argument 2 — G2 calibration shows bias is concentrated, not uniform.

`.claude/debug/a2-verification/llm_judge_opus.md` §2 + `llm_judge_opus2.md` per-stratum table:
- Bias is large on **tail (n=50), payout (n=21), provider (n=23), nuvei (n=23)** — `Δ rel −0.120` on nuvei alone (Judge #2). Heuristic over-states by ~3.5× on these strata.
- Bias is small or zero on **aircash (n=9), interac (n=9), trustly (n=4), webhook (n=23, mostly tied)**. Judge #2: KEEP/UNK strata "bit-for-bit identical, 15/15."
- A targeted re-label of the ~80 queries in tail+payout+provider+nuvei (where bias lives) buys ≥90% of the label-quality gain at ≤40% of the cost.

### Argument 3 — Multi-pass on hard queries beats single-pass on everything (per the project's own bias rule).

`feedback_code_rag_judge_bias.md` (auto-memory): *"Opus code-biased, MiniLM prose-biased; always cross-check 2 judges + calibrate on gold labels."* Single-pass labeling at any volume violates this. Position A's "dual-Opus" passes Opus twice — two correlated samples from the same biased distribution, not two independent judges. **3 Opus passes with rotated rubric framings** (relevance / direct-answer-presence / does-this-pin-down-the-fix) approximate the cross-check intent for queries where signal matters; cheap heuristic suffices on the saturated rows.

### Argument 4 — We need a hand-vetted gold subset anyway; this builds it as a side-effect.

The eval has zero rows the project can claim are *gold-by-construction* (Judge #1 file empty, Judge #2 cross-check status DISAGREE-or-MISSING per `llm_judge_opus2.md` §"Cross-check vs Judge #1"). Deep-graded 30-50 = the gold-strata seed for every future labeler we A/B (P8 router, P9 reranker-off-for-doc-intent, future FT runs). Auto-labeling 200 leaves us still without that anchor.

**Cost (concrete):** 50 queries × 10 candidates × 3 passes × ~30s/grade = ~12.5 hours wall, fits one autonomous session. 200 × 10 × 1 = ~5.5 hours but each label is a single-judge measurement we already know is biased.

### Acknowledged strongest counter

Position A's strongest attack is selection-bias: *"Choosing the 30-50 'most-uncertain' queries by `|heuristic R@10 − LLM rel-rate|` over-samples the rows where the heuristic was already broken. The resulting eval becomes a 'where-heuristic-fails' subset; n=200 with uniform labels eliminates that bias and is comparable across runs without re-sampling."*

### Pre-rebuttal of that counter

The attack is real but **bounded and addressable**:
1. The selection metric is **disagreement-magnitude**, not heuristic-error-direction. It captures both heuristic-over and heuristic-under cases (Q9 trustly: heuristic-under, +0.45 rel; nuvei dup-saturation: heuristic-over, −0.120 rel). It is direction-agnostic.
2. The other ~150 rows **stay in the eval** with their existing heuristic labels. Macro R@10 / NDCG remain comparable to historical runs; the 30-50 deep-graded rows form an additional `eval_v3_gold` slice reported alongside macro.
3. With **92/192 saturated**, uniform re-labeling cannot avoid spending most cost on rows where label is fixed by retrieval. The "selection bias" critique applies symmetrically: A's plan is biased toward expensive labels on rows that don't matter.

(R1 + pre-rebut: ~575 words.)
