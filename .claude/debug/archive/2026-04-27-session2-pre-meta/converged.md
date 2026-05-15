# CONVERGED — jira recall improvement strategy (planning debate, 2026-04-27)

> 3 teammates (Pragmatist / Systematist / Refactorist), 2 rounds.
> Goal: lift jira hit@10 from 41.6% to ≥60% by ROOT CAUSE attack, not symptom treatment.

## Winner — primary approach (3/3 KEEP after round 2)

### **W1: Two-stage retrieval contract (R-A2)**

Refactor `hybrid_search()` to separate concerns:
- **Stage 1 — RECALL**: build the candidate pool with NO boosts, NO penalties, NO stratum-routing-as-gate. Measurable SLO: `P(GT ∈ pool@K) ≥ 0.95` with tunable K (start K=500). Includes: FTS5, vector (both towers), code_facts, env_vars, glossary-expansion, equivalence-class hits.
- **Stage 2 — PRECISION**: rerank + boosts + penalties operate in ONE normalized space (currently boosts multiply pre-rerank in raw RRF, penalties subtract post-rerank in [0,1] — structurally asymmetric, drives 10× doc-top-1 over-rep on jira misses).

**Why all three teammates kept it**:
- P (downscoped form): "B/P single normalization step kills H3+H9 class-of-bug in ~50 LOC"
- S: "subsumes my partitioned-metric idea; gateable SLO; recall/precision invariant unrepresentable"
- R: "kills the CLASS of bug behind H3+H9+H4+H10+IR6"

**Failure mode**: full SLO-gated contract is ~500 LOC across 3 files (hybrid.py + service.py + new metric helpers). Risk of slipping into "framework" territory. **Mitigation**: ship downscoped P-form first (just unify B/P normalization → kills 70% of the class without touching pool building); land Stage-1 SLO instrumentation as separate change.

**Expected lift**: +4-8pp jira hit@10; v2 docs neutral; future eval high.

## Tier 2 — supporting approaches (2/3 KEEP)

### **W2: Bench-prod parity + glossary growth (P-A1, R-Ad1)**

- One-line fix: `bench_routing_e2e.py:107` calls `hybrid_search(query)` directly; production calls `expand_query(query)` first via `service.py:68`. Wire `expand_query()` into bench so we measure the pipeline production actually serves (kills IR5).
- Grow `profiles/pay-com/glossary.yaml` from jira-miss tokens (current 68 mappings → 120-150 entries from observed payment-domain terms missing in jira eval).

**Why kept**: P (rank 1), R (Ad1 "ship Monday").
**Why S didn't list explicitly**: contracts-frame, but R points out S's reachability invariant requires this fix as prerequisite.
**Failure mode**: glossary expansion may degrade ALREADY-clean queries by adding synonym noise to long PR titles; solution = bound expansion to queries < N tokens or only when no FTS hits.
**Expected lift**: +2-5pp jira hit@10 (capped because only ~7-15% of misses contain glossary-mappable tokens).

### **W3: Typed FTS5Query value (S-A1)**

Pure type wrapper: `FTS5Query(raw, sanitized, valid)` constructor that PROVES the query won't raise OperationalError. Today's IR2 patch (`_sanitize_fts_input` regex extension) is the runtime fix; the type makes the invariant unrepresentable in code (any code calling `chunks MATCH` accepts only `FTS5Query`).

**Why kept**: S (rank 2), R (rank 2 Ad3).
**Why P drops**: "IR2 fix already lands the runtime invariant; the type is overhead".
**Failure mode**: type wrapper without a registry of producers can drift back to string usage (mitigation: lint rule against `chunks MATCH ?` with raw string).
**Expected lift**: +0pp incremental over IR2 sanitize fix; structural benefit (prevents regression).

### **W4: Equivalence-class index column (S-A3)**

