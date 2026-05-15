---
role: refactorist
round: 1
prior: optimize for the second/third change — value generic mechanisms over single-purpose tweaks
date: 2026-04-27
---

# Round 1 — Refactorist

> Frame: jira recall floor (hit@10 41.6 %, R@10 7.05 %) is one of THREE recall regimes
> the system must serve concurrently:
>   (a) jira PR titles — short English, GT = code+lock files
>   (b) doc-intent eval-v3 — natural-language questions, GT = provider docs
>   (c) future user query distribution — unknown shape, ground truth absent
> Any approach that lifts (a) at the cost of (b) is a local optimum. Any approach
> that establishes a generic stage (query understanding, indexing, evaluation)
> that 3+ later improvements plug into is a high-value pattern even if first
> iteration impact is modest.
>
> Excluded by my prior:
> - hard-coding GOTCHAS_BOOST=1.0 / DOC_PENALTY=0.5: solves jira, regresses
>   doc-intent eval-v3 (was tuned for it). Single-customer fix. Single-purpose.
> - extending `_sanitize_fts_input` regex by another five chars: necessary but
>   already in flight (IR2 fix landed at fts.py:79). One-shot bug fix, not a
>   pattern. Counts as plumbing, not architecture.
> - re-indexing backoffice-web with relaxed filters: lifts R@10 ceiling for
>   jira but does nothing for the OTHER two regimes. Single-purpose if framed
>   alone.
>
> The three approaches below each build a generic pipeline stage. Each has at
> least two named follow-on improvements that plug into the same stage.

---

## A1: Query understanding pipeline (rewrite/expand/decompose) as a first-class stage

- rank: **1**

- pattern_extracted:
  Promote the *query* from a string into a structured object that flows through
  the pipeline:
  ```
  Query = {
    raw: str,
    expansions: List[str],           # glossary, synonym graph, learned acronyms
    typed_tokens: {                  # what part-of-token IS each surface form?
       provider: List[str],          # nuvei, plaid, trustly...
       feature:  List[str],          # webhook, refund, settlement...
       artifact: List[str],          # workflow, grpc, schema...
       code_id:  List[str],          # camelCase + UPPER_SNAKE
    },
    sub_queries: List[Query],        # decomposed for multi-aspect titles
    intent: Literal["docs","code","mixed","unknown"],
    expected_corpus: List[FileType], # not just one tower vote
  }
  ```
  Today we have a degenerate version split across three files (`expand_query`
  in `fts.py`, `_query_wants_docs` in `hybrid.py`, ad-hoc regex in `code_facts.py`).
  Promoting it to a single stage with a typed object makes every later
  improvement a *new field on that object* rather than a new branch in
  `hybrid_search`.

