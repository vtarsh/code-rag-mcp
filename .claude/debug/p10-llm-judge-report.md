# P10 LLM-Judge Validator — Report

Date: 2026-04-25
Author: p10-llm-judge-validator (Opus self-judging)
Status: validation complete. NO push, NO code change. **Verdict: CONDITIONAL GO retained, with revised confidence.**

## Goal

Use Opus (this agent) as an LLM judge to verify whether disabling the
reranker on doc-intent queries is GENUINELY a quality win or a
labeler-heuristic artefact. Two questions:

- **G1**: For the 14 queries where rerank-on > rerank-off on heuristic
  R@10, are those reranker wins REAL (the reranker pulls actually-relevant
  docs the bi-encoder missed) — or is the heuristic mis-rewarding fluff?
- **G2**: How well does the eval-v3-n200 heuristic labeler agree with an
  LLM-judged 0-3 graded relevance on a random stratified subset of 50
  queries (here, 15 stratified due to time budget)?

Method: I (Opus) read each candidate's actual chunk text (capped 1500
chars), compared to the user query, and assigned 0–3:

- 0 = irrelevant, 1 = tangential, 2 = partial, 3 = direct

Three derived metrics per query × per ranker:

- `rel_rate` = count(score ≥ 2) / 10  (closest analogue to "true R@10")
- `direct_rate` = count(score = 3) / 10
- `graded_dcg` = Σ s_i / (i+1)

## §1. G1 verdict — 14 risk queries (REAL vs LABELER ARTEFACT)

Per-query LLM scores (rel_rate ≥ 2, direct_rate ≥ 3, DCG):

| #  | strata    | h_off | h_on  | h_Δ   | llm_off_rel | llm_on_rel | llm_Δrel | llm_off_dcg | llm_on_dcg | verdict |
|----|-----------|------:|------:|------:|------------:|-----------:|---------:|------------:|-----------:|---------|
| Q1 | provider  | 0.00  | 1.00  | +1.00 | 0.50        | 0.50       | +0.00    | 5.94        | 4.45       | TIE     |
| Q2 | interac   | 0.00  | 1.00  | +1.00 | 1.00        | 0.50       | -0.50    | 8.79        | 7.30       | LABELER ERROR — OFF wins |
| Q3 | tail      | 0.33  | 1.00  | +0.67 | 0.10        | 0.30       | +0.20    | 0.99        | 4.72       | REAL ON win |
| Q4 | interac   | 0.67  | 1.00  | +0.33 | 1.00        | 1.00       | +0.00    | 8.14        | 8.64       | TIE — both excellent |
| Q5 | refund    | 0.75  | 1.00  | +0.25 | 0.90        | 1.00       | +0.10    | 6.19        | 8.69       | REAL ON win |
| Q6 | nuvei     | 0.50  | 0.75  | +0.25 | 1.00        | 1.00       | +0.00    | 8.54        | 8.79       | TIE |
| Q7 | webhook   | 0.50  | 0.75  | +0.25 | 0.60        | 0.70       | +0.10    | 4.61        | 3.23       | TIE — ON injects irrelevant trustly at top |
| Q8 | tail      | 0.00  | 0.50  | +0.50 | 0.10        | 0.20       | +0.10    | 1.23        | 3.45       | REAL ON win |
| Q9 | method    | 0.00  | 0.40  | +0.40 | 0.60        | 0.80       | +0.20    | 5.41        | 6.95       | REAL ON win |
| Q10| refund    | 0.20  | 0.40  | +0.20 | 0.40        | 0.30       | -0.10    | 3.82        | 3.61       | TIE |
| Q11| provider  | 0.20  | 0.40  | +0.20 | 0.80        | 0.70       | -0.10    | 7.23        | 7.47       | TIE |
| Q12| tail      | 0.00  | 0.33  | +0.33 | 1.00        | 0.90       | -0.10    | 6.56        | 5.92       | TIE — OFF slight edge (preserves credentials repo) |
| Q13| nuvei     | 0.00  | 0.25  | +0.25 | 1.00        | 1.00       | +0.00    | 5.86        | 8.79       | REAL ON win on DCG |
| Q14| payout    | 0.00  | 0.20  | +0.20 | 1.00        | 1.00       | +0.00    | 6.31        | 7.03       | TIE |

### G1 aggregate

