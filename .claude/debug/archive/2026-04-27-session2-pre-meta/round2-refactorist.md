---
role: refactorist
round: 2
prior: optimize for the second/third change — value generic mechanisms over single-purpose tweaks
date: 2026-04-27
---

# Round 2 — Refactorist

## On Pragmatist (P)

**Summary in my words (≤3 sentences):**
P proposes (1) wire `expand_query` into the bench harness AND grow the
glossary by 30-50 entries mined from jira-miss tokens, (2) drop the
`apply_penalties` gate and zero out gotchas/reference/dictionary boosts in
conventions.yaml, (3) re-index `backoffice-web` with relaxed extractor
filters. P estimates +2-5 pp from A1 alone, +3-5 pp from A2, ≤+10 pp from
A3, and explicitly vetoes any "5000-line pipeline stage" — which by
implication includes my A1 (query understanding pipeline). The ship-this-
Monday position is YAML+bench-wire only; A2 needs an A/B against v2 docs
eval; A3 won't ship alone.

**Strongest point:**
P's A1 — the bench/prod parity diagnosis. `bench_routing_e2e.py:107` calls
`hybrid_search(q, ...)` raw while `service.py:68` calls `expand_query(q)`
first. **Every recall number this debate has discussed is computed against
a pipeline production never serves.** That is not a tuning concern; it
invalidates the unit of measurement before any approach runs. Fixing it
is a one-liner, costs zero, and from my prior's perspective it's also
the cheapest way to enforce the parity contract that A1-refactorist
A1 builds at 200 lines. P got there with one line of YAML and one line of
Python. I should have seen that and didn't.

**Weakest point:**
P's A2 — "force `apply_penalties=True` always + zero out doc-promotion
boosts". Two problems. (1) The boosts were *tuned for doc-intent eval-v3*
(see `project_loop_2026_04_25.md`); zeroing them ships a known regression
to a different customer (v2 docs eval) and the proposal acknowledges the
A/B requirement but treats it as deferrable. (2) More importantly, this
is exactly the *class* of fix the loop_2026_04_25 lessons say not to do
again — pure boost tuning was the entire content of the 17 rejected
iterations. P frames this as "10 lines edited, no new code paths" — a
pragmatist virtue — but the lines edited are the same lines that have
been edited and reverted three times. The simplicity is real; the
durability is not. From my prior's perspective: if a fix gets reverted on
the second customer, the third change is a fresh debate, not a refinement.

---

## On Systematist (S)

**Summary in my words (≤3 sentences):**
S frames the floor as three broken contracts (C1 query↔FTS5, C2 GT↔index,
C3 query-token↔index-token) and proposes (A1) a typed `FTS5Query` value
that cannot raise `OperationalError`, (A2) a partitioned recall metric
that reports `R@10[GT ∩ index]` separately from `drift_loss[GT \ index]`,
and (A3) an **index-time equivalence-class** built from the glossary so
that `index(q) = index(expand(q))` and class-id tokens are queried directly.
The ranking is by which invariant is tightest (A1 = 28.4 % violation rate,
trivially fixable; A2 = denominator integrity, blocks rational
prioritization; A3 = generalizes A1 and unblocks the user's grep
proposal). S's verdict on the user's proposal: grep "always finds" because
its corpus and query share a tokenization — re-establishing that here means
moving synonyms to index time, not search time.

**Strongest point:**
S's A3 — **moving synonyms to index time** as an equivalence-class column.
This is materially different from my A1 (search-time query understanding)
and from the user's proposal (search-time fuzzy expansion). At index time
the contract is symmetric: every chunk that mentions `webhook` carries
`#class:webhook`, every query token `webhook` rewrites to the same class
id, and `chunks MATCH '#class:webhook'` cannot fail and cannot miss
synonym-aware corpus members. The C3 contract becomes structurally true,
not best-effort. From my prior, this is the most generic mechanism on the
table — every future synonym addition (LLM-mined, telemetry-mined,
manually curated) plugs into a single indexing primitive without changing
search code. P's "grow the glossary YAML" extends only the *data*; S's A3
extends the *contract* the data lives under.

**Weakest point:**
S's A2 (partitioned metric) — invariant is correct but the *value of
having it* is overstated. Reporting `R@10[GT ∩ index]` separately from
`drift_loss` lets the team allocate effort rationally between indexing
and ranking, but it doesn't lift the headline number. The brief asks for
hit@10 ≥ 60 %; A2 reports the same 41.6 % more honestly. Worth doing,
but as a metric upgrade it sequences AFTER one of the actual lift
mechanisms lands. S ranks it #2; I would rank it #3 in a "what closes the
floor" ordering. (S's frame — "rank by tightest invariant" — is an
internally consistent position, just not the one the brief is graded on.)

