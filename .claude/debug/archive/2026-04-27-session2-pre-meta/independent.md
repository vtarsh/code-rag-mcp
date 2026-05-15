# Independent Investigation — jira recall floor

bench: `bench_runs/jira_e2e_wide_off_session2.json` (n=908)
- hit@10 = 41.63 %  recall@10 = 7.05 %  ndcg@10 = 0.138
- p50 latency 4667 ms (vs ~340 ms when router_bypassed=true)
- 530/908 (58.4 %) zero-hit; 56 % of those have correct repo in top-10

source read: `src/search/hybrid.py`, `src/search/fts.py`, `src/search/vector.py`,
`src/search/code_facts.py`, `src/search/env_vars.py`, `src/search/service.py`,
`scripts/bench_routing_e2e.py`, `profiles/pay-com/glossary.yaml`,
`profiles/pay-com/jira_eval_n900.jsonl`, `db/knowledge.db`.

---

## Pass 1 (independent)

### IR1: index coverage hard-caps recall@10 at ~0.40

- rank: **1**
- evidence: 13 001 / 22 459 (57.9 %) of GT (repo, file) pairs are NOT present in
  `chunks` table at all. Verified by enumerating distinct (repo_name, file_path)
  in `chunks` (29 682 pairs) and joining against eval. Oracle hit@10 ceiling =
  0.9053; oracle recall@10 ceiling = **0.3955**. Current 7.05 % reaches only
  18 % of the achievable ceiling, but the ceiling itself is 39.6 %, not 1.0.
  86/908 queries (9.47 %) have ZERO indexed expected paths — recall guaranteed 0.
  Missing extensions dominated by `.js` (4467), `.ts` (2628), `.json` (2506),
  `.tsx` (946); top-missing repo is `backoffice-web` (2740 missing GT files,
  yet only 1721 chunks indexed for that repo).
