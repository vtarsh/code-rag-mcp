# Deep Audit: code-rag-mcp Benchmarks & Eval Data

**Date:** 2026-05-16
**Auditor:** Kimi Code CLI (subagent)
**Scope:** `profiles/pay-com/eval/jira_eval_clean.jsonl`, `eval_jira_daemon.py`, recent bench runs, `db/tasks.db` cross-validation

---

## Executive Summary

The benchmark infrastructure has **one critical bug and one critical operational issue** that make recent numbers meaningless:

1. **eval_jira_daemon.py silently drops failed queries and still counts them as misses in the denominator.** This means every daemon crash turns into hundreds of false negatives, collapsing hit@10 from ~60% to ~10-20% depending on when the crash happens.
2. **The daemon crashes during long eval runs on macOS due to PyTorch/SentenceTransformers multiprocessing semaphore exhaustion.** The `time.sleep(0.1)` throttle is insufficient.

Beyond the infrastructure bugs, the eval data itself is mostly sound after cleaning, but the methodology (hit@10 with unbounded GT sets) is too lenient and masks real quality problems.

---

## 1. Critical Infrastructure Bugs

### 1.1 eval_jira_daemon.py silently drops errors → bogus aggregates

**File:** `scripts/eval/eval_jira_daemon.py`  
**Lines:** 55-71

```python
for i, row in enumerate(rows, 1):
    query = row["query"]
    try:
        retrieved = _search_via_daemon(query, args.limit)
    except Exception as exc:
        print(f"[{i}/{len(rows)}] ERROR: {exc}", flush=True)
        continue          # ← BUG: skips recording, but n = len(rows) still counts it
    ...
    is_hit = 1 if (expected & set(retrieved)) else 0
    hit += is_hit
    eval_per_query.append({...})   # ← never reached for failed queries

n = len(rows)                      # ← denominator = 665
result = {
    "aggregates": {
        f"hit_at_{args.limit}": round(hit / n, 4) if n else None,  # ← hit / 665
    },
}
```

**Impact:** When the daemon crashes after evaluating 108 queries, the script reports `hit@10 = 108_hits / 665 = 10.1%`. The remaining 557 queries are silently treated as misses. They do not appear in `eval_per_query`, making post-hoc analysis impossible.

**Evidence from overnight runs:**

| Run | aggregate_n | eval_per_query length | Missing | Reported hit@10 |
|-----|-------------|----------------------|---------|-----------------|
| overnight_run1 | 663 | 108 | 555 | 10.1% |
| overnight_run2 | 664 | 23 | 641 | 2.3% |
| overnight_run3 | 664 | 203 | 461 | 21.1% |

All successful queries are a **strict prefix** of the eval file (indices 0..N-1). This proves the daemon died mid-run and every subsequent query was lost.

**Fix:** Record errors explicitly in `eval_per_query` and compute `hit@10` over the number of *actually evaluated* queries, not the total file length. Better yet, restart the daemon on connection errors and retry.

### 1.2 Daemon crashes from macOS multiprocessing semaphore exhaustion

**File:** `daemon.py` (indirect — CrossEncoder reranker via `sentence-transformers`)  
**Trigger:** Rapid sequential search calls that invoke the CrossEncoder reranker.

The daemon logs show:

```
/Library/Frameworks/Python.framework/Versions/3.12/lib/python3.12/multiprocessing/resource_tracker.py:254: UserWarning: resource_tracker: There appear to be 1 leaked semaphore objects to clean up at shutdown
```

On macOS, `torch` + `sentence-transformers` multiprocessing creates POSIX named semaphores that are not reliably cleaned up between requests. After ~100-200 reranker invocations, the process runs out of semaphores and subsequent calls hang or crash.

The eval script attempts mitigation with `time.sleep(0.1)` (line 71), but this is insufficient for macOS semaphore limits.