User's grep+synonym proposal — implemented at indexing boundary, not search-time pipeline.
- Add `equiv_class_id` column to `chunks` table during build.
- For known synonym pairs (`ach ↔ "automated clearing house"`, `apm ↔ "alternative payment method"`), assign same class id.
- FTS5 query expanded to include `OR equiv_class_id = X` for the query's tokens — bounded expansion, no query-length blowup.

**Why kept**: S (rank 3), R (Ad2 "replaces my round-1 A1, structurally cleaner").
**Why P drops**: "schema change + rebuild for +2-4pp contingent on A1+A2".
**Failure mode**: equivalence pairs need source-of-truth (glossary alone is small; mining from real prod queries is harder); incorrect classes can collapse distinct entities.
**Expected lift**: +2-4pp on jira (after W1 lands; without W1 the lift is buried in B/P asymmetry noise).

## Tier 3 — supporting metric, not lift-producing (3/3 keep but rank-low)

### **W5: Partitioned recall@10 metric (S-A2)**

Bench output reports `R@10_indexed` (over GT pairs in index) AND `R@10_drift` (drift tax) separately, plus `hit@10_indexed` vs `hit@10_drift_weighted`. Lifts INTERPRETATION not numbers, but unblocks rational priority calls — currently every recall change is debated as "is this drift or ranking?".

**Why kept**: 3/3 keep. P adopts; S round-1 origin; R Ad4.
**Failure mode**: partitioned metrics don't help if eval set itself is noisy (IR4 GT noise = lockfile/generated paths). R's deferred A3 (GT pipeline) would be the next step.
**Expected lift**: 0pp on bench. +∞ on debate-team velocity.

## Disagreements — must resolve with evidence before shipping

### **D1: P-A2 (drop `_query_wants_docs` penalty gate + set boosts to 1.0)**

P keeps at rank 2: "<10 lines edited; targets 116 doc-top-1 misses (29× over-rep); A/B before ship".
R DROPS: "boosts were tuned for v2 docs eval-v3; ships a known regression on a different customer; same class as 17 rejected iterations".
S DEFERS: "structurally subsumed by W1 if W1 normalizes B+P unified".

**Resolution evidence needed**: A/B test on **BOTH** evals.
1. Apply P-A2 changes in local hybrid.py (zero `gotchas_boost / reference_boost / dictionary_boost`; force `apply_penalties=True` always).
2. E2E bench v2 calibrated docs (n=161): if hit@10 < baseline 0.6087 by ≥1pp → R is right, DROP P-A2.
3. E2E bench jira (n=908): if hit@10 > baseline 0.4163 by ≥3pp → P is right, KEEP P-A2 conditional on v2 not regressing.
4. If both POSITIVE → ship P-A2 alongside W1.
5. If v2 regresses but jira gains → SUBSUME under W1's unified normalization (R is right).

**Cost to resolve**: ~70 min e2e bench × 2 evals + bootstrap CI. NO code change beyond local hybrid.py edit. Reversible.

### **D2: P-A3 (re-index backoffice-web with relaxed extract filters)**

P keeps at rank 5: "Recall ceiling unlock (R@10 capped at 40% by drift), not floor fix. Bundle with A1+A2."
R DROPS: "single-purpose if framed alone; lifts only jira ceiling".
S DROPS: "long-payback".

**Resolution evidence needed**: not blocking. Can defer until W1+W2 land and we measure remaining ceiling. If post-W1 hit@10 is stuck at <55% → re-index becomes the path. **Cost**: ~30-90 min rebuild + bench, plus risk of frontend chunk noise on other-repo searches.

### **D3: User's grep+synonym proposal — implementation form**

All three teammates AGREE proposal is partially viable; they DISAGREE on form:
- P: glossary YAML extension (data, not framework) → W2
- S: equivalence-class column at index time → W4
- R: subsumed under W1 stage-1 as one of N recall sources

**Resolution**: implement BOTH W2 (data) and W4 (index column). They're complementary — W2 gives fast iteration on synonym pairs (edit YAML, restart daemon); W4 gives bounded query expansion at search time. Neither alone is a framework rewrite. **Reject**: any "Query understanding pipeline" object form (R conceded round 2).

