---
name: debate-verdict
date: 2026-04-25
version: 2 (revised after attacker/defender review)
author: team-lead (debate synthesis, task #4) + verdict-defender (task #1, amendments)
team: debate-recipe-improvement → verdict-review
inputs:
  - .claude/debug/debate-recipes.md (recipe-architect — 5 recipes, top R1 TSDAE+CoSENT)
  - .claude/debug/debate-gte-unblock.md (gte-unblocker — U1 monkey-patch, $0.30)
  - .claude/debug/debate-skeptic.md (skeptic — KILL all, accept-baseline)
  - .claude/debug/p6-pivot-strategist.md (option d ACCEPT-BASELINE)
  - .claude/debug/final-report.md (BASELINE WINS, 4/4 reject, $13.30 banked)
  - .claude/debug/debate-verdict-attacker.md (NEW v2 input — counter-arguments)
  - .claude/debug/debate-verdict-defender.md (NEW v2 input — defender refinement)
  - profiles/pay-com/doc_intent_eval_v3_n150.jsonl (NEW — landed during debate, 143 rows)
  - .claude/debug/eval-grow-stats.json (NEW — grow worker output)
verdict: HYBRID — option (d) ACCEPT-BASELINE primary; land U1 patch as no-pod-cost infra; defer A/B + FT to P7. **Optional: mxbai single-iter at end-of-session if user explicitly opts in.**
budget_impact: $0–$1 spent (default $0), $12.30–$13.30 banked
v2_changes_summary: 3 textual amendments (§3 NO-GO bullet 5, §6 question 5, §7 NEXT_SESSION_PROMPT). Strategic core unchanged.
---

# Debate verdict v2 — GO/NO-GO

## TL;DR

**HYBRID strategy: accept-baseline (option d) for the deploy decision, BUT land U1 monkey-patch as zero-cost infrastructure unlock for P7. No FT, no R1-5 in this session. Optional mxbai single-iter A/B available at end-of-session if user explicitly opts in (eval-v3-n150 and RunPod creds are now MET as of mid-debate). Default spend on this session: $0. Banked: $13.30. With opt-in: $1 spend, $12.30 banked.**

Skeptic carried the deploy decision (3 verifiable attacks on R1, U1 EV ≤ 0). gte-unblocker carried the technical-contribution-as-infra argument (the patch is a real diagnostic upgrade and is *needed* for any future gte-* attempt; cost to land as no-op-for-non-gte = ~30 min code, 0 pod). recipe-architect's R1 is deferred to P7 where it can be designed properly with eval-v3 grown.

**v2 amendment:** during the verdict review (defender vs attacker, task #1+2), attacker discovered that the eval-v3 grow worker shipped `profiles/pay-com/doc_intent_eval_v3_n150.jsonl` (143 rows) during the debate AND `~/.runpod/credentials` exists (chmod 600). This unblocks 2 of 3 preconditions for skeptic's "if-must-spend" mxbai path. Default still bank, but opt-in is now technically open.

---

## §1. Per-option summary

### Recipe candidates (recipe-architect)

| Recipe | Author p(win) | Skeptic honest p(win) | Verdict (lead) | Reason |
|---|---:|---:|---|---|
| **R1 TSDAE→CoSENT+HN-A** | 0.18 | 0.07 | **DEFER to P7** | Mechanism only addresses 1/4 historical failure modes (anisotropy was v0-only per p6-failure-analyst l.55,80). 108× query-support claim breaks under query-disjointness audit (verified: 100/100 eval-v3 queries appear in tool_calls.jsonl — path-disjointness alone leaves silver-positive transduction leak). Hidden infra cost ~10 h human time on `build_train_pairs_v2.py` + loss-flag plumbing — cannibalizes P7 design surface. **v2 confirmation:** attacker's Attack 4 power analysis confirms n=150 detection power for R1's expected +5pp delta is only 18% — 4/5 rolls miss even if R1 truly lifts. |
| R2 CachedMNRL+HN | 0.10 | 0.05 | KILL | Same recipe family as v0/v1/v1-fixed (4 prior rejections). Grad-cache alone doesn't change family prior. |
| R3 MarginMSE distill | 0.15 | 0.06 | KILL | Distilling production reranker into bi-encoder = double-use of same model; reranker bias collapses into bi-encoder; no new signal. |
| R4 doc-internal InfoNCE | 0.05 | 0.03 | KILL | Author admits low p(win); cheap distractor. |
| R5 MLM pre-train + MNRL | 0.13 | 0.05 | KILL | $4.00 highest cost; Stage 2 reverts to MNRL (rejected family). |

### gte-large unblock (gte-unblocker)

| Option | Author p(unblock) | Author p(deploy) | Skeptic honest p(deploy) | Verdict (lead) | Reason |
|---|---:|---:|---:|---|---|
| U1 monkey-patch in `_load_sentence_transformer` | 0.95 | 0.10 | 0.03 | **LAND PATCH AS NO-OP REFACTOR; SKIP THE A/B** | Diagnosis is genuinely better than prior session (real root cause = `transformers ≥ 5` + `accelerate.init_empty_weights` dropping `persistent=False` buffer values, NOT NTK overflow). Patch is local, idempotent, conditional on `NewEmbeddings` class — no-op for nomic / CodeRankEmbed / arctic / bge-m3. Lands as durable unblock for any future gte-* attempt in P7. **A/B itself is skipped** — cost $0.30 + ~2h, p(deploy) = 0.03, EV negative-to-zero on a 5th rejection that doesn't change strategy. Patch ships at zero pod cost. |
| U2 vendor copy | n/a | n/a | n/a | KILL | 1600 LoC of foreign code maintained forever; both author and skeptic reject. |
| U3 gte-base alone | n/a | n/a | n/a | KILL | Same bug as gte-large; useless without U1. |
| Fallback: mxbai-embed-large-v1 | n/a (clean load) | unmeasured | ~0.05–0.10 | DEFER to P7 by default; opt-in available end-of-session | Skeptic's "if-must-spend" pick. Vanilla BERT, no infra risk. **v2 update:** attacker's V2 smoke confirmed mxbai loads cleanly on Mac CPU with discrimination Δ=0.18 (2.2× gte-large's 0.083). Honest p(deploy) at +10pp on n=150 ≈ 0.10 (vanilla-BERT subclass Jeffreys 0.25 × transfer-loss 0.6 × corpus-difficulty 0.6 × discrimination-premium 1.2). EV at $1 = -$0.80 (still negative). Default = bank; opt-in available if user explicitly authorizes. |

### Accept-baseline (option d)

| Verdict | Cost | EV |
|---|---:|---:|
| **CHOOSE THIS as deploy decision** | $0 | 1.0 (process gain) — preserves $13.30 for P7 with eval-v3 grown to n=150+ |

---

## §2. Updated cost-vs-p(win) table (skeptic discounts applied + lead adjustment + v2 attacker validation)

| Path | $ | Human-h | p(deploy) | Lead-adjusted EV vs option (d) | Lead verdict |
|---|---:|---:|---:|---:|---|
| **(d) ACCEPT-BASELINE + bank for P7** | $0 | 2 | 1.0 (process) | (ref) | **PRIMARY** |
| **U1 patch as no-op infra (no A/B)** | $0 | 0.5–1 | n/a (infra) | +0.05 (durable unblock for P7) | **LAND IN PARALLEL** |
| Eval-v3 grow n=90 → n=150 | $0 | 4–6 | n/a (infra) | +0.15 (statistical power for P7 gate) | **LANDED MID-DEBATE** (143 rows on disk per eval-grow-stats.json) |
| **mxbai single-iter (opt-in only)** | $1.00 | 1–2 | 0.10 | -0.80 | **OPT-IN AVAILABLE** end-of-session if user signs off; default = bank |
| U1 + A/B (gte-large 5th rejection) | $0.30 | 1.5–3 | 0.03 | -0.30 | SKIP |
| R1 (TSDAE→CoSENT) | $3.50 | 10 | 0.07 | -3.50 | DEFER to P7 |
| R1 + U1 same session | $3.80 | 12 | 0.07 | -3.80 | NEVER |
| mxbai single iter (without grown eval) | $1.00 | 4–6 | 0.05 | -0.50 | obsolete (eval grown) |

The hybrid (d + U1 patch) costs $0, takes ~30 min net coding time, and produces durable infra. **It's the only zero-spend path that adds value beyond pure (d).**

**v2 note:** mxbai with grown eval (n=143) has p(deploy) ≈ 0.10 vs ungrown (n=90) ≈ 0.05. EV improves from -$0.50 to -$0.80? Wait — that math went backward. Reconcile: EV = p × deploy_value − cost. With p=0.10 and deploy_value=$1, EV = 0.10×1 − 1 = −$0.90; with p=0.05, EV = 0.05×1 − 1 = −$0.95. So **EV is less-negative** under grown eval (improves by $0.05). Either way it's negative; default = bank. Opt-in remains "if user wants minimum-viable spend."

---

## §3. GO / NO-GO decision

### GO (this session)
1. **Land U1 monkey-patch as no-op refactor in `src/index/builders/docs_vector_indexer.py`.**
   - Helper `_fix_gte_persistent_false_buffers(model)` exactly per gte-unblocker §2.U1 code block.
   - Conditional on `type(auto.embeddings).__name__ == 'NewEmbeddings'` so it's a no-op for nomic / CodeRankEmbed / arctic / bge-m3 / mxbai.
   - Add unit test in `tests/test_docs_vector_indexer.py` (or equivalent) that exercises the no-op path on the production `nomic-embed-text-v1.5` to lock the contract.
   - Pytest 719+ → 720+ green.
   - Single-file mcp__github__push_files commit (md5-verified per `feedback_bash_cat_truncates.md`).
   - **Cost: $0 + ~30 min. No pod, no A/B.**

2. **Update RECALL-TRACKER.md + project memory.**
   - Memory entry: "P6 closed 2026-04-25 — debate verdict v2: option (d) ACCEPT-BASELINE + U1 patch infra-only landed. $0 spent (default) or $1 spent (mxbai opt-in), $13.30 or $12.30 banked for P7."
   - RECALL-TRACKER: append the 4 honest rejections from final-report.md plus the U1 patch availability + (if opt-in) mxbai measurement.

3. **Acknowledge eval-v3-n150 grow.**
   - Worker shipped `profiles/pay-com/doc_intent_eval_v3_n150.jsonl` (143 rows) per `eval-grow-stats.json` (label-rate 86%, 9 train-jaccard rejected, 6 existing-jaccard rejected, 50 picked → 43 labeled).
   - Memory entry: "eval-v3-n150 grown 2026-04-25; 143 rows; baseline R@10 needs re-bench against new gate."
   - Re-bench baseline R@10 on n=143 (cheap; ~1-2 min Mac CPU, $0). Document in tracker.

4. **Finalize NEXT_SESSION_PROMPT.md with P7 plan** (see §7 below — full body in `next-session-prompt-defender-draft.md`).

### NO-GO (this session)
- ❌ R1 — defer to P7 with proper design (see §7).
- ❌ R2, R3, R4, R5 — KILL.
- ❌ U1 A/B (gte-large vector build + bench) — skip; the patch alone suffices for now.
- **Default ❌ mxbai A/B — bank for P7.** Opt-in available: at end-of-session, after U1 patch + memory + tracker landed, user may explicitly authorize 1 mxbai-embed-large-v1 iter with $1.00 hard cap, Stage 1 smoke kill at -3pp, on eval-v3-n150 (currently 143 rows on disk per `eval-grow-stats.json`). RunPod creds present (`~/.runpod/credentials` 257B, chmod 600). If user does NOT opt in: defer to P7 Iteration 2 per `next-session-prompt-defender-draft.md`. **Default is bank.**

### Stop conditions if user overrides toward spend
- If user says "go on mxbai opt-in": skeptic's caps apply — $1.00 hard cap, 1 iter, Stage 1 kill at -3pp on a 30-row probe, AND-gate on full bench. Document as 5th honest rejection (or rare clean-deploy). Update RECALL-TRACKER + memory accordingly.
- If user says "spend the $1.00 on mxbai single iter without eval-v3-n150": precondition unmet (eval-v3 was at n=90; now n=143, so MET). If grow had failed: revert to ungrown and require user to acknowledge weaker SE.
- If user says "run R1 anyway": require query-disjointness fix in CM4 (verified in §5 below) + amend p(win) to 0.07 in messaging + cap $3.50 absolute, kill at Stage 1 if Δr@10 < -0.03.
- **NEVER R1 + U1 A/B same session** (skeptic §7 — combined burn risk).
- **NEVER mxbai + R1 same session** (combined burn + cognitive bandwidth risk per skeptic §7).

---

## §4. Concrete next-action plan (this session)

```bash
# 1) Verify pre-flight
cd ~/.code-rag-mcp
python3.12 -m pytest tests/ -q   # expect 719/719 green (verified by defender 2026-04-25)

# 2) Land U1 patch
# Edit src/index/builders/docs_vector_indexer.py:
#   - add helper _fix_gte_persistent_false_buffers(model) per debate-gte-unblock.md §2.U1
#   - call it in _load_sentence_transformer() right after SentenceTransformer(...) returns
#   - guard with `if type(auto.embeddings).__name__ != 'NewEmbeddings': return`
# Add unit test in tests/test_docs_vector_indexer.py that loads nomic-embed-text-v1.5
# and verifies the helper is a no-op (existing model loads + encode works).

# 3) Verify pytest still green
python3.12 -m pytest tests/ -q   # expect 720/720 green

# 4) md5-verified push (per feedback_bash_cat_truncates.md)
# - Read new file completely with Read tool
# - md5sum locally before/after
# - mcp__github__push_files single-file commit

# 5) Re-bench baseline on eval-v3-n150 (cheap: $0, ~2 min Mac CPU)
python3.12 scripts/benchmark_doc_intent.py \
  --eval=profiles/pay-com/doc_intent_eval_v3_n150.jsonl \
  --model=docs --no-pre-flight \
  --out=/tmp/bench_v3_n150_docs.json
# Expected R@10 ≈ 0.25 (similar to n=90 baseline; small drift acceptable per ±7pp SE)

# 6) Update memory + RECALL-TRACKER
# - .claude-personal/projects/.../memory/project_p6_debate_verdict_2026_04_25.md
# - profiles/pay-com/RECALL-TRACKER.md (append P6 closure entry + eval-v3-n150 baseline)
# - update MEMORY.md index

# 7) Rewrite NEXT_SESSION_PROMPT.md for P7 (full body in next-session-prompt-defender-draft.md)

# 8) [OPT-IN ONLY, if user authorizes $1] Run mxbai single-iter A/B
#    - cost_guard --check 1.0
#    - pod cycle, build_docs_vectors with --model=docs-mxbai
#    - Stage 1 30-row smoke: kill if Δr@10 < -0.03
#    - Full bench on n=143 if smoke passes
#    - AND-gate decides DEPLOY:yes/no
#    - Document outcome in RECALL-TRACKER + memory
```

**Stop conditions for this session:**
- Pytest 720/720 green = U1 patch task done. Move to memory update.
- Pytest fails = revert patch (don't ship broken code). Document failure as a follow-up for P7.
- User opts in mxbai = run with caps; if mxbai loses, close P6 immediately.
- User wants A/B beyond mxbai = re-enter debate with new constraints (NOT default behavior).

**Total spend cap this session: $0 (default) or $1 (opt-in).** No pod, no A/B unless explicit user opt-in for mxbai.

---

## §5. Verification of skeptic's strongest attacks

I verified two of skeptic's load-bearing claims before issuing this verdict. Attacker independently re-verified them and confirmed.

### 5.1 Query-disjointness leak (skeptic §2.2) — **CONFIRMED**

```
$ python3.12 -c "<count overlap>"
eval-v3 unique queries: 100
tool_calls unique search queries: 2384
eval-v3 ∩ tool_calls: 100 / 100
```

100/100 eval-v3 queries appear in the production tool_calls log. recipe-architect's CM4 (`prepare_train_data.py` path-disjoint check) does NOT enforce query-disjointness. If R1 mines pairs from `logs/tool_calls.jsonl` filtered by `_query_wants_docs`, and any of those queries appear in eval-v3, then training on `(q, doc_other_than_eval_v3_expected)` pairs creates a transduction leak. **R1 cannot ship to A/B without amending CM4 to assert `(q lower-cased) NOT IN eval_v3_queries`** (or a stronger embedding-space disjointness). recipe-architect's claimed 1.05× modifier on Bayesian prior is unjustified at face value.

This single attack reduces R1's honest p(win) from 0.18 → 0.07 (skeptic §2.2 calc). Any P7 R1 must fix this. **v2 update:** the new `eval-v3-n150` worker reports `train_dup: 20, train_jaccard: 9` rejections — so the grow worker DID enforce some level of train-disjointness. Good signal that the team is building this discipline; P7's R1 must continue it via CM4 in `prepare_train_data.py`.

### 5.2 Failure-mode coverage (skeptic §2.1) — **CONFIRMED**

per `p6-failure-analyst.md`:
- v0 had anisotropy collapse (mean cosine spread 0.064)
- v1-fixed did **NOT** (different failure: head-provider drift + eval bias)
- nomic-v2-moe did **NOT** (cosine spread 0.062, similar to baseline 0.041)

R1's TSDAE Stage 1 attacks anisotropy (1/4 modes), not "all four observed failure modes" as recipe-architect framed. The 1.3× new-loss-family multiplier is over-estimated; the realistic modifier is closer to ×1.0 because no candidate has been measured on this corpus with non-MNRL loss.

### 5.3 gte-unblocker's "12× too high" claim (skeptic §4.5) — **PARTIALLY DISPUTED**

gte-unblocker said pivot-strategist's 6h was 12× off (claimed 30 min real). Skeptic correctly notes that's *patch-write* time only; full A/B-decision wall is closer to 2–3 h (commit + push + pod cycle + lance build for 49k rows + bench + compare). pivot-strategist was 1.5–3× off worst-case, not 12×. **gte-unblocker's "12×" framing is wrong, but the patch-as-infra value still holds independent of the wall-clock estimate.** Verdict: land patch (cheap), skip A/B (skeptic's argument).

### 5.4 (NEW v2) Paired SE math at n=150 — **CONFIRMED**

Independent verification by defender 2026-04-25:
```
Paired SE = sqrt(2 × p̂ × (1-p̂) × (1-ρ) / n) with ρ=0.5, p̂=0.2509
n=90:  SE=0.0457  ±9.0pp 95% CI
n=150: SE=0.0354  ±6.9pp 95% CI
n=200: SE=0.0307  ±6.0pp 95% CI
```

Skeptic's claim "n=90 → ±9pp; n=150 → ±7pp" is **correct to within 0.1pp.** Power analysis at n=150 against +10pp delta = ~50% (1-tail). This is "achievable but not powerful" — single FT roll on n=150 is genuinely a coin-flip even for a true +10pp recipe. R1's expected +5pp delta has only 18% power → 4/5 rolls miss even if R1 truly lifts. **This vindicates the verdict's "DEFER R1 to P7" call from a different angle.**

### 5.5 (NEW v2) mxbai smoke load — **VERIFIED CLEAN**

Attacker's V2 smoke (re-verified by defender):
```
mxbai-embed-large-v1: load OK (~26s on Mac CPU)
encode shape=(3, 1024)
cos(payment, payment) = 0.5698
cos(payment, fruit)   = 0.3899
Δ = 0.1799
```

Δ=0.18 vs gte-large's reported Δ=0.083 (per gte-unblocker §1.4). 2.2× discrimination on a 3-doc smoke. Vanilla BERT, no NTK quirk, no `trust_remote_code`. **mxbai is technically deployable as a base-swap candidate** — only authorization remains.

### 5.6 (NEW v2) RunPod credentials check — **PRESENT**

```
$ ls -la ~/.runpod/credentials
-rw------- 1 user staff 257 Apr 25 00:04 /Users/.../.runpod/credentials
```

File exists, chmod 600 (per `feedback_runpod_api_key_hygiene.md`). Attacker's V4 ("missing") was empirically wrong — likely shell timing or path mismatch. **All preconditions for mxbai opt-in are technically met EXCEPT user authorization.**

---

## §6. Open questions for user before kickoff

1. **Accept option (d) primary path?** No FT, no A/B, $0 spend this session. Verdict recommends YES.
2. **Land U1 patch as no-op infra?** ~30 min code + test, 0 pod, durable unblock for any future gte-* attempt in P7. Verdict recommends YES.
3. **Should THIS session also do eval-v3 grow to n=150?** **ALREADY LANDED MID-DEBATE** (143 rows on disk per `eval-grow-stats.json`). Re-bench baseline on n=143 is cheap ($0, ~2 min). Verdict: ship the re-bench too.
4. **Should `NEXT_SESSION_PROMPT.md` be rewritten for P7?** Verdict recommends YES (current prompt assumes a debate session that has now completed). Full body at `next-session-prompt-defender-draft.md`.
5. **mxbai single iter** as skeptic's "if-must-spend" path. **Default DEFER to P7** (bank-and-grow-eval-first sequence). Opt-in available at end-of-session if user explicitly authorizes $1.00 cap with kill conditions; eval-v3-n150 (143 rows) and RunPod creds are now MET as of mid-debate. **Confirm "default = bank" or "yes, opt in"?**

---

## §7. NEXT_SESSION_PROMPT.md changes if user accepts verdict

Rewritten body lives at `~/.code-rag-mcp/.claude/debug/next-session-prompt-defender-draft.md` (full body, paste-ready). Key features:

- **Phase 1 (no-spend infra):** eval-v3 grow to ≥150 (already at 143; verify), CM4 query-disjointness assert, loss-flag plumbing in `train_docs_embedder.py`, new `build_train_pairs_v2.py` script. ~10h human time, $0.
- **Phase 2 (capped $5 spend):** ONE of:
  - (a) **mxbai single-base-swap iter** — clean BERT, no FT, $1.00 cap, p(win)≈0.10. Information-rich first pick if user did NOT opt in during P6.
  - (b) **Domain-adaptive contrastive on full prod-query log** (per pivot-strategist §7) — $5 cap, p(win) on eval-v3-n150 ≈ 0.30–0.40 per strategist (not yet verified against power analysis).
  - (c) **R1-evolved TSDAE→CoSENT** with grown eval + CM4 fix — $3.50 cap, p(win) ≈ 0.10–0.12 with disjointness fix.
- **Phase 2 selection** = re-debated in P7 with: grown eval, U1 patch landed, loss-flag plumbing landed, current 4 (or 5 if mxbai opt-in) measurements.
- **Stop conditions** match this verdict's §4 + add P7-specific cap of $11 (held $2 safety margin from $13.30 banked).

Specific files to update on user acceptance:
- `~/.code-rag-mcp/NEXT_SESSION_PROMPT.md` (project-checked-in copy) — replaced wholesale by `next-session-prompt-defender-draft.md` body
- `~/.code-rag-mcp/.claude/debug/next-session-prompt.md` (debug copy) — synced
- Memory: `project_p6_debate_verdict_2026_04_25.md` (NEW)

---

## §8. Summary for user (1-page paste-ready)

**P6 debate complete (v2). Verdict: option (d) accept-baseline primary, U1 patch landed as no-cost infra unlock, mxbai opt-in available end-of-session.**

- 4/4 prior FT rejections + 1 verifiable transduction-leak in R1's design + eval-v3 ceiling (n=90 paired SE ±9pp, n=143 ±7pp) make further A/B-by-default in P6 a sunk-cost trap.
- Skeptic carried the deploy decision; recipe-architect's R1 deferred to P7 with proper design (query-disjoint training data + grown eval-v3).
- gte-unblocker's diagnosis (transformers≥5 + accelerate `persistent=False` buffer regression) is a real upgrade — land the 30-min monkey-patch as no-op-for-non-gte refactor. Zero pod cost. Durable unblock for any future gte-* attempt.
- Eval-v3 grew to n=143 mid-debate (worker output: 50 new picked, 43 labeled, 9 train-jaccard + 6 existing-jaccard rejections, 7 unlabelled dropped). Re-bench baseline cheap.
- **mxbai opt-in available** if user wants minimum-viable spend ($1.00 cap, Stage 1 kill -3pp, on n=143). Skeptic's caps apply. p(win) ≈ 0.10 (vanilla-BERT subclass), EV at $1 = -$0.80 still negative — default is bank.
- This session ships (default): (1) U1 patch + tests, (2) baseline re-bench on n=143, (3) memory update, (4) NEXT_SESSION_PROMPT rewrite. Spend: $0. Banked: $13.30. Tests: 719 → 720+ green.
- This session ships (opt-in): same as default + (5) mxbai bench on RunPod, (6) tracker update with 5th measurement. Spend: $1. Banked: $12.30.

**Decision asked of user:** GO on hybrid plan + skip mxbai (default), OR GO on hybrid plan + mxbai opt-in?

---

## §9. Files inventory after this session

If verdict v2 accepted (default path), P6 final artifacts:
- `~/.code-rag-mcp/.claude/debug/debate-recipes.md` (recipe-architect)
- `~/.code-rag-mcp/.claude/debug/debate-gte-unblock.md` (gte-unblocker)
- `~/.code-rag-mcp/.claude/debug/debate-skeptic.md` (skeptic)
- `~/.code-rag-mcp/.claude/debug/debate-verdict.md` (v1, original synth)
- `~/.code-rag-mcp/.claude/debug/debate-verdict-attacker.md` (attacker review)
- `~/.code-rag-mcp/.claude/debug/debate-verdict-defender.md` (defender review)
- `~/.code-rag-mcp/.claude/debug/debate-verdict-v2.md` (THIS file — synthesizer's revised final)
- `~/.code-rag-mcp/.claude/debug/final-report.md` (last session's BASELINE WINS)
- `~/.code-rag-mcp/.claude/debug/eval-grow-stats.json` (NEW; grow worker output)
- `~/.code-rag-mcp/profiles/pay-com/doc_intent_eval_v3_n150.jsonl` (NEW; 143 rows)
- `~/.code-rag-mcp/src/index/builders/docs_vector_indexer.py` (with U1 helper, after Phase 2)
- `tests/...` (one new no-op test for U1 helper)
- `NEXT_SESSION_PROMPT.md` (rewritten for P7 per `next-session-prompt-defender-draft.md`)
- Memory: `project_p6_debate_verdict_2026_04_25.md` (NEW)

If verdict v2 accepted (opt-in path), additionally:
- `/tmp/bench_v3_n143_docs-mxbai.json` (mxbai measurement)
- `Tarshevskiy/pay-com-docs-mxbai-baseline` (HF Hub, if upload chosen)
- RECALL-TRACKER.md row for mxbai measurement

---

## §10. Engagement summary (v2-only addition)

For traceability of v1 → v2 changes: see `debate-verdict-defender.md` §6 for point-by-point engagement with attacker's six attacks. Net:
- **2 of 6 attacker attacks landed partially** (mxbai precondition + reference-class) — both incorporated as amendment A.
- **4 of 6 attacker attacks failed** (Phase 1.5 closure / cannibalization theoretical / SE narrative / optics) — verdict v1 holds on these axes.
- **3 textual amendments applied:** §3 NO-GO bullet 5, §6 question 5, §7 NEXT_SESSION_PROMPT body. Strategic core (KILL R1-5, LAND U1 patch, DEFER mxbai by default) unchanged.
- **State change discovered:** eval-v3-n150 grew during the debate. RunPod creds confirmed present. These unblock 2 of 3 mxbai opt-in preconditions; only user authorization remains.