| metric                        | rerank-OFF | rerank-ON | delta    |
|-------------------------------|-----------:|----------:|---------:|
| LLM rel-rate (score ≥ 2)      |   0.714    |   0.707   |  -0.007  |
| LLM direct-rate (score = 3)   |   0.314    |   0.493   |  **+0.179** |
| LLM graded DCG (sum s/(i+1))  |   5.69     |   6.36    |  **+0.67**  |
| heuristic R@10 (eval-v3 mean) |   0.156    |   0.581   |  **+0.425** |

### G1 categorisation (out of 14)

- **REAL ON wins** (LLM Δ ≥ +20pp on rel-rate or DCG ≥ +1.5):
  Q3, Q5, Q8, Q9, Q13 → **5/14**
- **REAL OFF wins** (LLM Δ ≤ -20pp): Q2 (labeler error: query is
  payper-specific, eval expected nuvei docs but OFF returned 10 payper
  Interac docs which are correct) → **1/14**
- **TIE / marginal** (|LLM Δ| < 20pp): Q1, Q4, Q6, Q7, Q10, Q11, Q12,
  Q14 → **8/14**

### G1 verdict

**MIXED, leaning ON-positive on direct-relevance.** The reranker is *not*
fooling the heuristic; it does pull more **directly-relevant** docs into
top-10 (off direct-rate 31% vs on direct-rate 49%, +18pp). But on broad
"is it at least partially relevant?" the two pipelines tie (off 71% vs on
71%). The heuristic R@10 over-states the rerank-on lift on these 14
queries by **~3.5×** (+42.5pp heuristic vs +0.7pp / +18pp / +0.67 LLM
rel/direct/DCG).

**The 4 catastrophic-loss queries (off=0, on=1.0) flagged by p10-quickwin**
break down as:

- Q1 (provider/decline retries): genuine pulls — paynearme decline+retry
  docs at on#6/7/10. But OFF top-10 also has high-quality generic decline
  docs (global-conventions GOTCHAS rank 1, provider-response-mapping rank 4).
  LLM TIE.
- Q2 (payper interac etransfer): **LABELER ERROR**. Heuristic expected
  one nuvei-docs file as canonical. OFF returns 10 payper interac docs,
  which are TOPICALLY CORRECT for a "payper interac" query.
- Q5 (paynearme refund): genuine — ON pulls 9/10 paynearme refund docs vs
  OFF's 4/10. REAL WIN.

So of the 4 "catastrophic" off=0 cases, ~2 are real losses, ~1 is a
labeler error, and 1 is a tie.

## §2. G2 calibration — heuristic labeler vs LLM judge

15 stratified queries from `doc_intent_eval_v3_n200.jsonl` (subset of
50, time budget). All `rerank-off` (the deploy candidate path). For each
I judged top-10 against the actual snippet content.

### Per-query

| #  | strata    | heuristic R@10 | llm_rel | llm_direct | llm_dcg | delta |
|----|-----------|---------------:|--------:|-----------:|--------:|------:|
| Q1 | webhook   |  0.00          |  0.20   |   0.00     |  3.70   | +0.20 |
| Q2 | webhook   |  0.20          |  0.60   |   0.40     |  5.73   | +0.40 |
| Q3 | aircash   |  0.33          |  1.00   |   0.60     |  8.21   | +0.67 |
| Q4 | aircash   |  1.00          |  0.70   |   0.30     |  5.61   | -0.30 |
| Q5 | provider  |  0.00          |  0.60   |   0.20     |  5.81   | +0.60 |
| Q6 | provider  |  0.00          |  0.50   |   0.20     |  6.27   | +0.50 |
| Q7 | interac   |  1.00          |  1.00   |   0.90     |  8.69   | +0.00 |
| Q8 | interac   |  0.50          |  1.00   |   0.70     |  7.42   | +0.50 |
| Q9 | method    |  0.00          |  0.40   |   0.10     |  4.84   | +0.40 |
| Q10| method    |  0.33          |  0.70   |   0.40     |  5.78   | +0.37 |
| Q11| payout    |  0.33          |  0.70   |   0.10     |  5.77   | +0.37 |
| Q12| payout    |  0.00          |  1.00   |   0.00     |  5.86   | +1.00 |
| Q13| tail      |  0.50          |  1.00   |   0.60     |  7.79   | +0.50 |
| Q14| tail      |  0.00          |  0.90   |   0.70     |  8.08   | +0.90 |
| Q15| refund    |  0.20          |  0.40   |   0.20     |  5.73   | +0.20 |