## Sequencing recommendation

| order | approach | scope | expected lift (cumulative jira hit@10) |
|---|---|---|---|
| 1 | **IR2 sanitize fix** (already landed; bench in progress) | 5 LOC fts.py | ~46-49% (from 41.6%; +4-8pp) |
| 2 | **W2: bench expand_query parity + glossary 50 new entries** | 1-line bench fix + YAML | ~50-54% (+4-5pp) |
| 3 | **W1 downscoped: B/P unified normalization** (R round 2 form, P keeps as ADOPT) | ~50 LOC hybrid.py | ~54-58% (+4pp) |
| 4 | **W4: equivalence-class index column** | schema migration + chunker change | ~57-62% (+3-4pp) |
| 5 | **W5: partitioned metric** | bench output instrumentation | 0pp (gates priority calls) |
| 6 | **W3: typed FTS5Query value** | type wrapper + lint | 0pp (regression-safe) |
| 7 | **W1 full SLO**: stage-1 pool building separated; SLO `P(GT ∈ pool@500) ≥ 0.95` | ~500 LOC across 3 files | +1-2pp; primary value = unblocks future iterations |
| 8 | **D2 re-index backoffice-web** (only if 1-7 stuck below 55%) | full rebuild | +3-4pp on R@10 ceiling |

Bundled 1+2+3+4+5+6 plausibly reaches **57-62% hit@10** on jira (vs baseline 41.6%). Full 1-7 reaches **58-64%**. Not guaranteed ≥60% target on a single change; the floor unlock is structural.

## Watchlist — regression markers post-ship

1. **Post-W1 (B/P unified)**: monitor v2 docs hit@10 / R@10. If regression >1pp → revert + retune in unified space; do NOT re-introduce asymmetric B/P.
2. **Post-W2 (glossary growth)**: monitor `latency_p95_ms` — glossary expansion increases query length; FTS5 cost is O(token count). Alert if p95 >7s.
3. **Post-W4 (equivalence-class index)**: monitor cross-domain leak. If queries about `ach` start surfacing `automated clearing house` chunks even when query is code-intent → tune equiv class scope by file_type.
4. **Post-W5 (partitioned metric)**: if `R@10_indexed` ≥ 70% but `hit@10_indexed` stuck at <60% → ranking is the residual issue, not coverage. Move to Stage-2 reranker work.
5. **General**: jira eval set is noisy (IR4 — lockfile/generated/Dockerfile/etc.). After W1-W6 land, build a "semantic_target" filter for GT (only rank vs files that a search query could plausibly target) — separate scope (R's deferred A3).

## On user's original question

> "Could we apply Claude Code's grep approach with synonyms?"

**Yes — partially.** All three teammates agree some form of synonym/expansion is correct. The PROVEN form is:
- W2 (glossary YAML, ship-now) for fast iteration on payment-domain pairs
- W4 (equivalence-class index column) for bounded, type-safe expansion at search time

The REJECTED form is:
- A "Query understanding pipeline" framework (P vetoed; R conceded round 2)

The fix path is BOTH the synonym mechanism (W2+W4) AND the structural contract that uses it (W1 — without W1, synonym lift gets buried in B/P asymmetry noise; this matches the prior debug debate's finding that ranking failures dominate the floor).

## Convergence criteria check

- [x] Round 1, 2, 3 files exist (round1-{P,S,R}.md, round2-{P,S,R}.md, this converged.md)
- [x] Round 3 picks ONE primary approach (W1) + supporting tier 2 (W2-W4) + tier 3 (W5-W6)
- [x] Genuine disagreements listed explicitly (D1, D2, D3) with required evidence
- [x] Watchlist exists
- [x] No forced convergence — D1 unresolved without A/B; flagged for user

**Debate complete.** Primary winner: W1 (two-stage retrieval contract, downscoped P-form for first ship). Sequencing 1-7 above. User's grep+synonym proposal: viable as W2+W4 (data + index column), rejected as W1-A1 (object framework).
