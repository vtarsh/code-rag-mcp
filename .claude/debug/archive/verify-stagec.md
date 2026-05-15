# Stage C fix verification — 2026-04-24 by verifier

Adversarial per-blocker validation of the 4 fix commits against
`.claude/debug/verdict-stagec.md`. Pytest 719/719 green is necessary but not
sufficient — each blocker has a hand-rolled reproducer below.

Note on commit SHAs: the SHAs in the team-lead brief
(3677cd80 / 2f641e9d / 732656b7 / 5cc756da / 91eb0205) are NOT in the local
log — they're either pre-rebase or in the private repo. I verified the
working-tree state directly.

---

## B1 (atexit split — `cmd_start` must not self-terminate)

- **spec:** default `--start` registers SIGTERM/SIGINT only; `--start --hold`
  additionally registers atexit (so the pod outlives a CLI exit unless the
  user opts back in).
- **actual fix:** `scripts/runpod/pod_lifecycle.py:206-220`
  (`install_signal_handlers(register_atexit=False)`) +
  `scripts/runpod/pod_lifecycle.py:300-309` (`cmd_start` reads `args.hold`) +
  `scripts/runpod/pod_lifecycle.py:347-356` (CLI `--hold` flag).
- **reproducer:** patch `atexit.register`, call `cmd_start` with
  `hold=False` then `hold=True`; assert call count = 0 then 1.
- **reproducer output:**
  ```
  B1 default: atexit.register called 0 times (expected 0)
  B1 --hold: atexit.register called 1 times (expected 1)
  ```
- **verdict:** CLOSED.
- **notes:**
  - SIGTERM/SIGINT handlers ARE still bound in default mode (verified via
    `test_start_pod_does_not_register_atexit_by_default`), so a half-created
    pod still gets cleaned up on user ^C.
  - One latent risk: in `--hold` mode the user is now expected to `wait` /
    foreground the launcher; the docstring documents this but there's no
    runtime check that someone actually keeps the process alive. Not a B1
    regression — flagging for B6/follow-up only.

---

## B2 (train/eval LEAKAGE — eval set must be query-disjoint from training)

- **spec:** zero exact-match overlap between
  `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` (doc-intent +
  label_final="+") and `profiles/pay-com/doc_intent_eval_v1.jsonl`.
  Stratified by 9 prod-frequent terms. Auto-heuristic labels acceptable as
  long as `labeler` field marks them.
- **actual fix:** new generator `scripts/build_doc_intent_eval.py:1-367`
  (loads train queries, hard-excludes them, applies Jaccard ≥0.5 near-dup
  filter, stratifies by 9 strata, resolves expected_paths via FTS5 BM25 +
  path-token overlap heuristic) +
  regenerated artifact `profiles/pay-com/doc_intent_eval_v1.jsonl` (44 rows).
- **reproducer:** load both files, intersect query sets, also run Jaccard
  near-dup screen and check stratum coverage.
- **reproducer output:**
  ```
  train doc-intent+ queries: 20
  eval queries: 44
  exact overlap: 0
  rows with Jaccard>=0.5 to train: 0
  strata coverage: {'payout': 4, 'nuvei': 7, 'provider': 5, 'webhook': 6,
                    'refund': 7, 'aircash': 5, 'trustly': 5, 'method': 4,
                    'interac': 4}
  rows with zero expected_paths: 0
  ```