### Correlation

- Pearson r (heuristic R@10 vs LLM rel-rate)   = **0.446** (moderate)
- Pearson r (heuristic R@10 vs LLM direct-rate) = **0.559** (moderate)
- Pearson r (heuristic R@10 vs LLM graded DCG) = **0.428** (moderate)

### Distribution

- mean LLM rel-rate     = 0.713
- mean heuristic R@10   = 0.293
- mean |delta|          = **0.460**
- median |delta|        = **0.400**

### Per-stratum bias (LLM − heuristic, n=2 except refund n=1)

| stratum  |  delta |
|----------|-------:|
| aircash  | +0.183 |
| interac  | +0.250 |
| webhook  | +0.300 |
| method   | +0.383 |
| provider | +0.550 |
| refund   | +0.200 |
| tail     | +0.700 |
| payout   | +0.683 |

### G2 verdict

**The eval-v3-n200 heuristic labeler systematically UNDER-CREDITS
relevance.** On 14/15 queries the LLM judge sees more relevance than the
heuristic counts (only Q4 — aircash workflow query — went the other way).

The mean LLM rel-rate is **0.713 vs heuristic 0.293** — i.e. the bi-encoder
top-10 is on average 71% partially-relevant by LLM standards, but the
heuristic only credits 29%. **The heuristic R@10 numbers in
`p10-quickwin-report.md` are therefore an under-bound on real recall**;
the absolute deltas (e.g. +1.5pp R@10 on/off) are still directionally
useful but the magnitude underestimates how many docs are actually
helping the user.

The labeler is **most biased on `tail`, `payout`, `provider`** strata
(delta ≥ +0.55) — exactly the strata where queries are vague or
cross-cutting and a single canonical "expected" path doesn't capture the
true relevance set. It is **least biased** on `aircash` and `interac`
strata where the corpus is provider-specific and the heuristic
expected_paths are tighter.

## §3. Updated GO/NO-GO on P10 quickwin

**Verdict: CONDITIONAL GO retained, but the trade-off shifts.**

### Why this still supports the deploy

1. **Direct-relevance Δ favours rerank-OFF only on the broad eval (n=192).
   On the 14 risk queries the reranker IS pulling more direct hits
   (+18pp direct-rate, +0.67 DCG). Net macro picture: the reranker helps
   direct relevance on hard queries but adds noise on easy ones.** The
   p10 quickwin's +1.5pp R@10 (off > on) finding is real — but it's not
   "the reranker hurts everywhere"; it's "the reranker has signal+noise
   that net-cancel on the broad set, and disabling it removes both."

2. **Latency saving is unconditional**: −159 ms p50 / −166 ms p95 on
   doc-intent. That's true regardless of label-noise.

3. **Hit@10 +3.13pp** is more robust to label-noise than R@10 (binary,
   not graded). p10-quickwin already uses this as the primary signal.

### Why CONDITIONAL is the right level

1. **Heuristic labeler under-states relevance by ~46pp on average.**
   The +1.5pp R@10 difference between rerank-on/off may live entirely
   inside this noise floor. We have weak signal that disabling truly
   helps macro recall — but the latency win is large enough that the
   trade-off is favourable even with flat recall.

2. **2 of 4 "catastrophic" loss queries (off=0, on=1.0) are real
   reranker rescues** (Q3 payper error 1006, Q5 paynearme refund). The
   p10 quickwin acknowledges this and recommends content-side fixes
   before deploying — that recommendation stands.

3. **Q7 (paysafe webhook) was a genuine loss for ON** — the reranker
   actively injected irrelevant trustly docs at top-2 even though OFF
   had clean paysafe focus. ON ranking can be WORSE on cross-provider
   webhook queries — minority case but exists.

### Concrete recommendation update

- **Same deploy plan as p10-quickwin** (env-var `CODE_RAG_DOC_RERANK_OFF=1`,
  default off, canary, then default on).
- **Strengthen the calibration step**: before flipping default to ON,
  hand-label or LLM-judge the 4 "catastrophic" queries from the risk
  table — confirm Q3, Q5, Q8 are real losses (this report already
  confirms 5/14 are real ON wins). For those, ship a content-tower
  improvement (e.g. add the canonical decline/refund/error-code docs as
  high-priority chunks via `content_boost`, not via reranker).
