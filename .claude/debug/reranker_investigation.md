# CrossEncoder Reranker Investigation

**Date:** 2026-05-16  
**Investigator:** codebase exploration agent  
**Scope:** whether the CrossEncoder reranker helps or hurts search quality on code vs doc queries

---

## 1. How Reranking Works

### 1.1 Pipeline (src/search/hybrid_rerank.py, src/search/hybrid.py)

1. **FTS5 keyword search**: 150 candidates (2× weight, no per-repo cap)  
2. **Vector search**: 50–100 candidates (two-tower: code tower or docs tower, or both merged)  
3. **RRF fusion**: merges FTS + vector, plus code_facts / env_var / repo_prefilter boosts  
4. **Rerank cap**: `max(limit * 2, RERANK_POOL_SIZE)` candidates fed to CrossEncoder  
5. **CrossEncoder scoring** + combination + penalties  
6. **Sort by combined_score**, truncate to `limit`

### 1.2 Rerank Pool Size

- **`RERANK_POOL_SIZE = 200`** (default, overridable via `CODE_RAG_RERANK_POOL_SIZE`)
- Config: `src/config.py:222`
- Before P4.2 the cap was `limit * 2` (~20); widening to 200 gave +10pp R@10 for MiniLM-L-6.

### 1.3 Documents Passed to the Reranker

```python
doc = re.sub(r">>>|<<<|\.\.\.|\[Repo: [^\]+\]", "", r.get("snippet", ""))
doc = f"{dict_hint}{r['repo_name']} {r['file_path']} {doc}"[:1024]
```

- **Format:** `{repo_name} {file_path} {cleaned_snippet}`
- **Length cap:** 1024 characters
- **Optional dictionary hint prefix** (gated by `CODE_RAG_USE_DICT_RERANK_HINTS=1`)
- The reranker sees the **same candidate set** that survived RRF fusion; it does not fetch new candidates.

### 1.4 Score Combination

```python
rrf_norm   = (r["score"] - min_rrf) / (max_rrf - min_rrf)
rerank_norm = 1.0 / (1.0 + math.exp(-raw_score * 2.0))   # sigmoid, temp=2.0
combined    = 0.7 * rerank_norm + 0.3 * rrf_norm
```

- **70% reranker score** (sigmoid-normalized for cross-batch stability)
- **30% normalized RRF score**
- Then a **multiplicative penalty** is applied:
  ```python
  combined_score = combined * (1.0 - penalty)
  ```

### 1.5 Penalties: Before or After Reranking?

**After.**  
`src/search/hybrid_rerank.py:240-246` computes the combined score first, then classifies and applies the penalty. The reranker raw score is computed on the un-penalized document text; penalties are a post-rerank down-weight for doc/test/guide/CI paths on code-intent queries.