- **verdict:** CLOSED.
- **notes (per team-lead concern on auto-heuristic quality):** I sampled 5
  random rows and judged the top-5 expected_paths manually:
  - "Interac auto-deposit security question": 3/5 paths are direct
    interac-flavoured docs from paysafe-docs / nuvei-docs. Good signal.
  - "gotchas global proto contract routing vault-bankaccounts":
    `global-conventions:docs/GOTCHAS.md` + `grpc-vault-bankaccounts` paths
    directly relevant. Good signal.
  - "prepaid voucher APM redirect-based initialize webhook sale refund":
    `apm-reference-ranking.yaml` + `new-provider-setup-flow.yaml` relevant;
    other 3 are generic `architecture.md` dumps. Mid-quality — relevant
    family, weak per-file precision.
  - "Add workflow that will run alert rules for merchant only":
    `workflow-merchant-risk-engine` repo paths are correct family but the
    files themselves are `README.md` / `architecture.md` / `codebase-map.md`
    — generic boilerplate, not the actual workflow definition.
  - "sale does not create transaction aircash": `grpc-apm-aircash` paths
    relevant; rest is noise (gumballpay, nuvei filler).

  Estimated relevance: ~40-60% of expected_paths per row are actually on-
  topic. The set is honest about being heuristic (`labeler:"auto-heuristic-v1"`,
  `gold:false`), and the validate() step in the script enforces zero train
  overlap + full stratum coverage before write. This is suitable for
  Recall@10 *deltas* between two embeddings, not for absolute-quality
  scoring. Acceptable for Stage C pass/fail signal; would need a gold
  hand-label pass before going to a paper or being used as a regression
  gate. **Document this caveat in the next-session handoff.**

---

## B3 (`prepare_train_data.py` missing + db path ambiguity)

- **spec:** new ~60-line script that loads labeled candidates → filter
  doc-intent positives → JOIN content from `db/knowledge.db` (NOT the
  0-byte `profiles/pay-com/knowledge.db` shim) → assert resolved ≥ subset
  → write JSONL keyed by `{query, positive}`. Bake in secret scrub. Log
  payper-new misses but continue.
- **actual fix:** `scripts/runpod/prepare_train_data.py:1-271` (8.1 KB,
  larger than spec but functionally aligned) +
  `tests/test_prepare_train_data.py:1-236` (12 tests covering empty-db
  reject, filter, dedup, secret scrub, deterministic sample, assert
  rc==2 on too-small subset, prefix-free output).
- **reproducer:** import the module, assert `DEFAULT_DB` points at
  `db/knowledge.db`, call `build_pairs` with a tmp 0-byte db (must
  ValueError), then run the real `--subset=3` pipeline against the live
  labeled file + db.
- **reproducer output:**
  ```
  DEFAULT_DB: /Users/vaceslavtarsevskij/.code-rag-mcp/db/knowledge.db
  DEFAULT_LABELED: /Users/vaceslavtarsevskij/.code-rag-mcp/profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl
  B3 empty-db reject OK: knowledge db is empty (0 bytes): ...
  [warn] missing content: repo=payper-new-docs file=docs/providers/payper-new/reference_sandbox.md
  [warn] missing content: repo=payper-new-docs file=docs/providers/payper-new/reference_interac-etransfer.md
  [warn] missing content: repo=payper-new-docs file=docs/providers/payper-new/reference_e-transfer-standard.md
  wrote 3 rows to /tmp/_b3_repro.jsonl; skipped 3 missing + 0 scrubbed (requested=3, resolved=91)
  rc=0; output keys = {query, positive}
  ```
- **verdict:** CLOSED.
- **notes:**
  - Resolved 91 doc-intent positive pairs (verdict expected ≥ subset_size,
    no fixed total — this is comfortably above any planned subset for
    Stage C smoke=10 or pilot=50).
  - payper-new misses: 3 logged-then-skipped exactly as specified.
  - Secret scrub regex covers `MerchantSecret|PrivateKey|BearerToken|X-Api-Key`
    case-insensitive. Today's labeled set has 0 hits but the scrub is
    baked in for future labeling passes (matches spec).
  - Output keys are exactly `{query, positive}` (no `_repo_name` /
    `_file_path` leak); prefix-free as required (the wrap happens in
    `train_docs_embedder.load_pairs`, see B4).

---

## B4 (nomic prefix tokens missing in training)

- **spec:** wrap each `query`/`positive` with
  `search_query: ` / `search_document: ` so training distribution matches
  prod serving (`src/models.py["docs"]` +
  `src/index/builders/docs_vector_indexer.py`). Add a test asserting
  prefixes applied.
