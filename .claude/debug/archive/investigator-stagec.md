# Stage C readiness — independent investigation (2026-04-24, by investigator)

Scope: fresh Phase-1 read of code + docs + training data before comparing to teammate A's hypotheses. Every risk below is anchored in a concrete line of code or a measured fact (`python -c ...`) — no speculation.

## Risk ranking (severity desc)

### 1. Train/eval LEAKAGE — all 20 training queries are duplicated in the holdout eval set — severity: **CRITICAL** (BLOCKER)

- **evidence**: Measured, not inferred.

  ```
  train queries (doc-intent + label_final=+): 20
  eval  queries (doc_intent_eval_v1.jsonl):   50
  OVERLAP: 20   ← every single train query is also an eval query
  ```
  Reproducible with one-liner against `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` vs `profiles/pay-com/doc_intent_eval_v1.jsonl`.
- **impact**: Post-training Recall@10 lift is UNMEASURABLE. The fine-tuned model will look like it works (memorized positives) but you cannot tell if it generalizes. `NEXT_SESSION_PROMPT.md:252` explicitly states: "Holdout test set: 20-30 `(query, expected_path)` pairs **not perpetuating** the 103 training pairs. **Не перетинається.**" — that invariant is already violated **before** Stage C starts. All 103 regen-labeled train positives share the same 20 queries as the evaluation set.
- **fix-est**: 30 min — generate new holdout queries from `logs/tool_calls.jsonl` doc-intent subset (per the methodology in `project_docs_production_analysis_2026_04_24.md`), regenerate `doc_intent_eval_v1.jsonl` with zero query overlap, or split existing file on query granularity BEFORE spending any money on training. Without this, the $1 Stage C run produces un-interpretable numbers.

### 2. `prepare_train_data.py` does not exist AND the path it needs to resolve is broken — severity: **HIGH** (BLOCKER for step 1)

- **evidence**:
  - `ls scripts/runpod/` → `bench_large_models.py, cost_guard.py, pod_lifecycle.py, setup_env.sh, train_docs_embedder.py` — **no `prepare_train_data.py`**.
  - `memory/project_runpod_stage_ab_landed.md:36` confirms: "Open Stage A items NOT done: `prepare_train_data.py` (Stage C dependency, write before pod start)."
  - `train_docs_embedder.py:47` requires rows with keys `{query, positive}` (string content). The labeled jsonl has `file_path`, not `positive` content. So `prepare_train_data.py` must JOIN file_path → content.
- **impact**: Stage C step 1 is literally uncommitted code. `pod_lifecycle.py --start` will succeed and the meter starts running while you're still writing a 30-60 line script. At $0.34/h on RTX 4090 that's $0.05-0.30 wasted before you even upload data.
- **fix-est**: 30-45 min to write (load labeled jsonl → filter doc-intent+ → SELECT content FROM chunks WHERE file_path=? LIMIT 1 → emit `{query, positive}` subset). Low risk, but MUST land before any `--start`.

### 3. `file_path` schema of labeled data needs `db/knowledge.db`, NOT `profiles/pay-com/knowledge.db` — AND 3/103 positives don't resolve anywhere — severity: **HIGH**

- **evidence**:
  - Two files with the same name; the documented one is empty:
    ```
    profiles/pay-com/knowledge.db  → 0 bytes, no tables
    db/knowledge.db                → 172 MB, chunks table with 6 cols
    ```
    `NEXT_SESSION_PROMPT.md:156` says: "Resolved doc content через `SELECT content FROM chunks WHERE file_path=? LIMIT 1` проти snapshot knowledge.db" — doesn't clarify WHICH knowledge.db.
  - Measured resolution: **100 hit / 3 miss / 0 empty** (from 103 doc-intent positives against `db/knowledge.db`). Missing paths:
    ```
    docs/providers/payper-new/reference_sandbox.md
    docs/providers/payper-new/reference_interac-etransfer.md
    docs/providers/payper-new/reference_e-transfer-standard.md
    ```
    These all belong to `payper-new` — a repo that was either renamed, deleted, or never re-indexed after the labeled set was built.
