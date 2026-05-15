# Stage C regression scan — 2026-04-24 by regressor

Scope: hunt bugs INTRODUCED by 4 fix commits (B1-B7 patches) for Stage C.
Re-verifying fix correctness is the verifier's job (task #1) and is intentionally
skipped here. All probes run on working-tree state (fixes are uncommitted).

Test baseline: `python3.12 -m pytest tests/ -q` → **719 passed in 40.43s**.

---

## Probe 1 — cmd_stop / cmd_terminate / atexit split (B1 follow-on)
Result: **CLEAN** (1 MINOR observation)

Evidence:
- `cmd_stop POD_ID` and `cmd_terminate POD_ID` never call
  `install_signal_handlers()` (verified by patching `pod_lifecycle.atexit.register`
  during both flows → call_count = 0). The atexit-split fix only reshapes the
  `--start` path, leaving stop/terminate semantics intact.
- The `cmd_start` (no `--hold`) path now installs only SIGTERM/SIGINT handlers.
  `cmd_start --hold` still binds atexit (`atexit.register` call_count = 1, fn is
  `_teardown`).
- Double-stop hazard from SIGINT + atexit firing both is blocked by the
  `_teardown_running` flag at `pod_lifecycle.py:191-193`. Direct test:
  `_teardown(SIGINT)` then `_teardown()` → `stop_pod` called exactly 1 time.
- No other code path relies on auto-stop at process-exit. `tests/` only test the
  helper directly with `_started_pod_ids` seeded; no integration test expected
  the now-removed default-atexit behavior. 8 new B1 tests in
  `tests/test_runpod_lifecycle.py:266-325` cover both modes.

MINOR: `install_signal_handlers(register_atexit=True)` called twice in the same
process duplicates the atexit registration (verified: 2 vs 1). `_teardown` is
idempotent so the second invocation is a no-op, but it's wasted work and a
testability sharp edge if anyone reuses the helper across multiple
start-then-stop cycles in one Python process. CLI-driven workflow (single `--start`
per process) is unaffected. Fix is trivial (module-level `_atexit_registered`
guard) but not a Stage C blocker.

---

## Probe 2 — Double-prefix between prepare_train_data + train_docs_embedder (B3+B4)
Result: **CLEAN on intended pipeline**, **MINOR on hand-edited input**

Evidence:
- `prepare_train_data.py:184` writes `{query, positive}` rows with raw text;
  the test `test_output_jsonl_is_prefix_free` at
  `tests/test_prepare_train_data.py:221` enforces this contract.
- `train_docs_embedder.py:62-66` adds the `search_query: ` / `search_document: `
  prefix when `apply_nomic_prefix=True` (default). End-to-end smoke run
  confirmed:
  - `prepare_train_data.run()` → output: `{"query": "nuvei APM docs", "positive": "..."}` (prefix-free)
  - `train_docs_embedder.load_pairs(...)` → loaded: `"search_query: nuvei APM docs"` and `"search_document: ..."` (single-prefix)
- Re-running `prepare_train_data.py --subset=15` after `--subset=10` to the same
  `--out` overwrites cleanly (15 lines, not 25). `write_jsonl` uses `mode="w"`.
  With seed=42 the `random.Random.sample(20, k)` happens to produce
  superset-like results across `k=10` and `k=15` only by coincidence; this is
  documented as a property of `random.Random.sample` reproducibility for the same
  seed when k grows on the same population, but the user should still treat
  re-runs as "fresh sample" (not delta append).

MINOR (hand-edited input):
- If a user (or a future tool) bypasses `prepare_train_data.py` and writes a
  JSONL where `query` / `positive` are ALREADY prefixed
  (`"search_query: ..."`), `load_pairs` blindly re-prefixes:
  `"search_query: search_query: ..."`. Reproduced in a temp file run.
- Mitigations available but NOT applied:
  1. add a sentinel skip: `if not row["query"].startswith(NOMIC_QUERY_PREFIX)` before wrapping
  2. add a load-time assertion that fails loudly on already-prefixed input
- Not a Stage C blocker because the canonical pipeline runs through
  `prepare_train_data.py` (which guarantees prefix-free output).

---

## Probe 3 — `--dry-run` + body shape (B6)
Result: **CLEAN**

Evidence:
- `cmd_dry_run()` runs `_request("GET", "/pods")` only via `list_pods` mock; no
  POST / DELETE path. Verified live: `cmd_dry_run` returns 0 with mocked
  `list_pods`, and the test `test_dry_run_does_not_call_pod_create` already
  asserts `_request.call_count == 0` end-to-end (with `list_pods` mocked above
  it).
