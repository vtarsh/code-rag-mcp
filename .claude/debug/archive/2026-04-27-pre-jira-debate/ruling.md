# JUDGE — debate-rerank-split (2026-04-26 22:36)

## Verdict: **INSUFFICIENT EVIDENCE — test BOTH in Run 2**

Both sides argue concrete points grounded in repo data. Neither argument falsifies the other; the disagreement is *empirical* and resolvable in one $2 train cycle.

## Score per criterion

| Criterion | Affirmative | Negative |
|---|---|---|
| Evidence quality | High — Run 1 dual-axis numbers, 5.93:1 train ratio, file:line refs | High — V4 router churn +394 OOB / +12.9pp, n=80 ±9.3pp CI, RECALL-TRACKER 791-pair `val_loss=null` |
| Logic | Consistent (negative-transfer signature) | Consistent (router fragility + small-n CI + ops debt) |
| Counter handling | Strong (specialist-docs + stock-L6-code variant zeros out code-FT risk) | Strong (n=80 CI math invalidates the very deltas Affirmative leans on) |
| Constraint fit | $1.5–3 cost, 1.5x ops | $0.50 cost, 1x ops |

## Where they actually diverge
- **Affirmative's REVISED proposal** (= specialist-docs FT + stock-L6 code) is a different beast from "two FT'd models" Negative attacks. It only adds ONE new model (docs head), keeps code path unchanged → drops the "904-pair overfit" and "2x ops" criticisms by 80%.
- **Negative's loss-weighting alternative** (5× weight on 904 code rows in single reranker) is UNTESTED — neither side has data on it.

## Why I can't pick one outright
1. Run 1 deltas (+1/-5pp on n=80 / n=100) sit **inside the 95% CI** per Negative's math. Affirmative's "negative transfer signature" is *plausible* but not statistically confirmed.
2. Both proposed paths are cheap (~$2 combined). No reason to argue from theory when we can measure.
3. Router fragility is real but *applies to BOTH* — even Affirmative's specialist-docs split needs `is_doc_intent` to route correctly, so it's not a Negative-only veto.

## Recommended Run 2 design (3 candidates, ~$3 total on rtx4090)
1. **A — single rebalanced** (Negative's alt): `mxbai-rerank-base-v1` FT on combined data with `sample_weight = {code: 5.0, docs: 1.0}` (or class-balanced loss). Tests if cardinality fix alone closes the gap. ~$0.80.
2. **B — specialist docs + stock L6 code** (Affirmative's revised): FT `mxbai-rerank-base-v1` on docs-only subset (~5.4k pairs); inference router sends code-intent to stock L6 baseline. ~$0.80.
3. **C — full split** (Affirmative original): `mxbai-rerank-base-v1` on docs subset + `ms-marco-L6-v2` re-FT on code subset (904 pairs). Worst-case ops + risk; baseline for the bold version. ~$0.80.

Same eval (doc_intent_eval_v3 + code_intent_eval_v1) for all 3. Pick winner that **dominates baseline L6 on BOTH axes** OR call insufficient evidence and grow eval.

## Key risks per option
- A: loss-weighting may not help if the conflict is in *update direction*, not magnitude.
- B: 12% mis-route to docs reranker hurts code queries; Negative's strongest valid concern.
- C: 904 code pairs may overfit; needs early-stop watching code val loss (we don't have val split — generate one).

## Next step
Generate the 3 train recipes + JSONL splits (~30 min local). Spawn 3 in parallel on rtx4090 with `pod_watcher.py` armed. Eval all 3 vs baseline. If still insufficient, grow eval to n=300+ before another iteration.