- **impact**: If `prepare_train_data.py` is written to query the wrong db or the wrong column, it will silently emit an empty JSONL (0 rows) and the pod will fail training OR worse — succeed with no data and push an unchanged model to HF Hub. Unless the script validates post-resolution row count, this is a silent $1 burn.
- **fix-est**: 5 min — hard-code `db/knowledge.db` path, assert `len(resolved_rows) >= subset_size` before writing output. Log the 3 missing paths as warnings but continue.

### 4. `ports` field shape mismatch vs RunPod OpenAPI → `start_pod` 400s before SECURE is enforced — severity: **HIGH**

- **evidence**:
  - `pod_lifecycle.py:157`:
    ```python
    "ports": "22/tcp,8888/http",
    ```
    Single comma-separated string.
  - OpenAPI spec (fetched from https://rest.runpod.io/v1/openapi.json via Context7): `ports` is **array of strings** `"[port]/[protocol]"`. Expected: `["22/tcp", "8888/http"]`.
- **impact**: FIRST real `--start` call will return HTTP 400 from RunPod. You eat one round-trip (~$0), but the cost guard already ran, so on-paper you've "spent" up to the cap but nothing actually launched. Not financially catastrophic but would break the debate's success criterion ("pod runs end-to-end").
  Also worth: pre-flight `--dry-run` only hits `GET /pods` — it never exercises the POST body shape — so this was never caught in Stage B.
- **fix-est**: 2 min — change string to list literal, add one regression test (`test_start_pod_sends_ports_as_array`).

### 5. `gpuTypeIds` string values are unverified against actual API — severity: **MEDIUM**

- **evidence**:
  - `pod_lifecycle.py:50-52`: hardcodes `"NVIDIA GeForce RTX 4090"`, `"NVIDIA A100 80GB PCIe"`, `"NVIDIA A100-SXM4-80GB"`.
  - RunPod OpenAPI schema (via WebFetch): enum contains `"NVIDIA GeForce RTX 4090"` and `"NVIDIA H100 80GB HBM3"`, but the docs summary also references `GpuType.NVIDIA_A100_80GB_PCIe` as an enum reference. Without querying `GET /gputypes` live, it's not clear whether `"NVIDIA A100 80GB PCIe"` (space-delimited) is exactly what the REST API accepts, or whether it wants a slug like `"NVIDIA_A100_80GB_PCIe"`.
  - No test calls `GET /gputypes` to validate before use.
- **impact**: If user picks `--gpu=a100-80g` and the string is wrong, 400 response. If RTX 4090 ID is right (default) this never fires, but A100 path is untested.
- **fix-est**: 10 min — add a `verify_gpu_types_live()` utility that hits `GET /gputypes` once, cached for the process. Fail loud if a preset ID is not in the live list. Defer this fix until first A100 run; for subset run on RTX 4090, not a blocker.

### 6. No SSH/SCP upload mechanism — manual step hidden in the plan — severity: **MEDIUM**

- **evidence**:
  - `scripts/runpod/*` — grep `ssh\|scp\|rsync\|pod_host\|ssh_public\|public_ip` → **0 matches**.
  - `setup_env.sh:36` notes "Run: huggingface-cli login --token $HF_TOKEN" — presumes interactive shell on pod, implying user will manually `ssh` / `scp` the train file.
  - Task description step 4: "Upload train_v0.jsonl to pod (SSH/rsync — mechanism undefined in code)."
- **impact**: User does this by hand. Risk is: (a) typing lag during the paid hour; (b) the pod's SSH public IP/port isn't exposed by `start_pod` result — `_redact_pod_for_print` strips env but `ports` list isn't guaranteed to surface the ssh endpoint; (c) if user fat-fingers a path and uploads to wrong dir, train fails silently (script errors with FileNotFoundError → atexit stops pod → fine, but you lose 3-5 minutes of billable time iterating).
- **fix-est**: 15 min — after POST /pods returns, poll `GET /pods/{id}` until `publicIp` field populated, then print: `scp /tmp/train_v0.jsonl root@${ip}:/workspace/` as a ready-to-paste command. No automation needed — just surface the address.

### 7. sentence-transformers version drift between Mac (5.3.0) and pod (>=3.0,<4.0) — severity: **MEDIUM**

- **evidence**:
  - Local check: `pip3 show sentence-transformers` → `Version: 5.3.0`.
  - `setup_env.sh:22`: pins `"sentence-transformers>=3.0,<4.0"` on pod.
  - `train_docs_embedder.py:84` uses `model.fit(train_objectives=[(loader, loss_fn)])` — the legacy v2 API that sentence-transformers v3+ marks as **legacy**; in v4+ the recommended path is `SentenceTransformerTrainer`. The docs I fetched confirm fit() still works in v3 but is being phased out.
- **impact**: If we ever swap pin to `>=4.0`, `fit()` will emit deprecation warnings (not errors yet, per v4 release notes). Worse: the MNRL loss wrapper changed signatures across versions. If user upgrades the pod pin "because why not" the training silently does something subtly different. For the pinned `<4.0` run on Stage C this is NOT a blocker, just a footgun the moment anyone bumps the pin.
- **fix-est**: 10 min — tighten pin to `sentence-transformers==3.4.1` (exact), and add a local smoke test in `tests/test_train_docs_embedder_shape.py` that imports `losses.MultipleNegativesRankingLoss(model)` and drives 1 step without GPU.

### 8. No `_read_api_key()` redaction in exception traces — severity: **MEDIUM**

- **evidence**:
  - `pod_lifecycle.py:89-93`:
    ```python
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        raise PodLifecycleError(f"HTTP {e.code} {method} {path}: {msg[:300]}") from e
    ```
  - The URL in `url = f"{API_BASE}{path}"` doesn't contain the key (good — Bearer auth), and the 400 body shouldn't echo the key back. But an untypical 500 from a misconfigured proxy could leak Authorization headers back. `_redact_pod_for_print` only redacts `env` dict; it doesn't redact the exception string.
- **impact**: Claude Code user-visible transcripts may retain "HTTP 400 POST /pods: {authorization: Bearer rpa_...}" if RunPod ever mirrors headers in an error body. Policy violation (`feedback_runpod_api_key_hygiene.md` — API key must never enter Claude transcript).
- **fix-est**: 5 min — add `msg = msg.replace(os.environ.get("RUNPOD_API_KEY", ""), "***")` before the raise. Cheap insurance.

### 9. atexit + `SIGTERM` + re-raise after stop — teardown works only if python process is alive — severity: **MEDIUM**

- **evidence**:
  - `pod_lifecycle.py:169-190` installs atexit + SIGTERM + SIGINT handlers.
  - `signal.signal(signal.SIGTERM, _teardown)` — good. But if `kill -9` (SIGKILL) hits the python process, atexit doesn't run. If network drops mid-train, `_request` times out in 15s but the python process survives; atexit at Ctrl+C still fires.
  - One gap: `test_teardown_is_idempotent` asserts a second `_teardown()` call does nothing. Good. But `_teardown_running = True` is set BEFORE `stop_pod()` returns. If the first stop hangs (network stuck), subsequent SIGINT calls bounce off the idempotency flag → pod never stops, keep billing.
- **impact**: Edge case — network partition + manual ^C x 2 leaves pod running past auto-terminate (if user set time-limit=60m this caps damage; if they bump to 6h for full run, exposure is up to 6h × $0.89 ≈ $5.34).
- **fix-est**: 15 min — use `threading.Timer(30)` watchdog that calls `os._exit(1)` if `_teardown()` doesn't finish in 30s, then let the next process run invoke `--stop POD_ID` manually. Document this behavior.

### 10. `subset=10` covers only 7 of 20 unique queries — severity: **LOW-MEDIUM**

- **evidence**:
  - Measured with `random.seed(42); random.sample(di_pos, 10)`:
    ```
    unique queries in sample: 7
    ```
  - MultipleNegativesRankingLoss uses in-batch negatives. With batch_size=16 and only 10 rows total, you get ONE partial batch per epoch. 7 distinct queries means very little negative diversity. 100 training steps will overfit hard to those 7 queries.
- **impact**: Loss curve may look "clean" (goes down) but the pipeline test doesn't really exercise contrastive learning — it mostly tests "does the tokenizer + dataloader + push_to_hub path work?". Fine for a pipeline smoke ($1), but don't read anything from the loss number.
- **fix-est**: 5 min — sample stratified by query (e.g. 2 rows × 5 queries = 10 rows covering 5 distinct queries). Better distribution for 10-row pipeline check. Zero risk; just a sampler tweak.

### 11. Cost guard uses `/billing/pods` which returns empty for fresh accounts — `fetch_today_spend_usd` returns 0 whether billing is 0 or API is silently broken — severity: **LOW**

- **evidence**:
  - `cost_guard.py:68-83`: parses `/billing/pods` response. If `isinstance(data, list)` and empty → `rows=[]` → `total=0.0`. Same if `data={}` → empty sum → 0.0.
  - There's no "connectivity" assertion. Pre-flight `--dry-run` hit `GET /pods` (different endpoint) → doesn't prove billing endpoint works.
- **impact**: If /billing/pods has a regression (RunPod deprecates path) the guard thinks today_spend=$0 forever, and never fires the daily cap. Single-run cap still protects. Exposure: up to $daily_cap × N_runs if user runs many small jobs.
- **fix-est**: 15 min — add a sentinel "sanity check" at guard module startup: make one `_get("/billing/pods")` call, verify it returns a list (even if empty), log `[cost_guard] billing/pods shape OK (N rows today)`. Fail loud on 4xx/5xx.

### 12. `secrets scrub` is a grep-based list without profile-specific filter — severity: **LOW**

- **evidence**:
  - `NEXT_SESSION_PROMPT.md:170-174`:
    ```bash
    grep -rn "secret\|password\|token\|api_key\|Bearer \|MerchantSecret\|PrivateKey\|X-Api-Key" dataset/
    ```
  - But the actual training JSONL is `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` which contains only `{query, file_path}` pairs, not file content. Content is resolved at prep time from `db/knowledge.db`. Secrets live in the content, not the labeled index.
  - Test credentials (`profiles/pay-com/docs/references/test-credentials/*.md`) — per `NEXT_SESSION_PROMPT.md:174` "Виключити entirely з training dataset". But without `prepare_train_data.py`, the exclusion is **not coded**; user has to remember.
- **impact**: 103 paths × 1 chunk each = max 103 strings uploaded to RunPod. If even one chunk is from `test-credentials/`, real sandbox creds leak. Looking at our label paths:
  ```
  docs/docs/data-layer.md     ← safe (generic doc)
  docs/providers/nuvei/*.md   ← risk: Nuvei docs sometimes embed sample credentials in curl examples
  ```
- **fix-est**: 10 min — in `prepare_train_data.py`: (a) hard-exclude `docs/references/test-credentials/` prefix, (b) run the grep regex against the RESOLVED content (not file names), abort if hit, (c) emit count of rows excluded so user sees the filter worked.

### 13. Secure Cloud enforcement is one bool — pod_lifecycle will still pass "SECURE" to the API even if RunPod doesn't route you there — severity: **LOW**

- **evidence**:
  - `pod_lifecycle.py:136-138`:
    ```python
    if not secure_cloud:
        raise PodLifecycleError("Secure Cloud is required for pay-com data. Pass --secure-cloud.")
    ```
  - Body sends `"cloudType": "SECURE"` (line 152). **But** — if `SECURE` has no capacity in the chosen `dataCenterIds`, RunPod may fall back to COMMUNITY (depending on account settings). Our code doesn't validate `pod.cloudType == "SECURE"` after response.
- **impact**: Data residency surprise. pay-com data could land on Community Cloud if any fallback rule is enabled server-side.
- **fix-est**: 5 min — after POST returns, `assert pod.get("cloudType") == "SECURE"` — else `stop_pod(id)` + raise. Cheap invariant, zero false positives.

## Gaps A missed

(Phase 3 — read `.claude/debug/hypotheses-stagec.md`.)

**Unique contributions I add (A did not cover):**

- **Measured train/eval LEAKAGE = 20/20 (my #1)**: A's H6 raised "no holdout" plumbing-side (split logic missing in `train_docs_embedder.py`) but did NOT check whether the *existing* `doc_intent_eval_v1.jsonl` already overlaps train. I measured: every single one of the 20 train queries is in the 50-row eval set. Post-training Recall@10 on that eval set is meaningless — model will score high by memorization. **Concrete blocker, not abstract plumbing gap.**
- **`knowledge.db` path ambiguity (my #3)**: two files with the same name: `profiles/pay-com/knowledge.db` is 0 bytes; `db/knowledge.db` is 172 MB. `NEXT_SESSION_PROMPT.md:156` doesn't disambiguate. `prepare_train_data.py` could silently query the wrong one and emit empty JSONL. A's H1 noted the script is missing but didn't identify the ambiguous dependency.
- **3/103 unresolvable positives (my #3)**: measured live: `payper-new/reference_{sandbox,interac-etransfer,e-transfer-standard}.md` don't exist in current `db/knowledge.db` (repo renamed/removed). 2.9% silent data loss. A didn't enumerate this.
- **`ports` string vs array shape (my #4)**: A's H5 ("gpuTypeIds literal name may not match enum") is about `gpuTypeIds`. The concrete OpenAPI mismatch I verified is actually on **`ports`** — `pod_lifecycle.py:157` sends `"22/tcp,8888/http"` (string), API expects `["22/tcp","8888/http"]` (array). Will 400 on first `--start`.
- **sentence-transformers version drift Mac 5.3.0 vs pod pin <4.0 (my #7)**: A's H11 covers deprecation within the 3.x family. I add a related but different concern: the **local venv has v5.3.0**, so `python train_docs_embedder.py --dry-run` on Mac exercises a DIFFERENT code path than the pod. False green on local tests.
- **subset=10 + seed=42 covers only 7/20 queries (my #10)**: A's H6 notes "no holdout" but doesn't measure the query-coverage of the seed=42 sample. I computed: 7 distinct queries in the 10-row draw. If anyone believes the pipeline smoke exercised "10 unique queries" it's wrong.
- **API key exception-path redaction (my #8)**: A covers redaction nowhere. Exception strings could leak Bearer headers from RunPod 5xx.
- **`cloudType == "SECURE"` post-response check (my #13)**: A assumes setting `cloudType: "SECURE"` is sufficient. I flag that server-side fallback to Community is possible; no post-response assertion exists.

**Critical risks A caught that I missed — honest acknowledgment:**

- **H10: `--start` immediately triggers atexit → pod gets STOPPED within seconds of creation.** A read the code flow correctly: `install_signal_handlers()` at line 271, then `start_pod()` succeeds, `main()` returns, process exits → atexit fires → `_teardown()` stops the pod. This means the current `pod_lifecycle.py --start` does not actually leave a pod running for SSH to connect to. This is a first-order logic bug I missed. Should be HIGH/BLOCKER severity — without it, NONE of the Stage C SSH-upload step can happen.
- **H2: HF_TOKEN check AFTER `model.fit()`.** A identified exact line ordering in `train_docs_embedder.py:84-97` — training runs first, token validated only at push time. Wastes the entire run's GPU time if env var is missing. 5-min fix (move check to top of `train()`). I missed this completely.
- **H9: nomic prefix tokens (`search_query:` / `search_document:`) not applied at training time.** I verified `docs_vector_indexer.py:147-155` — production DOES apply `document_prefix`. But `train_docs_embedder.py:78-80` uses raw text. Training without prefix while inference has prefix → **silent quality collapse** (fine-tuned model performs worse than baseline). This is a correctness bug, not a plumbing one. Should be HIGH/CRITICAL severity. I missed this too.
- **H8: no pod-level `idleTimeoutInMin` / `terminationTime` / `spendingLimit` sent in POST body.** A is right — `--time-limit=60m` is only used in cost estimation math, never translated to a pod-side auto-terminate field. The account-level $5 cap is the only real stop. My #9 touched teardown edge cases, but A's framing is cleaner — the pod has no server-side deadline.

## Ranking disagreement

Revised after reading A. A's list has ~3 risks that deserve higher severity than mine; I had ~4 that A missed or rated vaguer. Here is the disagreement map:

- **Train/eval leakage**: I say CRITICAL (blocker, measured 20/20 overlap). A says MEDIUM (H6, "no holdout logic in train_docs_embedder"). **My ranking stands** — A framed it as a code gap; the actual bug is a data-leakage bug that's ALREADY present in existing files. You can't eval-on-training-queries even with perfect train/eval split code.
- **`prepare_train_data.py` missing**: I say HIGH (my #2), A says HIGH (H1). **Agree.** Entry gate for everything else.
- **atexit kills pod right after `--start` (A's H10)**: A says implied HIGH, I completely missed it. **A wins here — this is top-3 BLOCKER.** My investigation jumped straight to SSH/upload mechanism (#6) without realizing the pod wouldn't even be alive to receive the upload.
- **HF_TOKEN check ordering (A's H2)**: A says high, I missed it. **A wins, severity HIGH.** Wastes 10-20 min of GPU on every HF_TOKEN-less run.
- **nomic prefix tokens (A's H9)**: A says high, I missed it. **A wins, severity CRITICAL for training correctness** — not a plumbing bug, a model-quality bug. Training without prefix and serving with prefix = misaligned embeddings = fine-tuned model could be WORSE than baseline.
- **`ports` array shape (my #4)**: I say HIGH. A's H5 is adjacent but about `gpuTypeIds` not `ports`. **My finding stands** — concrete 400 trigger.
- **Secrets scrub (A's H4)**: A says HIGH (sandbox creds may leak). I measured: 0 test-credentials chunks in `db/knowledge.db` (SELECT on file_path LIKE %test-credentials%). So the specific vector A named is empirically closed. But A is right that the general scrub discipline must be coded into `prepare_train_data.py` (defense in depth). **Middle ground: MEDIUM not HIGH**, but must be coded before any upload.
- **RTX 4090 pricing underestimate (A's H3)**: A says low impact for $1 Stage C budget. **Agree — LOW.** Cost guard's caps protect regardless.
- **gpuTypeIds enum string vs slug (A's H5 / my #5)**: same finding, same severity MEDIUM. Fire only if `--gpu=a100-80g` ever used.
- **No pod-side auto-terminate fields (A's H8)**: I gave related concerns in #9 (atexit edge cases) but A's framing is sharper. **A wins framing, severity MEDIUM-HIGH.**
- **sentence-transformers version (A's H11 / my #7)**: overlapping — A covers 3.x deprecation-within-family; I cover Mac-pod drift. **Both together = MEDIUM.**

### Net revised ranking (integrating A's valid gaps)

| Rank | Risk | Source | Severity |
|------|------|--------|----------|
| 1 | Train/eval leakage — 20/20 queries overlap | my #1 | CRITICAL / BLOCKER |
| 2 | `prepare_train_data.py` missing + knowledge.db path ambiguity + 3/103 unresolvable paths | my #2/#3 (some overlap with A's H1) | HIGH / BLOCKER |
| 3 | `--start` → atexit → immediate pod stop | **A's H10** | HIGH / BLOCKER |
| 4 | nomic prefix tokens missing in training | **A's H9** | HIGH / CORRECTNESS |
| 5 | HF_TOKEN check after fit() | **A's H2** | HIGH / WASTED RUN |
| 6 | `ports` array shape mismatch | my #4 | HIGH / API 400 |
| 7 | No pod-side auto-terminate field in POST body | A's H8 | MEDIUM-HIGH |
| 8 | Subset=10 covers only 7/20 queries | my #10 | MEDIUM |
| 9 | sentence-transformers version drift + deprecation | my #7 ⊕ A's H11 | MEDIUM |
| 10 | gpuTypeIds string unverified vs live API | my #5 ⊕ A's H5 | MEDIUM |
| 11 | SSH/SCP upload mechanism undocumented | my #6 ⊕ A's H7 | MEDIUM |
| 12 | Cost guard `/billing/pods` silent failure | my #11 | LOW-MEDIUM |
| 13 | Secrets scrub not coded into prepare_train_data.py | A's H4 + my #12 | MEDIUM (A's higher rating wins) |
| 14 | API key redaction missing in exception path | my #8 | LOW-MEDIUM |
| 15 | cloudType "SECURE" post-response check | my #13 | LOW |
| 16 | RTX 4090 price hardcode possibly low | A's H3 | LOW |

**Bottom line: Stage C is NOT ready.** Ranks 1-6 are all blockers or correctness breakers, each cheap to fix individually (2-45 min), but collectively they require ~2-3 hours of code changes + a fresh eval holdout + a re-run of tests BEFORE the first `--start` call. Saving $1-2 pod cost is trivial compared to the cost of running the subset with any of these 6 unresolved (un-interpretable metrics, silent model quality collapse, or zero-op pod launches).