**Fix options:**
- Set `TOKENIZERS_PARALLELISM=false` and `OMP_NUM_THREADS=1` before starting the daemon.
- Force the reranker to use single-threaded CPU mode on macOS eval runs.
- Run evals on Linux where semaphore limits are higher.
- Add a retry loop in `eval_jira_daemon.py` that restarts the daemon on `HTTPError` / `URLError`.

---

## 2. Benchmark Instability & Determinism

### 2.1 Same queries return different results across runs

From `logs/search_feedback.jsonl`, 778 distinct queries were run multiple times with different result sets. Examples:

| Query | Runs | Unique result sets |
|-------|------|-------------------|
| `payment` | 3,259 | 40 |
| `payment gateway` | 520 | 44 |
| `Add settlement_account Option to LogicFieldsValueFieldType` | 14 | 8 |

Even for the 108 queries successfully evaluated in **both** run1 and run3, 11 disagreed on hit/miss (10.2% disagreement).

**Root causes:**
- LanceDB approximate nearest neighbor search is non-deterministic by default.
- The entity-boost fallback (`use_entity_boost` + fallback to expanded query) creates branching behavior.
- CrossEncoder reranker scores can vary slightly due to floating-point batching effects.
- `docs_index` two-tower routing may lazy-load the docs tower on first doc-intent query, causing a cold-start divergence.

**Fix:** Add a determinism validation script that runs the same query 5× and asserts identical results. Gate model changes on this check.

---

## 3. Eval Data Quality

### 3.1 Path canonicalization ✅ PASS

All 10,650 GT `(repo_name, file_path)` pairs in `jira_eval_clean.jsonl` exist in `db/knowledge.db`.

**Verification:**
```python
conn = sqlite3.connect("db/knowledge.db")
cur.execute("SELECT DISTINCT repo_name, file_path FROM chunks")
indexed = {(r, p) for r, p in cur.fetchall()}
# present = 10,650, missing = 0
```

The `build_clean_jira_eval.py` script correctly:
1. Drops noise paths (lockfiles, generated, tests, configs).
2. Suffix-matches extractor paths to canonical DB paths.
3. Drops queries with <3 GT pairs after cleaning.

### 3.2 Query diversity ⚠️ WARNINGS

| Metric | Value |
|--------|-------|
| Total queries | 665 |
| Unique queries | 665 (100%) |
| Mean query length | 43.6 chars |
| Short queries (<20 chars) | 24 (3.6%) |
| Near-duplicate pairs (80%+ token overlap) | 25 |
| Distinct repos in GT | 246 |
| Queries with backoffice-web in GT | 304 (45.7%) |

**Issues:**
- **Backoffice-web dominance:** Nearly half the eval queries have GT in `backoffice-web`. A model that always returns backoffice-web files would get ~45% hit@10 by accident.
- **Short queries:** 24 queries are <20 characters (e.g. `"Update mui packages"`, `"US MPA"`). These are ambiguous and hard to evaluate meaningfully.
- **Near-duplicates:** 25 query pairs have 80%+ token overlap (e.g. `BO-1157` and `BO-1239` both query `"Compliance Business Activity - Business Activity Block"` / `"Compliance Business Activity - Archived Block"`). This inflates apparent sample size without adding information.

### 3.3 Ground-truth correctness ⚠️ WARNINGS

**Manual sample verification (10 random queries):**
- 9/10 queries had GT paths that plausibly matched the task description.
- 1 query (`CORE-2435`: "migrate workflow-onboarding-merchant-providers to typescript") had GT paths including `grpc-core-entity/proto/service.proto` and `docs/docs/README.md`, which are unrelated to the migration. This suggests the `files_changed` field in Jira sometimes includes tangential changes.