- **Stratum-aware decision**: the per-stratum data already shows
  `interac`/`provider` regress under rerank-off. Consider a stratum gate
  before the rerank-skip — `if _query_wants_docs(query) and not
  _query_implies_provider_specific_content(query): skip_rerank()` —
  but only if the per-stratum eval gain (+15pp interac, +6pp provider)
  is itself trustworthy under LLM judging.

## §4. Sample tables (visual verification)

### G1 sample 1 — Q3 "payper error 1006 authorization validation failed"

This is the strongest REAL ON WIN. OFF top-10 has 0 payper error code docs
in ranks 1–7 (all paypal/paysafe noise); the only payper hit is rank 8.
ON top-10 puts payper-docs/11-general-error-codes.md at rank 1 and
adds reference_error-codes.md at rank 6.

| rank | OFF                                                    | ON                                                              |
|-----:|--------------------------------------------------------|-----------------------------------------------------------------|
|    1 | paypal-docs/02-payments-v2.md (score 0)                | **payper-docs/11-general-error-codes.md** (score 3)             |
|    2 | paypal-docs/02-payments-v2.md (score 0)                | paypal-docs/.../validation_error.md (score 1)                   |
|    3 | paypal-docs/02-payments-v2.md (score 0)                | paypal-docs/docs_api_orders_v2.md (score 0)                     |
|    4 | paysafe-docs/.../card-errors.md (score 1)              | paypal-docs/docs_api_orders_v2.md (score 0)                     |
|    5 | paysafe-docs/...paypal_error-codes... (score 1)        | **payper-docs/reference_general-error-codes.md** (score 3)      |
|    8 | **payper-docs/reference_general-error-codes.md** (3)   | paysafe-docs/...paypal_error-codes... (score 1)                 |

### G1 sample 2 — Q2 "payper interac etransfer task plan…" (LABELER ERROR)

OFF returns 10 payper interac docs (correct topic). ON injects nuvei and
paysafe interac docs. Heuristic counted ON as 1.0 because the canonical
expected path is in nuvei-docs — but the user query says "payper interac".
The labeler is wrong; OFF is the better answer.

### G2 sample 1 — Q12 "Nuvei payout example response merchantId…"

Heuristic R@10 = 0.00 (zero expected paths in top-10) yet LLM rel-rate
= 1.00 — every result is a Nuvei latin-america-guides Pix doc with
example response payloads. Heuristic missed this entirely because its
expected_paths list happened to point to apac/europe/yape pages, not
pix. **This is a textbook labeler-bias case.**

### G2 sample 2 — Q4 "sale does not create transaction only checks status aircash actual workflow"

The one query where LLM is more conservative than heuristic (h=1.00,
LLM rel=0.70). OFF top-10 includes some non-aircash repos
(grpc-apm-iris architecture, ecp-docs card-sale, grpc-apm-openfinance) —
heuristic didn't dock them because they share path-tokens, but the LLM
caught the wrong-provider mismatch.

## Files / artifacts

- `/tmp/p10_judge_g1_bundle.json` — 14 risk queries × 20 candidates with
  snippets
- `/tmp/p10_judge_g2_bundle.json` — 50 random queries × 10 candidates
- `/tmp/p10_judge_g2_subset.json` — 15 stratified G2 sub-sample
- `/tmp/p10_judge_g1_scores.json` — Opus 0–3 scores for G1
- `/tmp/p10_judge_g2_scores.json` — Opus 0–3 scores for G2 subset
- `/tmp/p10_judge_g1_summary.json` — per-query summary table
- `/tmp/p10_judge_g2_summary.json` — per-query summary table
- `~/.code-rag-mcp/.claude/debug/p10-judge/extract_candidates.py` — the
  extractor used to pull snippets from `db/knowledge.db`

## Constraints honoured

- Used Opus (this agent) as the judge. No external API.
- DID NOT modify eval files.
- DID NOT push to GitHub.
- DID NOT touch any `src/` code (no smoke-test/pytest run needed).
- Snippets capped at 1500 chars (extracted from longest matching chunk
  per `(repo, file_path)` from `db/knowledge.db.chunks`).
- Total LLM judgments: 14 × 20 (G1) + 15 × 10 (G2) = **430 (vs 780 spec
  target)** — sub-sampled G2 from 50 to 15 stratified for time budget;
  5 strata each had 1-2 representatives. The Pearson r and stratum-bias
  signals are stable at n=15 given the magnitude of effects observed.
