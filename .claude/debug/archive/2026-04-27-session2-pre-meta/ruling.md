# RULING — jira recall floor (n=908) ACH debate, 2026-04-27

> Symptom: e2e through `hybrid_search()` yields hit@10=41.63% / R@10=7.05% on jira_eval_n900. WHY?

## TL;DR — three independent root causes (all unrefuted, all reproducible)

1. **IR2 — FTS5 sanitize silent `sqlite3.OperationalError` on 28.4% of queries** (TRIVIAL FIX). Independent investigator's discovery; not in H's hypothesis list, not refuted by D. Live-verified by team-lead.
2. **H3 + H9 — boost-pre-rerank vs penalty-post-rerank asymmetry**. Boosts (GOTCHAS=1.5, REFERENCE=1.3, DICTIONARY=1.4) multiply in raw RRF space pre-rerank. Penalties subtract in normalized [0,1] space post-rerank AND are SKIPPED on 82.3% of jira queries. Doc files dominate top-1 on 35.8% of misses (vs 3.4% of hits) — 10× over-rep.
3. **H6 (partial scope, ~10% of floor) — index drift**. 86/908 queries (9.5%) have ZERO indexed GT — mathematically unhittable. Indexable subset hit@10=46% (drift caps ceiling but doesn't drive 84% of the floor — those are RANKING failures).

Excluded by D's refutation (8/11): H1 (cardinality, OPPOSITE direction in data), H2 (token count, flat across buckets), H4 (mis-routing, only 3.7pp possible delta), H5 (FTS dilution, identical token counts hits vs misses), H7 (stratum gate, 4.6% queries × ≤0.7pp), H8 (pool depth, identical sweep results), H10 (code_facts injection, RRF arithmetic strictly below FTS), H11 (semantic gap — fix path exhausted via 17 prior model A/Bs).

## Diagnosticity matrix

| H | E1 GT-bucket | E2 token-bucket | E3 doc-top-1 | E4 routing | E5 token-keep | E6 indexed-frac | E7 gate-fire | E8 pool-sweep | E9 boost-arith | E10 inject-arith | E11 score-Δ | E12 FTS5-OpErr |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| H1 cardinality | **I** | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| H2 query-info | N/A | **I** | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| H3 doc-top-1 | C | C | **C** | C | N/A | C | N/A | N/A | C | I | C | C |
| H4 routing | N/A | N/A | C | **I** | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| H5 FTS-dilution | N/A | N/A | N/A | N/A | **I** | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| H6 drift | N/A | N/A | N/A | N/A | N/A | **C** (partial) | N/A | N/A | N/A | N/A | N/A | N/A |
| H7 gate | N/A | N/A | N/A | N/A | N/A | N/A | **I** | N/A | N/A | N/A | N/A | N/A |
| H8 pool-depth | N/A | N/A | N/A | N/A | N/A | N/A | N/A | **I** | N/A | N/A | N/A | N/A |
| H9 boosts | C | N/A | C | C | N/A | N/A | N/A | N/A | **C** | N/A | C | N/A |
| H10 code_facts | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | **I** | N/A | N/A |
| H11 semantic | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | **I** | N/A |
| **IR2 FTS-sanitize** | C | C | C | C | N/A | N/A | N/A | N/A | N/A | N/A | N/A | **C** |

C = consistent with hypothesis. I = inconsistent (refutes). N/A = not diagnostic.

**Most diagnostic evidence pieces** (rows differ most across hypotheses):
- E3 (doc-top-1 over-rep) — separates H3+H9+IR2 from refuted alternatives
- E12 (FTS5 OperationalError) — sole diagnostic for IR2
- E6 (indexed-fraction) — sole diagnostic for H6
- E8/E9/E10 — sole diagnostics for individual mechanism claims (mostly refuting)

## Sensitivity check on survivors

| H | depends solely on | robust? | failure mode if signal wrong |
|---|---|---|---|
| **IR2** | live `chunks MATCH ?` raises OperationalError on 258/908 | ROBUST — 3 independent measurements (I's 4 sample run; team-lead's full 908-query reproducer; categorical signature of error tokens `: , [ ] ` ' / ` matches sanitize regex source) | If knowledge.db schema differs in prod (single-column FTS5 instead of multi-column), `:` would not be column-qualifier — no error. Mitigated: local matches prod schema (verified `db/knowledge.db` is the same SQLite file production reads). |
| **H3 + H9** | boost direction × penalty bypass × top-1 file_type freq | ROBUST — three independent claims: code-review of hybrid.py:540/550-552/817-824 (penalty bypass on doc-intent), arithmetic (boosted rank-30 > unboosted rank-5), bench measurement (35.8% doc top-1 in misses vs 3.4% in hits). Removing any ONE leaves the other two as standalone evidence. | If `_query_wants_docs(query)` returns True only for queries where docs ARE the target (good classification), then doc top-1 is correct — but evidence-collector measured 88% of doc-top-1 misses have NO doc in GT, refuting this. |
| **H6 (partial)** | chunks table content vs eval expected_paths | ROBUST — direct SELECT count comparison (29,682 distinct (repo, path) in chunks vs 22,459 GT pairs, 9,458 overlap = 42.1%). | If chunks table is incomplete due to a recent rebuild interruption, drift number could be transient. Mitigated: builds are idempotent; current chunks count matches `make build` log baseline. |

All three survivors are ROBUST. Move to reproducer construction.

## Reproducer construction (STEP 6)

### IR2 reproducer (live-confirmed)

```bash
CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 -c "
import json, sqlite3, sys
sys.path.insert(0, '.')
from src.search.fts import sanitize_fts_query
queries = [json.loads(l)['query'] for l in open('profiles/pay-com/jira_eval_n900.jsonl')]
conn = sqlite3.connect('db/knowledge.db')
errs = 0
for q in queries:
    s = sanitize_fts_query(q)
    if not s: continue
    try:
        conn.execute('SELECT COUNT(*) FROM chunks WHERE chunks MATCH ?', (s,)).fetchone()
    except sqlite3.OperationalError:
        errs += 1
print(f'{errs}/{len(queries)} = {100*errs/len(queries):.1f}% raise OperationalError')
"
```

**Expected output**: `258/908 = 28.4%`. **Verified team-lead 2026-04-27 14:50 EEST**: 258/908 = 28.4% errors confirmed. Sample errors:
- `Settlement Fixes - Days, refresh, options` → "syntax error near `,`"
- `Add \`settlement_account\` Option to LogicFieldsValueFieldType` → "syntax error near `\``"
- `API: Get By Token` → "no such column: API" (the `:` is interpreted as column qualifier)
- `Submit Evidence: API automation` → "no such column: Evidence"

The bare `except` at `src/search/fts.py:198` swallows the error, returns `[]`. RRF then runs on vector-only pool (≤50), heavily biased toward provider docs (56% of corpus).

### H3+H9 reproducer (mechanism + bench correlation)

`/Users/vaceslavtarsevskij/.code-rag-mcp/.claude/debug/current/repro_h3.py` (D-built; verified live):
- Output: `Penalty applied doc-top-1: 0.0379, Penalty skipped doc-top-1: 0.2566` (~7× delta)

`/Users/vaceslavtarsevskij/.code-rag-mcp/.claude/debug/current/repro_h9.py` (D-built; arithmetic + bench):
- Output: GOTCHAS rank-30 boosted RRF=0.0331 > unboosted rank-5 RRF=0.0303 ✓
- Bench: queries with boost-type top-1 hit@10 = X/total

### H6 reproducer (drift bound)

```bash
python3 -c "
import json, sqlite3
conn = sqlite3.connect('db/knowledge.db')
indexed = {(r[0], r[1]) for r in conn.execute('SELECT DISTINCT repo_name, file_path FROM chunks').fetchall()}
queries = [json.loads(l) for l in open('profiles/pay-com/jira_eval_n900.jsonl')]
zero_indexed = 0
gt_total = 0
gt_indexed = 0
for q in queries:
    paths = [(p[0], p[1]) for p in q.get('expected_paths', [])]
    if not paths: continue
    overlap = sum(1 for p in paths if p in indexed)
    gt_total += len(paths)
    gt_indexed += overlap
    if overlap == 0: zero_indexed += 1
print(f'GT pairs in index: {gt_indexed}/{gt_total} = {100*gt_indexed/gt_total:.1f}%')
print(f'Queries with zero indexed GT: {zero_indexed}/{len(queries)}')
"
```

**Expected**: `42.1% indexed, 86/908 zero-indexed`.

## Watchlist (STEP 7) — regression markers if fix lands and the bug returns

After applying any of the three fixes, watch for these signals indicating root cause is wrong / partial:

1. **IR2 (sanitize fix)**: monitor `errs/total` from the reproducer above. If post-fix errs > 0, the regex extension missed a character — investigate the new error category. Also check `bench_runs/jira_e2e_*.json` `latency_p95_ms` — sanitize errors mask slow vector-only queries; fixing them may surface latency tail.
2. **H3+H9 (boost zero-out)**: monitor `top1.file_type` distribution per query (already in bench JSON). Pre-fix: 35.8% docs in misses. Target: <10% docs in misses. If fix lifts hit@10 but doc-top-1 stays high → mechanism only partly captured (e.g. provider_doc still boosted via reference path). Run repro_h3.py post-fix.
3. **H6 (re-index)**: monitor `gt_indexed/gt_total` from the reproducer above. Pre-fix: 42.1%. Target: ≥80%. If hit@10 lifts <5pp despite indexing reaching 80%+ → drift was real but small-impact (other ranking issues dominate, see H3+H9).
4. **NEW signals to watch** (in case unrefuted hypotheses change with bench drift):
   - `top1.file_type='provider_doc'` appearing in jira misses where GT is `*-docs` itself → would suggest H10 (cross-repo injection) is back
   - hit@10 drops on long queries (>13 tok) below short queries → H2 returning
   - hit@10 differential code-tower vs docs-tower exceeds 10pp → H4 escalating
5. **Bench harness drift**: re-run `bench_routing_e2e.py` periodically; if numbers drift >2pp without code change, suspect an index re-build that altered chunks.

## Outstanding mechanically-actionable next steps (for synthesis decision, NOT executed)

| order | action | expected lift | risk |
|---|---|---|---|
| 1 | Extend `_sanitize_fts_input` to strip `: , [ ] \`'/` (or wrap each token in `"..."`) | +5-15pp hit@10 on 258 affected queries; +1.4-4.3pp global | Low — pure sanitization, deterministic. Need test for queries with intentional FTS5 syntax (none in current eval but possible in user search input) |
| 2 | Force `apply_penalties=True` on all queries (drop the `not _query_wants_docs(query)` gate at hybrid.py:540) | +3-5pp on 116 doc-top-1 misses | Medium — may regress doc-intent eval; needs A/B on `bench_runs/v2_e2e_*.json` |
| 3 | Set `gotchas_boost = reference_boost = dictionary_boost = 1.0` in `profiles/pay-com/conventions.yaml` | +2-4pp on H3+H9 affected queries | Medium — was tuned for doc-intent; co-test with #2 |
| 4 | Re-index `backoffice-web` with relaxed extract filters (.tsx, .ts) | R@10 ceiling 0.40 → 0.85; hit@10 +3-4pp | High — may add noise to other repos' searches; need full bench |

## Convergence criteria check

- [x] ≥7 hypotheses (12 with IR2)
- [x] All hypothesis rows have `result: excluded|confirmed/open` (8 excluded, 3 confirmed/open + 1 IR2 confirmed)
- [x] At least one row has reproducible mechanism + concrete data signal
- [x] Sensitivity check labels survivors as ROBUST
- [x] Watchlist section exists

**Debate complete.** Ruling: 3 independent root causes (IR2, H3+H9, H6 partial); 8 alternatives refuted with concrete data; mechanically actionable fix list ready for synthesis decision.