- mechanism: extractor / chunker filter or skip large generated files
  (package-lock.json, graphql.schema.json, src/generated/*.ts) AND
  selectively skip many real .ts/.tsx source files. The eval treats them as
  GT regardless. The recall metric thus measures coverage as much as ranking
  quality. No amount of reranker/router tuning closes a 60 % index gap.

### IR2: silent FTS5 sanitize failure on 28.4 % of queries

- rank: **2**
- evidence: replicated `sanitize_fts_query()` against all 908 queries vs
  `db/knowledge.db`. **258/908 (28.4 %)** raise `sqlite3.OperationalError`
  inside `fts_search()` and are swallowed by the bare `except` at
  `src/search/fts.py:198`, returning `[]` (empty FTS pool).
  Of those 258: **235 (91.1 %) end up zero-hit**. They are 235/530 = **44.3 %
  of all zero-hit queries**.
  Error categories (sample of 258):
    - colon `:` (FTS5 column qualifier): 85 — e.g. `Tech Debt: utils-s3 migrate ...`, `[API] Retry attempt counter`
    - bracket `[` / `]`: 78 — e.g. `[Webhooks] Extend customer block review`
    - backtick `` ` ``: 48 — e.g. `Add \`settlement_account\` Option ...`
    - slash `/`: ~25 — e.g. `Pagination/Scroll`, `applepay/googlepay`
    - comma `,`: 18 — e.g. `Settlement Fixes - Days, refresh, options`
    - apostrophe `'`: ~15 — e.g. `Cannot read properties ... (reading 'field')`
  `_sanitize_fts_input` only strips `*"()` and AND/OR/NOT/NEAR; everything
  else flows into the FTS5 string as-is.
- mechanism: when FTS pool is empty, the RRF fusion has only ≤50 vector
  candidates instead of the 150 + 50 = 200 candidate-pool the system was
  designed for. Reranker_pool_size=200 receives ≤50 chunks and the
  cross-encoder picks among predominantly doc chunks (provider_doc + docs
  dominate the embedding space because they are 56 % of all chunks).
  Empirical: top1 file_type for FTS-failing queries is docs/provider_doc/reference
  in **60.9 %** of cases vs **3.7 %** for FTS-working queries.

### IR3: corpus dominated by provider docs, GT dominated by code

- rank: **3**
- evidence: chunks-by-file_type breakdown:
  ```
  provider_doc 25 643   docs 16 599   reference 6 481   ===  48 723 (56 %)
  frontend     14 823   library 4 287  workflow 5 531   ===  ~33 000 (38 %)
  ```
  Top repos by chunk count are all *-docs* (nuvei-docs 5145, plaid-docs 2981,
  paypal-docs 1773, ppro-docs 1690, ecp-docs 1446, paysafe-docs 1154 …).
  Yet GT expected-paths by extension are dominated by `.js` / `.ts` /
  `.tsx` (>9000 paths combined, mostly code).
  Single doc files contribute 1500 + chunks each
  (`plaid-docs/llms-full.txt.md` = 1537 chunks; `checkout-docs/blog.md` = 506).
  These files alone fill the FTS top-150 pool when the query has any token
  overlapping with the doc body.
- mechanism: keyword pool capacity is 150 candidates; provider docs flood
  it because (a) huge md files have many candidate chunks, and (b) FTS5
  weighting (`KEYWORD_WEIGHT=2.0`) doesn't compensate for the fact that the
  same query token appears 100× in doc files vs once in code files. The
  reranker partially recovers but its pool is already biased.

### IR4: PR ground truth is path-noisy (lock + generated + boilerplate)

- rank: **4**
- evidence: top expected_paths by frequency across 22 459 GT entries:
  `package-lock.json` 1141, `package.json` 1025, `consts.js` 237,
  `src/generated/graphql.ts` 214, `src/generated.ts` 206, `graphql.schema.json`
  188, `service.proto` 170, `.drone.yml` 105, `scylla.cql` 95, `.gitignore`
  66, `Dockerfile` 53, `tests/index.spec.js` 53. **47.6 % of queries
  (432/908) have package-lock.json in expected_paths.** These are mechanical
  cross-cutting files PRs touch but no search query semantically targets.
  Mean expected_paths per query = 24.7; p95 = 90; max = 471. With limit=10,
  the math ceiling for recall@10 is `min(10, n)/n`; per-query average ceiling
  = 0.668 BEFORE indexing gaps.
- mechanism: ground-truth labels conflate "files touched in PR" with "files
  semantically retrievable from PR title". A reranker has no signal that
  predicts package-lock.json from "ACH and RTP payment method options".
  Every PR with a Node.js dep change is forced into recall@10 ≤ 10/(N+2)
  even when retrieval correctly finds every code file. Recall metric is
  bottlenecked by GT noise more than by retrieval quality.

### IR5: bench harness skips `expand_query()` (production drift)

- rank: **5**
- evidence: `scripts/bench_routing_e2e.py:107` calls
  `hybrid_search(q, limit=...)` directly. Production path
  `src/search/service.py:68` calls `expanded = expand_query(query)` BEFORE
  `hybrid_search(expanded, ...)`. Glossary at
  `profiles/pay-com/glossary.yaml` has 68 mappings (ach, apm, sca, kyc,
  webhook, settlement, ...). Phrase glossary adds another 48 patterns.
  Bench therefore measures a pipeline that production never serves: same
  hybrid_search, but fed the raw query.
- mechanism: queries like `ACH and RTP payment method options` should
  expand to `automated clearing house ... real time payments ... payment
  method type` in production but stay raw in bench. Real recall depends on
  glossary coverage; bench under-reports. Direction of error not
  symmetric — for queries lacking glossary keys (most BO-* / CORE-*
  English titles), expansion adds nothing and the bench is honest. For
  the subset where expansion matters (provider/payment-domain queries),
  bench is pessimistic.

### IR6: doc-intent router routes most jira-titles to docs tower

- rank: **6**
- evidence: jira titles like "Refactor update merchant", "Settlement Fixes",
  "Show all individuals" have NO code signature (no `\.(js|ts|py|go|proto)`,
  no UPPERCASE_IDENT, no camelCase) and NO repo-token (no `grpc-`/`workflow-`
  prefix). They fall through to the absence heuristic
  `_query_wants_docs:333` → `2 <= len(tokens) <= 15` → **TRUE**.
  hybrid:712 then routes vector leg to `model_key="docs"` (nomic tower)
  ONLY, skipping the code tower entirely. For PR queries whose GT is code,
  this misses every code-tower hit. Stratum gate at hybrid:847 may also
  skip the reranker on doc-intent queries — eval-v3 calibration was on
  doc-intent, not on jira PR titles, so transfer is unproven.
- mechanism: the V4 router was tuned for doc-intent eval-v3 (n=200,
  Opus-calibrated). Its absence-heuristic correctly captures
  "how is APM integrated" but mis-classifies short PR titles as doc-intent
  too. Vector leg then queries only the docs tower; FTS still has 150
  pool; but the merged vector result set is 50 docs-tower chunks. Plus
  reranker may be SKIPPED on these strata (`webhook`, `payout`, `method`
  etc. → `_DOC_RERANK_OFF_STRATA`), so candidate set is ranked by raw RRF
  alone. RRF on docs-only vector + (potentially empty) FTS = doc-heavy
  top10. Combined with IR2 (FTS empty), the top10 has no escape valve to
  the code corpus.

### IR7: latency p50 = 4.7 s suggests serial two-tower fanout + slow rerank

- rank: **7**
- evidence: `router_bypassed=false` runs at 4667 ms p50; `router_bypassed=true`
  same data at 35–340 ms. 13× slowdown. `hybrid_search` ambiguous-intent
  branch runs `vector_search` for code AND docs sequentially (lines
  723–727); each performs its own `provider.embed()` because the two
  towers are different models. Plus the cross-encoder rerank pool is 200
  candidates by default. Plus `_apply_code_facts` runs an additional
  `fts_search` per query and may inject more chunks. Plus `_expand_siblings`
  runs ≤2 extra DB queries on top results.
- mechanism: not a recall cause directly, but it indicates the production
  pipeline is doing more work than its outputs reflect. If recall is 7 %,
  spending 4.7 s per query is a *capability waste*, not the bug. Worth
  noting because it constrains ablation experiments — running 908 queries
  through the wide bench is ~70 minutes vs ~5 minutes router-bypassed.

### IR8: chunk content drift — same content indexed under two repo names

- rank: **8**
- evidence: top-most-chunked files appear in pairs:
  ```
  plaid-docs/docs/providers/plaid/llms-full.txt.md            1537 chunks
  provider-plaid-docs_llms-full.txt/docs/references/...        1537 chunks
  checkout-docs/.../blog.md                                     506 chunks
  provider-checkout-blog/docs/references/...                    506 chunks
  worldpay-docs/.../access_products_releases.md                 211 chunks
  provider-worldpay-access_products_releases/...                211 chunks
  ```
  Same content indexed twice with different (repo, file) keys. RRF treats
  them as independent candidates; both compete for the top-150 FTS pool.
- mechanism: candidate-pool dilution. Two near-identical doc chunks
  consume two slots, reducing the diversity of the pool by ~1 every time.
  With 80k+ chunks and 150 pool, the effect is marginal per query but
  systematic across the corpus.

### IR9: code_facts injection prefers FIRST chunk of file (header)

- rank: **9**
- evidence: `code_facts.py:fetch_chunks_for_files` issues
  `WHERE repo_name = ? AND file_path = ? ORDER BY rowid LIMIT 1` — picks
  the **first chunk** by rowid. For a multi-chunk file (avg 2.92 chunks),
  the actual matching code may be in chunks 2–5 yet the injected
  candidate is the file header. The reranker scores the header against
  the query, which on average will score lower than the actual matching
  body. Net effect: code_facts hits are systematically under-ranked
  vs FTS hits that point to the matching chunk.
- mechanism: minor recall leak. Pre-rerank position is decent (RRF score
  derived from `CODE_FACT_INJECT_WEIGHT=0.5`), but rerank uses snippet
  text — header content rarely contains the discriminative tokens.

### IR10: vector-search dedupe by rowid keeps WORSE-ranked code position

- rank: **10**
- evidence: `hybrid.py:730` ambiguous branch dedupes vector results by
  rowid keeping FIRST occurrence: `for vrow in list(code_results) +
  list(docs_results)`. Comment claims "code tower first → its ranking
  wins on collisions". But two-tower has DIFFERENT embedding spaces — a
  chunk hit by code tower at rank 30 might be hit by docs tower at rank 2.
  Keeping rank-30 code position throws away the rank-2 docs signal. RRF
  sees the worse position; the chunk is under-weighted.
- mechanism: minor. Affects only chunks that surface in BOTH towers
  (probably a small fraction). But systematic because direction is fixed.

---

## Pass 2 — Comparison

H produced H1-H11; E populated evidence-matrix.md.

### Mapping IR → H

| IR | rank | maps to H | match strength |
|---|---|---|---|
| IR1 (index coverage caps recall) | 1 | **H6** (index/eval drift) | exact match, both data-driven |
| IR2 (FTS5 silent sanitize fail 28.4 %) | 2 | **none — gap in H** | NOT covered by any H |
| IR3 (corpus dominated by provider docs) | 3 | partial overlap with **H3 / H9** (boost) but framed as candidate-pool dilution, not boost magnitude | partial |
| IR4 (PR-GT path noise: lock + generated) | 4 | overlaps **H1** (cardinality) but extends it: GT is also semantically un-retrievable | partial |
| IR5 (bench skips `expand_query`) | 5 | **none — gap in H** | NOT covered |
| IR6 (router routes to docs tower) | 6 | **H4** (two-tower mis-routing) | exact, but H4 measured 82.3 % routed; converges |
| IR7 (latency 4.7 s) | 7 | **none** | not a recall hypothesis but worth flagging |
| IR8 (chunk content drift — same content twice) | 8 | **none — gap in H** | NOT covered |
| IR9 (code_facts injects header chunk) | 9 | overlaps **H10** but H10 framed as cross-repo injection; mine is "wrong chunk per file" | partial |
| IR10 (vector dedupe keeps worse rank) | 10 | **none — gap in H** | NOT covered |

### Missed by H but present in I

1. **IR2 — FTS5 sanitize silently fails on 28.4 % of queries.** This is the single largest correctable cause I found. H5 looked at tokenization but concluded "0 % of misses have empty FTS"; my measurement on `db/knowledge.db` directly via `chunks MATCH` raises `OperationalError` for **258/908 (28.4 %)** of the eval queries because of `:` `[` `]` `` ` `` `,` `/` `'`. The FTS path returns `[]` (line 198 swallows). E's matrix says "0 % of misses have empty FTS" — that measurement counted *post-sanitize tokens* but didn't actually run the sanitized string through FTS5. **235/530 zero-hit queries are this bug** — 44.3 % of the floor. Top-1 file_type for FTS-failing queries is docs/provider_doc/reference 60.9 % vs 3.7 % when FTS works. Trivial fix: extend `re.sub(r'[*"()]', '', query)` to also drop `[]` `` ` `` `,` `/` `'` `:` (or per-token quote them).
2. **IR5 — bench skips `expand_query()`.** `bench_routing_e2e.py:107` calls `hybrid_search(q, ...)` directly; production calls `expand_query(q)` first. H/E never note this. Means the bench measures a pipeline that production never serves. Direction of error is asymmetric (bench under-reports for queries with glossary keys; honest for plain English titles). Worth confirming in synthesis.
3. **IR8 — duplicate-chunk repos.** `plaid-docs/.../llms-full.txt.md` and `provider-plaid-docs_llms-full.txt/.../...md` index the same content with 1537 chunks each. H/E never measure this. Pool dilution side-effect. Probably second-order vs IR1 + IR2 but worth a one-line audit in synthesis.
4. **IR9 — code_facts header bias.** `fetch_chunks_for_files` always picks first-rowid chunk per file. H10 only considers cross-repo dilution; misses that even when the file is correct, the chunk is wrong (and the reranker scores the *header*, not the matching body). Minor.
5. **IR10 — two-tower dedupe direction.** Not in H. Minor.

### Missed by I but found by H/E

1. **H1 cardinality math** — I noted ceiling but H/E quantified it more precisely (mean cap 0.40, median 0.33). Equivalent finding.
2. **H3/H9 boost vs penalty asymmetry** — H/E correctly identified that GOTCHAS=1.5 multiplier in raw RRF space competes with DOC_PENALTY=0.15 in normalized post-rerank space — structurally different scales, so penalty cannot offset boost. I undercounted this; my IR3 framed it as candidate-pool dilution. H3+H9 is sharper.
3. **H6 indexed-only re-eval** — E correctly notes 100 %-indexed queries STILL only hit 35 % — proving index drift bounds R@10 but does not explain the hit@10 floor. I missed this nuance — my IR1 implies fixing index drift would lift hit@10 by ~46 %, but E shows even fully-indexed queries miss. Strong falsifier of "index coverage is enough".
4. **H4 routing measurement** — E got concrete routing telemetry (82.3 % docs-tower, 71.9 % via absence-heuristic alone). I only argued mechanism; E had data.

### Disagreement on rank

- **My IR2 (FTS sanitize) = rank 2; H/E rank ≈ 0 (not present).** This is the largest ranking disagreement. If 235/530 zero-hit are FTS-fail, that is the *first* fix to land — strictly mechanical, no model retraining. Synthesis should call this out.
- **My IR1 (index coverage) = rank 1; H/E H6 = "DOMINANT BOUND" but ranked behind H3+H9 in the diagnostic top-3.** I agree with E that hit@10 is not driven by drift (E's nuance is correct). But I rank it higher because R@10 = 7.05 % is the headline metric in the bug brief, and drift caps it at ~40 %. Choice depends on which metric drives the synthesis decision.
- **H3+H9 boost asymmetry — H/E rank 1; my IR3 rank 3.** I underweighted this. After reading their analysis I would re-rank IR3 (boost-pre-rerank vs penalty-post-rerank in different scales) up to position 2 — tied with IR2. The empirical 22 % doc-top-1 + 29× over-rep on misses is a sharper signal than my "corpus dominated by provider docs" framing.
- **H4 routing — H/E rank 3; my IR6 rank 6.** I matched their concern but ranked it lower because the measured Δhit@10 is only 3.7pp. Agree with E's downgrading.

### Net synthesis input from I

If forced to combine: top-3 root causes after Pass 2 should be
1. **FTS5 sanitize bug (IR2 / not in H)** — 235/530 zero-hit, instant fix
2. **Boost-pre-rerank vs penalty-post-rerank asymmetry (H3 + H9 / IR3)** — 116/530 doc-top-1 misses
3. **Index-coverage cap on R@10 (H6 / IR1)** — explains why R@10 = 7.05 % even with perfect ranking

Index drift is the recall ceiling; FTS sanitize is the largest correctable hit floor; boost asymmetry is the largest correctable ranking floor.