- `start_pod` body now contains `"ports": ["22/tcp", "8888/http"]` (list[str]),
  `"idleTimeoutInMin": 15`, `"terminationTime": <epoch+seconds>`. Captured live
  body via `_request` patch:
  ```
  {"ports": ["22/tcp","8888/http"], "idleTimeoutInMin": 15, "terminationTime": 1777066652, ...}
  ```
- 4 new B6/B7 tests in `tests/test_runpod_lifecycle.py:329-424` cover this
  contract (ports type, ports values, idle timeout = 15, terminationTime
  arithmetic).

---

## Probe 4 — HF_TOKEN check ordering (B5)
Result: **CLEAN**, MINOR per task spec (no token-validity pre-check)

Evidence:
- `train_docs_embedder.py:84-86`: `_parse_out(out)` + token check happens
  BEFORE `load_pairs(...)` and BEFORE the `from sentence_transformers import ...`
  block at line 95. Verified live with `HF_TOKEN` deleted: `train(..., out="hf:owner/repo")`
  raises `RuntimeError("HF_TOKEN missing — required for hf: push")` without
  triggering torch / sentence_transformers import.
- Local-dir output (`out="/tmp/foo"`) skips the check — token only required for
  `hf:` push.

MINOR (per task spec, "not a blocker"):
- A present-but-invalid token still passes the check, then bombs at
  `model.push_to_hub()` after 100 training steps (~10-20 min, ~$0.10-0.30).
  A pre-flight `HEAD https://huggingface.co/api/whoami` (or any cheap
  authenticated GET) before `model.fit()` would catch this in seconds. Not
  applied; flag for future hardening.

---

## Probe 5 — `build_doc_intent_eval.py` public-repo scoping (B2 follow-on)
Result: **MINOR REGRESSION** (scoping smell, not a leak)

Evidence:
- `scripts/build_doc_intent_eval.py:44,47` hard-codes
  `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` (TRAIN_PATH)
  and `profiles/pay-com/doc_intent_eval_v1.jsonl` (OUT_PATH) and
  `db/knowledge.db` (which is also profile-populated).
- Script is in `scripts/` (public path in repo `vtarsh/code-rag-mcp`), but
  paths reference the gitignored `profiles/pay-com/` (private) directory and
  the gitignored `db/` directory. The script reads `CODE_RAG_HOME` env var so
  paths are user-configurable, but defaults are `pay-com`-shaped.
- This means a fresh clone of the public repo cannot run this script without
  the private profile installed. No code or data leak — paths are STRINGS,
  not contents — but it violates `.claude/rules/conventions.md` line 1: "All
  org-specific data in profiles/{name}/, zero hardcoded org names in src/".
  Caveat: the rule is scoped to `src/`, and this is in `scripts/`.
- Pre-existing pattern: `scripts/benchmark_doc_intent.py:49` does the same
  thing (`EVAL_PATH = ROOT / "profiles" / "pay-com" / "doc_intent_eval_v1.jsonl"`),
  and a wider grep shows other `scripts/benchmark_*.py` files mirror this. So
  Stage C didn't INTRODUCE the smell — it reinforced a pre-existing pattern.
  Mark MINOR / PRE-EXISTING; not a Stage C launch blocker.

Recommended hardening (not required):
- Read profile name via `ACTIVE_PROFILE` env var or `.active_profile` file (the
  conventions-blessed mechanism), then build paths via `f"profiles/{profile}/..."`.
  Same change applies to `benchmark_doc_intent.py` for symmetry.

---

## Probe 6 — `terminationTime` semantics (B7)
Result: **CLEAN** (failure mode is documented + harmless)

Evidence:
- `start_pod` computes `start_ts = int(now_fn())` (default `time.time`) which
  returns Unix seconds since 1970-01-01 UTC; this is timezone-agnostic by
  definition. Mac in EEST (UTC+3) gives the same epoch number as a UTC server.
- RunPod REST OpenAPI does not document `terminationTime` on `PodCreateInput`.
  Two failure modes:
  1. Field is silently ignored → pod runs until `idleTimeoutInMin: 15` triggers
     (15 min idle) OR the user's dashboard `Auto-terminate after: 1 hour` fires
     OR account-level `$5 single-run` cap.
  2. Field is rejected → 400 from POST /pods (cost guard already cleared, so
     no money spent; obvious failure surface).
