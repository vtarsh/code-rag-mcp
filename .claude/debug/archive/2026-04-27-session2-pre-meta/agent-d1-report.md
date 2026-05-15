# Agent D1 — zero boosts + force-penalties A/B prep

Status: changes applied, NOT pushed, NOT benched. Working tree dirty pending lead's bench.

## Files changed

### 1. `src/search/hybrid.py` (line 540)

Before:
```python
    # Skip doc/test penalties when the query explicitly asks for them.
    apply_penalties = not _query_wants_docs(query)
```

After:
```python
    # Skip doc/test penalties when the query explicitly asks for them.
    apply_penalties = True  # D1 A/B: penalty always on (was: not _query_wants_docs)
```

### 2. `profiles/pay-com/conventions.yaml` (lines 350–352)

Before (lines 350–351):
```yaml
  gotchas_boost: 1.500
  reference_boost: 1.300
```
(no `dictionary_boost` key — fell back to `1.4` default in `src/config.py:191`)

After (lines 350–352):
```yaml
  gotchas_boost: 1.0   # D1 A/B: zeroed (was 1.500)
  reference_boost: 1.0 # D1 A/B: zeroed (was 1.300)
  dictionary_boost: 1.0 # D1 A/B: explicit override (default 1.4 in src/config.py)
```

Note: `dictionary_boost` was not previously present in the YAML; default in `src/config.py:191` is `1.4`. Adding the explicit `1.0` line forces the override (per ruling spec — zero out all 3).

## md5 (post-change)

```
cb3b5912ca081ce817994980173fa9d9  src/search/hybrid.py
0a6f6d67c4ee88185aa50fd68984153e  profiles/pay-com/conventions.yaml
```

## Pytest

`python3.12 -m pytest tests/test_hybrid.py tests/test_hybrid_doc_intent.py tests/test_rerank_skip.py -q`

Result: **6 failed, 52 passed in 1.24s**

All 6 failures are in `tests/test_hybrid.py::TestRerankPenalties` and assert the OLD behavior (penalty=0 when query is doc-intent). With D1 forcing `apply_penalties = True`, these expectations no longer hold; failure is expected and predicted by the hypothesis.

Failing tests:
- `test_penalty_skipped_when_query_asks_for_docs`
- `test_checklist_query_disables_penalty`
- `test_framework_query_disables_penalty`
- `test_severity_rules_query_disables_penalty`
- `test_matrix_reference_sandbox_overview_disable_penalty`
- `test_ci_path_exempted_when_query_asks_docs`

Sample assertion error (representative): `assert 0.5 == 0.0` — penalty is now 0.5 (the `_classify_penalty` value) instead of 0.0 because the doc-intent skip no longer fires.

Per task instructions: tests NOT fixed; reported only. If A/B confirms D1, these tests should be inverted; if A/B rejects D1, both files revert and tests stay green.

## Diff

`git diff` only shows `src/search/hybrid.py` because `profiles/pay-com/conventions.yaml` is git-ignored (per CLAUDE.md: profiles/ is git-ignored except profiles/example/). YAML change verified by direct read; see md5 above.

```
--- a/src/search/hybrid.py
+++ b/src/search/hybrid.py
@@ -537,7 +537,7 @@
-    apply_penalties = not _query_wants_docs(query)
+    apply_penalties = True  # D1 A/B: penalty always on (was: not _query_wants_docs)
```

## Revert values (for lead, if A/B fails)

`src/search/hybrid.py:540`:
```python
    apply_penalties = not _query_wants_docs(query)
```

`profiles/pay-com/conventions.yaml:350–351` (delete the dictionary_boost line entirely):
```yaml
  gotchas_boost: 1.500
  reference_boost: 1.300
```

## Out of scope (not touched)

- `scripts/bench_routing_e2e.py` — parallel agent
- `profiles/pay-com/glossary.yaml` — parallel agent
- `db/knowledge.db` — read-only
- `bench_runs/` — read-only