**Provider mismatch heuristic:**
- 12 queries mention a specific provider (e.g. `nuvei`, `worldpay`, `trustly`) but have **no** provider repo in their GT.
- Examples:
  - `PI-41`: "finish nuvei integrations testing" → GT only in `workflow-tasks`
  - `CORE-2409`: "Worldpay US Reconciliation" → GT only in `workflow-transaction-reconciliation`, `workflow-sync`

These are not necessarily wrong (the task might have touched only workflow/orchestration code), but they are **hard negatives** for a search system: a query about "Worldpay" should surface `grpc-apm-worldpay` or `grpc-providers-worldpay`, but the GT says those repos were never touched. This creates ambiguity about what "correct" means.

### 3.4 GT path count distribution ⚠️ WARNING

| Stat | Value |
|------|-------|
| Min GT paths | 3 |
| Max GT paths | 184 |
| Mean GT paths | 16.0 |
| Median | ~12 |

**Top offender:** `BO-1335` has 184 GT paths ("Migrate Individuals to New Structure with Multi-Relation Support"). With 184 possible correct answers, hit@10 is almost guaranteed to be 1 unless the search system is completely broken. This inflates the aggregate metric.

**Fix:** Cap GT to the most relevant N paths per query, or use a metric that penalizes false positives (e.g. precision@K, MRR).

---

## 4. Eval Methodology Issues

### 4.1 hit@10 is too lenient for multi-GT queries

The current metric: `is_hit = 1 if (expected & set(retrieved)) else 0`

- For a query with 184 GT paths, the system has 184 chances to get a hit in the top 10.
- This is not recall — it does not measure "what fraction of GT was found."
- It is not precision — it does not measure "how many of the top 10 are correct."

**Recommended metrics:**

| Metric | Formula | Why it helps |
|--------|---------|--------------|
| **Recall@K** | \|GT ∩ retrieved\| / \|GT\| | Measures coverage of the task's actual changes |
| **Precision@K** | \|GT ∩ retrieved\| / K | Measures noise in the top K |
| **MRR** | 1 / rank(first_hit) | Rewards getting *any* correct answer early |
| **nDCG@K** | discounted cumulative gain | Rewards finding more GT paths at higher ranks |

### 4.2 No stratification or per-repo analysis

The aggregate hit@10 hides regressions in specific domains:
- A change might improve backoffice-web hit@10 by +5pp while collapsing provider-repo hit@10 by -15pp.
- The aggregate would show -2pp and the regression would be missed.

**Fix:** Report per-repo and per-stratum breakdowns in every eval run.

### 4.3 No false-positive penalty

A search system could game hit@10 by returning 10 results from `backoffice-web` for every query. Since 45.7% of queries have backoffice-web in GT, this naive strategy would achieve ~45% hit@10.

**Fix:** Add precision@10 or a combined F1@10 score.

---

## 5. Cross-Validation with task_history

### 5.1 Coverage

| Source | Count |
|--------|-------|
| task_history rows with files_changed | 910 |
| jira_eval_clean queries | 665 |
| task IDs in both | 665 |
| task IDs missing from eval | 245 |

**Why 245 tasks were dropped:**
- 166 had <3 GT paths after noise filtering (reasonable).
- 79 had GT paths not present in `db/knowledge.db` (unhittable).

**Verdict:** The eval set is a reasonably filtered subset. No tasks in eval are fabricated.

### 5.2 Representativeness

The eval set is **Jira-only** (all strata = `["jira"]`). It does not represent:
- Doc-intent queries (evaluated separately in `doc_intent_eval_v3.jsonl`).
- Ad-hoc code search queries from agent sessions.
- Provider-swap reformulation chains.

**Fix:** Maintain a blended eval that samples from all query sources proportionally.

---

## 6. Recommendations (Prioritized)

### P0 — Fix before trusting any benchmark number

1. **Fix eval_jira_daemon.py error handling**
   - Do not `continue` on exception. Record `{"query_id": ..., "error": str(exc), "hit_at_k": 0}` in `eval_per_query`.
   - Compute `hit@10` over `len(eval_per_query)` (actually evaluated), not `len(rows)`.
   - Add a retry loop: on `HTTPError` / `URLError`, wait 5s and retry up to 3×.

