# Agent W1 — Implementation Report

**Date**: 2026-04-27
**Scope**: Unify boost + penalty in single normalized post-rerank space (Option A from Refactorist proposal)
**File**: `src/search/hybrid.py` ONLY
**Bench**: NOT run (deferred to team-lead)

---

## 1. Before / After

### Region A — `rerank()` function (post-rerank, normalized [0,1] space)

**Before** (lines 539-552):

```python
539  # Skip doc/test penalties when the query explicitly asks for them.
540  apply_penalties = not _query_wants_docs(query)
541
542  for i, r in enumerate(results):
543      rrf_norm = (r["score"] - min_rrf) / rrf_range
544      rerank_norm = (scores[i] - min_score) / score_range if i < len(scores) else 0
545      r["rerank_score"] = float(scores[i]) if i < len(scores) else 0
546      combined = 0.7 * rerank_norm + 0.3 * rrf_norm
547
548      # P4.1: down-weight doc/test/guide chunks so production code ranks higher
549      # on code-related queries. Stored on result for observability.
550      penalty = _classify_penalty(r.get("file_type", ""), r.get("file_path", "")) if apply_penalties else 0.0
551      r["penalty"] = penalty
552      r["combined_score"] = combined - penalty
```

**After** (new BOOST_BONUS dict + boost added in inner loop):

```python
539  # Skip doc/test penalties when the query explicitly asks for them.
540  apply_penalties = not _query_wants_docs(query)
541
542  # W1 (2026-04-27): unified boost+penalty in normalized post-rerank space.
543  # Was multiplicative on raw RRF (lines 815-824) — couldn't be offset by
544  # additive penalty. Mapping: 1.5 -> +0.10 (≈ -0.15 DOC_PENALTY symmetric
545  # for gotchas), 1.3 -> +0.06, 1.4 -> +0.08.
546  BOOST_BONUS = {
547      "gotchas": 0.10,
548      "reference": 0.06,
549      "dictionary": 0.08,
550  }
551
552  for i, r in enumerate(results):
553      rrf_norm = (r["score"] - min_rrf) / rrf_range
554      rerank_norm = (scores[i] - min_score) / score_range if i < len(scores) else 0
555      r["rerank_score"] = float(scores[i]) if i < len(scores) else 0
556      combined = 0.7 * rerank_norm + 0.3 * rrf_norm
557
558      # P4.1: down-weight doc/test/guide chunks so production code ranks higher
559      # on code-related queries. Stored on result for observability.
560      penalty = _classify_penalty(r.get("file_type", ""), r.get("file_path", "")) if apply_penalties else 0.0
561      r["penalty"] = penalty
562      boost = BOOST_BONUS.get(r.get("file_type", ""), 0.0)
563      r["boost"] = boost
564      r["combined_score"] = combined - penalty + boost
```

### Region B — Multiplicative boost loop (lines 815-824 originally)

**Before**:

```python
815  for _rid, data in scores.items():
816      ft = data.get("file_type", "")
817      if ft == "gotchas":
818          data["score"] *= GOTCHAS_BOOST
819      elif ft == "task":
820          data["score"] *= TASK_BOOST.get(data.get("chunk_type", ""), 1.0)
821      elif ft == "reference":
822          data["score"] *= REFERENCE_BOOST
823      elif ft == "dictionary":
824          data["score"] *= DICTIONARY_BOOST
```

**After** (only TASK_BOOST retained):

```python
829  for _rid, data in scores.items():
830      ft = data.get("file_type", "")
831      if ft == "task":
832          data["score"] *= TASK_BOOST.get(data.get("chunk_type", ""), 1.0)
```

Plus a comment block (lines 810-816) explaining why GOTCHAS/REFERENCE/DICTIONARY moved out and why TASK_BOOST stays multiplicative.

### Region C — Imports

Removed three now-unused imports: `DICTIONARY_BOOST`, `GOTCHAS_BOOST`, `REFERENCE_BOOST`.

---

## 2. Pytest result

```
$ python3.12 -m pytest tests/test_hybrid.py tests/test_hybrid_doc_intent.py tests/test_rerank_skip.py -q
..........................................................               [100%]
58 passed in 1.11s
```

