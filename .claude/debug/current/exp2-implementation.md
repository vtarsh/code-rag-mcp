# EXP2 / RE1 — Soft Repo Prefilter Implementation

**Status**: code complete, tests green, awaiting lead bench.
**Date**: 2026-04-27
**Author**: agent EXP2 (RE1 implementer)

## Goal

Soft repo prefilter — predict top-3 most-relevant repos for a query via FTS5
BM25 over per-repo summaries, then multiplicatively boost RRF score for chunks
from those repos by 1.4×. Targets the 85.4 % of jira queries that have ≥ 50 %
of GT in a single repo. Goal: clear 70 % hit@10 (baseline 63.97 % on
`jira_eval_clean.jsonl` after FTS5-fix; expected lift +6 to +12 pp).

## Files modified / added

| File | Lines | md5 (post-edit) | Change |
|------|------:|-----------------|--------|
| `src/search/hybrid.py`             | 1100 | `fa94c8fbc8e6d24ec9940dd1080563da` | imports + `_repo_prefilter` + `_apply_repo_prefilter` + wiring |
| `src/config.py`                    |  324 | `9cc1154f99503f9cbd8da1dcb1a01536` | `REPO_PREFILTER_BOOST`, `REPO_PREFILTER_TOP_K` |
| `scripts/build_repo_summary_index.py` |  189 | `2c374bb6a1d500e99d1103b3255866b2` | new — builds `repo_summaries` FTS5 table |
| `tests/test_repo_prefilter.py`     |  235 | `404e3bd4b8ffdcc651f1e41e4829968a` | new — 9 unit + integration tests |

Baseline `hybrid.py` md5 was `c2e1b2a7bcd7c849ac4f33069e8d45d7` (matches spec
"wide-OFF baseline state").

### `src/search/hybrid.py` — exact edits

* lines 16-19: added `import sqlite3` after `import re`.
* lines 32-33: imported `REPO_PREFILTER_BOOST`, `REPO_PREFILTER_TOP_K` from
  `src.config`.
* line 42: `from src.search.fts import fts_search, sanitize_fts_query` (added
  `sanitize_fts_query`).
* lines 651-700: new `_repo_prefilter(query, top_k)` function. Sanitizes
  query via `sanitize_fts_query`, runs `MATCH ? ORDER BY rank LIMIT ?` over
  `repo_summaries`, returns ≤ K repo names. Returns `[]` on (a) empty query,
  (b) missing table — with a single one-shot warning via the
  `_warned_missing_table` attribute, (c) any sqlite/other error — silent.
* lines 703-723: `_apply_repo_prefilter(scores, query)` — guard
  `if REPO_PREFILTER_BOOST <= 1.0: return` (kill switch); calls prefilter;
  multiplies `data["score"]` by `REPO_PREFILTER_BOOST` for chunks whose
  `repo_name` is in the predicted set; appends `"repo_prefilter"` to
  `data["sources"]` for observability.
* lines 880-886: new wiring inside `hybrid_search()` — runs
  `_apply_repo_prefilter(scores, query)` AFTER the FTS+vector RRF loop and
  AFTER `_apply_code_facts` / `_apply_env_vars`, BEFORE the rerank cut
  (`rerank_cap = max(limit*2, RERANK_POOL_SIZE)`). Matches the existing
  multiplicative boost pattern at the `code_facts` block (~line 600).

### `src/config.py` — exact edits

* lines 213-220: added `REPO_PREFILTER_BOOST` (default 1.4) and
  `REPO_PREFILTER_TOP_K` (default 3). Both honour the env overrides
  `CODE_RAG_REPO_PREFILTER_BOOST` / `CODE_RAG_REPO_PREFILTER_TOP_K` and the
  `tuning.repo_prefilter_boost` / `tuning.repo_prefilter_top_k` keys in
  `conventions.yaml`. Setting `=1.0` cleanly disables the behavior (the
  `_apply_repo_prefilter` guard short-circuits before any DB call).

## Design decisions

### 1. Storage = single FTS5 virtual table

`repo_summaries(repo_name, summary)` with `tokenize='porter unicode61'` to
match the main `chunks` table. One row per repo. BM25 ranking via the
default FTS5 `rank` column gives consistent semantics with the rest of the
pipeline. Alternatives considered:

* **A separate `BM25Okapi` over precomputed term frequencies (Python)** —
  rejected. Adds a TF-IDF dependency, requires its own re-build path, and
  is no faster than SQLite for a 1254-row table.
* **Augmenting the existing `chunks` table** with a new `is_summary`
  boolean — rejected. Pollutes the main search index and would require
  extra `WHERE is_summary=0/1` filters everywhere.

### 2. Source priority for summary content

Per spec: README → `file_type='reference'` → top-3 chunks by length. Real
coverage on the live `knowledge.db` (1254 repos):

| Source | Repos covered |
|--------|--------------:|
| README | 454 |
| reference | 454 |
| length fallback | 346 |
| **none** | **0** |

Each summary is **prefixed with the repo name** so a query like
`trustly callback` scores the `grpc-apm-trustly` repo on the name token even
when the README/refs don't repeat it. Truncated at 4000 chars per spec.

### 3. Soft boost vs hard filter

Multiplicative boost (×1.4), no penalty for non-prefilter chunks. Rationale:

* Multi-repo GT queries (the remaining 14.6 % of jira) keep working —
  chunks in non-top-3 repos still rank on their base RRF.
* Reranker still has final authority on ordering (boost happens **before**
  rerank cut, in RRF-score space — same pattern as `CODE_FACT_BOOST`).
* `boost = 1.0` (env override) is a clean kill-switch with zero
  side-effects (the guard short-circuits before any DB call).