---

## Defense and concession on P's "5000-line framework" veto

**Concession (partial):**
P is right that what I sketched in round 1 — `Query = {raw, expansions,
typed_tokens, sub_queries, intent, expected_corpus}` — reads like a
framework, not a fix. The typed-tokens dict alone is four sub-fields
each requiring extractors, validators, and tests. From the receiving end
(another engineer reads round 1) it's a pull request that touches
`hybrid.py`, `service.py`, `fts.py`, `code_facts.py`, `bench_*.py`,
plus a new `query.py` module — and ships none of them done. That is
the framework smell P names.

**Defense:**
But P's veto conflates two different things. The *typed-object form* of
my A1 is overbuilt for a first iteration; the *contract* it enforces is
not. P's own A1 (wire `expand_query` into bench) IS the parity-contract
fix from my A1 — they just shipped it as a one-liner instead of an
object. P's framing makes it a single bug fix; my framing makes it a
durable invariant that bench/prod must enter through the same function.
Both ship the same Monday change. The **only** difference is whether
`expand_query` later grows fields (intent, sub_queries) or stays a
glossary lookup. If the glossary is enough, P wins; if telemetry mining
shows multi-aspect titles ("Settlement Fixes — Days, refresh, options")
need decomposition, the typed object earns its keep on the third
change.

**Resolution:**
I retract the typed-object form as a round-1 deliverable. I keep the
*claim* that query rewriting is one stage, not three (`expand_query` in
fts.py, `_query_wants_docs` in hybrid.py, regex-tokenization in
code_facts.py — see hybrid.py:512, hybrid.py:540, hybrid.py:712,
hybrid.py:845). Those FOUR call sites of `_query_wants_docs` alone tell
me the abstraction is leaking; P's veto says don't fix it now, my prior
says fix it the *third* time someone wants to add a query field. Defer
A1-typed-object until that third occasion arises; ship A1 as one-liner
parity fix concurrent with P.

---

## Is S's A3 a substitute or a complement to my A1?

**Verdict: substitute for the search-time half; complement to the
routing/decomposition half.**

