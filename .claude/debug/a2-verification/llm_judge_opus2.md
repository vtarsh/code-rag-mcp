# P10 A2 Verification — Judge #2 (Opus 4.7)

Date: 2026-04-26.  Bundle: `/tmp/p10_a2_judge_bundle_opus2.json`. Scores: `/tmp/p10_a2_judge_scores_opus2.json`.

## Sampling

n=30 stratified from `doc_intent_eval_v3_n200.jsonl` (rows with `expected_paths`), `seed=42`. Distribution: nuvei 5, refund 3, webhook 3, aircash 2, trustly 2 (OFF strata = rerank-skipped in A2, n=15); provider 3, interac 3, tail 4, payout 3, method 2 (KEEP/UNK = rerank kept, n=15).

## Macro lift (A2 vs baseline, n=30)

| metric              | A2     | baseline | Δ           |
|---------------------|-------:|---------:|------------:|
| LLM rel-rate (≥2)   | 0.7000 | 0.7133   | **−0.013**  |
| LLM direct-rate (3) | 0.3033 | 0.2900   | **+0.013**  |
| LLM graded DCG      | 9.350  | 9.399    | **−0.049**  |
| heuristic R@10      | 0.243  | 0.214    | +2.89pp     |

LLM macro lift is **flat-to-slightly-negative on rel-rate, flat-positive on direct-rate, flat-negative on DCG**. **Heuristic +2.89pp R@10 lift is not corroborated.**

## Per-stratum (A2 − ON)

| stratum  | n  | Δ rel  | Δ direct | Δ DCG  |
|----------|---:|-------:|---------:|-------:|
| nuvei    |  5 | −0.120 | −0.100   | −1.105 |
| refund   |  3 | +0.033 | +0.033   | +0.537 |
| webhook  |  3 | +0.067 | +0.067   | +0.235 |
| aircash  |  2 | −0.050 | +0.100   | −0.123 |
| trustly  |  2 |  0.000 | +0.200   | +0.995 |
| KEEP/UNK | 15 |  0.000 |  0.000   |  0.000 |

KEEP/UNK rankings are bit-for-bit identical (15/15). OFF aggregate: Δ rel −0.027, Δ direct +0.027, Δ DCG −0.098. **Nuvei loses on every metric** — A2 saturates with off-topic SPEI/VIP-preferred-sdk chunks. **Trustly/refund/webhook modestly improve on direct-rate.**

## Categorisation (threshold |Δrel|≥0.10 OR |ΔDCG|≥1.0)

- REAL A2 WIN: 5 — Q1 nuvei createUser checksum, Q4 createUser params, Q8 paysafe handle-activities, Q11 webhook idempotency dedup, Q14 DirectDebitMandate.
- REAL BASELINE WIN: 6 — Q2 gwErrCode Lost/Stolen, Q3 addUPOAPM billingAddress, Q5 createUser errCode, Q7 refund attempt, Q9 Webhook events, Q13 aircash sale.
- TIE: 19 — including all 15 KEEP/UNK.

## 5 most-decisive queries

1. **Q3 addUPOAPM** (ΔDCG=−6.04 ON WIN). A2 saturates 10× VIP-preferred-sdk (off-topic); ON top-3 = `apm-input-parameters` DIRECT.
2. **Q4 createUser params** (ΔDCG=+2.52 A2 WIN). A2 has authentication.md Hashing (DIRECT R5); ON saturates 9× SPEI duplicates.
3. **Q2 gwErrCode Lost/Stolen** (ΔDCG=−2.50 ON WIN). ON has `testing-cards.md` (DIRECT Lost/Stolen table) R6-R10; A2 only response-handling errCode (partial).
4. **Q11 webhook idempotency** (ΔDCG=+2.33 A2 WIN). A2 has Monek idempotency-token (DIRECT R2,R7), worldpay isPendingRefund (R5,R8), Cross River idempotency (R6); ON sprays into silverflow/stripe/moneybite.
5. **Q1 nuvei createUser checksum** (ΔDCG=+1.86 A2 WIN). A2 has grpc-apm-nuvei GOTCHAS createUser checksum DIRECT R1,R3,R4 + authentication R3; ON has R1 then 9× SPEI.

## Verdict (Judge #2)

- **Macro:** Δ rel = **−0.013**, Δ direct = **+0.013**, Δ DCG = **−0.049**.
- **Counts:** 5 REAL A2 WIN / 19 TIE / 6 REAL BASELINE WIN / 0 LABELER ERROR.
- **Verdict: MIXED.** Heuristic +2.89pp R@10 not LLM-corroborated. Baseline wins (6) ≥ A2 wins (5). Nuvei loses ≥ trustly/webhook/refund gain. The heuristic over-counts A2 because skipping reranker keeps top-10 dominated by bi-encoder near-duplicates of the same chunk — counted multiple times if path matches `expected_paths`, but penalised here when the duplicate is off-topic SPEI.

## Cross-check vs Judge #1

- `/tmp/p10_a2_judge_scores_opus.json` is **empty** (`queries: []`).
- `.claude/debug/a2-verification/llm_judge_opus.md` **does not exist**.
- Judge #1 IDs file uses UUID hashes vs eval-v3 query_ids — different sampling scheme. Only **7/30 queries overlap by string match**.
- **Pearson r / disagreement count / verdict alignment:** all uncomputable.

**Cross-check status: DISAGREE-or-MISSING.** Judge #2 stands alone.

**Confidence: medium.** n=30 small, effect ±1pp. Most signal in nuvei. Pattern matches prior G1/G2 finding (heuristic over-states by ~3.5×).

## 5-line summary

1. Macro LLM lift A2 vs baseline on 30 stratified queries: Δ rel −1.3pp, Δ direct +1.3pp, Δ DCG −0.05 — **flat**, not the +2.89pp R@10 the heuristic reports.
2. KEEP/UNK strata (15/30) are bit-for-bit identical; all signal lives in 15 OFF-stratum queries.
3. Per-stratum OFF: nuvei loses (−12pp rel, −1.1 DCG); trustly/refund/webhook tie or modest gain; lift is duplicate-saturation, not graded relevance.
4. Counts: 5 REAL A2 / 6 REAL BASELINE / 19 TIE — verdict **MIXED**.
5. Cross-check vs Judge #1 **DISAGREE-or-MISSING** — judge_opus.md absent, scores empty, only 7/30 queries overlap. Confidence medium.
