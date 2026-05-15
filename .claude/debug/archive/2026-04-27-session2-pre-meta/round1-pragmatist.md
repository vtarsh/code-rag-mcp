# Round 1 — Pragmatist

Top 3 strategic approaches for jira recall lift (target hit@10 ≥60% from 41.6%).

Prior anchors: ship simplest thing that works; reject framework rewrites; if it
can be a glossary edit + a 30-line patch, that beats a new pipeline stage.

## A1: Wire `expand_query()` into bench harness AND grow `glossary.yaml` from jira misses

- rank: 1
- justification: production already calls `expand_query()` on `service.py:68`, but
  `scripts/bench_routing_e2e.py:107` calls `hybrid_search(q, ...)` raw — IR5 gap.
  Bench measures a pipeline production never serves. **One-line fix** to wire
  expansion in (`q = expand_query(q)`) plus a data work-stream: take the 530 zero-hit
  jira queries, mine the most frequent unmapped tokens (`Settlement`, `Refactor`,
  `Backoffice`, repo-prefixed PR-titles), and add 30-50 entries to
  `profiles/pay-com/glossary.yaml`. Glossary is currently 68 lines — even a 2× growth
  is a YAML edit, not a new pipeline stage. This is the user's grep+synonyms idea
  done as a pure data extension, not a query-rewriting framework.
- failure mode: glossary-key coverage on jira misses is 6.8% vs 7.9% on hits
  (measured) — most PR titles use English nouns ("Refactor merchant settlement",
  "Show all individuals") with no payment-domain abbreviation. So harness-fix alone
  yields ≤3pp; the lift must come from the YAML growth, which depends on whether
  PR-title tokens have learnable synonyms. If the bottleneck is actually
  *paths* (filenames, repo names) and PR titles never name them, adding
  English→English synonyms doesn't help and we cap at ~50%.

## A2: Force `apply_penalties=True` always + zero out doc-promotion boosts in conventions.yaml

- rank: 2
- justification: H3+H9 are confirmed-with-data: doc files dominate top-1 on 35.8% of
  misses vs 3.4% of hits (29× over-rep), and the `apply_penalties = not
  _query_wants_docs(query)` gate at `hybrid.py:540` skips penalty on 82.3% of jira
  queries. Penalties are subtract-in-[0,1]-post-rerank; boosts (gotchas=1.5,
  reference=1.3, dictionary=1.4) are multiply-in-raw-RRF-pre-rerank. The scales are
  not commensurable — penalty cannot offset boost regardless of magnitude. Two
  changes: (a) drop the gate (one line), (b) set the three boost multipliers to 1.0
  in YAML. Total < 10 lines edited, no new code paths.
- failure mode: 116 doc-top-1 misses is the **observed** delta but not all of them
  will flip to a code GT in top-10 — the chunk pool may simply not contain the GT
  (overlaps with H6 drift). Realistic lift +3-5pp, not +10pp. May regress doc-intent
  eval-v3 (where the gate was tuned) — needs A/B against `bench_runs/v2_e2e_*.json`
  before promoting. Not a shipping risk if we keep the YAML override per-profile.

## A3: Re-index `backoffice-web` (and other code-light repos) by relaxing extractor filters

- rank: 3
- justification: H6 confirms 13,001/22,459 (57.9%) of GT (repo, file) pairs are NOT
  in `chunks`; oracle ceiling for hit@10 is 90.5% (capped by 86 unhittable queries),
  but observed 41.6% sits at 46% of ceiling — drift is the **R@10 ceiling** at ~40%
  even with perfect ranking. `backoffice-web` is the largest offender (2,740 missing
  GT files, only 1,721 indexed). The work is operational: re-run the extractor with
  `.tsx`/`.ts`/`.js` filters relaxed (skip generated/lock files only). No new code,
  no new pipeline stage — extend the skip-pattern list in the existing extractor.
- failure mode: indexable subset (queries with ≥1 GT in chunks) hit@10 is already
  46% — even doubling the indexed share gives at most ~10pp lift on hit@10, not the
  ~30pp needed to clear 60%. Plus relaxed filters add ~2× chunks across many repos,
  which dilutes the FTS pool of 150 — could regress non-jira benches. Would not ship
  alone; it's the recall ceiling unlock, not the floor fix. Bundle with A1+A2.

## On user's grep+synonym proposal

- viable as-is? **partial** — Claude Code's "grep always finds" effectiveness comes
  from THREE things, only one of which is synonyms: (1) full-text scan of an
  uncompressed working tree, (2) regex/case fuzziness, (3) iterative refinement
  ("if grep1 misses, try grep2"). Our FTS5 already does (2)-equivalent
  (sanitize + OR fusion + camelCase split via `.`-tokens). The synonyms piece is
  exactly what `expand_query()` + `glossary.yaml` already implement. Token-fuzziness
  is what FTS5's `unicode61` tokenizer gives us. So "Adapt + filter clean" reduces
  to "grow the glossary".
- simplest implementation: **glossary YAML extension** + bench-harness one-liner.
  No new module. If we want regex-fallback ("if FTS pool < N, retry with prefix-OR
  on each token"), that's another ~30 lines in `fts.py` — still inline, not a
  framework. Anything more ambitious (LLM-based query rewriting, learned synonym
  expansion, BM25-on-paths layer) is a 5000-line pipeline stage and a pragmatist
  veto.
- expected lift estimate: **+2-5pp** if we just wire `expand_query` into bench AND
  grow glossary by 30-50 entries derived from jira-miss tokens. Not a hit@10 ≥60%
  fix on its own.
- why I'd / wouldn't ship this: I'd ship the **YAML+bench-wire** version Monday —
  zero risk, easy revert, real production parity gain. I would NOT build a
  query-rewriting framework or LLM-based expansion layer; the math doesn't justify
  the engineering cost when 7% of misses even contain a glossary-mappable token.
