# Round 2 — Pragmatist critique of S and R

Prior reminder: ship simplest thing that works; reject framework rewrites; ask
"could this be 50 lines instead of 5000?".

---

## On S (systematist)

**Summary in my words (≤3 sentences):**
S frames the floor as three broken type contracts (C1 FTS5 validity, C2 eval
reachability, C3 query-token ↔ index-token equivalence). A1 wraps the FTS query
in a typed sum-type so `OperationalError` becomes structurally impossible; A2
partitions recall@k by reachability so drift loss is reportable separately from
ranking loss; A3 moves equivalence classes to **index time** (sidecar FTS column
with `#class:ach` synthetic tokens). All three are about making invariants
observable, not about lifting numbers per se.

**Strongest point:** **A2 (partitioned recall metric).** Cheapest possible
change with the highest payback ratio under my prior. It is literally an edit to
`eval_finetune.py` + `bench_routing_e2e.py` — emit `recall@10_indexed` alongside
the raw value. Maybe 30 lines. After it lands, every future "+1pp" claim has
to specify which bucket, ending the multi-month re-debate about whether a
regression is real or GT-noise. This is exactly the data-extension over
abstraction trade I want.

**Weakest point:** **A3 (equivalence-class index, sidecar FTS column with
synthetic class-id tokens).** This requires a schema change to `chunks`, a
re-build of the corpus to inject `#class:ach` tokens, weight tuning so class-id
matches sit "strictly below" literal matches in BM25 (a magic constant in
disguise), and ongoing curation of the glossary as an indexing primitive. It's
exactly the kind of generic mechanism I distrust: estimated lift is "+2-4pp,
contingent on A1+A2 done first" — same as my A1 (glossary growth) which is a
YAML edit. The complexity multiplier is at least 50× for the same expected lift.
A3 is a reasonable design IF we ever observe a glossary-driven recall ceiling,
but we have not.

---

## On R (refactorist)

**Summary in my words (≤3 sentences):**
R sees the floor as one symptom of treating queries as opaque strings, and
proposes three generic stages: A1 a typed Query object with `expansions /
typed_tokens / sub_queries / intent / expected_corpus` flowing through the
pipeline; A2 a two-stage recall→precision split with explicit `P(GT ∈ pool)`
SLO and boost/penalty unification at the precision layer; A3 a GT pipeline that
ingests Jira→annotates per-path semantic vs mechanical→projects scored
expected_paths. R wants every future improvement to plug in as a new field /
provider / source rather than another `_apply_X` branch in `hybrid_search`.

**Strongest point:** **A2 stage-2 boost/penalty unification.** R correctly
identifies that H3+H9 is structural — boost lives in raw RRF pre-rerank, penalty
lives in normalized [0,1] post-rerank, the scales are not commensurable. Moving
both to the same normalized score space at the precision layer kills the CLASS
of bug. The minimum-viable form of this is ~50 lines: drop the
`apply_penalties` gate, normalize boosts before they apply (divide by max RRF
in pool), apply both as additive logits in the same pre-rerank pass. That I'd
ship. The full "two-stage retrieval with K=500 pool guarantee" is over-built
but the core insight is correct.

**Weakest point:** **A1 (Query object as a 5-field typed pipeline stage with
sub_queries, typed_tokens, expansions, intent, expected_corpus).** This is the
canonical pragmatist veto. It's a registry-of-fields whose first iteration
delivers "+6 to +12pp over 2-3 changes" — i.e. each individual field's lift is
unmeasured, and the +12pp is conditional on landing 4 sub-projects (synonym
graph, decomposition, learned routing, parity). The current code does the same
work in 3 functions across 2 files (~80 lines total: `expand_query`,
`_query_wants_docs`, code-id regex in `code_facts`). R's mitigation ("the
object grows only when a branch the bench can falsify needs a field") is
correct in theory and ignored in practice — frameworks always grow.

---

## Updated ranked list (post-Round-2)

| order | approach | source | KEEP / ADOPT / DROP | reason |
|---|---|---|---|---|
| 1 | Wire `expand_query()` into `bench_routing_e2e.py` + grow glossary 30-50 entries from jira misses | P-A1 | **KEEP** | YAML + one-line patch; hardest evidence under my prior |
| 2 | Drop `_query_wants_docs` penalty gate AND set boosts to 1.0 in conventions.yaml | P-A2 | **KEEP** | <10 lines; targets confirmed H3+H9 |
| 3 | Partition recall@10 into `recall@10_indexed` + drift component | S-A2 | **ADOPT** | ~30 lines methodology fix; ends rediscovery debates |
| 4 | Boost/penalty unification at single normalization step (minimum-viable form, NOT full 2-stage redesign) | R-A2 (core) | **ADOPT (downscoped)** | Adopts insight, rejects K=500 pool + SLO scaffolding |
| 5 | Re-index `backoffice-web` with relaxed extract filters | P-A3 | **KEEP** | Index-coverage fix; bundle with above |
| 6 | Typed `FTS5Query` sum-type at the FTS boundary | S-A1 | **DROP** | The IR2 sanitize fix already landed; per-token quoting at sanitizer call-site is the 5-line version of S's invariant. The full type wrapper is the 500-line version with no incremental lift. |
| 7 | Equivalence-class index (sidecar FTS column) | S-A3 | **DROP** | Schema change + rebuild for "+2-4pp contingent on A1+A2 first". Reject under prior. |
| 8 | Query Understanding pipeline (typed Query object) | R-A1 | **DROP** | 5-field registry for +6-12pp distributed over 4 sub-projects. Reject under prior. |
| 9 | Two-stage retrieval with K=500 pool + SLO | R-A2 (full) | **DROP** | Useful insight (B/P unification) extracted as #4 above; the rest is ceremony. |
| 10 | GT pipeline as system | R-A3 | **DEFER** | R themselves admit "doesn't lift jira hit@10 the day it lands". Worth doing eventually but not on critical path. |