Penalty values (code-intent queries only):
- `GUIDE_PENALTY = 0.25` (AI-CODING-GUIDE.md / CLAUDE.md / README.md)
- `TEST_PENALTY = 0.20` (*.spec.js, *.test.py, /tests/...)
- `CI_PENALTY = 0.50` (ci/deploy.yml, k8s/.github/workflows/*)
- `DOC_PENALTY = 0.15` (doc/task/gotchas/reference/dictionary/provider_doc/flow_annotation)

Penalties are **skipped** when `_query_wants_docs(query)` is True.

### 1.6 Reorder vs Filter?

The reranker **reorders** the existing RRF candidate pool. It does not expand the pool or fetch new documents. However, because the final output is `results[:limit]`, any candidate that falls below the limit after reordering is effectively **filtered out** at the presentation layer. The pool size entering reranking is ~200; the output limit is typically 10.

---

## 2. Empirical Evidence: Does the Reranker Help or Hurt?

### 2.1 Docs Intent (Default L6 Reranker)

| Eval Set | With Rerank | NO RERANK | Δ R@10 | Δ Hit@10 | Δ Latency p50 |
|----------|------------:|----------:|-------:|---------:|--------------:|
| `baseline_prod_docs_v3` (n≈90) | 0.2365 | 0.2609 | **-2.44pp** | -4.45pp | +153 ms |
| `baseline_prod_docs_n200` (n≈200) | 0.2138 | 0.2289 | **-1.51pp** | -3.13pp | +169 ms |
| `v2_calibrated_L6` (n≈202) | 0.7249 | 0.6311 | **+9.38pp** | +4.34pp | +262 ms |

**Observation:** On the *production* doc-intent evals (`baseline_prod_docs_v3`, `n200`) the L6 reranker **hurts** recall and hit rate. On the *calibrated* eval (`v2_calibrated`) it helps massively. The difference likely stems from label quality / distribution — the calibrated set may be cleaner or more code-like.

**L12 FT on docs is even worse:**
- `v2_calibrated_l12` (l12 FT docs-only): R@10 = 0.6045 vs L6 baseline 0.7249 → **-12.0pp**
- Docs-only fine-tuning of L-12 was explicitly **REJECTED** (`rerank_ft_l12_result.md`)

### 2.2 Code Intent

| Eval Set | With Rerank | NO RERANK | Δ R@10 | Δ Hit@10 | Δ Latency p50 |
|----------|------------:|----------:|-------:|---------:|--------------:|
| `baseline-L6_code_bench` (n=80) | 0.1756 | 0.1288 | **+4.68pp** | +3.75pp | +220 ms |
| `jira_n900_prod_L6` (n=908) | 0.0682 | 0.0587 | **+0.95pp** | +4.08pp | +128 ms |
| `v2_calibrated_L6` (code mixed) | 0.7249 | 0.6311 | **+9.38pp** | +4.34pp | +262 ms |

**Observation:** On code queries the L6 reranker is **consistently positive**.

**L12 FT on code:**
- Claimed +3.31pp top-10 on jira n=908 vs L6 (bootstrap CI [+0.88, +5.73])
- `jira_n900_l12`: R@10 = 0.0762 vs L6 0.0682 → +0.80pp (direction confirmed, smaller gain on this specific run)
- `rerank-l12_code_bench`: R@10 = 0.1869 vs L6 code bench 0.1756 → +1.13pp

### 2.3 Per-Stratum Breakdown (Docs)

From `baseline_prod_docs_v3.json` vs `NO_RERANK` (L6 reranker):

| Stratum | Rerank ON | Rerank OFF | Δ |
|---------|----------:|-----------:|----|
| **webhook** | 0.1758 | 0.2667 | **-9.09pp** |
| **trustly** | 0.1778 | 0.2889 | **-11.11pp** |
| **method** | 0.1537 | 0.1093 | +4.44pp |
| **payout** | 0.0485 | 0.0788 | -3.03pp |
| **nuvei** | 0.2424 | 0.3439 | **-10.15pp** |
| **aircash** | 0.2556 | 0.3444 | **-8.88pp** |
| **refund** | 0.3909 | 0.4409 | -5.00pp |
| **interac** | 0.6296 | 0.4815 | **+14.81pp** |
| **provider** | 0.2333 | 0.1533 | **+8.00pp** |
| **tail** | 0.0444 | 0.1407 | **-9.63pp** |

This data drove the **stratum-gated rerank skip** (`_DOC_RERANK_OFF_STRATA`):
- **OFF (skip rerank):** webhook, trustly, method, payout
- **KEEP (run rerank):** nuvei, aircash, refund, interac, provider

The v2 LLM-calibrated eval (10 Opus agents, ~2200 judgments, n=192) refined this map:
- Hurt strata: webhook +3.35pp, trustly +2.68pp, method +1.30pp, payout +1.11pp (skip → lift)
- Help strata: nuvei -7.58pp, aircash -8.78pp, refund -14.51pp (keep to avoid regression)

---

## 3. Prod-Traffic Churn Evidence

From `p10-quickwin-report.md` (100 prod doc-intent queries, seed=42):

- **100% of queries** had their top-10 reordered by the reranker.
- **Average top-10 overlap:** only 36.5% (6.35 files swapped per query).
- Direction agreement: 6/9 negative-delta strata also show high prod churn (≥6.0).

This means the reranker is not a gentle nudge — it is a **violent reshuffle** of the top-10 on every doc query.

---

## 4. Specific Answers to Questions

| Question | Answer |
|----------|--------|
| **What is the rerank pool size?** | `RERANK_POOL_SIZE = 200` (default). Cap = `max(limit*2, 200)`. |
| **What documents are passed?** | `{repo_name} {file_path} {snippet}` (markers stripped, 1024-char cap). Optional dict hint prefix. |
| **How are scores combined?** | `combined = 0.7 * sigmoid(rerank_score, temp=2.0) + 0.3 * normalized_RRF`. Then multiplicative penalty. |
| **Are penalties before or after reranking?** | **After.** Reranker scores the raw document; penalties apply to the combined score. |
| **Does reranker help code but hurt docs?** | **Yes.** Code: L6 helps (+4.7pp on code bench, +1.0pp on jira). Docs: L6 hurts on prod doc evals (-1.5pp to -2.4pp). L12 FT hurts docs even more (-12pp). Stratum-gated skip implemented to mitigate. |

---

## 5. Summary Verdict

1. **On code queries:** the CrossEncoder reranker is a clear win. Production now routes code-intent queries to the fine-tuned L12 model (`Tarshevskiy/pay-com-rerank-l12-ft-run1`), which beats the L6 baseline by a further +3.31pp.

2. **On doc queries:** the default L6 reranker is **net harmful** on the production doc-intent benchmarks. It violently reshuffles the top-10 (100% of queries, 6.35 avg churn) and drops recall on most strata. A stratum-gated skip partially mitigates this, disabling the reranker on the worst-affected strata (webhook, trustly, method, payout) while keeping it on strata where it rescues results (nuvei, aircash, refund, interac, provider).

3. **Latency cost:** +130–260 ms p50 depending on eval set. On docs this is a ~2.4× slowdown with negative quality return.

4. **Root cause hypothesis:** the production reranker is **code-trained** (L12 FT was trained on 904 code pairs). It transfers poorly to doc-intent queries because the document distribution (provider API docs, gotchas, task plans) differs radically from the code distribution it was fine-tuned on. Docs-only FT attempts have also failed (L-12 docs FT: -11.5pp; BGE FT: overfit, destroyed), suggesting the doc reranking problem is harder than simply fine-tuning a CrossEncoder.

---

## 6. Files Consulted

- `src/search/hybrid_rerank.py` — reranking logic, score combination, penalties
- `src/search/hybrid.py` — hybrid search pipeline, RRF fusion, two-tower routing, stratum-gated skip
- `src/search/hybrid_query.py` — doc-intent classifier, stratum detection, OFF/KEEP strata definitions
- `src/config.py` — `RERANK_POOL_SIZE`, penalty constants, tuning knobs
- `src/container.py` — intent-based reranker routing (l12 for code, L6 for docs)
- `src/embedding_provider.py` — LocalRerankerProvider implementation
- `bench_runs/baseline_prod_docs_v3.json` + `NO_RERANK` — docs v3 with/without rerank
- `bench_runs/baseline_prod_docs_n200.json` + `NO_RERANK` — docs n200 with/without rerank
- `bench_runs/baseline-L6_code_bench.json` + `baseline_prod_code_NO_RERANK.json` — code bench with/without rerank
- `bench_runs/jira_n900_prod_L6.json` + `jira_n900_no_rerank.json` — jira with/without rerank
- `bench_runs/v2_calibrated_L6.json` + `v2_calibrated_NO_RERANK.json` — calibrated eval with/without rerank
- `bench_runs/v2_calibrated_l12.json` — l12 FT on docs (rejected)
- `bench_runs/jira_n900_l12.json` — l12 FT on jira
- `.claude/debug/p10-quickwin-report.md` — stratum-gated skip rationale, prod churn data
- `.claude/debug/rerank_ft_l12_result.md` — L-12 FT docs-only rejection report
- `.claude/debug/rerank_ft_bge_result.md` — BGE FT attempt (overfit, model lost)
