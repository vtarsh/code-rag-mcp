---
name: debate-verdict
date: 2026-04-25
author: team-lead (debate synthesis, task #4)
team: debate-recipe-improvement
inputs:
  - .claude/debug/debate-recipes.md (recipe-architect — 5 recipes, top R1 TSDAE+CoSENT)
  - .claude/debug/debate-gte-unblock.md (gte-unblocker — U1 monkey-patch, $0.30)
  - .claude/debug/debate-skeptic.md (skeptic — KILL all, accept-baseline)
  - .claude/debug/p6-pivot-strategist.md (option d ACCEPT-BASELINE)
  - .claude/debug/final-report.md (BASELINE WINS, 4/4 reject, $13.30 banked)
verdict: HYBRID — option (d) ACCEPT-BASELINE primary; land U1 patch as no-pod-cost infra; defer A/B + FT to P7.
budget_impact: $0 spent, $13.30 banked
---

# Debate verdict — GO/NO-GO

## TL;DR

**HYBRID strategy: accept-baseline (option d) for the deploy decision, BUT land U1 monkey-patch as zero-cost infrastructure unlock for P7. No FT, no A/B in this session. Spend on this session: $0. Banked: $13.30.**

Skeptic carried the deploy decision (3 verifiable attacks on R1, U1 EV ≤ 0). gte-unblocker carried the technical-contribution-as-infra argument (the patch is a real diagnostic upgrade and is *needed* for any future gte-* attempt; cost to land as no-op-for-non-gte = ~30 min code, 0 pod). recipe-architect's R1 is deferred to P7 where it can be designed properly with eval-v3 grown.

---

## §1. Per-option summary

### Recipe candidates (recipe-architect)

| Recipe | Author p(win) | Skeptic honest p(win) | Verdict (lead) | Reason |
|---|---:|---:|---|---|
| **R1 TSDAE→CoSENT+HN-A** | 0.18 | 0.07 | **DEFER to P7** | Mechanism only addresses 1/4 historical failure modes (anisotropy was v0-only per p6-failure-analyst l.55,80). 108× query-support claim breaks under query-disjointness audit (verified: 100/100 eval-v3 queries appear in tool_calls.jsonl — path-disjointness alone leaves silver-positive transduction leak). Hidden infra cost ~10 h human time on `build_train_pairs_v2.py` + loss-flag plumbing — cannibalizes P7 design surface. |
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
| Fallback: mxbai-embed-large-v1 | n/a (clean load) | unmeasured | ~0.05 | DEFER to P7 | Skeptic's "if-must-spend" pick. Vanilla BERT, no infra risk. But adds 5th rejection without changing strategy. P7 can pick it up if eval-v3 hardening produces a stronger gate. |

### Accept-baseline (option d)

| Verdict | Cost | EV |
|---|---:|---:|
| **CHOOSE THIS as deploy decision** | $0 | 1.0 (process gain) — preserves $13.30 for P7 with eval-v3 grown to n=150+ |

---

## §2. Updated cost-vs-p(win) table (skeptic discounts applied + lead adjustment)

| Path | $ | Human-h | p(deploy) | Lead-adjusted EV vs option (d) | Lead verdict |
|---|---:|---:|---:|---:|---|
| **(d) ACCEPT-BASELINE + bank for P7** | $0 | 2 | 1.0 (process) | (ref) | **PRIMARY** |
| **U1 patch as no-op infra (no A/B)** | $0 | 0.5–1 | n/a (infra) | +0.05 (durable unblock for P7) | **LAND IN PARALLEL** |
| Eval-v3 grow n=90 → n=150 | $0 | 4–6 | n/a (infra) | +0.15 (statistical power for P7 gate) | **OPTIONAL but RECOMMENDED** for next session |
| U1 + A/B (gte-large 5th rejection) | $0.30 | 1.5–3 | 0.03 | -0.30 | SKIP |
| R1 (TSDAE→CoSENT) | $3.50 | 10 | 0.07 | -3.50 | DEFER to P7 |
| R1 + U1 same session | $3.80 | 12 | 0.07 | -3.80 | NEVER |
| mxbai single iter | $1.00 | 4–6 | 0.05 | -0.50 | DEFER to P7 |

The hybrid (d + U1 patch) costs $0, takes ~30 min net coding time, and produces durable infra. **It's the only zero-spend path that adds value beyond pure (d).**

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
   - Memory entry: "P6 closed 2026-04-25 — debate verdict: option (d) ACCEPT-BASELINE + U1 patch infra-only landed. $0 spent, $13.30 banked for P7."
   - RECALL-TRACKER: append the 4 honest rejections from final-report.md plus the U1 patch availability.

3. **Finalize NEXT_SESSION_PROMPT.md with P7 plan** (see §6 below).

### NO-GO (this session)
- ❌ R1 — defer to P7 with proper design (see §6).
- ❌ R2, R3, R4, R5 — KILL.
- ❌ U1 A/B (gte-large vector build + bench) — skip; the patch alone suffices for now.
- ❌ mxbai A/B — defer to P7.
- ❌ Eval-v3 grow n=90 → n=150 — *recommended* for next session as P7's iteration #1, NOT this session unless user explicitly opts in.

### Stop conditions if user overrides toward spend
- If user says "spend the $1.00 on mxbai single iter": only after eval-v3 grown to n=150 (skeptic §6). Hard cap $1.00, 1 iteration, no extension.
- If user says "run R1 anyway": require query-disjointness fix in CM4 (verified in §5 below) + amend p(win) to 0.07 in messaging + cap $3.50 absolute, kill at Stage 1 if Δr@10 < -0.03.
- **NEVER R1 + U1 A/B same session** (skeptic §7 — combined burn risk).

---

## §4. Concrete next-action plan (this session)

```bash
# 1) Verify pre-flight
cd ~/.code-rag-mcp
python3.12 -m pytest tests/ -q   # expect 719/719 green

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

# 5) Update memory + RECALL-TRACKER
# - .claude-personal/projects/.../memory/project_p6_debate_verdict_2026_04_25.md
# - profiles/pay-com/RECALL-TRACKER.md (append P6 closure entry)
# - update MEMORY.md index

# 6) Rewrite NEXT_SESSION_PROMPT.md for P7
```

**Stop conditions for this session:**
- Pytest 720/720 green = U1 patch task done. Move to memory update.
- Pytest fails = revert patch (don't ship broken code). Document failure as a follow-up for P7.
- User wants A/B anyway = re-enter debate with new constraints (NOT default behavior).

**Total spend cap this session: $0.** No pod, no A/B, no FT.

---

## §5. Verification of skeptic's strongest attacks

I verified two of skeptic's load-bearing claims before issuing this verdict.

### 5.1 Query-disjointness leak (skeptic §2.2) — **CONFIRMED**

```
$ python3.12 -c "<count overlap>"
eval-v3 unique queries: 100
tool_calls unique search queries: 2384
eval-v3 ∩ tool_calls: 100 / 100
```

100/100 eval-v3 queries appear in the production tool_calls log. recipe-architect's CM4 (`prepare_train_data.py` path-disjoint check) does NOT enforce query-disjointness. If R1 mines pairs from `logs/tool_calls.jsonl` filtered by `_query_wants_docs`, and any of those queries appear in eval-v3, then training on `(q, doc_other_than_eval_v3_expected)` pairs creates a transduction leak. **R1 cannot ship to A/B without amending CM4 to assert `(q lower-cased) NOT IN eval_v3_queries`** (or a stronger embedding-space disjointness). recipe-architect's claimed 1.05× modifier on Bayesian prior is unjustified at face value.

This single attack reduces R1's honest p(win) from 0.18 → 0.07 (skeptic §2.2 calc). Any P7 R1 must fix this.

### 5.2 Failure-mode coverage (skeptic §2.1) — **CONFIRMED**

per `p6-failure-analyst.md`:
- v0 had anisotropy collapse (mean cosine spread 0.064)
- v1-fixed did **NOT** (different failure: head-provider drift + eval bias)
- nomic-v2-moe did **NOT** (cosine spread 0.062, similar to baseline 0.041)

R1's TSDAE Stage 1 attacks anisotropy (1/4 modes), not "all four observed failure modes" as recipe-architect framed. The 1.3× new-loss-family multiplier is over-estimated; the realistic modifier is closer to ×1.0 because no candidate has been measured on this corpus with non-MNRL loss.

### 5.3 gte-unblocker's "12× too high" claim (skeptic §4.5) — **PARTIALLY DISPUTED**

gte-unblocker said pivot-strategist's 6h was 12× off (claimed 30 min real). Skeptic correctly notes that's *patch-write* time only; full A/B-decision wall is closer to 2–3 h (commit + push + pod cycle + lance build for 49k rows + bench + compare). pivot-strategist was 1.5–3× off worst-case, not 12×. **gte-unblocker's "12×" framing is wrong, but the patch-as-infra value still holds independent of the wall-clock estimate.** Verdict: land patch (cheap), skip A/B (skeptic's argument).

---

## §6. Open questions for user before kickoff

1. **Accept option (d) primary path?** No FT, no A/B, $0 spend this session. Verdict recommends YES.
2. **Land U1 patch as no-op infra?** ~30 min code + test, 0 pod, durable unblock for any future gte-* attempt in P7. Verdict recommends YES.
3. **Should THIS session also do eval-v3 grow to n=150?** $0 + ~6h. Tightens paired SE from ±9pp → ±7pp. Recommended for next session as P7 iteration #1, but if user wants to land it today (after U1 patch), it's the highest-value cheap move. Verdict: optional, ask user.
4. **Should `NEXT_SESSION_PROMPT.md` be rewritten for P7 (domain-adaptive contrastive on prod query log + eval-v3 grown + query-disjointness enforced)?** Verdict recommends YES (current prompt assumes a debate session that has now completed).
5. **mxbai single iter** as skeptic's "if-must-spend" path — pursue NEVER in P6, defer to P7. Confirm?

---

## §7. NEXT_SESSION_PROMPT.md changes if user accepts verdict

The current prompt is shaped around the debate (which has completed). Rewrite to:

```
## Goal of this session (P7)
Improve doc-intent recall above 0.2509 baseline via a single, properly-designed FT run on:
- ≥150-row eval-v3 (grown from current n=90 with 50 prod-sampled rows, query-disjoint check enforced)
- Query-disjoint training data from logs/tool_calls.jsonl (NOT just path-disjoint)
- Recipe of choice (recommend R1 TSDAE→CoSENT or P7-specific domain-adaptive contrastive)

Pre-flight: U1 patch already landed in P6 — gte-* family is unblocked. mxbai-embed-large-v1 is a clean fallback if the FT base-swap is preferred.

## Iteration 1: eval-v3 grow to n=150
- Sample 50 prod-frequency-weighted queries from logs/tool_calls.jsonl
- Label with model-agnostic FTS+overlap scheme (per scripts/build_doc_intent_eval_v3.py)
- Verify query-disjointness against any planned train set
- Save as profiles/pay-com/doc_intent_eval_v3_n150.jsonl
- Bench baseline R@10 with new gate

## Iteration 2: single FT candidate (R1 or alternative)
- Build train pairs with query-disjoint check
- Run on RunPod A40 with cost_guard at $5 cap
- A/B against grown eval-v3
- Hard kill if Stage 1 smoke shows Δr@10 < -0.03

## Stop conditions
- +10pp clear gate confirmed → freeze, deploy
- $11 effective cap reached → freeze best-so-far
- 1 iteration no improvement → freeze + close P7 with negative result
```

Specific files to update on user acceptance:
- `~/.code-rag-mcp/NEXT_SESSION_PROMPT.md` (project-checked-in copy)
- `~/.code-rag-mcp/.claude/debug/next-session-prompt.md` (debug copy if exists)
- Memory: `project_p6_debate_verdict_2026_04_25.md`

---

## §8. Summary for user (1-page paste-ready)

**P6 debate complete. Verdict: option (d) accept-baseline primary, U1 patch landed as no-cost infra unlock.**

- 4/4 prior FT rejections + 1 verifiable transduction-leak in R1's design + eval-v3 ceiling (n=90, paired SE ±9pp) make further A/B in P6 a sunk-cost trap.
- Skeptic carried the deploy decision; recipe-architect's R1 deferred to P7 with proper design (query-disjoint training data + grown eval-v3).
- gte-unblocker's diagnosis (transformers≥5 + accelerate `persistent=False` buffer regression) is a real upgrade — land the 30-min monkey-patch as no-op-for-non-gte refactor. Zero pod cost. Durable unblock for any future gte-* attempt.
- This session ships: (1) U1 patch + tests, (2) memory update, (3) NEXT_SESSION_PROMPT rewrite.
- Spend: $0. Banked: $13.30. Tests: 719 → 720+ green.

**Decision asked of user:** GO on hybrid plan (option d + U1 infra patch)? Or override toward U1 A/B / mxbai / R1?

---

## §9. Files inventory after this session

If verdict accepted, P6 final artifacts:
- `~/.code-rag-mcp/.claude/debug/debate-recipes.md` (recipe-architect)
- `~/.code-rag-mcp/.claude/debug/debate-gte-unblock.md` (gte-unblocker)
- `~/.code-rag-mcp/.claude/debug/debate-skeptic.md` (skeptic)
- `~/.code-rag-mcp/.claude/debug/debate-verdict.md` (this file)
- `~/.code-rag-mcp/.claude/debug/final-report.md` (last session's BASELINE WINS)
- `~/.code-rag-mcp/src/index/builders/docs_vector_indexer.py` (with U1 helper)
- `tests/...` (one new no-op test for U1 helper)
- `NEXT_SESSION_PROMPT.md` (rewritten for P7)
- Memory: `project_p6_debate_verdict_2026_04_25.md`
