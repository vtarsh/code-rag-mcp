# Wide A2 OFF Revert Proposal — 2026-04-27 14:50 EEST

> **Status**: validated end-to-end via real `hybrid_search()` pipeline.
> Awaiting user confirmation to push.

## TL;DR

Revert `c3f7aab fix(rerank): narrow A2 OFF set to webhook+trustly only` on remote main. Restore wide A2 OFF set `{webhook, trustly, method, payout}` (= local main state).

**Trade-off**: BIG win on docs eval, small loss on jira eval. Weighted prod traffic = net **+2.86pp hit@10**.

## Side-by-side (real e2e bench, n=161 docs + n=908 jira)

| metric | REMOTE (narrow OFF, current prod) | LOCAL (wide OFF, this proposal) | Δ | bootstrap CI |
|---|---|---|---|---|
| **doc-intent eval n=161** | | | | |
| top-5 | 40.4% | **47.8%** | **+7.48pp** | [+3.11, +12.42] **POS** |
| top-10 | 54.0% | **60.9%** | **+6.85pp** | [+1.86, +11.80] **POS** |
| recall@10 | 23.2% | **27.2%** | **+3.93pp** | [+0.82, +6.98] **POS** |
| nDCG@10 | 27.5% | **34.2%** | **+6.68pp** | [+2.22, +12.10] **POS** |
| **jira eval n=908** | | | | |
| top-5 | **36.6%** | 35.8% | -0.77pp | [-1.43, -0.22] **NEG** |
| top-10 | **42.3%** | 41.6% | -0.66pp | [-1.21, -0.22] **NEG** |
| recall@10 | **7.22%** | 7.05% | -0.18pp | [-0.44, -0.02] **NEG** |
| nDCG@10 | **14.3%** | 13.8% | -0.41pp | [-0.78, -0.09] **NEG** |
| **weighted prod (47% doc / 53% jira)** | | | | |
| top-10 | 47.8% | **50.7%** | **+2.86pp net** | — |

## Per-stratum on doc eval (where the v2 win comes from)

| stratum | n | wide h@10 | narrow h@10 | Δ |
|---|---|---|---|---|
| method | 16 | 0.75 | 0.25 | **+50pp** ← biggest fix |
| interac | 9 | 0.78 | 0.56 | +22pp |
| provider | 20 | 0.50 | 0.40 | +10pp |
| aircash | 8 | 0.50 | 0.625 | -12.5pp (small loss) |
| webhook,nuvei,trustly,refund,payout,tail | unchanged | | | 0 |

## Why c3f7aab was wrong

`c3f7aab` (2026-04-25 late) narrowed OFF based on simulation predicting `method -15pp, payout -19pp under skip`. Real e2e bench through `hybrid_search()` shows the OPPOSITE direction — method gains +50pp under skip.

Root cause: the simulation that justified the narrowing used `benchmark_doc_intent.py` which has `router_bypassed: True` and uses pure VECTOR→RERANK pipeline. Production `hybrid_search()` uses FTS5+vector→RRF→reranker — different candidate pool, different rerank dynamics.

## Code diff

**`src/search/hybrid.py`** (already in local main, md5 c2e1b2a7):

```python
# OFF: add method, payout
_DOC_RERANK_OFF_STRATA = frozenset({"webhook", "trustly", "method", "payout"})
# KEEP: remove method, payout
_DOC_RERANK_KEEP_STRATA = frozenset({"nuvei", "aircash", "refund", "interac", "provider"})
# ORDER: OFF first (incl method, payout), then KEEP
_STRATUM_CHECK_ORDER = (
    "trustly", "webhook", "method", "payout",
    "nuvei", "aircash", "refund", "interac", "provider",
)
# Comment block updated to reflect real measured per-stratum deltas.
```

**`tests/test_rerank_skip.py`** (already in local main, md5 0f4a5728): assertions match wide OFF behavior — adds method/payout to OFF set test, removes them from KEEP set test.

## How to push (when confirmed)

```bash
# Via mcp__github__push_files with both files
files = [
  {"path": "src/search/hybrid.py", "content": <local md5 c2e1b2a7>},
  {"path": "tests/test_rerank_skip.py", "content": <local md5 0f4a5728>}
]
owner=vtarsh, repo=code-rag-mcp, branch=main
```

Post-push verify via `mcp__github__get_file_contents` + md5 compare.

## Risks / caveats

1. **jira NEGATIVE -0.66pp h@10** — small but bootstrap CI confirmed. Real regression on code-intent traffic. Mitigated by larger v2 win.
2. **2026-04-27 prod analysis used 47/53 weight**; if real prod is more code-heavy (e.g. 30/70), the win shrinks: 0.30×6.85 - 0.70×0.66 = +1.59pp net (still positive).
3. **No tail stratum** in bench eval — real prod has 50% 'tail' (no stratum tokens). For tail queries, behavior is unchanged (rerank runs in both wide and narrow configs). So real prod impact = strictly the strata-specific gains/losses, capped at the % of doc-intent + matched-stratum traffic.

## Bench artifacts (in `bench_runs/`)

- `v2_e2e_baseline_session2.json` — wide OFF (hit@10=0.6087)
- `v2_e2e_narrow_off_session2.json` — narrow OFF (hit@10=0.5404)
- `v2_e2e_method_only_off_session2.json` — method-only intermediate (hit@10=0.6087, R@10 -1.45pp)
- `jira_e2e_wide_off_session2.json` — wide OFF on jira (hit@10=0.4163)
- `jira_e2e_narrow_off_session2.json` — narrow OFF on jira (hit@10=0.4229)
- `jira_e2e_method_only_off_session2.json` — method-only on jira (hit@10=0.4174)

All bootstrap CIs in `loop2_log.md` Ticks 0-3.