---

## Revisions to my Round 1

1. **I underweighted methodology.** S's A2 (partitioned recall metric) is
   strictly better than my Round-1 ranking implied. It is mechanically simpler
   than my A2 (boost/penalty fix) AND it unblocks rational decision-making on
   what to fix next. Promote it to position 3 globally (after my A1+A2). I
   should have proposed this; I didn't.
2. **I underweighted the boost/penalty asymmetry being structurally fixable.**
   R's "B/P at the same normalization step" reframes my A2 as a class-of-bug
   fix rather than a one-off override. Same lines of code, broader payoff. I
   adopt R's framing of the change while keeping my downscoped scope.
3. **My estimate of "A1+A2+A3 reaches 50-55%" stands.** Neither S nor R provides
   a credible single-change path to ≥60%. R's headline +6-12pp is conditional
   on landing 4 sub-projects in Approach 1 alone — that's a quarter of work,
   not a debate outcome.

---

## SPECIFIC critique of R's "query understanding pipeline" (per task ask)

R's failure-mode argument: "Over-engineering: if the typed object becomes a
12-field registry no one remembers to populate, queries silently lose
information. Mitigation: drive it from a measurement — for each existing
branch in `hybrid_search` currently inferring something from the raw query
(and there are at least five), there is one field on the object."

**Does this argument hold against my pragmatist VETO? No, for three reasons:**

1. **The mitigation is a promise, not a constraint.** "The object grows only
   when a branch that the bench can falsify needs a field" sounds disciplined,
   but Python has no nominal types and no constructor that enforces "every
   field has a falsifying bench". The mitigation will be enforced by *culture*,
   which decays by the second contributor. Six months in, someone adds a
   `learned_priors` field "just in case the rerank loop wants it", populates it
   from a heuristic, and now we have a hidden coupling no one sees. I have
   watched this exact pattern happen to `SearchResult` in this very codebase —
   it gained `chunk_type`, `snippet`, `combined_score`, `routing_provenance`
   over a year, and only `chunk_type` and `combined_score` see actual reads.
2. **The headline lift is unattributable.** R claims "+6 to +12pp over 2-3
   changes" but the changes are: synonym graph (+3pp), decomposition (+4pp),
   learned routing (+3pp). Each of those can be done WITHOUT the typed Query
   object. The synonym graph is a glossary edit (my A1). Decomposition is a
   sub-query loop in `hybrid_search`. Learned routing replaces
   `_query_wants_docs` with a classifier — same call-site. The Query object is
   a refactor under each of those, not a precondition. So R is double-counting:
   either we credit the lift to the individual changes (in which case the
   typed object is unjustified scaffolding), or we credit it to the typed
   object (in which case the individual changes are not separable units). I
   reject both bookings.
3. **The maintenance cost is one-way.** Once `Query` is a typed object passed
   through `hybrid_search`, every future search-path change has to update the
   constructor, the dataclass, the `bench_routing_e2e` synthetic-Query
   builders, the production `service.py` builders, and probably 3-4 test
   fixtures. This is the cost a refactorist accepts in exchange for the
   "second/third change" payoff — but the payoff is, as I argued in (2), the
   individual lifts which would have been done either way.

**Specific veto:** I reject R-A1 in its proposed form. I would accept the
`expansions` *field* (i.e. the user's grep+synonym proposal) IF it lands as
either (a) a YAML glossary growth that `expand_query()` already consumes, or
(b) a single new function `expand_query_with_synonyms(q) → str` that returns a
plain string into the existing pipeline. Both are 50 lines, both ship Monday,
both deliver +2-5pp.

---

## The user's grep+synonym proposal — final position

S frames it as A3 (equivalence-class index): index-time class injection. R
frames it as A1.expansions field or A2 stage-1 source. I frame it as
glossary growth + bench-harness wire-up.

The pragmatist arithmetic: glossary grows in YAML (cost = 0 LOC); harness wire
is 1 LOC; that's the entire project. Anything richer (sidecar FTS column,
typed Query field, four-source recall stage) is a >100× LOC multiplier for the
same expected lift. SHIP the YAML + 1-LOC wire. DEFER the structural form
unless we measure the YAML version capping out at <60%.
