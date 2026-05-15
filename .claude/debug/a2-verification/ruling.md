# A2 verification — Judge ruling

## Verdict
- WINNER: Defender
- Confidence: medium

## Reasoning

The Defender's mechanical argument is decisive and the Skeptic effectively
concedes it: A2 is a deterministic router between two pre-measured
pipelines, not a fitted model. I verified five exact identities on OFF
strata against `/tmp/p10_a2_stratum_gated.json:30-41` vs
`/tmp/bench_v3_n200_docs.json:17-28` (nuvei 0.3978, aircash 0.3778,
trustly 0.2667, webhook 0.2797, refund 0.3987 — identical at 4 decimals)
and one identity on KEEP (interac 0.6296 from `/tmp/p10_rerank_on_parity.json:24-35`).
Provider regresses -0.87pp at n=23 (per-query granularity ~4.3pp), so it
is sub-noise. Because A2 inherits per-stratum recall from priors measured
weeks ago, "training noise on the same data" — the Skeptic's framing — does
not literally apply: there is no model fit, only a token gate that selects
which prior to use. Macro +2.89pp = 0.2427 vs 0.2138 is the size-weighted
arithmetic of those two priors plus the unknown-bucket falling through to
rerank-on. I confirmed: OFF n=72, KEEP n=32, unknown n=88, summing to 192.

The Skeptic's strongest point — selection-on-eval optimism — is real but
bounded. They are correct that OFF/KEEP membership was chosen FROM eval-v3
deltas (`p10-quickwin-report.md:70-82`), so the choice is in-sample. They
are also correct that the prod corroboration is churn, not recall
(`p10-quickwin-report.md:184-187` admits "no prod ground truth"), and that
the LLM-judge G1 set is non-random. However, the Skeptic's own
falsifier-#2 (token coverage on prod long-tail) actually argues against a
catastrophe: if tokens fail to fire on the prod tail, A2 degrades to
plain rerank-on (the conservative fallback in `hybrid.py:418-427`), not to
something worse. Risk of regression is therefore upper-bounded by
rerank-on baseline — A2 cannot do worse than rerank-on except via the
provider -0.87pp / tail -0.67pp leakage that's already inside per-query
granularity.

Where the Defender genuinely overshoots: the +2.89pp macro is partly an
unknown-bucket artifact. Method (+2.35pp) and payout (+1.59pp) move
relative to rerank-on, meaning queries the eval tags as method/payout
contain OFF tokens (e.g. "Nuvei payout..." routes to nuvei). This is fine
production behavior but means the +2.89pp also includes selection on
queries that overlap multiple strata. Discounting by the Skeptic's
proposed 1pp in-sample-optimism band leaves +1.89pp, still above the §5
+1.5pp pre-registered forecast. A held-out fold remains the right next
piece of evidence — but the deploy has bounded downside.

## Verified claims

- A2 R@10 = 0.2427 — VERIFIED (`/tmp/p10_a2_stratum_gated.json:26`)
- rerank-on parity R@10 = 0.2138 — VERIFIED (`/tmp/p10_rerank_on_parity.json:20`)
- rerank-off R@10 = 0.2289 — VERIFIED (`/tmp/bench_v3_n200_docs.json:13`)
- 5 OFF-stratum identities A2 ≡ rerank-off — VERIFIED (4-decimal match)
- interac KEEP identity 0.6296 — VERIFIED
- provider -0.87pp, tail -0.67pp — VERIFIED (within 1/n granularity)
- OFF n=72, KEEP n=32, unknown n=88 sum to 192 — VERIFIED
- Per-stratum n: trustly=4 — VERIFIED. Skeptic correct that this is
  fragile (1 query ≈ 25pp), but trustly contributes only 4/192 = 2.1%
  of macro weight, so its leverage on +2.89pp is bounded at ~0.5pp.
- Gate code at `hybrid.py:337-428` matches the design — VERIFIED, OFF-first
  check order and unknown→False fallback are present.
- LLM-judge G2 mean |Δ|=0.460, Pearson r=0.446 — VERIFIED
  (`p10-llm-judge-report.md:125,133`). Skeptic's "16x gain" framing is
  rhetoric — the bias is per-query, the gain is paired-macro, so the
  noise floors don't directly multiply.
- 6/9 negative-delta strata also show ≥6.0 prod churn — VERIFIED
  (`p10-quickwin-report.md:73-83`). Skeptic's rebuttal "churn ≠ recall"
  is correct; the corroboration is directional, not magnitude.

## Key risks (regardless of winner)

- Same-eval selection: OFF/KEEP set was chosen from eval-v3 deltas. A
  proper held-out fold (split-half stratified) would tighten the
  estimate. Skeptic's falsifier-#1 is the cleanest follow-up.
- Token coverage on prod long-tail unmeasured. If <50% of prod
  doc-intent queries match any token, A2 ≈ rerank-on for the majority.
  Bounded downside (no worse than rerank-on), but bounded upside too.
- Trustly n=4 is statistically meaningless; +8.34pp on this stratum is
  decorative.
- LLM-judge labeler bias is largest on tail/payout/provider — strata A2
  routes to KEEP / unknown. Bias direction is benign for A2 but
  cautions against extending KEEP set without re-judging.
- Provider -0.87pp could be the leading edge of true regression at
  larger n. Watch in canary.

## Recommended next action

Keep A2 deployed as-is and proceed to canary + telemetry collection (path
(a) per the brief). The mechanical-router argument plus the bounded
downside (token miss → rerank-on fallback) make A2 deploy-safe even if
the Skeptic is right that +2.89pp is half-optimism. Before promoting from
canary to default, run Skeptic falsifier-#1 (stratified split-half
re-fit) — that is the single piece of evidence that converts confidence
medium → high. Falsifier-#2 (token coverage on prod 1242 doc-intent
queries) is cheap and should run during the canary window. Do NOT pursue
A4 (CrossEncoder retraining) until the held-out fold lands; expanding
KEEP without re-judging risks recreating the problem A2 just solved.