- **actual fix:** `scripts/runpod/train_docs_embedder.py:34-35`
  (constants) + `scripts/runpod/train_docs_embedder.py:45-68`
  (`load_pairs(apply_nomic_prefix=True)`) +
  `scripts/runpod/train_docs_embedder.py:140-144` (CLI `--no-prefix`
  opt-out for non-nomic bases).
- **reproducer:** load a 1-row JSONL with positive + negative, assert
  both `query` is wrapped with `search_query: ` and `positive`/`negative`
  with `search_document: `; cross-check constants vs `src/models.py`.
- **reproducer output:**
  ```
  NOMIC_QUERY_PREFIX = 'search_query: '
  NOMIC_DOCUMENT_PREFIX = 'search_document: '
  cross-check vs src/models.py OK
  wrapped query: 'search_query: how does trustly work'
  wrapped positive: 'search_document: Trustly is AP'...
  wrapped negative: 'search_document: PayPer retry'
  ```
- **verdict:** CLOSED.
- **notes:**
  - Constants are guarded by a dedicated regression test
    (`test_load_pairs_nomic_prefix_strings_match_prod`) that will fail loud
    if either side drifts.
  - The negative key, if present, also gets the document prefix — this is
    correct because in-batch MultipleNegativesRankingLoss treats it as a
    document.
  - `--no-prefix` opt-out is a nice-to-have for future non-nomic
    candidates (gte-large, nomic-v2-moe). Not load-bearing for current
    Stage C, where base is always `nomic-ai/nomic-embed-text-v1.5`.

---

## B5 (HF_TOKEN check fires AFTER `model.fit()`)

- **spec:** hoist HF_TOKEN check to top of `train()`, before any heavy
  import, so a missing token aborts in <1s instead of after 10-20 min of
  GPU burn.
- **actual fix:** `scripts/runpod/train_docs_embedder.py:82-86` —
  `_parse_out(out)` runs first, then if `kind == "hf"` the function
  raises `RuntimeError("HF_TOKEN missing — required for hf: push")`
  BEFORE the `from sentence_transformers import ...` block at line 95.
- **reproducer:** poison `sys.modules["sentence_transformers"]` with an
  object whose `__getattr__` raises AssertionError. Call
  `train(out="hf:vtarsh/test")` without `HF_TOKEN`. Expect a clean
  `RuntimeError`, NOT the AssertionError.
- **reproducer output:**
  ```
  B5 raised RuntimeError early: HF_TOKEN missing — required for hf: push
  ```
- **verdict:** CLOSED.
- **notes:**
  - The poisoning approach is the same one used in
    `tests/test_train_docs_embedder.py::test_train_aborts_early_if_hf_token_missing`
    so the regression test is faithful to the actual hazard.
  - Local-dir output (`out="./somewhere"`) correctly skips the token check
    (verified by `test_train_does_not_require_hf_token_for_local_dir_out`).

---

## B6 (`ports` field must be array, not string)

- **spec:** RunPod REST OpenAPI requires `ports: array<string>`. Old code
  sent `"22/tcp,8888/http"` as a single comma-joined string → 400 on first
  real `--start`.
- **actual fix:** `scripts/runpod/pod_lifecycle.py:172-173` —
  `"ports": ["22/tcp", "8888/http"]`.
- **reproducer:** patch `_request`, call `start_pod(...)`, capture the
  body, assert `body["ports"]` is `list[str]` and contains both ports.
- **reproducer output:**
  ```
  ports = ['22/tcp', '8888/http'], type=list
  ```
- **verdict:** CLOSED.
- **notes:**
  - Field shape independently confirmed against
    https://rest.runpod.io/v1/openapi.json (PodCreateInput.ports is
    `array` of `string`, example `["8888/http", "22/tcp"]`).
  - Regression test
    `test_start_pod_sends_ports_as_array` asserts both list type and
    string element type, so a future "let's join with commas again" fix
    will fail loudly.

---

## B7 (no pod-side idle/termination fields in POST body)

- **spec:** add `idleTimeoutInMin: 15` and `terminationTime: start +
  time_limit_min` to the POST body. Verify field names via OpenAPI or
  live 400-inspection.