- Both modes are documented in the docstring at `pod_lifecycle.py:138-144`.
  Ladder-of-defense (Mac SIGTERM/SIGINT → atexit-on-`--hold` → `idleTimeoutInMin` →
  dashboard auto-stop → spending cap) keeps the worst case bounded.

---

## Probe 7 — `scripts/runpod/__init__.py` namespace package
Result: **CLEAN** (PEP 420 namespace), MINOR (pyright noise)

Evidence:
- `scripts/runpod/` and `scripts/` both lack `__init__.py`. PEP 420 (implicit
  namespace packages, Python 3.3+) handles this transparently.
- Direct `python3.12 -c "from scripts.runpod import {prepare_train_data,
  pod_lifecycle, train_docs_embedder, cost_guard}"` → all 4 IMPORT OK after
  inserting project root into `sys.path` (which `tests/conftest.py:11-15` does
  unconditionally).
- pytest collection finds 40 tests across `tests/test_runpod_lifecycle.py` +
  `tests/test_prepare_train_data.py` (12 + 28). Module imports succeed.
- Pyright "unknown-import" warning is a false positive at edit time when the
  IDE doesn't have `extraPaths`/`extraRoots` configured. Runtime is unaffected.

MINOR: adding an empty `scripts/runpod/__init__.py` would silence pyright and
make the package discoverable by static tools without `sys.path` hacks. Trivial
and zero risk; left out for now since the entire `scripts/` tree is namespace.

---

## Probe 8 — Live API leakage in tests
Result: **CLEAN**

Evidence:
- `grep -rn "rest.runpod.io\|RUNPOD_API_KEY" tests/` returns 8 hits, all in
  `tests/test_runpod_lifecycle.py`. Every `RUNPOD_API_KEY` reference is either
  a `monkeypatch.setenv("RUNPOD_API_KEY", "rpa_test_xxx")` fixture (line 39)
  or a fixture name (`env_with_key`, `env_no_key`) or a redaction-fixture
  string (`hf_secret123`, `rpa_xyz` — lines 258-261).
- No test imports `urllib.request.urlopen` directly; every HTTP path is mocked
  via `patch.object(pod_lifecycle, "_request", ...)` or
  `patch.object(pod_lifecycle, "list_pods", ...)`. No `requests` / `httpx` use.
- `cost_guard._read_api_key()` is invoked only after `monkeypatch.setenv` puts
  a fake key in env; `assert_can_spend` is never given a real `today_spend_fn`
  in tests (always a lambda or the spy `_spy`).
- Test count growth: 689 → 719 (+30). Breakdown: +12 lifecycle tests
  (`test_runpod_lifecycle.py`), +8 prep tests (`test_prepare_train_data.py`),
  +10 elsewhere (memguard / docs_vector_indexer additions). Pytest
  `--collect-only` confirms.

---

## Summary

| # | Probe | Result |
|---|---|---|
| 1 | cmd_stop/terminate + atexit split | CLEAN (1 MINOR: dup atexit on repeat install) |
| 2 | prepare/train double-prefix | CLEAN canonical, MINOR hand-edit hazard |
| 3 | --dry-run + body shape | CLEAN |
| 4 | HF_TOKEN ordering | CLEAN, MINOR (no token-validity pre-check) |
| 5 | build_doc_intent_eval.py scoping | MINOR REGRESSION (pay-com hard-code in public path) |
| 6 | terminationTime semantics | CLEAN |
| 7 | scripts/runpod/__init__.py | CLEAN (PEP 420), MINOR (pyright noise) |
| 8 | live API leakage in tests | CLEAN |

**No GO-blocking regressions found.** Five MINOR items, none of which would
burn money or produce broken Stage C output. Recommended follow-up after Stage C
ships:

1. Probe 2 hand-edit guard: skip prefix wrap if input already prefixed (5 lines).
2. Probe 5 profile parameterization: read `ACTIVE_PROFILE` (5 lines, applies to
   `build_doc_intent_eval.py` + `benchmark_doc_intent.py`).
3. Probe 1 `_atexit_registered` guard (3 lines).
4. Probe 4 token pre-flight (10 lines, +1 test).
5. Probe 7 add empty `__init__.py` (silences pyright; 0 lines runtime).

All B1-B7 fixes shipped without introducing GO-blocker regressions. Stage C
launch surface is clean from the regression angle.
