# Round 2 — Systematist

## On Pragmatist (P)

**Summary in my words.** P proposes three cheap, mostly-data fixes: (1) wire `expand_query()` into the bench harness and grow `glossary.yaml` by 30–50 jira-mined entries; (2) drop the doc-intent gate and zero out three boost multipliers in YAML; (3) re-index `backoffice-web` with relaxed extractor filters. P treats the user's grep+synonym proposal as already implemented in `expand_query` + `unicode61` tokenization, so the only delta is more YAML.

**Strongest point.** A2 (penalty-gate drop + boost zero-out). It targets the most data-anchored finding in the matrix (H3+H9: doc top-1 over-rep 35.8% vs 3.4%, 29× delta), is <10 lines, ships Monday, and is trivially revertible. Critically, the change is **observable at the metric**: re-running jira eval immediately tells you whether the boost-asymmetry was the dominant ranking floor or not — it's a falsifier as well as a fix. That's compatible with my prior because dropping the gate at `hybrid.py:540` *partially* establishes an invariant: "penalties apply unconditionally". Not full type-level, but mechanically equivalent for this one rule.

**Weakest point.** A1 (bench harness `expand_query` wire + glossary growth). P measures glossary-key coverage on jira misses at 6.8% — that's a self-confessed +0.5–3pp ceiling at most, regardless of how much YAML grows. P's own failure-mode paragraph says "we cap at ~50%". Selling it as rank-1 contradicts the math; it should sit behind A2. Worse, this approach **establishes the wrong invariant**: it makes the bench match production, but production is exactly the thing whose recall is 41.6% — bench-prod parity is necessary but not sufficient (you still have a broken pipeline, you just measure it more honestly). The work is real, the priority is wrong.

**Contracts P FAILS to establish.** P's "ship simple" approach establishes ZERO of my three contracts:
- **C1 (typed FTS5)**: P explicitly punts to "in-flight IR2 fix" without typing the return. P would land the regex extension and stop. The next character class (e.g. `~`, `^`, `{`, `}`, or a future FTS5 syntax) silently re-introduces the bug — there is no compile- or boundary-time check. `OperationalError` remains representable.
- **C2 (eval reachability partition)**: P does not separate `R@10[GT ∩ index]` from drift-loss; A3 ("re-index backoffice-web") *acts* on drift but does not partition the metric, so the team continues to compete on a conflated number. Every future "+1pp" claim is still ambiguous.
- **C3 (query-token ↔ index-token equivalence)**: P keeps `expand_query` as a search-time best-effort. The synonym graph stays asymmetric (search-time only); index-side equivalence classes are absent. Glossary growth without index-side pairing produces brittle gains: each new entry helps only if a chunk literally contains the long form.

P's gain is real but capped at instance-level fixes. The class of bug is preserved.

---

## On Refactorist (R)

**Summary in my words.** R proposes three pipeline-stage promotions: (A1) lift the query from a string to a structured object (`expansions`, `typed_tokens`, `sub_queries`, `intent`) so future improvements become "new fields" not "new branches"; (A2) split retrieval into a recall stage with measurable `P(GT ∈ pool@500) ≥ 0.95` and a precision stage where boosts/penalties live in one normalized space; (A3) promote ground truth from a JSONL snapshot to an annotate→project→snapshot pipeline that weights paths by `semantic_target` vs `mechanical_touch` vs `lockfile`. R recommends sequencing A2 → A1 → A3, treating grep+synonym as one provider behind A1/A2 — never as the arbiter.

**Strongest point.** A2 (two-stage recall→precision contract). This is the single approach in either P or R that **establishes a measurable invariant of the same shape as my C2**: `P(GT ∈ pool@K) ≥ 0.95` is exactly a typed reachability contract on the recall stage, and "boosts/penalties live in normalized score space at the precision layer" is a typed-commensurability contract that kills H3+H9's bug-class structurally. The two contracts together turn five entangled hypotheses (H3, H4, H9, H10, IR6) into separately falsifiable layers — which is precisely the diagnostic invariant my prior demands. R is solving the same disease I am, with a richer surface than my A1+A2 individually.

**Weakest point.** A1 (query understanding pipeline). The typed `Query` object with five fields (`expansions`, `typed_tokens`, `sub_queries`, `intent`, `expected_corpus`) is a registry waiting to drift. R's mitigation ("only add a field when a falsifiable branch needs it") is good discipline, but the proposed initial shape *already* has speculative fields (`sub_queries`, `expected_corpus`) without a pinned bench-falsifier. This is the half-finished-implementation failure mode — the object grows because someone "should populate provider for completeness" — and exactly the over-engineering my prior penalises. A1 also overlaps A2 (where `expansions` and `intent` belong as recall-stage feature inputs); landing A1 first creates a second source-of-truth for query state that A2 then has to absorb. R itself sequences A2 first, which is correct; A1 should be **deferred or deleted** until A2 surfaces a *measured* gap that needs typed_tokens to close.