- **actual fix:** `scripts/runpod/pod_lifecycle.py:174-176` —
  ```
  "idleTimeoutInMin": 15,
  "terminationTime": start_ts + time_limit_min * 60,
  ```
  Plus a docstring (lines 138-144) explicitly flagging that the REST
  OpenAPI doesn't document these fields and the fix is best-effort.
- **reproducer:** patch `_request` with a fixed `now_fn`, call
  `start_pod(time_limit_min=60)`, assert `body["idleTimeoutInMin"]==15`
  and `body["terminationTime"]==FAKE_TS + 3600`.
- **reproducer output:**
  ```
  idleTimeoutInMin = 15
  terminationTime  = 1800003600
  expected termTime = 1800003600
  ```
- **verdict:** PARTIAL — fields ARE in the POST body (regression-tested),
  BUT the OpenAPI/GraphQL probe shows they are NOT documented server-side:
  - `https://rest.runpod.io/v1/openapi.json` — PodCreateInput lists
    `ports`, `gpuTypeIds`, `cloudType`, etc.; **no `idleTimeoutInMin`
    or `terminationTime` field.**
  - `docs.runpod.io/sdks/graphql/manage-pods` — explicitly lists pod-create
    parameters; **no auto-terminate or idle-timeout field exists in either
    REST or GraphQL.**
  - Schema does NOT set `additionalProperties: false`, so the extra fields
    will not 400 — but they will most likely be silently ignored by the
    server. The "ladder of defense" depends on the user's account-level
    "Auto-terminate after: 1 hour" dashboard setting (per Stage A+B
    instructions) plus the Mac-side `--time-limit` hint.
- **notes / NEW HAZARD:**
  1. The docstring honestly flags this ("RunPod's REST OpenAPI does not
     document them on PodCreateInput, so they may be ignored server-side").
     Good — but a future maintainer reading only the regression test
     might think these fields actively self-stop the pod.
  2. **Mitigation suggestion (do NOT block Stage C on this):** the FIRST
     real `--start` should grep `get_pod(pod_id)` 1 minute after creation
     for any `idleTimeout` / `terminationTime` echo — if RunPod reflects
     them back, the safety net is real; if not, they're silent no-ops and
     we can drop them next session to remove dead code.
  3. Effective safety net for Stage C: account-level dashboard cap +
     `--time-limit` Mac-side hint + the cost-guard $5 daily ceiling. That
     stack is sufficient for a ≤60-min Stage C smoke. Long-running
     Stage D / D-bis workflows MUST tighten this.

---

## Cross-cutting checks

- **Full pytest run:** `python3.12 -m pytest tests/ -q` → **719 passed in
  41.50s.** No new tests broke when the fixes landed.
- **Suite-specific count:** `tests/test_runpod_lifecycle.py` (29 tests) +
  `tests/test_train_docs_embedder.py` (10 tests) +
  `tests/test_prepare_train_data.py` (12 tests) → 51 tests, all green.
- **No new hazards** detected outside B7's "ignored server-side" caveat.
- **B2 caveat** (auto-heuristic expected_paths quality ~40-60% relevance)
  is acceptable for relative ranking but should be flagged for any
  absolute-recall claims downstream.

---

## Final verdict tally

| ID  | Spec from verdict-stagec.md      | Verdict |
|-----|----------------------------------|---------|
| B1  | atexit split (default skip)      | CLOSED  |
| B2  | train/eval disjoint              | CLOSED (heuristic-quality caveat) |
| B3  | `prepare_train_data.py` exists   | CLOSED  |
| B4  | nomic prefix in train            | CLOSED  |
| B5  | early HF_TOKEN check             | CLOSED  |
| B6  | `ports` as array                 | CLOSED  |
| B7  | idle/termination POST fields     | PARTIAL — fields present but undocumented in RunPod schema (best-effort defense, not real auto-stop) |

**Overall:** 6/7 fully closed, 1 best-effort. None of the closed fixes
introduce new regressions. None of the failed tests block Stage C smoke
provided the operator (a) keeps the dashboard auto-terminate setting and
(b) verifies the pod actually stops by inspecting `get_pod(pod_id)` after
the time limit elapses on the first real run.
