# Verdict-review investigation — attacker hypotheses (2026-04-25)

Verdict-attacker (task #2) ran 6 required attacks against the P6 debate verdict
(option (d) accept-baseline + U1 patch infra, $0 spend). Each attack is treated
as a hypothesis: does the proposed deviation from option (d) hold up under
computed evidence and verifications, or does the verdict survive intact?

Required reading: debate-verdict.md, debate-skeptic.md, debate-recipes.md,
debate-gte-unblock.md, p6-pivot-strategist.md, final-report.md, NEXT_SESSION_PROMPT.md.
Real verifications run in §5 of debate-verdict-attacker.md.

- H1: mxbai precondition met today → upgrade NEVER → DO-IT-THIS-SESSION | challenged: yes | status: confirmed-refuted (preconditions UNMET on disk; defer holds)
- H2: U1 patch + multi-candidate A/B as Phase 1.5 closure (~$0.30/each) | challenged: yes | status: excluded
- H3: P7 design surface cannibalization is theoretical, not load-bearing | challenged: yes | status: excluded
- H4: SE argument is convenient narrative, not decisive | challenged: yes | status: excluded (math vindicates skeptic ±0.1pp)
- H5: 0/4 base rate is wrong reference class for vanilla-BERT base-swap | challenged: yes | status: excluded (sub-class prior 0.25 holds, but EV at $1 still negative)
- H6: closing P6 with zero candidate on eval-v3-n150 reads weak | challenged: yes | status: excluded

---

## H1: mxbai precondition met today → run today
- evidence: skeptic §6 said "spend the first 4 hours growing eval-v3 to n=150, THEN run mxbai" — same-session sequencing. User is "growing eval-v3 to n=150 in parallel" per task brief.
- test: filesystem check on eval-v3 line count + RunPod credentials presence + daemon status.
- repro: `wc -l profiles/pay-com/doc_intent_eval_v3.jsonl` → expect 150 if precondition met. `ls ~/.runpod/credentials` → expect file present, chmod 600.
- result: confirmed (preconditions UNMET — refutes "do mxbai today"). Filesystem checks: `wc -l doc_intent_eval_v3.jsonl` = **100** (not 150). `ls ~/.runpod/credentials` = **No such file or directory**. Daemon offline (`lsof -i:8742` empty). All 3 preconditions UNMET as of 2026-04-25 → "DO-IT-THIS-SESSION" attack mechanically blocked. Defer-by-default operationally correct. Defender's framing should be amended from "DEFER to P7 forever" → "DEFER unless preconditions clear" — this is a textual nuance, not a verdict change.

## H2: multi-candidate A/B as Phase 1.5 closure
- evidence: defender §3 NO-GO bullet 3 says "❌ U1 A/B — skip; the patch alone suffices." Marginal A/B cost is ~$0.30; 3 candidates (gte-large, mxbai, gte-base) = $0.90 to grow measurements 4 → 7.
- test: compute EV under correlated bets at honest sub-class prior. Three rolls at 0.10 with same-eval correlation give 1-(1-0.10)³ ≈ 0.27 raw, ~0.15 corrected for correlation.
- repro: 3 × $0.30 = $0.90 cost, expected deploy upside = 0.15 × $1 = $0.15 → **EV = -$0.75**.
- result: excluded. Defender already did the math (debate-verdict.md §2 row 4: U1+A/B at $0.30, p=0.03, EV=-$0.30). Three rolls just multiplies the negative EV. Skip.

## H3: P7 design surface cannibalization is theoretical
- evidence: skeptic §2.3 quantifies hidden infra cost at ~10h human time on `build_train_pairs_v2.py` + loss-flag plumbing. Attacker claim: P7 will rebuild the script anyway, so cannibalization is theoretical.
- test: filesystem audit of claimed-missing infra files.
- repro: `ls scripts/runpod/build_train_pairs_v2.py` → No such file (skeptic correct on v2). `ls scripts/runpod/prepare_train_data.py` → 8.1K (skeptic loose; v0/v1 builder DOES exist). `wc -l scripts/runpod/train_docs_embedder.py` → ~5.8KB hardcoding MNRL (skeptic correct).
- result: excluded. Skeptic over-stated ("all train-data infra missing") but the *v2 mining + multi-loss* infra is genuinely missing. Cannibalization is half-real. Combined with `feedback_agent_hallucination_detection.md` (~30% rate on 10h of new code) the risk multiplier holds. Primary R1 KILL is the EV math (0.07 × $3.50 = -$3.26), which is independent of cannibalization framing.

## H4: SE argument is narrative, not decisive
- evidence: skeptic claims paired SE on n=90 = ±9pp; n=150 = ±7pp.
- test: compute SE = sqrt(2 × p̂(1−p̂)(1−ρ) / n) with ρ=0.5, p̂=0.2509 across n ∈ {90, 100, 120, 150, 200}.
- repro: `python3.12` → n=90 SE=0.0457 ±9.0pp / n=150 SE=0.0354 ±6.9pp / n=200 SE=0.0307 ±6.0pp. Power for true +10pp at n=150: 10/6.9 = 1.45 σ → 1-tail p≈0.077 → power≈0.50.
- result: excluded. Skeptic's ±9pp/±7pp claim is correct to within 0.1pp. Power=0.50 at n=150 for true +10pp is the honest description. Argument actually FAVORS option (d): under-powered to detect small lifts, so any single-iteration spend has high false-negative risk. Vindicates skeptic's grow-first sequencing.

## H5: 0/4 reference class is wrong for vanilla-BERT base-swap
- evidence: skeptic invokes Jeffreys 1/9 = 0.11 across 4 historical failures. Attacker claim: 4 failures are heterogeneous (FT bug, FT bug, MoE base swap, MoE base re-eval). For mxbai (vanilla BERT, no FT), reference class is "base-swap subclass" with n=1 (only nomic-v2-moe).
- test: compute Jeffreys for sub-class. (k+0.5)/(n+1) at k=0, n=1 → 0.25.
- repro: smoke `mxbai-embed-large-v1` discrimination Δ on payment-vs-fruit, compare to gte-large's reported 0.083. Result: **Δ=0.18** (2.2× gte-large). Combined honest p(win) = 0.25 × 0.6 × 0.6 × 1.2 = 0.108.
- result: excluded (refutes the actionable claim). Skeptic's 0/4 IS too coarse for mxbai specifically — honest mxbai p(win) ≈ 0.10 (not 0.05). But EV at $1 cost = 0.10 × $1 deploy − $1 spend = -$0.90 → still negative. Attack lifts mxbai from "NEVER" to "if-must-spend best option" but does NOT unlock action — converges with skeptic's own §6 minimum-viable plan and defender's overall verdict.

## H6: closing P6 with zero candidate on eval-v3-n150 reads weak
- evidence: 4 prior REJECTs were measured on eval-v3 at n=90. No measurement on n=150 yet. Attacker claim: "closed without testing on better eval" reads as ducking the question.
- test: power analysis — would n=150 change any of the prior 4 verdicts?
- repro: prior REJECTs had |Δ| ∈ {0.041, 0.083, 0.108}. At n=150 paired SE ±6.9pp 95% CI, all 4 stay rejected (|Δ| > 1.96σ for 3 of them; v2-moe at 0.041 is on the boundary but still rejected by AND-gate per-stratum drop conditions).
- result: excluded. P6 closes with 4 honest rejections + 6 process gains. 5th measurement at n=150 is rounding noise; doesn't change conclusion. Pivot-strategist Win 3 ("document the negative result") is the deliverable.

---

## Synthesis (attacker self-review)

Score: 2/6 attacks land partially (H1, H5). 4/6 KILL (H2, H3, H4, H6).

Both landing attacks (H1, H5) point to the same conclusion: **mxbai is the only defensible non-zero spend, and only as a P7 Iteration 2 conditional on preconditions being met. They do NOT support a P6 mxbai spend.**

Defender's verdict holds with one minor amendment: promote mxbai from "DEFER to P7" → "Default DEFER to P7; opt-in 1-iter only if (a) eval-v3 grown to n=150 file-on-disk, (b) RunPod creds present, (c) user explicit sign-off."

All 3 preconditions UNMET as of 2026-04-25 → operational answer = DEFER (converges with defender, but for more honest reasons). Output of investigation: full deliverable at `~/.code-rag-mcp/.claude/debug/debate-verdict-attacker.md`.
