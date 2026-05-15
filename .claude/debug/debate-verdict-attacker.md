---
name: debate-verdict-attacker
date: 2026-04-25
author: verdict-attacker (debate teammate, task #2)
team: verdict-review
inputs:
  - .claude/debug/debate-verdict.md (synthesis to attack)
  - .claude/debug/debate-recipes.md (R1..R5 — attacking R1 only)
  - .claude/debug/debate-gte-unblock.md (U1 patch — attacking deferral)
  - .claude/debug/debate-skeptic.md (skeptic — primary target)
  - .claude/debug/p6-pivot-strategist.md (option d anchor)
  - .claude/debug/final-report.md (4/4 reject record)
  - NEXT_SESSION_PROMPT.md (target for §4)
budget_remaining_usd: 13.30
verdict_one_line: "Verdict mostly correct, but the cleanest non-zero spend ($1 mxbai post-eval-v3-grow) is defensible and should be ARGUED FOR — not denied. Skeptic's 0/4 reference class is wrong for vanilla-BERT base-swap. SE math vindicates skeptic on n=90 but shifts at n=150."
attacks_landed: 2 of 6 (partial)
attacks_failed: 4 of 6 (skeptic was right; defender's verdict holds)
---

# Verdict-attacker — strongest case to deviate from option (d)

> Default frame: "accept verdict as-is." I argue OUT of the default. **Sycophancy is failure mode #1.** But if the skeptic was right, I say so explicitly with verification — that is honesty, not capitulation.

**Bottom line up front:** Of the 6 required attacks, only **#1 (mxbai post-grow)** and **#5 (0/4 reference-class is the wrong urn for base-swap)** land with computed evidence. The other four reduce to "sounds right but skeptic and verdict actually addressed it." I push for **a single $1 mxbai A/B run AS A POST-GROW ITERATION 1 OF P7, not in P6**, with kill conditions stricter than skeptic's. **I do NOT push for R1, U1+A/B, R3/R5, or any in-P6 spend.** Skeptic carried the deploy decision. The verdict is correctly conservative.

The user said "не спішимо." That phrase outranks any rhetorical clever attack I can construct. Where my attack lands partially, I label it "argument for further investigation in P7" — not "do this now."

---

## §1. Six attacks (with concrete data check, not rhetoric)

### Attack 1 — "mxbai precondition is being met TODAY by eval-v3 grow → upgrade NEVER → DO-IT-THIS-SESSION"

**Skeptic's exact text** (debate-skeptic.md §6): *"Single $1.00 spend: build_docs_vectors run on a single new candidate. Iteration cap: 1. Eval rebuild before spend: spend the first 4 hours of this iteration growing eval-v3 from n=90 to n=150 (50 prod-sampled rows). This costs $0 and tightens paired SE from ±9pp to ±7pp. The +10pp AND-gate becomes statistically achievable. THEN run mxbai. If eval-v3 cannot grow this session, skip the spend entirely and revert to option (d)."*

**Skeptic offered a conditional GREEN.** The verdict downgrades it to "DEFER to P7" by adding a constraint skeptic did not impose: that eval-v3 grow happens *in a separate session*. Defender's §3 NO-GO list bullet 5: "❌ mxbai A/B — defer to P7." This is a step further than skeptic.

**The data check:** Re-read skeptic's §6 verbatim. Skeptic explicitly says "**THEN run mxbai**" — meaning in the same session, after the grow. The verdict misquotes/over-tightens skeptic on this point.

**SE verification** (computed by me, 2026-04-25):
```
Paired SE = sqrt(2 × p̂ × (1−p̂) × (1−ρ) / n) — Bernstein-style, p̂=0.2509, ρ=0.5
n=90:    SE=0.0457   ±9.0pp 95% CI
n=100:   SE=0.0434   ±8.5pp 95% CI
n=120:   SE=0.0396   ±7.8pp 95% CI
n=150:   SE=0.0354   ±6.9pp 95% CI
n=200:   SE=0.0307   ±6.0pp 95% CI
```

Skeptic's "n=90 → ±9pp; n=150 → ±7pp" is **correct to within 0.1pp.** Confirmed.

**Why this argument lands partially:**
- The +10pp AND-gate sits at ~1.45 σ above expected null at n=150 (10pp / 6.9pp ≈ 1.45). Two-sided p ≈ 0.15 to detect a true +10pp delta with ~80% power; achievable but not powerful. Skeptic's "becomes statistically achievable" is true but soft.
- **However:** if grow is happening "in parallel" (per task brief), the *one-session-vs-two-session* boundary is artificial. If grow lands today, mxbai A/B is mechanically just one more `make build && benchmark_doc_intent.py` invocation.

**Why this argument fails to dominate option (d):**
- Skeptic was conditional ("if eval-v3 can grow this session"). The verdict treats the precondition as not-yet-met because n=150 file is not actually written yet. **Verifiable claim** (filesystem check, by me, 2026-04-25):
  ```
  $ wc -l profiles/pay-com/doc_intent_eval_v3.jsonl
  100 profiles/pay-com/doc_intent_eval_v3.jsonl   ← NOT 150
  ```
  Eval-v3 is still at n=100 (n_eval=90 effective). Grow is *announced* but not *done*. Until it is done, skeptic's precondition is unmet.
- "User is growing in parallel" is brief language; whether it actually lands today is unverified. Defender's defer-to-P7 is **correct** if the grow doesn't actually land.

**Verdict on Attack 1: argument for further investigation. NOT a "do mxbai today" mandate.** I propose: defender's §3 bullet 5 (defer mxbai) should be amended from "DEFER to P7" to "DEFER to P7 by default; upgrade to single-iter run AT END of this session if (a) eval-v3 actually reaches n=150 file-on-disk and (b) RunPod credentials are confirmed and (c) skeptic's $1.00 cap is signed off explicitly by user." That's 3 unmet preconditions; 0/3 today. So today: still defer. **But defender's "NEVER P6" framing is one notch too strong.**

---

### Attack 2 — "U1 patch + A/B as Phase 1.5 closure of P6 (~$0.30/candidate)"

**Defender's §3 NO-GO bullet 3:** "❌ U1 A/B (gte-large vector build + bench) — skip; the patch alone suffices for now."

**Attacker's claim:** Once U1 patch lands AND eval-v3 grows AND we have a clean candidate sitting there, the marginal A/B cost is ~$0.30 each. Cumulative: $0.60-1.20 to bench all 3 (gte-large, mxbai, gte-base) and close the doc-tower hypothesis with 7 measurements instead of 4. Why not?

**Data check:** gte-unblocker §3 honest p(deploy) for gte-large = 0.10. Skeptic discounts to 0.03. mxbai is unmeasured but vanilla BERT, similar p ≈ 0.05-0.10.

**EV math:**
- Three benches × $0.30 = $0.90
- p(any of 3 deploys at +10pp gate, n=150) ≈ 1 - (1-0.10)³ ≈ 0.27 — but these are *correlated* (same eval, same corpus), so honest p ≈ 0.15
- Information value of three rejections is real but diminishing (4 → 7 measurements)
- **EV vs option (d):** -$0.76 (cost minus expected deploy upside) — negative under standard EV math.

**Why this argument lands partially:**
- Defender's verdict §2 has the SAME argument zeroed out at "U1 + A/B (gte-large 5th rejection): $0.30, p=0.03, EV = -0.30". Defender already did the math and concluded SKIP.
- My addition (mxbai + gte-base in same session) just multiplies the SKIP — it doesn't reverse it.
- gte-unblocker themselves admit: "Honest p(beat baseline by AND-gate +0.10pp) = ~0.10". With 4/4 prior, applying skeptic's discount this drops to ~0.03. Three rolls at 0.03 × small-correlation gives 1-(1-0.03)³ ≈ 0.087 — still not enough to flip the EV.

**Why this argument fails to dominate option (d):**
- The patch IS already landing (defender's §3 GO bullet 1). The A/B is the marginal extra step — and **the marginal extra step is sunk cost on top of a no-information-yet base.**
- Eval-v3 at n=150 doesn't exist on disk. Until it does, an A/B at n=90 or n=100 has paired SE ±9pp — same as the 4 prior failed runs.

**Verdict on Attack 2: KILL the attack.** Defender is right. The patch lands as no-op infra; the A/B is correctly skipped. No 5+ measurements arms race.

---

### Attack 3 — "P7 design surface cannibalization is theoretical, not load-bearing"

**Skeptic §2.3:** *"the team has $13.30 banked but a fixed number of human iterations, and 10 h on R1 prep is 10 h not spent on Eval-v3 expansion / Router term-whitelist / Reranker A/B harness."*

**Attacker's claim:** R1's recipe specifics (TSDAE+CoSENT+reranker-mined HNs) is generic enough to be evolved further in P7 anyway. The "cannibalization" is theoretical because P7 will rebuild the script anyway.

**Data check:** skeptic §2.3 quantifies hidden infra costs as ~10 h human time on `build_train_pairs_v2.py` + loss-flag plumbing. **Verified by me:**
```
$ ls scripts/runpod/build_train_pairs_v2.py
ls: ... No such file or directory                     ← skeptic correct: file does not exist
$ wc -l scripts/runpod/train_docs_embedder.py
... (existing 5.8 KB, hard-codes MNRL)                 ← skeptic correct: needs loss-flag patch
$ ls scripts/runpod/prepare_train_data.py
... 8.1K (exists, contains train-pair builder for v0/v1 path)   ← skeptic was OFF on this one
```
Skeptic over-stated the "all infra missing" claim — `prepare_train_data.py` does exist for the simpler 91-pair case. But the multi-loss + reranker-mining infra is genuinely missing.

**Why this argument lands partially:**
- Skeptic is right about the *missing* infra (10 h human time is realistic for new mining + loss-flag).
- Attacker's claim that "P7 will rebuild it anyway" is *true but irrelevant* — the choice is "build it now and use it now" vs "build it later when we know more about what we need." The value of "later" is the option to redesign based on grown eval-v3 and user feedback.
- Cannibalization is **NOT theoretical** in one specific sense: the team has documented "agent hallucination rate ~30%" (memory `feedback_agent_hallucination_detection.md`). 10 h of new code with ~30% hallucination risk on a 4/4-rejected pattern is high-risk + high-cost.

**Why this argument fails to dominate option (d):**
- Even if cannibalization is half-theoretical, R1's honest p(win) of 0.07 doesn't clear the EV bar regardless of cannibalization framing.
- Attacker has no counter-argument that R1's mechanism is *substantively better* than nomic-v2-moe's "drop-in upgrade" claim that was REJECTED on eval-v3.

**Verdict on Attack 3: KILL the attack.** Cannibalization is a real-but-secondary argument. The primary R1 KILL is "Jeffreys × honest discount = 0.07 × $3.50 = $-3.26 EV" — which holds regardless of cannibalization framing.

---

### Attack 4 — "SE argument is convenient narrative, not decisive"

**Skeptic's claim:** n=90 paired SE is ±9pp; growing to n=150 tightens to ±7pp. The +10pp AND-gate becomes "statistically achievable."

**Attacker's claim:** If the AND-gate is +10pp, SE on n=150 is ±7pp, then required Z-score = 10/7 ≈ 1.43 → 1-tail p ≈ 0.077. **At a single FT roll, this is genuinely tight.** But across multiple rolls, it's ~0.30 in 4 rolls. So if FT helps at all, n=150 catches it.

**Data check (computed by me, 2026-04-25):**

Computed paired SE under three correlation assumptions (test-retest correlation ρ between paired comparisons on same queries):

| n | ρ=0.3 (loose) | ρ=0.5 (skeptic) | ρ=0.7 (tight) |
|---|---|---|---|
| 90 | ±10.6pp | **±9.0pp** | ±6.9pp |
| 150 | ±8.2pp | **±6.9pp** | ±5.4pp |
| 200 | ±7.1pp | **±6.0pp** | ±4.7pp |

Skeptic's ρ=0.5 is the standard assumption for paired R@10 under shared eval queries. **Numbers match within 0.1pp.** Skeptic is mathematically correct.

**Power analysis (computed by me):**
- True Δ = +10pp, σ_Δ = ±7pp (n=150, ρ=0.5): one-sided power ≈ 0.50 (50/50 to detect a true +10pp).
- True Δ = +13pp: power ≈ 0.78.
- True Δ = +5pp: power ≈ 0.18.

**The honest read:** at n=150, the gate is detectable with ~50% power for a true +10pp lift. That's coin-flip. **Above the noise floor but below the reliability bar.** Skeptic's "achievable" is technically right but the realistic picture is "detectable, with 50% chance of false-negative even if FT works."

**Why this argument lands partially:**
- Skeptic over-states "achievable" — power=0.50 is detectable but not powerful.
- Attacker's "if FT helps at all, n=150 catches it" is **wrong** — at +5pp (which is recipe-architect's expected R1 delta), power is 0.18, so 4 out of 5 rolls miss.
- BUT: this also kills R1 — if R1's expected Δ is +5pp and detection power is 0.18, R1 is destined to look like a null result on n=150 even if it provides a real but small lift. **This is an argument FOR option (d), not against.**

**Why this argument fails to dominate option (d):**
- The attacker's framing ("decisive" or "narrative") is just rhetorical labeling. Skeptic's math is correct. n=150 is the right move; running ANY A/B on n=90 or n=100 right now is genuinely under-powered.
- This vindicates skeptic's "grow first, THEN spend" sequencing.

**Verdict on Attack 4: KILL the attack.** Skeptic's SE math is correct. The grow-first sequencing is mathematically supported. Defender's verdict aligns with skeptic on this.

---

### Attack 5 — "0/4 base rate is wrong reference class for vanilla-BERT base-swap"

**Skeptic invokes Jeffreys 1/9 = 0.11 across all 4 prior failures** to argue mxbai p(win) ≤ 0.05.

**Attacker's claim:** 4 candidates were heterogeneous failures:
1. payfin-v0 — FT, 10 pairs, **double-prefix bug**
2. payfin-v1-fixed — FT, 91 pairs, **key-remap-not-retrain**
3. nomic-v2-moe — base swap (NOT FT), eval-v2 BIASED, then re-bench on eval-v3 still REJECT
4. payfin re-evals on eval-v3 — same artifacts, just re-measured

**For mxbai** (vanilla BERT, no NTK rope, no MoE, no FT, no `trust_remote_code` quirk), the closest reference class is "base-swap to non-nomic family" → 0/1 (only nomic-v2-moe). Jeffreys at 0/1 = (0+0.5)/(1+1) = **0.25**.

**Data check (computed by me, 2026-04-25):**

```
Sub-class breakdown:
- 'all 4 historical' (skeptic ref):   n=4, k=0  → Jeffreys 1/9 = 0.111
- 'base-swap subclass':                n=1, k=0  → Jeffreys 1/4 = 0.25 (only nomic-v2-moe)
- 'vanilla BERT on private corpus':    n=0, k=0  → no project evidence

Combined honest mxbai p(beat baseline +10pp on eval-v3 n=150):
  base-swap subclass prior:           0.25
  × 0.6 (eval-v3 R@10=0.25 = HARD ceiling)
  × 0.6 (MTEB → private-corpus transfer ratio loss)
  × 1.2 (mxbai discrimination Δ=0.18 on smoke; gte-large=0.083; baseline likely ~0.09)
  ≈ 0.108
```

**Smoke verification** (executed by me, 2026-04-25, mxbai-embed-large-v1):
```
load OK (25.9s)
encode shape=(3, 1024)
cos(payment, payment)=0.5698  cos(payment, fruit)=0.3899  Δ=0.1799
```
Δ=0.18 — **2.2× the discrimination of gte-large** (which gte-unblocker reported as Δ=0.083 on equivalent 3-doc smoke). This is real.

**Why this argument lands:**
- Skeptic's 0/4 lumps mxbai with FT-on-91-pairs-with-bugs. Mechanism-wise, that's wrong.
- Honest p(win) for mxbai ≈ 0.07-0.11 (not skeptic's 0.05).
- Smoke discrimination signal is genuinely better than gte-large's.

**Why this argument does NOT dominate option (d):**
- Even at p(win)=0.10, EV at $1.00 = -$0.90 vs option (d). Still negative.
- Skeptic was wrong about the *prior* but right about the *EV* (0.07 vs 0.05 is rounding error at $1 cost).
- The argument lands as "skeptic mis-stated the prior" but the verdict (DEFER to P7) is still correct.

**Verdict on Attack 5: argument for further investigation.** Skeptic's 0/4 reference class IS too pessimistic for mxbai specifically. Honest mxbai p(win) is ~0.10, not 0.05. **But this doesn't unlock action — it makes mxbai a slightly stronger P7 candidate, not a P6 spend.**

---

### Attack 6 — "Closing P6 with ZERO candidate on eval-v3-n150 feels weak"

**Attacker's claim:** Process gains are real but optical. "P6 closed with no candidate even attempted on eval-v3-n150" reads as a session that ducked the question. Should P6 close with at least 1 honest A/B on the better eval?

**Data check:**
- 4 candidates were ALREADY measured on eval-v3 (n=90): all REJECT with deltas -0.041, -0.083, -0.108, plus gte-large BLOCKED.
- Adding "1 candidate on eval-v3-n150" shifts the count from "4 measurements at n=90" to "4 at n=90 + 1 at n=150." It's not "zero on better eval" → it's "first measurement at slightly tightened CI."

**Reframe:** the question isn't whether the eval is better; it's whether n=150 changes any of the prior verdicts. **Power analysis:**
- All 4 prior REJECTs had |Δ| ≥ 0.041; at n=150 paired SE ±7pp 95% CI, all 4 stay rejected (|Δ| > 1.96σ).
- A 5th measurement on n=150 only changes the answer if the candidate has a *novel mechanism* → p(novel mechanism wins | 4/4 prior fail) ≈ 0.07-0.10 per Attack 5.

**Why this argument lands partially:**
- Optical concern is real — "closing without a fresh A/B" is harder to defend in retrospective writeup.
- Information value of "1 candidate at n=150" is non-zero: lets the team verify the AND-gate is actually achievable on grown eval before committing more.

**Why this argument fails to dominate option (d):**
- "Optics" doesn't outweigh $1.00 + 4-6h human time.
- The "test the AND-gate achievability" justification is a different framing — it's a *sanity check* for P7, not a P6 win.
- Pivot-strategist's "win 3" already calls for "Document the negative result." That IS a real deliverable.

**Verdict on Attack 6: KILL the attack.** P6 closes with 4 honest rejections + 6 process gains. That's a complete record. Adding a 5th rejection-on-n150 doesn't change the conclusion — it just adds noise to the writeup.

---

## §2. Counter-proposal: revised verdict (one-spend variant)

The verdict's hybrid (option d + U1 patch infra) is fundamentally correct. **My only proposed amendment is to relax §3 NO-GO bullet 5 from "❌ mxbai A/B — defer to P7" to "❌ mxbai A/B in default plan; upgrade to opt-in single-iter A/B at end-of-session conditional on 3 preconditions all met."**

### Conditional (do NOT enact unless ALL of these are signed off):
1. **Eval-v3 actually grows to n=150 file-on-disk** in this session (not next session, not "in parallel" — actually shipped).
2. **RunPod credentials confirmed** (`~/.runpod/credentials` exists, chmod 600). **Verified by me, 2026-04-25:** file does NOT exist at that path (`ls: No such file or directory`). So precondition currently UNMET.
3. **User explicitly authorizes the $1.00 spend.** Default is bank.

### Honest p(win) recalculation under fair priors:
```
Sub-class prior (vanilla BERT base-swap):     0.25
× transfer-loss discount (MTEB → private):     0.60
× corpus-difficulty discount (eval R@10=0.25): 0.60
× mxbai discrimination signal premium:         1.20
  (Δ=0.18 vs baseline likely ~0.09 on equivalent smoke)
                                               ─────
Honest mxbai p(deploy at +10pp on eval-v3 n=150) = 0.108
                                               ≈ 0.10
```

### Cost: $1.00. EV = +$0.10 deploy_value − $1.00 spend = −$0.90 (assuming deploy_value = $1.00).

**This is still NEGATIVE EV.** I am not pushing for this as a default action. I am pushing only for the *option* to enact it if user explicitly opts in after preconditions are met.

### Kill conditions if enacted:
- Stage 1 smoke (50-row probe on eval-v3 subset): if Δr@10 < -0.03, abort before full bench (saves $0.50).
- Full bench: if AND-gate not cleared on any of 5 conditions, REJECT and document as 5th measurement.
- Hard cap: $1.00 absolute, no extension to "let me try one more."

### Net change vs defender's verdict:
- §3 NO-GO bullet 5: amended from "DEFER to P7" → "DEFER to P7 by default; opt-in 1-iter conditional on preconditions."
- §6 open question 5: amended from "Confirm DEFER to P7?" → "Confirm DEFER to P7 by default; opt-in 1-iter cap available if user wants minimum-viable spend after preconditions met."
- §7 NEXT_SESSION_PROMPT: see §4 below.

**Everything else in defender's verdict stands. R1 KILL. R2/R3/R4/R5 KILL. U1 patch lands. U1 A/B SKIPS. R1+U1 NEVER same session.**

---

## §3. Q3 — mxbai verdict: argue for "yes-conditional" not "never"

Defender's §3 bullet 5: "❌ mxbai A/B — defer to P7."

**My amendment:** mxbai is the only "if-must-spend" path that's defensible. Skeptic explicitly said so (debate-skeptic.md §6: *"mxbai dominates U1 in EV"*). Defender's "defer to P7" is one notch too strong — it should be "defer by default, allow opt-in if preconditions met."

**Reasoning:**
1. **Skeptic's own minimum-viable spend lists mxbai as the chosen if-must-spend path.** Skeptic-attacker disagreement here = false (we agree).
2. **Smoke discrimination signal favorable.** Δ=0.18 on 3-doc payment-vs-fruit smoke is 2.2× gte-large's 0.083 (gte-unblocker §1.4 reported gte-large Δ=0.083 on equivalent test). This is genuine signal that mxbai's vanilla BERT geometry may be better-suited than nomic's MoE-style geometry for this corpus.
3. **Subclass prior 0.25 (not 0.11)** per Attack 5. Honest p(win) ≈ 0.10.
4. **Cost is small** ($1.00 hard cap, ~30 min wall on RunPod A40).
5. **Information value** is moderate: a 5th measurement on a *non-FT, non-nomic* base is genuinely new evidence about whether the corpus has a base-swap ceiling vs FT-recipe ceiling.

**Q3 verdict: YES-CONDITIONAL.** Not NEVER, not DO-IT-NOW. The conditional path is:
- IF user opts in
- AND eval-v3 reaches n=150 on disk
- AND RunPod credentials are confirmed (currently NOT — file missing)
- THEN run 1-iter mxbai with $1.00 cap and Stage 1 smoke kill at -3pp.

**If any precondition fails: DEFAULT IS BANK.** No mxbai spend in P6. Carry to P7.

**Probability all preconditions are met today:** very low (RunPod creds missing as of this writing). Attacker's realistic Q3 final answer = "DEFER to P7" via precondition failure, which converges with defender's verdict — but for *different and more honest reasons*.

---

## §4. Q4 — NEXT_SESSION_PROMPT.md proposal (differs from defender's)

Defender's §7 assumes NO mxbai A/B was attempted in P6. My §4 differs: assume **either (a) mxbai opt-in completed in P6 → P7 starts with 5 measurements, or (b) mxbai opt-in deferred → P7 inherits unchanged.**

### Branch A — if mxbai opt-in enacted in P6 (low probability today):
```markdown
## Goal of this session (P7)
Doc-tower hypothesis closed (5/5 rejections including mxbai on eval-v3 n=150).
Pivot to ROUTER + RERANKER axes for any further doc-recall lift.

Pre-flight: U1 patch + mxbai measurement landed in P6.
Eval-v3 at n=150 is the canonical artifact.

## Iteration 1: router term-whitelist (c3 in pivot-strategist)
- 30 hand-curated regexes for prod top-30 doc-intent terms
- $0, ~6h, p(any positive lift)=0.78
- Bench against eval-v3-n150

## Iteration 2: reranker A/B harness setup (P8 prep)
- Wire bge-reranker-v2-m3 vs reranker_ft_gte_v8 in shadow mode
- Multi-week regression test
- $2-4 spent on bench-only runs
```

### Branch B — if mxbai opt-in deferred (high probability today):
```markdown
## Goal of this session (P7)
Improve doc-intent recall above 0.2509 baseline.

## Iteration 1: eval-v3 grow to n=150
- 50 prod-frequency-weighted queries from logs/tool_calls.jsonl
- Model-agnostic FTS+overlap labeling
- Verify query-disjointness against any planned train set
- Save as profiles/pay-com/doc_intent_eval_v3_n150.jsonl
- Bench baseline R@10 with new gate

## Iteration 2: mxbai opt-in (if user wants it as P7 prelude)
- Single-iter, $1.00 cap, Stage 1 kill at -3pp
- A/B on grown eval-v3
- Documents 5th measurement before any FT spend

## Iteration 3: domain-adaptive contrastive (proper R1-evolved recipe)
- Build train pairs with query-disjoint check
- Run on RunPod A40 with cost_guard at $5 cap
- A/B against grown eval-v3
- Hard kill if Stage 1 smoke shows Δr@10 < -0.03

## Stop conditions
- +10pp clear gate confirmed → freeze, deploy
- $11 effective cap reached → freeze best-so-far
- 1 iteration no improvement → freeze + close P7 with negative result
```

### Recommended for synthesis:
**Default = Branch B.** It dominates A unless mxbai actually runs today (low prob). Branch B preserves all of defender's strategic intent while making mxbai an explicit P7 Iteration 2 instead of "deferred indefinitely."

### One specific text amendment to defender's §7:
Change defender's text:
> *Pre-flight: U1 patch already landed in P6 — gte-* family is unblocked. mxbai-embed-large-v1 is a clean fallback if the FT base-swap is preferred.*

→ to:
> *Pre-flight: U1 patch already landed in P6 — gte-\* family is unblocked. mxbai-embed-large-v1 is the recommended P7 Iteration 2 cheap-probe (single-iter, $1.00 cap) before any FT spend. P7 Iteration 1 grows eval-v3 to n=150; Iteration 2 runs mxbai (if user signs off); Iteration 3 runs the chosen FT recipe.*

This amendment:
- Promotes mxbai from "fallback" → "scheduled P7 Iteration 2"
- Makes the spend sequence explicit: grow → cheap probe → FT
- Preserves option (d) for P6
- Justifies the $1.00 spend in P7 budget rather than P6's

---

## §5. Real verifications (non-rhetorical evidence)

### V1 — Paired SE math (skeptic's claim verified)
```python
SE = sqrt(2 × p̂(1−p̂)(1−ρ) / n) with ρ=0.5, p̂=0.2509
n=90:  SE=0.0457  ±9.0pp 95%
n=150: SE=0.0354  ±6.9pp 95%
n=200: SE=0.0307  ±6.0pp 95%
```
**Skeptic claim "n=90 → ±9pp; n=150 → ±7pp" CONFIRMED to within 0.1pp.** Skeptic's math is honest.

### V2 — mxbai-embed-large-v1 smoke load (Mac CPU, sentence-transformers 5.3.0)
```
import OK (12.6s)
load OK (25.9s)
encode shape=(3, 1024)
cos(payment, payment)=0.5698
cos(payment, fruit)=0.3899
Δ=0.1799
```
**Loads cleanly. No NTK quirk. No `trust_remote_code` regression. Discrimination Δ=0.18 — 2.2× gte-large's reported 0.083 on equivalent smoke.** Attacker's "easy mxbai A/B" claim does NOT collapse — mxbai is technically ready.

### V3 — Daemon status (precondition for in-session A/B)
```
$ lsof -i:8742
(no output)                                          ← daemon NOT running
```
Daemon is OFFLINE. To run an A/B today, would need:
```
kill -9 $(lsof -ti:8742); sleep 2
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 daemon.py &disown
```
This is not a blocker (~10 sec restart) but it's an unmet precondition.

### V4 — RunPod credentials precondition
```
$ ls ~/.runpod/credentials
ls: No such file or directory
```
**RunPod credentials file is MISSING.** Without it, no pod can be cycled. **This single check kills attacker's "we could run mxbai today" path.** Without RunPod, the only mxbai path is local Mac MPS — which adds 1-2h wall and risks RAM exhaustion (mxbai-large = 1.3GB resident + 49k chunks indexing on 16GB Mac is borderline).

### V5 — Eval-v3 file size (skeptic claimed n=90; current state)
```
$ wc -l profiles/pay-com/doc_intent_eval_v3.jsonl
100 profiles/pay-com/doc_intent_eval_v3.jsonl
```
File has 100 rows (n_eval=90 per final-report.md after dropping rows with no expected_paths). **NOT n=150.** Skeptic's precondition for mxbai opt-in (eval-v3 grown to n=150) is **UNMET as of 2026-04-25 12:30 UTC.**

### V6 — prepare_train_data.py existence (skeptic claim audit)
```
$ ls scripts/runpod/prepare_train_data.py
... 8.1K (exists, contains v0/v1 train-pair builder)
```
**This file DOES exist** — skeptic's framing in §2.3 of debate-skeptic.md was loose ("`scripts/build_train_pairs_v2.py` does not exist"). The *v2* builder doesn't exist; the original `prepare_train_data.py` does. **Skeptic was correct about v2 specifically but the framing implied "all train-data infra missing" which is false.** Minor honesty point in skeptic's favor — they were precise about v2.

---

## §6. Attacker's honest summary

| Attack | Claim | Computed evidence | Verdict |
|---|---|---|---|
| 1 — mxbai immediate | Skeptic's precondition met today | Eval-v3 still n=100; RunPod creds missing; preconditions UNMET | **Lands partial** — defer-by-default but allow opt-in if preconditions clear |
| 2 — Phase 1.5 closure | U1+mxbai+gte-base trio benches close hypothesis cleanly | EV math still negative; correlated bets | **KILL** — defender's math is right |
| 3 — Cannibalization theoretical | R1 doesn't really steal P7 design surface | Skeptic's quantification holds; agent-hallucination memory adds risk multiplier | **KILL** — secondary argument, primary R1 EV is still negative |
| 4 — SE narrative convenient | n=150 is power=0.50 only, not "achievable" | Power analysis confirms 50/50 detection at +10pp; vindicates skeptic's "grow first" | **KILL** — argument actually FAVORS option (d) |
| 5 — 0/4 wrong reference | Vanilla-BERT subclass is 0/1, not 0/4 | Subclass Jeffreys = 0.25; attacker honest p(win) = 0.10 | **Lands partial** — skeptic's prior is too pessimistic but EV stays negative |
| 6 — Optics of zero-on-n150 | "Closed without testing" reads weak | Power analysis shows 4 prior REJECTs stay rejected at n=150; 5th is rounding | **KILL** — optics doesn't dominate $1+human time |

**Score: 2/6 attacks land partially, 4/6 fail.**

The two landing attacks (1 and 5) BOTH point to the same conclusion: **mxbai is the ONLY defensible non-zero spend, and only as a P7 Iteration 2 conditional on preconditions being met.** They do NOT support a P6 mxbai spend.

**Defender's verdict holds with one minor amendment:**
- Promote mxbai from "DEFER to P7" → "Default DEFER to P7; opt-in 1-iter at end-of-session if (a) eval-v3 file at n=150, (b) RunPod creds present, (c) user signs off."
- All preconditions are UNMET as of 2026-04-25. **So today, the operational answer remains: DEFER.**

The user said "не спішимо." That instruction outranks any clever attacker frame. **Default is bank.**

---

## §7. What attacker concedes to skeptic + defender

Where I attempted to attack and the math came back vindicating skeptic:
- SE formula at n=90 ±9pp, n=150 ±7pp — exactly correct (Attack 4).
- Power at n=150 is 0.50 for true +10pp detection — soft, vindicating skeptic's "achievable but not powerful."
- 4/4 reference class is too coarse for mxbai but the EV at $1 is still negative (Attack 5).
- Cannibalization is half-real: skeptic's quantification of 10 h infra holds (Attack 3).
- "Closing weak" optical concern doesn't beat the EV math (Attack 6).
- U1 + mxbai + gte-base trio is correlated bets at small-correlation; EV stays negative (Attack 2).

**Attacker's clean argument:** the only "do this today" recommendation I can defend with computed numbers is **NULL.** All preconditions for the one defensible spend (mxbai) are unmet. **Today: bank.**

**Attacker's secondary argument (for synthesis to consider):** defender's NEXT_SESSION_PROMPT should explicitly schedule mxbai as P7 Iteration 2 (between eval-v3 grow and FT spend), rather than vague "fallback if FT base-swap preferred." This is a textual amendment to defender's §7, not a verdict change.

---

## §8. Files inventory

If verdict synthesizer (task #3) accepts attacker amendments:
- `~/.code-rag-mcp/.claude/debug/debate-verdict.md` — minor amendment to §3 NO-GO bullet 5 + §6 question 5 + §7 NEXT_SESSION text
- `~/.code-rag-mcp/NEXT_SESSION_PROMPT.md` — promote mxbai to P7 Iteration 2 explicit slot
- `~/.code-rag-mcp/.claude/debug/debate-verdict-attacker.md` — this file

If synthesizer rejects attacker amendments:
- `~/.code-rag-mcp/.claude/debug/debate-verdict.md` — unchanged
- `~/.code-rag-mcp/.claude/debug/debate-verdict-attacker.md` — this file (for record)

**No code changes proposed by attacker.** U1 patch landing remains defender's call.
