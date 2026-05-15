## Frame

The recall floor is not a tuning bug. It is **three broken invariants** at three boundaries (query→index, index→eval, eval→bench). Every approach below is justified ONLY by which invariant it makes statically representable. Reject anything that just tweaks weights.

The pipeline today violates these contracts:

1. **C1 query↔FTS5**: not every query string `q` produced by `expand_query` is a syntactically valid FTS5 expression. 28.4% raise `OperationalError` and the bare `except` (`fts.py:209`) silently degrades to vector-only. The contract `sanitize_fts_query: str → ValidFTS5` is unenforced — return type is `str`, but the type carries no parse-validity guarantee.
2. **C2 GT↔index**: not every `(repo, file)` in `expected_paths` is reachable from `chunks`. 57.9% are unreachable; 9.5% of queries are mathematically unhittable. The eval treats reachability as if it held, so the metric conflates "ranking failure" with "indexing failure".
3. **C3 query-token↔index-token**: not every domain term in a query has a guaranteed path to its index-side equivalents. `expand_query` is best-effort (68 mappings), no failure mode is typed; bench skips it entirely (`bench_routing_e2e.py:107`), so the bench-pipeline ≠ prod-pipeline. Two equivalence classes ("ACH" vs "automated clearing house" vs `ach_processor.go`) collide only by accident.

Everything that has been tried (model A/Bs, pool sweeps, boost tuning, stratum gating) lives downstream of these violations. They cannot lift the floor because the floor is set by the broken contracts, not by ranking quality.

---

## A1: Typed FTS5 query — make `OperationalError` unrepresentable

- rank: **1**
- invariant_established: every value of type `FTS5Query` round-trips through `sqlite3.execute("MATCH ?", q)` without raising.
- justification: The pipeline currently lies about its own contract. `sanitize_fts_query` returns `str`; the bare `except sqlite3.OperationalError: return []` (fts.py:209) treats every parse error as "no results" indistinguishably from a successful zero-row query. Until the function returns an *FTS5Query* sum-type (or panics on unsanitizable input) the bug is structurally invisible — IR2's discovery only happened because someone executed every query manually. Replace the str return with a typed wrapper whose constructor either (a) per-token quotes everything (`'"' + tok.replace('"', '""') + '"'`, joined with OR) so any byte sequence is legal, or (b) parses through `sqlite3_prepare_v2` at construction time and raises a typed `MalformedQuery`. Add an assert in `fts_search` that `chunks MATCH q` cannot raise; downgrade the bare except to `pass` only on `OperationalError` whose message is not `"syntax error"` / `"no such column"`. Sanitizer fixed at the call-site is a patch; the invariant is what stops the next character (`{`, `}`, `~`, `^`) from re-introducing the bug six months from now.
- failure mode: per-token quoting suppresses FTS5's own column-prefix `path:foo` features that internal callers may rely on; need to audit `code_facts_search` and any other `chunks MATCH` callsite and either preserve their structured queries (separate constructor) or accept the loss. If the type is bypassed (raw str passed to `MATCH`), no compile-time check catches it — Python has no nominal types — so this is a test-and-runtime invariant, not a compile-time one. Mitigated by a single `assert isinstance(q, FTS5Query)` at the FTS boundary plus a smoke that fires every query through the sanitizer.

## A2: Eval contract — partition recall metric by reachability

- rank: **2**
- invariant_established: every reported recall@k is conditioned on an explicit reachability set, i.e. `R@10 = R@10[GT ∩ index] + drift_loss[GT \ index]` and the two components are reported separately.
- justification: H6 already proved indexed-fraction is 42.1% and 86 queries are unhittable. The current bench reports a single number that conflates "we ranked badly" with "we never had a chance" — every fix landed in the past month was scored against the wrong denominator. The fix is purely methodological: change `eval_finetune.py` and `bench_routing_e2e.py` to emit `recall@10_indexed` (denom = `min(|GT ∩ index|, k)`) alongside the raw value, and refuse to print a single-number headline. This makes the broken contract observable at the metric type, so every future "+1pp" claim has to specify which bucket. With this invariant in place, the team can decide rationally whether to grow the index (extractor relaxation in `backoffice-web`) or improve ranking — without it, every approach competes on a metric that mixes the two losses. The goal-of-≥60% hit@10 may already be reachable by indexing alone (oracle ceiling 90.5%); unknown without partitioning.
- failure mode: re-indexing `backoffice-web` `.tsx`/`.ts` files (the dominant missing extension per IR1: 2740 missing GT files in this repo alone) may add high-noise chunks that hurt code-intent ranking on *other* queries — the partitioned metric reveals this honestly but doesn't prevent a regression. Also: PR-title GT noise (IR4 — 47.6% of queries have `package-lock.json` as a "gold" path) sits *below* the reachability invariant; cleaning it up is a separate eval-construction contract (filter mechanical-only paths from GT before scoring). Without that follow-up, even a perfect indexed-recall metric is bounded by `min(10, |GT|) / |GT|` ≈ 0.668.

