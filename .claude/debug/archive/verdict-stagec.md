# Stage C launch verdict — 2026-04-24 by team-lead

## Recommendation: **NO-GO**

Ready-to-launch score: **2/10** — 7 confirmed blockers, all cheap to fix individually but collectively ~2-3 h of prep before any `--start`. Running Stage C now would either burn $ on silently-broken training OR produce un-interpretable results.

---

## Confirmed blockers — MUST fix before `pod_lifecycle.py --start`

### B1 — `cmd_start` → atexit → pod self-terminates (H10)
- **Where:** `scripts/runpod/pod_lifecycle.py:278-282` registers atexit via `install_signal_handlers()`, creates pod, returns. When `main()` returns → process exits → atexit fires → `_teardown()` stops the pod we just made.
- **Confirmed by:** A (initial catch) + B (re-framed: "current code synchronously creates+stops any pod").
- **Reproducer:** No test covers the full `cmd_start → process-exit → atexit` flow. All current tests mock stop_pod/list_pods.
- **Proposed fix (do NOT apply yet):** split `cmd_start` into two modes: default `--start` registers ONLY SIGTERM/SIGINT handlers (for interrupt-during-creation); `--start --hold` additionally registers atexit (for local-driven training where Mac stays connected). Alternative: `os._exit(0)` after print to skip atexit.

### B2 — Train/eval LEAKAGE: 20/20 train queries already in `doc_intent_eval_v1.jsonl` (C#1)
- **Where:** `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` (training source) vs `profiles/pay-com/doc_intent_eval_v1.jsonl` (eval).
- **Measured:** 20 unique train queries, 50 unique eval queries, **overlap = 20** (all train queries ∈ eval set).
- **Impact:** Post-Stage-C Recall@10 is not a signal — fine-tuned model memorizes training queries that are also in eval. Can't distinguish generalization from overfit. Violates `NEXT_SESSION_PROMPT.md:252` invariant "Не перетинається".
- **Proposed fix:** regenerate eval set from `logs/tool_calls.jsonl` doc-intent subset with query-disjoint sampling (methodology in `project_docs_production_analysis_2026_04_24.md`), OR split by query not by row.

### B3 — `prepare_train_data.py` missing + db path ambiguity (H1 + C#2 + C#3)
- **Where:** Does not exist in `scripts/runpod/`. Stage C step 1 would fail with `No such file or directory`.
- **Added risk:** two files named `knowledge.db` — `db/knowledge.db` 172 MB has data; `profiles/pay-com/knowledge.db` 0 bytes empty. Plan text is silent on which. Also 3/103 positives have `file_path` under `docs/providers/payper-new/` which are NOT in `db/knowledge.db` (repo removed/renamed).
- **Proposed fix:** write ~60-line script that loads `v12_candidates_regen_labeled_FINAL.jsonl` → filter `query_tag=="doc-intent" & label_final=="+"` → JOIN content via `db/knowledge.db` → assert resolved ≥ subset size → write JSONL with keys `{query, positive}`. Hard-code `db/knowledge.db`. Log payper-new misses but continue.

### B4 — nomic prefix tokens missing in training (H9)
- **Where:** prod inference wraps text via `search_query:`/`search_document:` prefixes (`src/models.py:60-61` + `src/index/builders/docs_vector_indexer.py:147-155`). `scripts/runpod/train_docs_embedder.py:78` passes raw `r["query"]` and `r["positive"]` unchanged.
- **Impact:** Training in unprefixed mode while serving prefixed mode = silent quality COLLAPSE. Fine-tuned model could rank WORSE than vanilla nomic-v1.5 on the same eval, and the only way you'd find out is after spending $4-5 on Stage D.
- **Proposed fix:** inside `load_pairs` (or `train()`), wrap each row as `query = f"search_query: {row['query']}"; positive = f"search_document: {row['positive']}"`. Add a test asserting prefixes applied.

### B5 — HF_TOKEN check fires AFTER `model.fit()` (H2)
- **Where:** `scripts/runpod/train_docs_embedder.py:84-98`. Training runs 100 steps first, THEN checks HF_TOKEN at push. If token missing → 10-20 min GPU (~$0.10-0.30) wasted + no local save fallback.
- **Proposed fix:** hoist HF_TOKEN check to line 1 of `train()`, before heavy imports. ~2 min.