### 4. Graceful degradation when the table is missing

`_repo_prefilter` returns `[]` when `repo_summaries` is absent and logs a
single warning per process (`_warned_missing_table` flag). This means the
production daemon can be deployed before `build_repo_summary_index.py` has
been run, and the search behaves identically to the wide-OFF baseline.

### 5. FTS5 query sanitization reuse

`sanitize_fts_query` from `src.search.fts` is reused, so the same
`OperationalError`-prevention contract that just shipped in `fts.py` (the
"FTS5 sanitize fix" mentioned in the spec) automatically applies to the
prefilter — no duplicated regex.

### 6. Performance

Live measurement (1254-repo `repo_summaries`):

| Query | Latency |
|-------|--------:|
| `trustly callback signature` | 0.7 ms |
| `nuvei refund webhook`       | 0.3 ms |
| `paynearme initialize sale`  | 0.2 ms |
| `apple pay sdk integration`  | 0.7 ms |
| `graphql resolver merchants` | 0.4 ms |

p99 ≪ 50 ms budget. The table is tiny (≤ 4 KB × 1254 rows ≈ 5 MB), the
`MATCH` is a single-column FTS5 lookup, and `ORDER BY rank LIMIT 3` runs in
constant time.

### 7. Build is offline / re-runnable

`build_repo_summary_index.py` does `DROP TABLE` + recreate, populates from
`chunks`, runs FTS5 `optimize`. 110 s wall-clock for the full 1254-repo
index. Should be added to `make build` / nightly incremental jobs (lead
discretion — out of scope for EXP2).

## Test coverage (`tests/test_repo_prefilter.py`)

9 tests, all pass. Each test uses a fresh `:memory:` SQLite DB so no live-
DB coupling.

| Test | Asserts |
|------|---------|
| `test_top_repo_for_clear_query` | `_repo_prefilter("trustly callback signature")` returns `grpc-apm-trustly` first |
| `test_empty_query_returns_empty` | `""` and `"   "` both return `[]` |
| `test_no_match_returns_empty` | `"xyzqq notarealtoken"` returns `[]` |
| `test_missing_table_returns_empty` | DB without `repo_summaries` returns `[]` (no exception) |
| `test_top_k_respected` | `top_k=2` yields ≤ 2 results |
| `test_boost_applied_to_prefilter_repo_chunks` | `_apply_repo_prefilter` multiplies scores by 1.4 only for prefilter repos; sets `"repo_prefilter"` in `sources` |
| `test_disabled_when_boost_is_one` | `REPO_PREFILTER_BOOST=1.0` is a no-op |
| `test_prefilter_repo_chunk_outranks_unrelated` | end-to-end through `hybrid_search` — boost flips order on adjacent FTS hits |
| `test_disabled_when_table_missing` | `hybrid_search` works unchanged when `repo_summaries` is absent |

### Full pytest run

```
1032 passed in 54.57s
```

No regressions from baseline (was 1023 before EXP2; +9 from new file).

## Tradeoffs / risks

1. **Top-3 isn't always perfect on docs-heavy queries.** Smoke check showed
   `nuvei refund webhook` returns `pr-review-learnings` first (a generic
   doc repo), not `grpc-apm-nuvei`. The boost is soft (×1.4), so this only
   pushes generic docs up; it doesn't suppress true GT chunks. Should
   surface in bench as a small win, but worth watching for doc-intent
   strata regression.
2. **Repo summary quality varies by source.** README-backed summaries are
   richest (454 repos). Reference and length-fallback repos may bias the
   prefilter on token overlap with refs. Mitigation: prefix with repo name,
   so the name itself contributes a strong signal.
3. **Build-time cost = 110 s** for 1254 repos. Acceptable for nightly /
   on-demand builds; not for per-query computation. Already gated through
   the script.
4. **No incremental rebuild path.** `build_repo_summary_index.py` is full
   rebuild each run — fine because it's fast (110 s) and only needs to run
   when `chunks` changes meaningfully. If lead wants nightly auto-refresh,
   add it to `scripts/full_update.sh` after `build_index.py`.
5. **Score blow-up risk.** If a chunk is already boosted by `code_facts`
   (×1.15) and `env_vars` (×1.05) and now `repo_prefilter` (×1.4), peak
   multiplier ≈ 1.69×. RRF base scores are tiny (1/61 .. 1/210) so this
   stays well within the rerank pool's relative ranking — no overflow risk
   and no order inversion vs `CODE_FACT_BOOST` pattern that already ships.

## What lead should run

1. **Build the index** (one-time, before benches):
   ```
   CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com \
     python3 scripts/build_repo_summary_index.py
   ```
2. **Daemon restart** (per project CLAUDE.md gotcha):
   ```
   kill -9 $(lsof -ti:8742); sleep 2
   CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 daemon.py &disown
   ```
3. **Bench**:
   * `jira_eval_clean.jsonl` (n = 619)
   * `profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl` (n = 161)
   * Bootstrap CI vs FTS5-fix baseline (`bench_runs/jira_clean_fts5fix_session2.json`)
4. **Kill-switch verification** if bench regresses anywhere:
   ```
   CODE_RAG_REPO_PREFILTER_BOOST=1.0 python3 daemon.py
   ```
   restores byte-for-byte baseline ordering.

## Constraints honored

* No push to remote (lead handles).
* No bench runs.
* No edits to `glossary` / `service.py` / `fts.py`.
* Reversible via env (`CODE_RAG_REPO_PREFILTER_BOOST=1.0`).
* Safety net: missing `repo_summaries` table → silent fallback + warning.
* Stayed under the 6 h budget.