## A3: Equivalence-class index — every query token has typed path to its synonyms

- rank: **3**
- invariant_established: for every query token `t`, the index contains `expand(t)` (a closed equivalence class) under the SAME indexing path that `t` would hit. I.e. `index(q) = index(expand(q))` for every `q` the user could type.
- justification: This is the structural form of the user's grep+synonym proposal. Today, query expansion is **asymmetric**: `expand_query` runs at search time (when it runs at all — bench skips it, prod runs it), but the corpus is indexed with raw tokens. So "ACH" finds chunks that already say "ACH" or chunks that match the expansion `automated clearing house`, but only if the chunk literally contains the expanded phrase. Domain glossary collisions (`apm` → `alternative payment method`) only succeed when the doc happens to contain the long form. Establishing the invariant requires moving the equivalence class to **index time**: at chunk ingest, every term hit by the glossary is tagged with its canonical class id (`#class:ach`), stored in a sidecar FTS column or as a synthetic token. Query path then maps `q` → set-of-class-ids → exact match on the sidecar. Result: a typed channel ("class-id index") that guarantees retrieval whenever the glossary covers the term, independent of whether the chunk contains the long form. This is what the user means by "Claude Code grep always finds": grep doesn't fuzzy — it succeeds because the corpus and the query share a tokenization. Re-establishing that contract here means making the synonym graph an indexing primitive, not a search-time hack. Expected effect on the 235/530 zero-hit FTS-fail queries that have domain abbreviations (`SCA`, `KYC`, `ACH`, `APM`, …): these become deterministically retrievable because the class-id token can never raise OperationalError and never miss its synonyms.
- failure mode: equivalence classes are noisy. `webhook` glossary expands to `callback notification async DMN express-webhooks workflow-provider-webhooks` — over-expansion will explode candidate-pool dilution (IR3) on long-form queries that mention multiple anchors. Mitigation: weight class-id matches strictly below literal matches in BM25 / RRF, and gate class injection on token-class-confidence (only inject when `expand_query` returns a one-token expansion, not a multi-token bag). Also: the glossary is hand-curated (68 entries) — coverage decay invalidates the invariant silently. Type the glossary itself (`Glossary = dict[Token, EquivalenceClass]` with a coverage assertion against jira-eval token frequency) so additions/removals are visible.

---

## On user's grep+synonym proposal

- which invariant does it establish? *Partial* — it gestures at C3 (query-token ↔ index-token equivalence) but leaves the contract informal ("expand and filter"). It does NOT establish C1 (FTS validity) or C2 (eval reachability), which together account for **≥58% of the floor** (235/530 zero-hit are FTS5-fail, 86/908 are unhittable). Synonym/expansion alone cannot pull these out of zero-hit because for 28.4% of queries the FTS leg literally never runs.
- viable under your prior? **partial**. The "always finds what's needed" framing is misleading — Claude Code grep succeeds because (a) the corpus is the user's working tree and reachability is by construction 100%, and (b) regex matches on raw bytes, no tokenizer abstraction layer to drop chars. Both of those are absent here: index drift breaks reachability (C2), and FTS5's tokenizer + parser is a leaky abstraction (C1). Synonym graph alone, without C1+C2 fixed, will land on the same broken contracts and lift hit@10 by less than the FTS5 sanitize+typed-query fix already does.
- expected lift: **+2–4pp hit@10 in isolation**, contingent on C1+C2 done first; **+0pp incremental** if landed AFTER A1+A2 because the glossary already runs at search time in prod and the expansion footprint on jira-PR-titles is small (most are domain-light English: "Refactor update merchant", "Settlement Fixes - Days, refresh, options"). The proposal's structural form (A3 above) is worth pursuing only AFTER A1+A2 establish a measurable baseline — otherwise the lift is invisible against the noise of the broken denominators.

---

## Summary

The floor is structural. Three broken contracts → three approaches. Rank by which invariant is **tightest** (smallest scope, largest measured violation):

1. **A1** (typed FTS5) — 28.4% of queries currently violate; instant verifiable; touches one file
2. **A2** (partitioned eval metric) — every past benchmark depends on this; unblocks rational priority on ranking-vs-indexing fixes
3. **A3** (equivalence-class index) — generalizes A1 and unblocks the user's grep proposal; biggest scope, requires schema change

Anything else (boost tuning, model swaps, stratum re-gates) competes downstream of contracts that aren't enforced. Fix the contracts; the floor lifts as a consequence.