S's A3 (index-time equivalence class) handles the synonym-and-glossary
function of my A1's `expansions` field structurally instead of
procedurally. Once the corpus carries `#class:ach` tokens, you don't need
a search-time expansion stage to OR the glossary in — the chunk already
matches. So the synonym-graph payoff I claimed for A1 (improvement #1)
moves to A3. That is a substitute, and a strictly better one: search-
time expansion grows query length and pool dilution; index-time class
ids are bounded.

But A3 does NOT handle:
- intent classification (my A1 improvement #3): `_query_wants_docs`
  vs typed_tokens.provider/feature is a runtime decision over the
  query string, not the corpus.
- sub_query decomposition (my A1 improvement #2): "Settlement Fixes —
  Days, refresh, options" needs to fan out at SEARCH time into three
  sub-pools; index-time tagging cannot decompose what was never
  written as separate chunks.
- bench/prod parity (my A1 improvement #4): A3 changes the index, not
  the entry contract; bench can still skip the expansion stage.

So: A3 substitutes the largest single payoff of my A1, leaves the
other three intact, and does so by making the synonym contract
*structural* rather than *procedural* — which is more aligned with
my prior than my own A1 was. **I update.**

---

## Updated ranked list (KEEP / ADOPT / DROP)

### KEEP

**K1: Two-stage retrieval contract (round 1 A2)**
- still rank-1 in my list. Untouched by either P or S; addresses the
  recall/precision tangle that P's "drop the gate + zero the boosts"
  fix only patches at one site. A2 makes that fix STRUCTURAL — the
  boost/penalty asymmetry cannot exist as a class once both live in
  the precision layer.
- it is also the cleanest insertion point for grep-fallback as a
  recall source (user's proposal, unwrapped).

### ADOPT (from other side)

**Ad1: Bench/prod parity one-liner (P's A1, sub-mechanism)**
- replace my A1 typed-object form with P's one-liner: bench harness
  calls `expand_query` first. Costs zero, ships Monday, kills IR5 as
  a class of bug.
- glossary growth (P's data work-stream) — adopt as a continuous
  background activity, not a debate item. Every jira miss is a
  candidate term; mining is mechanical. Sequence after S's A3 lands
  so growth populates equivalence classes, not search-time
  expansions.

**Ad2: Index-time equivalence-class column (S's A3)**
- adopt as my new A1 (replacing the round-1 typed-object form).
  Better-aligned with refactorist prior than what I originally
  proposed; same payoff target, structural contract instead of
  procedural pipeline stage.
- specifically: chunk ingest tags `#class:<canonical>` tokens via
  glossary lookup; query path maps tokens → class ids and queries
  the class-id FTS column directly. Future synonym additions (LLM-
  mined, telemetry, manual) all plug into one indexing primitive.

**Ad3: Typed FTS5 query (S's A1)**
- adopt as the right form of the IR2 sanitize fix. The current fix
  at fts.py:79 (extend regex) is a patch; S's typed `FTS5Query`
  value is the contract that prevents the next character from
  re-introducing the bug six months from now. Cheap to layer on top
  of the patch.

**Ad4: Partitioned recall metric (S's A2)**
- adopt with caveat: useful, but rank below lift-producing changes.
  Land AFTER one of K1 / Ad2 ships so the partitioned metric scores
  a system that has actually changed. Otherwise it's a metric
  upgrade against an unchanged system and the same debate runs in
  six weeks.

### DROP

**D1: Round-1 A1 typed-object form (Query = {raw, expansions, ...})**
- conceded above. Overbuilt for first iteration; the same contract
  is established by Ad1 (parity one-liner) plus Ad2 (equivalence-
  class index) at a fraction of the surface area. The typed object
  earns its keep only on the third call site that wants a query
  field — defer until that happens.

**D2: P's A2 (zero out doc-promotion boosts globally)**
- reject under refactorist prior. Solves jira at known cost to v2
  docs (the eval the boosts were tuned for); is also the same class
  of fix as the 17 rejected iterations from loop_2026_04_25. K1
  (two-stage retrieval) makes the boost/penalty asymmetry
  structurally impossible without the regression risk on v2 docs.
  P's fix is the symptomatic version of K1.

**D3: P's A3 / round-1 A3 (index regen + GT pipeline)**
- partially overlapping; both lift only the recall ceiling
  interpretation, not the retrieval mechanism. Defer behind
  K1/Ad1/Ad2/Ad3. Index regen for `backoffice-web` should ride on
  Ad4 (partitioned metric) so it is gated on a measurable
  reachability gain rather than a hopeful one.

---

## Final ordering (refactorist round 2)

| # | item | source | first-change cost | future plugs |
|---|---|---|---|---|
| 1 | bench/prod parity one-liner + glossary growth | adopted from P A1 | 1 line + YAML | every future expansion provider |
| 2 | typed `FTS5Query` value | adopted from S A1 | one type wrapper, ~80 lines | every future FTS callsite |
| 3 | two-stage retrieval contract (recall vs precision separation) | round-1 A2 KEPT | architectural, ~300 lines | grep-fallback, learned routing, future bias signals |
| 4 | index-time equivalence-class column | adopted from S A3 | schema column + extractor pass | every future synonym/canonicalization |
| 5 | partitioned recall metric | adopted from S A2 | bench JSON schema | every future eval-set |
| 6 | round-1 A1 typed-object form | DROPPED | — | reactivate only when 3rd call site wants a query field |

Rationale for this order: 1+2 ship Monday and cost essentially nothing
(both are contracts encoded as small types/calls). 3 is the architectural
investment that kills the H3+H9+H4+H10+IR6 class. 4 substitutes the
largest payoff of my round-1 A1 with a structurally cleaner mechanism. 5
gates index regen on measurable reachability gain. 6 stays dropped until
demand materializes.

---

## Self-revision noted

I revise round 1 A1 from rank 1 to dropped (D1). The procedural typed-
object form was the wrong shape for first iteration; S's A3 (index-time
equivalence class) is the right shape for the same payoff target — and
it is more aligned with my own prior than what I originally proposed.
Round 1 A2 (two-stage retrieval) holds at rank 1 in lift-producing
items; round 1 A3 (GT pipeline) holds at low priority and rolls into
S's A2.

P deserves credit for naming the bench/prod parity one-liner that I
encoded as a 200-line stage. S deserves credit for finding the
structural form of synonym expansion that I missed by reaching for a
search-time pipeline first.