- 2nd-3rd-change-payoff:
  1. **Synonym graph plug-in** (the user's grep+synonym proposal lives here).
     Add `expansions` from glossary today; tomorrow add a learned synonym
     graph mined from PR titles ↔ files_changed (24.7 expected_paths/query
     gives free supervision for "title → real code identifier" pairs);
     next week add a learned-acronym extractor for new domain terms.
     All three are new providers behind the same `expansions` field.
  2. **Multi-aspect query decomposition.** Jira titles like "Settlement Fixes —
     Days, refresh, options" have ≥3 sub-aspects today competing as one bag-of-
     tokens. Once `sub_queries` is a list, retrieval can fan out and union
     candidate pools, fixing a recall regime that no boost or rerank can.
     Same field, same code path, plugs into reranker as an aggregation layer.
  3. **Routing signal** stops being a binary `_query_wants_docs` heuristic and
     becomes a function of `typed_tokens.provider/feature` strength → the
     existing 71.9 % "absence-heuristic" mis-routes (IR6) become a closed-form
     classifier rather than a regex.
  4. (Bonus) bench/production parity: production calls `expand_query` from
     `service.py:68`, bench bypasses it (IR5). Once the stage is *the* entry
     contract, both production and bench enter through it. IR5 dies as a
     class of bug.

- justification:
  hit@10 41.6 % is split across three orthogonal failure modes — sanitize bug,
  boost asymmetry, mis-routing — but every one of them is a SYMPTOM of the
  pipeline treating the query as an opaque string. Boost asymmetry dominates
  because the system can't tell a doc-intent query from a code-intent one;
  mis-routing dominates because tokens aren't typed; sanitize was silently
  swallowed because there was no contract that "the query string entering FTS
  is a typed thing". Build the pipeline stage and four future improvements
  (synonym graph, decomposition, learned routing, parity) plug in without
  another `_apply_X` branch in `hybrid_search`.

- failure mode:
  Over-engineering: if the typed object becomes a 12-field registry no one
  remembers to populate, queries silently lose information. Mitigation: drive
  it from a *measurement* — for each existing branch in `hybrid_search`
  currently inferring something from the raw query (and there are at least
  five), there is one field on the object. The object grows only when a
  branch that the bench can falsify needs a field. No speculative fields.

- expected lift on jira: +6 to +12 pp hit@10 over 2-3 changes
    (sub_queries decomposition catches multi-aspect titles ~ +4 pp;
     learned routing fixes IR6 71.9 % absence-heuristic mis-routes ~ +3 pp;
     synonym graph fixes "Refactor update merchant" type queries ~ +3 pp)
- expected lift on v2 docs eval: NEUTRAL to +2 pp
    (typed routing helps doc-intent classification too; expansion is
     additive in the OR-FTS query)
- expected lift on future eval: HIGH
    (every new query distribution that surprises us — say, German support
     queries, or deep multi-clause technical questions — adds expansion
     providers / decomposers / extractors without core pipeline change)

---

## A2: Two-stage retrieval (recall → precision) with explicit candidate-set guarantees

- rank: **2**

- pattern_extracted:
  Today retrieval is one stage with a 200-candidate RRF + rerank. A two-stage
  contract:
  ```
  Stage 1 — RECALL (high-pool, cheap):
    pool_size ≥ K=500
    must satisfy: P(GT ∈ pool) ≥ 0.95  (measurable, gateable)
    sources: FTS5 (≥200) + vector code-tower (≥150) + vector docs-tower (≥150) + grep-fallback (≥50)
    NO boosts, NO penalties, NO routing
  Stage 2 — PRECISION (small-pool, expensive):
    rerank top 200 from stage 1
    apply boosts/penalties HERE in normalized score space (not in RRF)
    apply routing as RE-RANKING SIGNAL (feature, not a gate)
  ```
  The current `hybrid.py` interleaves recall and precision concerns —
  GOTCHAS_BOOST=1.5 lives in raw RRF space (recall layer), DOC_PENALTY=0.15
  lives in normalized post-rerank space (precision layer), and they can't
  be reasoned about together (this is exactly H3+H9). Same for routing:
  `_query_wants_docs(query)` collapses the candidate pool at the recall
  layer (IR6) — once it has decided, the pool no longer contains code-tower
  hits, so no precision-layer fix can recover them.

- 2nd-3rd-change-payoff:
  1. **Boost/penalty unification.** Once both live in the same normalized
     score space at the precision layer, the H3+H9 asymmetry simply cannot
     exist as a class. Future authors adding a new file_type-specific bias
     (say, `setup_session` is recall-priority for trustly queries) get a
     single API: `precision_layer.add_signal(name, fn)`. They cannot
     accidentally multiply in RRF and skip in normalized.
  2. **The user's grep+synonym proposal slots in as a recall-stage source.**
     Claude Code's grep wins by being recall-greedy with synonym-and-fuzzy
     expansion. Adding it as a fourth recall source (alongside FTS, code-vec,
     docs-vec) is one stanza in `Stage 1` — and it raises the *recall pool*
     guarantee (currently ≤200 chunks, often ≤50 when FTS errors). It does
     NOT have to be the primary signal: the precision layer rerank still
     decides. So the proposal is salvaged without making grep the new arbiter.
  3. **Pool-coverage SLO becomes measurable.** Today we measure hit@10 — a
     rank-and-pool-coupled metric. With the contract, we measure
     P(GT ∈ pool@500) separately from rerank precision. We will know
     instantly which stage failed each miss bucket. Over the next year of
     pipeline changes, that is a debugging multiplier.
  4. **Two-tower routing becomes a feature, not a gate.** Currently 82.3 %
     of queries skip the code tower entirely (IR6). At the recall layer,
     BOTH towers always run; the routing signal becomes an input to the
     precision-layer aggregator (rerank with a "doc-intent prior").
     Worst case it does nothing extra; best case it stops dropping rank-2
     code hits because the docs heuristic decided to skip them.

- justification:
  Five of the eleven hypotheses (H3, H4, H9, H10, IR6) are different views
  of the same underlying confusion: recall-stage and precision-stage
  decisions are tangled in `hybrid_search`. A clean separation kills the
  CLASS of bug, not the instance. And it is the only architecture change
  among the three that lets us *prove* the recall floor (P(GT ∈ pool))
  separately from the rank floor — which means future losses in either
  half become diagnosable in one bench run instead of an 11-hypothesis
  debate.

- failure mode:
  Latency. Stage 1 with K=500 across three sources increases candidate
  count 2.5×; Stage 2 rerank stays at 200 so the dominant cost is
  unchanged, but the FTS5 side currently caps at limit=50/150 and pulling
  500 means more snippet generation. Mitigation: stage 1 returns rowid+score
  only, snippet is generated lazily for the top-200 reranked. Today's 4.7 s
  p50 is ALREADY dominated by reranker — bigger pool is +5 % not +50 %.

- expected lift on jira: +4 to +8 pp hit@10
    (driven by the code tower no longer being routed away on 82.3 % of
     queries, and grep-fallback recall-source covering FTS-fail residual)
- expected lift on v2 docs eval: NEUTRAL
    (same precision-layer rerank decides; routing becomes a feature
     instead of a gate, no first-order regression)
- expected lift on future eval: HIGH
    (any new ranker, any new candidate source, any new bias plugs into
     a contracted layer; probability of inventing the next H3+H9
     boost-asymmetry-class bug drops sharply)

---

## A3: Ground-truth pipeline as a system, not a snapshot

- rank: **3**

- pattern_extracted:
  Today ground truth is a JSONL frozen file (`jira_eval_n900.jsonl`) plus a
  snapshot of `expected_paths`. Promote it to a pipeline:
  ```
  GT pipeline:
    ingest:     PR title + files_changed + repos_changed (Jira/GH source-of-truth)
    annotate:   per-path label {semantic_target, mechanical_touch, generated, lockfile}
    project:    per-eval-set, derive a SCORED expected_paths
                  (semantic_target: weight 1.0
                   mechanical_touch: weight 0.1
                   generated/lock:   weight 0.0 — exclude from recall denom)
    snapshot:   versioned, model-agnostic, joinable to current index
    bench:      compute hit@10 / R@10 against scored GT, with sensitivity bands
  ```
  Today: package-lock.json is in 1141 expected_paths (47.6 % of queries) —
  per IR4. No retrieval system can learn the function "predict
  package-lock.json from any PR title with a Node.js dep change". Reporting
  R@10 = 7.05 % against this GT is mostly measuring GT noise, not retrieval
  quality. Same for `consts.js` (237), `src/generated/graphql.ts` (214),
  `.drone.yml` (105), etc.

- 2nd-3rd-change-payoff:
  1. **Eval-set as compositional product.** Once `annotate` is a stage,
     deriving a new eval set ("only semantic_target hits, n=908") or a
     stricter one ("require provider+feature in title", n=400) is a flag,
     not a re-collection. Loop in `project_loop_2026_04_25.md` already
     proved we need eval-v3 model-agnostic — same problem, same solution.
  2. **Sensitivity bands become reportable.** Currently a hit@10 number is
     a point. With weighted GT, we report (lower, point, upper) and the
     gap reveals whether a "regression" is signal or GT-noise. The
     `feedback_eval_gate_needs_holdout.md` lesson generalizes here.
  3. **Index drift becomes a tracked side-channel.** IR1 / H6 today is
     a one-off SELECT. With the projected GT, the gap between "all
     expected_paths" and "indexed expected_paths" is reported every
     bench run, and any rebuild that drops below threshold trips an
     alarm. Index health stops being a per-debate rediscovery.
  4. **Recall ceiling becomes interpretable.** With weights, the math
     ceiling for R@10 reflects what the system *can semantically learn*
     to retrieve, not the union of every file Git happened to touch.
     Today's 0.40 ceiling is a measurement artifact, not a capability
     limit.

- justification:
  Half of the recent debates trace back to eval-set quality
  (P5/P10/eval-v3/loop_2026_04_25). Building this as a pipeline once
  prevents N future debates from each rediscovering "the GT has noise" or
  "the GT has drift" or "the GT was tuned for the wrong distribution".
  This is the most boring of the three approaches and the one with the
  longest payback period — which is exactly the refactorist signature.

- failure mode:
  Doesn't lift jira hit@10 the day it lands. The number that moves is
  the *interpretation* of the number — which is correct but
  unsatisfying when the brief asks for ≥60 %. Could be sequenced AFTER
  A1 / A2 deliver the headline lift. But not deferring it indefinitely:
  every debate without it costs another full investigation.

- expected lift on jira: +2 to +4 pp hit@10 *as reported*, mostly because
    weighted GT removes lock/generated noise from the denom. Real retrieval
    quality unchanged. R@10 reported lift +5 to +15 pp by removing GT noise.
- expected lift on v2 docs eval: NEUTRAL
    (eval-v3 already has model-agnostic labeler; this generalizes the
     mechanism rather than displacing it)
- expected lift on future eval: HIGH
    (every future eval-set inherits the pipeline; we never collect raw
     `expected_paths` from Jira `files_changed` again as the canonical
     truth)

---

## On user's grep+synonym proposal

- pattern it enables:
  A *recall-greedy candidate source* with synonym/fuzzy expansion, which
  fits cleanly in two of my three approaches:
  - A1 stage: `expansions` field gets a "synonym graph" provider; query rewriting can
    invoke ripgrep-style fuzziness on the typed_tokens.code_id list.
  - A2 stage 1 (recall): a "grep+synonym" source joins FTS+vector as the
    fourth candidate stream.

  The mechanism Claude-Code grep uses is NOT mystical: it is (a) literal
  substring matching with no tokenizer (so "API:" matches "API:" verbatim
  unlike SQLite's unicode61), (b) regex fuzziness around camelCase/snake_case,
  and (c) the user — that is, a follow-up query — fixes mistakes. (a) and (b)
  we can replicate; (c) is exactly what A1's typed_tokens enables (the system
  acts as the "follow-up").

- viable under your prior?
  **Partial — yes when wrapped in A1/A2, no as a standalone replacement.**

  Standalone, it would solve jira (likely +5 to +8 pp hit@10 because grep
  with synonym recall covers the FTS5 sanitize residual, the IR6 routing
  collapse, AND multi-aspect query decomposition by accident — three
  symptoms with one tool). But it bypasses the reranker and ranking
  semantics completely; precision on doc-intent v2 eval would regress
  sharply (a synonym-expanded grep over 1537-chunk plaid-docs file
  matches everything). It is a single-customer fix in disguise.

  Wrapped in A2 as one of four recall sources, it adds candidates without
  bypassing ranking, and the precision layer decides. Wrapped in A1 as a
  query-rewriting provider, the typed_tokens drive *which* synonyms
  expand (don't fuzz a docs-intent query against code identifiers).

- expected lift:
  - on jira: standalone +5 to +8 pp hit@10; wrapped in A2 +3 to +6 pp on
    top of the A2 baseline (overlapping recovery with FTS-sanitize and
    grep, so additive but with diminishing returns)
  - on v2 docs eval: standalone -3 to -7 pp (precision regression);
    wrapped: NEUTRAL
  - on future eval (qualitative): standalone — high variance, depends
    on query distribution; wrapped — robust, the synonym graph itself
    is the asset that grows.

- recommendation:
  Adopt the *intent* (recall-greedy with synonym/fuzzy expansion) but
  reject the *form* (grep as new arbiter). The form belongs as one
  provider behind A1's expansions field and one source behind A2's
  Stage 1. Both placements compose with future improvements.

---

## Sequencing under refactorist prior

If forced to pick ONE for first execution: **A2 (two-stage retrieval)**.

Reasons:
1. It contains A1's `expansions` field as a recall-stage source insertion
   point; A1 can land later as a refinement to that source.
2. It is the only one that makes the recall floor (P(GT ∈ pool))
   *separately measurable* from rank failure — which is the diagnostic
   debt the entire H1-H11 + IR1-IR10 debate represents.
3. It immediately accommodates the user's grep+synonym proposal as a
   non-disruptive plug-in.
4. A3 (GT pipeline) lifts only the *interpretation* of numbers, not
   numbers, and can sequence after the architectural fix without
   blocking.

Order: **A2 → A1 → A3**, with grep+synonym proposal landing as a
recall-stage provider concurrent with A2.