### B6 — `ports` field must be array, not string (C#4)
- **Where:** `scripts/runpod/pod_lifecycle.py:157` sends `"ports": "22/tcp,8888/http"` (single string).
- **Confirmed via:** RunPod OpenAPI spec (https://rest.runpod.io/v1/openapi.json) — `ports` is `array<string>` format `"[port]/[protocol]"`.
- **Impact:** First real `--start` returns HTTP 400. Cost guard has already run, so you don't burn $, but Stage C fails at step 3.
- **Proposed fix:** `"ports": ["22/tcp", "8888/http"]`. ~2 min. Add one regression test.

### B7 — No pod-side idle/termination fields in POST body (H8)
- **Where:** `start_pod` body (lines 148-159) has no `idleTimeoutInMin` nor `terminationTime`. `--time-limit` is a Mac-side hint only, never transmitted to RunPod.
- **Impact:** If the Mac-side launcher crashes / SSH drops / user closes terminal, pod runs until account-level daily cap ($5) or user's account-level per-pod `Auto-terminate after: 1 hour` (set manually by user in dashboard per Stage A+B instructions). The ladder-of-defense stack currently leans entirely on the dashboard setting + Mac process health.
- **Proposed fix:** add `idleTimeoutInMin: 15` and `terminationTime: start + time_limit_min` to POST body. Verify field names via live `POST /v1/pods` 400-inspection or OpenAPI spec re-check.

---

## Non-blocking warnings (proceed, note for follow-up)

| # | H | Axis | Status | Note |
|---|---|---|---|---|
| W1 | H3 | cost | absorbed | Secure Cloud RTX 4090 likely $0.51-0.68/h vs preset $0.34/h; under $5 single-run cap for 60m Stage C. Fix before multi-hour Stage D. |
| W2 | H4 | security | dormant | JSONL has 0 test-credentials refs; knowledge.db has 0 test-credentials chunks. Scrub logic should still be BAKED INTO prepare_train_data.py regardless. |
| W3 | H5 | api | open | "NVIDIA GeForce RTX 4090" matches RunPod GraphQL displayName but NOT live-proven for REST. Risk of 400 on A100 path; RTX 4090 path ≈ safe. |
| W4 | H6 | eval | refined | No holdout in `train_docs_embedder.py` is fine — that's `prepare_train_data.py`'s job (rolls into B3). |
| W5 | H7 | plumbing | refined | SSH/scp upload path undocumented. Fix by printing `scp` command after pod is RUNNING + has publicIp. |
| W6 | W-C | env | medium | ST version drift: local 5.3.0, setup_env.sh pins `>=3.0,<4.0`. ST 3 supports `model.fit()`; 4.x+ doesn't. Pod should use what's pinned. |

---

## Excluded

- **H11** — `model.fit()` deprecated: verified via ST 5.3 source inspection; legacy API still works.

---

## Plan correctness (beyond code blockers)

Stage C's 6-step plan is shaped correctly but **missing 2 pieces**:

1. **Eval harness step omitted.** Plan goes: train → push → Stage D. Stage C per NEXT_SESSION_PROMPT.md §15 says "Report back: cost actual, time taken, did pipeline run end-to-end?" — that's a plumbing check, not a model-quality check. That's **OK for subset=10 smoke**, but the current eval leakage (B2) means even Stage D's validation would be broken. Fix B2 before Stage C so Stage D has a clean signal.
2. **Post-train download + integrate step unspecified.** Plan mentions "vtarsh/pay-com-docs-embed-v0" but not: local HF download → register in `src/models.py` → build LanceDB vectors → run `benchmark_doc_intent.py`. These exist in Stage D (steps 20-22) but should already be tested in a dry-run for Stage C to prove the full cycle.

---

## Fix matrix — suggested order

| # | Fix | Effort | Blocker | Prereq |
|---|---|---:|---|---|
| 1 | B6: `ports` → array | 2 min | B6 | — |
| 2 | B5: hoist HF_TOKEN check | 2 min | B5 | — |
| 3 | B4: add nomic prefixes | 5 min | B4 | — |
| 4 | B1: split atexit from cmd_start OR `--hold` flag | 15-30 min | B1 | — |
| 5 | B7: add idleTimeoutInMin + terminationTime | 15 min | B7 | — |
| 6 | B3: write `prepare_train_data.py` | 30-45 min | B3 | — |
| 7 | B2: regenerate `doc_intent_eval_v1.jsonl` with query-disjoint sampling | 30-60 min | B2 | — |

Total: **1.5-2.5 h** before `--start`. All reversible, all code-only, zero money spent.

---

## Handoff

User decides: apply fixes (I can implement any subset), or abandon Stage C for now. **Do NOT run `--start` until at least B1, B2, B3, B4, B6 are landed** — these are the ones that either burn $ silently or produce junk signal.