2. **Fix daemon stability on macOS**
   - Export `TOKENIZERS_PARALLELISM=false` and `OMP_NUM_THREADS=1` in `overnight_bench.sh` before starting the daemon.
   - Consider disabling the CrossEncoder reranker for eval runs (`CODE_RAG_DOC_RERANK_OFF=1` is already a kill-switch; document it for eval use).

3. **Add a daemon health check inside the eval loop**
   - Every 50 queries, call `/health` and abort if it returns non-200.

### P1 — Improve methodology

4. **Add MRR, Recall@10, and Precision@10**
   - These metrics are more informative than binary hit@10 for multi-GT queries.

5. **Cap or normalize GT sets**
   - For queries with >20 GT paths, consider keeping only the most central files (e.g. exclude `package.json`, `generated/*`, test files).
   - Alternatively, use recall-based metrics that naturally handle large GT sets.

6. **Stratify reporting**
   - Report hit@10 broken down by:
     - Primary repo (backoffice-web vs provider repos vs workflows)
     - Query length (short <20 vs medium 20-60 vs long >60)
     - Single-repo vs multi-repo GT

### P2 — Improve data quality

7. **Add determinism checks**
   - Run a subset of 50 queries 3× against the same daemon. Assert >95% rank agreement.
   - Gate model/index changes on this check.

8. **Filter or flag suspicious GT**
   - For queries mentioning a provider but lacking a provider repo in GT, flag as "hard negative" and report separately.
   - Remove or manually review queries with >50 GT paths.

9. **Deduplicate near-duplicate queries**
   - Remove or merge the 25 near-duplicate pairs to avoid information leakage.

---

## 7. Validation Script

Created: `scripts/eval/validate_jira_eval.py`

Run it anytime with:
```bash
python3 scripts/eval/validate_jira_eval.py
```

It checks:
- DB coverage of GT paths
- Query diversity stats
- Suspicious provider mismatches
- Recent benchmark result sanity (detects silent drops)

---

## Appendix: Raw Findings

### Query IDs with provider mismatch
```
PI-41     → mentions 'nuvei'      but GT only in workflow-tasks
BO-719    → mentions 'ppro'       but GT only in backoffice-web, microfrontends-web
CORE-2409 → mentions 'worldpay'   but GT only in workflow-reconciliation-*
CORE-2411 → mentions 'silverflow' but GT only in workflow-reconciliation-bank-transfer
CORE-2427 → mentions 'silverflow' but GT only in workflow-transactions-error-fallback, etc.
CORE-2428 → mentions 'tabapay'    but GT only in grpc-providers-tabapay (actually OK)
CORE-2441 → mentions 'ppro'       but GT only in grpc-core-settings
CORE-2475 → mentions 'worldpay'   but GT only in workflow-reconciliation-fraud, node-libs-common
CORE-2500 → mentions 'worldpay'   but GT only in workflow-reporting-master, etc.
CORE-2582 → mentions 'ppro'       but GT only in grpc-auth-apikeys2, libs-types
```

### Top queries by GT path count
```
BO-1335   → 184 paths (Migrate Individuals to New Structure)
CORE-2581 → 178 paths (Migrate all services to latest pg lib)
CORE-2186 → 151 paths (Optimizations for payment-gateway sale)
BO-825    → 135 paths (BO / Task Management screens)
BO-1336   → 101 paths (Entity Websites)
```

### Near-duplicate query pairs (sample)
```
BO-1157 / BO-1239: "Compliance Business Activity - Business Activity Block"
BO-1158 / BO-1239: "Compliance Business Activity - Website & Processing Info Block"
BO-1150 / BO-1151: "Compliance Overview - Review Details Block" / "Risk Details Block"
```