- **58 passed**, **0 failed**
- `TestRerankPenalties` (8 tests): all pass — they only assert penalty values + ordering, never reference multiplicative boost constants
- `TestContentBoost`: does NOT exist (verified via grep — no test class named that)
- `test_hybrid_doc_intent.py`, `test_rerank_skip.py`: zero references to `boost` / `gotchas` / `reference` / `dictionary` / `combined_score` — fully orthogonal to W1

No fail-fix needed.

---

## 3. md5

| Stage           | md5                                |
|-----------------|------------------------------------|
| Pre-change      | `c2e1b2a7bcd7c849ac4f33069e8d45d7` |
| Post-change     | `aa47442cea89906447ab2dd9e3370c3a` |

---

## 4. Bonus mapping rationale

| file_type    | old multiplier | new bonus | symmetry rationale |
|--------------|----------------|-----------|--------------------|
| `gotchas`    | 1.5            | +0.10     | Strongest boost — paired with DOC_PENALTY=0.15. A gotchas chunk that gets penalized incorrectly (e.g. wrong intent classification) gets the bonus back roughly to net. Balance against a borderline code chunk: gotchas with bonus +0.10 outranks a base code chunk by 0.10 — non-trivial signal but not overwhelming. |
| `dictionary` | 1.4            | +0.08     | Mid-strength. 1.4 sits between gotchas (1.5) and reference (1.3); +0.08 mid between +0.10 and +0.06. Linear scaling preserved. |
| `reference`  | 1.3            | +0.06     | Weakest of the three. Reference is structurally lower-confidence than gotchas (gotchas = runtime traps; reference = structural lookup tables). +0.06 < DOC_PENALTY=0.15 means reference cannot dominate ahead of code chunks when penalty is in play, but on doc-intent queries (penalty=0) it gets a clear lift over generic doc chunks. |

**Why these specific magnitudes (not 0.15/0.10/0.12)?**

Three constraints:

1. **No bonus should exceed DOC_PENALTY (0.15)** — otherwise a boosted curated-doc chunk with penalty applied could still outrank a same-rerank-score code chunk, which violates the entire P4.1 design intent (code-intent → code first).
2. **Bonus magnitudes preserve relative ordering of the original multipliers** — gotchas > dictionary > reference, mirroring 1.5 > 1.4 > 1.3.
3. **Bonus is meaningful at the rerank-tied boundary** — when two chunks have identical `combined = 0.7 * rerank_norm + 0.3 * rrf_norm` (rare but exists when reranker returns near-identical scores in dense pool), the bonus difference of 0.10 is large enough to flip the order, which is the original design intent.

The mapping `1.5 → +0.10` (and proportionally) lands at ~67% of DOC_PENALTY for the strongest boost, leaving a clear "code-intent dominates" zone above.

---

## 5. Known risks (if W1 ships)

### Low-risk

- **Pytest is fully green** — all 58 tests pass. Penalty assertions still hold because `apply_penalties` semantics are unchanged.
- **Imports cleaned up** — no dead imports left.

### Tests that *might* need updating in the future

If a future test asserts `combined_score` directly for a `gotchas`/`reference`/`dictionary` chunk, the value will now include a `+ boost` term. None such exist today (verified via grep on `tests/`), but worth flagging for downstream `test_search_e2e.py` if added.

### Behavior risks

- **TASK_BOOST kept multiplicative** — by design (it's chunk_type-keyed, not file_type-keyed; lives on a different axis). If routing later finds task chunks under-perform similarly, a follow-up W1' could migrate TASK_BOOST too. Out of scope for W1.
- **`code_facts` boost (CODE_FACT_BOOST=1.15) and `env_vars` boost stay multiplicative** — these operate on raw RRF candidate-pool scores and are tied to candidate INJECTION (not just re-weighting). Migrating these would require restructuring `_apply_code_facts` and `_apply_env_vars` — out of W1 scope.
- **Bench expectation** — the structural fix should restore some of the wins lost when D1 zeroed boosts entirely. Whether bonus magnitudes (0.10/0.06/0.08) are optimal is a tuning question; bench will reveal.

### Operational

- `conventions.yaml` still has `gotchas_boost=1.500`, `reference_boost=1.300`, `dictionary_boost=1.400` — these are now **dead config** (consumed by `src/config.py` constants but those constants are no longer imported in `hybrid.py`). Not removed in W1 to keep the patch surgical and reversible. Follow-up: either remove from yaml or repurpose as bonus values via env-driven loading.

---

## 6. Files changed

- `src/search/hybrid.py` (only file touched, +21 / -12)

No new files. No other tracked changes.
