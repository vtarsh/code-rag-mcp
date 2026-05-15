# Stage C launch verdict v2 — 2026-04-24 by team-lead

## Recommendation: **GO-WITH-CAVEATS** for Stage C smoke (subset=10, ≤60min, $1-2)

Ready-to-launch score: **8/10** (was 2/10 in v1).

Pytest 719/719 green (689 baseline + 30 new). Stage C dry-run passes 4/4 mac-runnable steps + 3/3 bonus.

---

## Per-blocker closure (verifier T1)

| ID | Status | Note |
|---|---|---|
| **B1** atexit split | ✅ CLOSED | `--start` registers 0 atexit; `--start --hold` registers 1; SIGTERM/SIGINT bound default |
| **B2** train/eval disjoint | ✅ CLOSED w/ caveat | overlap=0/44, 9/9 strata. **expected_paths are auto-heuristic-v1, NOT human-graded.** Tagged `gold:false`. Suitable for **Recall@10 deltas vs baseline**, NOT for absolute-recall claims |
| **B3** prepare_train_data | ✅ CLOSED | Default db=`db/knowledge.db`, rejects 0-byte loudly, 91 resolved / 3 payper-new skipped/warned |
| **B4** nomic prefix | ✅ CLOSED | Constants byte-match `src/models.py`; query+positive+negative wrapped; `--no-prefix` opt-out |
| **B5** early HF_TOKEN check | ✅ CLOSED | sys.modules poisoning reproducer: train() raises before ST imported |
| **B6** ports array | ✅ CLOSED | `["22/tcp", "8888/http"]`, matches OpenAPI `array<string>` |
| **B7** pod-side timeouts | ⚠️ PARTIAL / best-effort | Fields in POST body but **NOT in RunPod OpenAPI** (REST or GraphQL). Likely silently ignored server-side. Real safety net = (1) dashboard "Auto-terminate after: 1h" + (2) Mac-side `--time-limit` + (3) cost-guard $5 cap |

---

## New regressions (regressor T2) — 5 MINOR, 0 BLOCKING

| Probe | Verdict | Note |
|---|---|---|
| `--stop`/`--terminate` after atexit split | CLEAN | `_teardown_running` flag absorbs double-call |
| **Double-prefix hazard** | MINOR | If user hand-edits a JSONL with `"search_query: ..."` already in `query`, `load_pairs` re-applies prefix → silent collapse. Canonical Stage C path safe (`prepare_train_data.py` guarantees prefix-free + test enforces). 5-line sentinel `if not row["query"].startswith(NOMIC_QUERY_PREFIX)` would close it. |
| `--dry-run` body shape | CLEAN | ports list, idleTimeoutInMin, terminationTime all serialised |
| HF_TOKEN ordering | CLEAN | minor: no token-validity pre-check (could still burn $ on invalid token) |
| **Public script hardcodes private path** | MINOR pre-existing | `scripts/build_doc_intent_eval.py` (PUBLIC) reads `profiles/pay-com/...` (PRIVATE). Not a leak (paths only) but reinforces a smell already in `benchmark_doc_intent.py` |
| terminationTime semantics | CLEAN | UTC epoch, fail-mode = ignored field (B7 caveat) |
| `scripts/runpod` package | CLEAN | PEP 420 namespace works at runtime; pyright noise only |
| Live API leakage in tests | CLEAN | All HTTP mocked, RUNPOD_API_KEY only via monkeypatch |

---

## E2E Stage C (e2e T3)

4/4 mac-runnable steps PASS. 3/3 bonus PASS. $0 spent.

| Step | Result |
|---|---|
| 1. `prepare_train_data --subset=10` | PASS — 10 rows {query, positive}, prefix-free |
| 2. secrets grep | PASS — empty (exit 1 = no matches) |
| 3. `pod_lifecycle --dry-run` | PASS — `rpa_` key loaded, `GET /v1/pods` 200 |
| 4. upload (SSH/scp) | N/A — no live pod |
| 5. `train_docs_embedder --dry-run` | PASS — 10 pairs, `nomic_prefix=True`, target=`vtarsh/pay-com-docs-embed-v0` |
| Bonus: `--list` 0 pods | PASS |
| Bonus: `cost_guard --check 0.50` | PASS — within $5/$5 caps |
| Bonus: GPU string only in source-of-truth | PASS — 0 hardcodes in tests |

### 2 runbook gotchas (must read before live `--start`)

1. **HF_TOKEN passthrough not wired.** `cmd_start` hardcodes `env={}` in POST body. CLI cannot inject `HF_TOKEN`. Workaround: `ssh` into pod and `export HF_TOKEN=...` before `train_docs_embedder.py`. `train()` fail-fasts on missing token before any ST import (~1 sec lost, $0 wasted).
2. **Prep prefix-free + Train prefix-add asymmetry is load-bearing.** Edits to either side must stay in sync OR train/serve distributions diverge silently. Test `test_output_jsonl_is_prefix_free` enforces prep side; train side relies on `load_pairs(apply_nomic_prefix=True)` default.

---

## Outstanding limitations

- **B7 best-effort:** server-side timeouts unverified by docs. **MUST verify after first live `--start`:** wait `--time-limit` minutes, then `pod_lifecycle.py --list` — if pod still RUNNING past time, the field is ignored and we rely on dashboard + cost-cap.
- **B2 heuristic eval:** auto-heuristic-v1 expected_paths suitable for **deltas**. For absolute recall claims (Stage D deploy criteria), need human-graded gold subset.
- **3/103 payper-new positives missing** from `db/knowledge.db` (3.3% gap). Skipped+warned. Trivial at subset=10; needs investigation before Stage D full run.
- **Public script + private data path** smell reinforced. Not a Stage C blocker. Defer.

---

## Plan correctness

Stage C 6-step plan verified end-to-end. **Add 1 new step before --start:**

> **Step 0: Verify dashboard auto-terminate is ON.** Login to console.runpod.io/user/settings → confirm "Auto-terminate after: 1 hour" + spending limit $5/day. This is the load-bearing safety net for B7.

---

## GO conditions (all 3 must hold)

1. Operator confirms dashboard auto-terminate=1h is active (Step 0 above).
2. Operator plans to `ssh` into pod after `--start` to `export HF_TOKEN=...` before launching training.
3. Operator commits to `pod_lifecycle.py --list` check ~65 minutes after `--start` to validate B7 best-effort net (or terminate manually if pod still RUNNING).

If any of these is unacceptable → defer Stage C until B7 has a verified server-side mechanism OR `--env` passthrough is added to cmd_start.

---

## Concrete next action

If user confirms 3 GO conditions:
- Apply 1 small follow-up before `--start`: add `--env KEY=VAL` passthrough to `pod_lifecycle.py cmd_start` (10 min). Then `export HF_TOKEN=hf_...` locally + pass to `--env`. This eliminates GO condition #2.
- OR proceed without `--env` passthrough, document SSH workaround, run Stage C now.

User decides which.
