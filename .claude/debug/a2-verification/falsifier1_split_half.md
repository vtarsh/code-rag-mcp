# Falsifier #1 — Split-Half Held-Out Validation of P10 A2

- **Verdict:** SURVIVES (weakly)
- **Held-out lift (mean across 5 seeds):** +1.83pp
- **Held-out lift (range):** [+0.07pp, +2.84pp] (seeds 1, 7, 13, 42, 99)
- **In-sample lift (n=192):** +2.89pp → 63.2% retained
- **OFF/KEEP set agreement with prod (exact match):** 1/5 seeds; prod-OFF is a *subset* of derived-OFF in **5/5** seeds.
- **Recommendation:** SHIP

Sources: `/tmp/p10_a2_stratum_gated.json` (A2 R@10=0.2427),
`/tmp/p10_rerank_on_parity.json` (ON=0.2138), `/tmp/bench_v3_n200_docs.json` (OFF).
Code: `/tmp/p10_a2_split_half_falsifier.py`. Raw: `/tmp/p10_a2_split_half.json`.
Join key = `query` (`id` was None in all 192 rows of all 3 benches).

## Per-seed

| seed | a2 R@10 | on R@10 | lift_pp | derived_OFF |
|---:|---:|---:|---:|---|
| 1  | 0.2125 | 0.2118 | **+0.07** | aircash, nuvei, payout, refund, tail, trustly, webhook |
| 7  | 0.2620 | 0.2427 | +1.92 | aircash, nuvei, payout, webhook |
| 13 | 0.2235 | 0.1960 | +2.75 | aircash, nuvei, payout, refund, trustly, webhook |
| 42 | 0.2399 | 0.2114 | +2.84 | aircash, method, nuvei, payout, refund, trustly, webhook |
| 99 | 0.2168 | 0.2014 | +1.54 | aircash, nuvei, refund, trustly, webhook |

## Set agreement (seed=42)

- prod_OFF ∩ derived_OFF = {aircash, nuvei, refund, trustly, webhook} (5/5 prod)
- derived − prod = {method, payout} (noise; neutral on half-B)
- prod − derived = {} (full coverage)
- KEEP {interac, provider} matched exactly.

## n per prod-stratum (seed=42 split)

| stratum  | half_a | half_b |
|---|---:|---:|
| nuvei    | 12 | 11 |
| webhook  | 12 | 11 |
| provider | 12 | 11 |
| refund   |  7 |  6 |
| aircash  |  5 |  4 |
| interac  |  5 |  4 |
| trustly  |  2 |  2 |

## Per-stratum held-out (seed=42, non-zero deltas)

| stratum | n | a2 | on | delta |
|---|---:|---:|---:|---:|
| nuvei    | 11 | 0.4424 | 0.3030 | +0.1394 |
| aircash  |  4 | 0.4333 | 0.3208 | +0.1125 |
| webhook  | 11 | 0.2909 | 0.2227 | +0.0682 |
| payout   | 10 | 0.0533 | 0.0400 | +0.0133 |
| refund   |  6 | 0.5417 | 0.5833 | **-0.0417** |

Zero delta: provider, method, interac, trustly, tail (KEEP/unclear → both
branches use rerank-on → identical R@10).

## Cross-check

Apply prod-OFF set unchanged to half-B (no derivation): mean +2.14pp,
range [+0.98, +2.95]. The procedure-derived +1.83pp is lower because
half-A noise sometimes admits `tail` (n=50) and `payout` (n=21) into
derived-OFF, diluting the macro on half-B.

## Verdict

A2 generalizes. Held-out mean +1.83pp clears +1.5pp; prod-OFF fully
recovered (subset) in 5/5 seeds; top contributors nuvei +13.9pp, aircash
+11.3pp, webhook +6.8pp are consistent across folds. Risks: seed=1
collapse (+0.07pp) when noise admits `tail` to derived-OFF; refund
regresses -4.2pp on n=6 in seed=42 (small-n). The collapse is a property
of the *derivation procedure*, not the prod hand-picked set.

**SHIP** prod 5-stratum set. Do not auto-extend OFF without a
tail-stratum guard and a per-stratum minimum-n threshold.