**Contracts R establishes (vs. what I claimed in Round 1).**
- **R-A2 establishes my C2 directly**: `P(GT ∈ pool) ≥ 0.95` is the partitioned reachability invariant. Stronger than my A2 because it sets a numeric SLO, not just a partitioned report.
- **R-A2 also establishes a NEW invariant I missed**: "boost and penalty live in the same normalized score space" — this is a commensurability contract on the precision layer. It makes H3+H9-class bugs structurally unrepresentable. Round 1 me underweighted this because I framed boost asymmetry as a tuning issue (downstream of contracts); I now see the asymmetry IS a contract violation, just at a different boundary (recall-layer arithmetic vs precision-layer arithmetic). **I revise my Round 1 priors**: this should be elevated.
- **R-A2 partially establishes C3**: grep+synonym as a recall-stage source raises pool size without breaking ranking, but does not move the equivalence class to *index time* — so it's a weaker form of my A3, not a replacement.
- **R-A1 attempts C3** via `expansions` field but doesn't pin it to index-side equivalence either; same weakness as P.
- **R-A3 generalises C2 to a GT pipeline**, which is good (eval-set noise IR4 disappears as a class), but slow-payback and not load-bearing for the floor.

**Is R-A2 just refactoring or a real invariant?** Real invariant. `P(GT ∈ pool@K) ≥ 0.95` is a *gateable, measurable* contract — every bench run can assert it, every regression that drops below trips a typed alarm. Recall-vs-precision separation is not aesthetic: it makes the failure mode of every miss attributable to one of two layers (today they're tangled). The latency cost (P estimates +5%, dominated by reranker) does not violate any invariant; it's a non-functional concern that mitigation already addresses (rowid-only stage 1, lazy snippet). I now consider R-A2 **strictly stronger than my Round 1 A2 alone**, because it bundles the partitioning with the structural fix to H3+H9.

---

## Updated ranked list

| rank | approach | provenance | invariant_established |
|---|---|---|---|
| **1** | Two-stage retrieval (recall→precision contract) | **ADOPT-from-R** (R-A2) | C2 partitioned reachability + precision-layer commensurability (kills H3+H9-class) |
| **2** | Typed FTS5Query (make `OperationalError` unrepresentable) | KEEP my A1 | C1 query↔FTS5 parse-validity |
| **3** | Equivalence-class index (synonym graph at index time) | KEEP my A3 | C3 query-token↔index-token symmetry; salvages user's grep proposal as a typed channel |

**Sequencing**: 2 → 1 → 3.
- Land typed FTS5 first (one file, instant +5–15pp on 258 affected queries; restores the recall-stage source that R-A2's contract assumes works).
- Then land R-A2 two-stage with the SLO; the partitioned metric exposes which strata still bleed.
- Then A3 equivalence-class indexing addresses the residual where `expand_query` was masking C3 violations.

**Drop**: my Round 1 A2 (partitioned eval metric) as a standalone — fully subsumed by R-A2's `P(GT ∈ pool@K) ≥ 0.95` SLO.

**Drop**: P-A1 (bench wire + glossary growth) as a primary — keep as a one-line cleanup landed alongside the typed FTS5 fix. P's measured 6.8% miss-token coverage caps lift at <3pp; not load-bearing.

**Drop / defer**: R-A1 (typed Query object) — overlaps R-A2; defer until R-A2 surfaces a measured gap requiring typed_tokens. Avoids speculative fields.

**Defer**: R-A3 (GT pipeline) — establishes a long-payback meta-invariant but does not lift the floor. Sequence after #1–#3.

## Round-1 revision

I revise my Round 1 ranking. R-A2 is the stronger frame for what I called "C2 partitioned recall metric": the SLO + commensurability bundle is a tighter invariant than my "split the denominator in the report" version. R deserves rank 1 in synthesis. My A1 (typed FTS5) is unaffected — it's the smallest-scope, largest-violation contract and remains the right first ship. My A3 (equivalence-class index) is unaffected — neither P nor R proposes index-time synonym typing; it's still the only way to typify the user's grep+synonym intent.

Net: 2/3 of my Round 1 list survives; the changed rank reflects that R surfaced a tighter form of my C2 than I had drafted.
