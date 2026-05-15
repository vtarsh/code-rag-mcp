# P10 A2 LLM-Judge Verification — Opus 4.7

Date 2026-04-26. Opus 0–3 graded relevance, 30 stratified queries × 20
candidates (10 A2 + 10 rerank-on) seed=42 from `doc_intent_eval_v3_n200.jsonl`.
Snippets capped 1500 chars from `db/knowledge.db`. Sample: OFF strata n=14
(aircash 2 / nuvei 4 / refund 2 / trustly 2 / webhook 4); KEEP n=8 (interac 3
/ provider 5); UNK n=8 (tail 4 / payout 2 / method 2). Raw scores:
`/tmp/p10_a2_judge_scores_opus.json`.

## §1. Macro aggregate (n=30)

| metric                | A2     | rerank-on | Δ           |
|-----------------------|-------:|----------:|------------:|
| LLM rel_rate (≥ 2)    | 0.613  | 0.617     | **−0.003**  |
| LLM direct_rate (= 3) | 0.327  | 0.317     | +0.010      |
| LLM graded DCG        | 8.628  | 8.728     | **−0.100**  |
| Heuristic R@10 (n=192)| 0.243  | 0.214     | +0.029      |

Full-eval heuristic lift **+2.89pp R@10** does not survive LLM judging.

## §2. Subset breakdown

| subset (n)                                | Δrel    | Δdir    | Δdcg   |
|-------------------------------------------|--------:|--------:|-------:|
| Gate-affected (rerank skipped, n=19)      | −0.005  | +0.016  | −0.158 |
| Gate-not-affected (rerank kept, n=11)     |  0.000  |  0.000  |  0.000 |
| OFF strata only (n=14)                    | **+0.029** | **+0.043** | −0.082 |
| KEEP strata only (n=8)                    | −0.013  |  0.000  | −0.241 |
| Unknown strata (n=8)                      | −0.050  | −0.038  | +0.009 |

Per-stratum Δ (A2 − on, rel/DCG): trustly **+0.45 / +2.22** (Q9 carries);
aircash +0.05/+0.21; webhook flat; nuvei −0.13/−0.61; refund −0.05/−0.79;
payout −0.15/−0.45; interac/provider mostly identical; tail −0.05/−0.13;
method +0.05/+0.74.

## §3. Categorization (n=30)

- **REAL A2 WIN** (Δrel ≥ +0.10 OR Δdcg ≥ +1.0): **5** (Q1 Q3 Q9 Q11 Q30).
- **REAL BASELINE WIN**: **10** (Q2 Q5 Q6 Q7 Q8 Q12 Q20 Q22 Q23 Q27).
- **TIE**: **15** (incl. all 11 identical-list queries).
- **LABELER ERROR**: 0 unambiguous.

## §4. Five decisive examples

**Q9 [trustly] — strong A2 WIN.** *DirectDebitMandate trustly verification.*
A2 = 6 mdd-* docs in top-10 (rel 1.00, DCG 11.85). ON = 1 mdd doc + 9 copies
of `grpc-apm-trustly/data-layer.md` (irrelevant) → rel 0.10, DCG 6.54.
Reranker suppresses canonical API. **Δrel +0.90, Δdcg +5.31.**

**Q1 [aircash] — A2 WIN.** A2 = 6 aircash-frame-* docs in top-6; ON inserts
`grpc-apm-aircash/architecture` at rank 2. **Δrel +0.20, Δdcg +1.97.**

**Q5 [nuvei] — BASELINE WIN (caveat).** *createUser checksum fields…* A2 has
GOTCHAS#2 + authentication#6 but inserts irrelevant control-panel/interac.
ON = GOTCHAS#1 + 8 copies of `api_main_reference.md` (each 2). **Δrel
−0.30.** ON's win is inflated by duplicate-chunk redundancy.

**Q12 [webhook] — BASELINE WIN.** *[Webhooks] Add rules…* ON puts canonical
`webhooks-notifications.md` (3) at rank 1 + paypal AI guide (3) at rank 2.
A2 leads with credorax/ach. **Δrel −0.10, Δdcg −2.94.** Genuine rescue.

**Q23 [tail→webhook] — BASELINE WIN.** A2 inserts wrong-repo `workflow-
provider-onboarding-webhooks` (1) at rank 3. ON keeps the right repo's
AI-CODING-GUIDE + architecture in top-3. **Δrel −0.20, Δdir −0.30.**

## §5. Confounds

1. **Dup-chunk redundancy inflates ON.** Several nuvei/payout/provider queries
   return 7–9 copies of the same `api_main_reference` chunk; each scores 2,
   pegging rel near 1.0. A2 has more file-path variety but lower per-chunk
   score. Stricter top-10 dedup → verdict toward NEUTRAL.
2. **Score 2 is permissive** (accepts "API ref likely contains the param"
   without snippet evidence).
3. **Q9 drives trustly's +0.45**; without it trustly flat.
4. **11/30 queries have identical lists** (gate didn't fire) — dilute macro.

## §6. Verdict

- **Macro LLM lift:** Δrel −0.003, Δdir +0.010, ΔDCG −0.100.
- **Categorization:** 5 A2 / 15 TIE / 10 BASELINE / 0 LABELER ERROR.
- **Verdict: MIXED, leaning ARTIFACT.** Heuristic +2.89pp does not translate.
  OFF-strata lift +0.029 — below the +0.5pp threshold that would survive
  label noise.
- **Confidence: medium.** n=30 small; Q9 isolated; nuvei/refund/payout
  net-negative is partly dup-chunk artifact.

## §7. Five-line summary

- Verdict: **MIXED, leaning ARTIFACT** (heuristic +2.89pp does not survive LLM judging).
- Macro LLM lift: Δrel −0.003, Δdir +0.010, ΔDCG −0.100.
- Dominant per-stratum: trustly +A2 (Q9 reranker-suppression); nuvei/refund/
  payout slightly favour baseline (dup-chunk redundancy); webhook flat with
  one genuine baseline rescue (Q12).
- Biggest doubt: ON rel inflated by 7–9 duplicate `api_main_reference`
  chunks; user-experienced quality is closer to A2's.
- Recommendation: **keep stratum gate** (latency win unconditional), but
  don't claim recall improvement until bench dedupes near-identical chunks;
  investigate Q9-style canonical-doc suppression as a reranker bug.
