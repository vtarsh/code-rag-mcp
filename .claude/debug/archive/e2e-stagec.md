# Stage C end-to-end dry-run — 2026-04-24 by e2e

Phase-2 end-to-end runnability check. All Mac-runnable Stage C steps executed
without starting a real pod. `source ~/.runpod/credentials` used before any
pod_lifecycle / cost_guard call to export `RUNPOD_API_KEY`.

---

## Step 1: prepare_train_data.py

- cmd: `python3.12 scripts/runpod/prepare_train_data.py --subset=10 --seed=42 --out=/tmp/stagec_smoke.jsonl`
- exit: 0
- stdout: (empty — script writes status to stderr)
- stderr:
  ```
  [warn] missing content: repo=payper-new-docs file=docs/providers/payper-new/reference_sandbox.md
  [warn] missing content: repo=payper-new-docs file=docs/providers/payper-new/reference_interac-etransfer.md
  [warn] missing content: repo=payper-new-docs file=docs/providers/payper-new/reference_e-transfer-standard.md
  wrote 10 rows to /tmp/stagec_smoke.jsonl; skipped 3 missing + 0 scrubbed (requested=10, resolved=91)
  ```
- validations:
  - exit 0 ✔
  - 10 rows ✔ (`wc -l`)
  - every row has exactly `{query, positive}` ✔
  - NO nomic `search_query:` / `search_document:` prefix in prep JSONL ✔ (prep is prefix-free by design; prefixes are applied by `train_docs_embedder.load_pairs`)
  - 91 doc-intent positives available in labeled set → 10 is comfortable
  - query lens 13-53 chars, positive lens 205-4052 chars (sane)
- verdict: **PASS**
- note: 3 `payper-new-docs` files are known missing from `db/knowledge.db` chunks. Non-blocking for --subset=10, but for `--full` (Stage D) the coverage gap (3/91 ≈ 3.3%) should be logged. The warnings correctly surface what was skipped; not a regression.

---

## Step 2: secrets grep

- cmd: `grep -n "secret\|password\|token\|api_key\|Bearer\|MerchantSecret\|PrivateKey\|X-Api-Key" /tmp/stagec_smoke.jsonl`
- exit: 1 (grep: no matches — the desired outcome)
- stdout: (empty)
- stderr: (empty — tool printed "0 matches for ..." on this machine's grep wrapper, still exit 1)
- verdict: **PASS**
- note: The prep script's built-in `_SECRET_RE` scrub (MerchantSecret / PrivateKey / BearerToken / X-Api-Key, case-insensitive) acts as a first line of defense even before this grep. `skipped ... 0 scrubbed` from Step 1 confirms no rows were dropped by the prep regex either. Belt-and-braces: prep scrub + post-write grep both clean.

---

## Step 3: pod_lifecycle.py --dry-run

- cmd: `source ~/.runpod/credentials && python3.12 scripts/runpod/pod_lifecycle.py --dry-run`
- exit: 0
- stdout:
  ```
  DRY RUN — API base: https://rest.runpod.io/v1
  API key:  loaded (OK)
  API auth: OK (GET /pods returned)
  ```
- stderr: (empty)
- validations:
  - `rpa_`-prefixed API key loaded from env ✔
  - GET /v1/pods returned 200 ✔ (auth works end-to-end)
  - no mutating calls made ✔
- verdict: **PASS**

**Real pod start explicitly skipped** per task instructions. `cmd_start` path inspected: it runs `install_signal_handlers(register_atexit=<hold>)` → `start_pod()` → cost-guard → POST /pods with SECURE + idleTimeoutInMin=15 + terminationTime. Looks wired correctly, but only live-start can prove it; that belongs to Stage C execution, not this dry-run.

---

## Step 4: upload JSONL to pod

- **SKIPPED** — no live pod. Will be `runpodctl send /tmp/train_v0.jsonl` during real Stage C.
- verdict: **N/A**

---

## Step 5: train_docs_embedder.py --dry-run

- cmd: `python3.12 scripts/runpod/train_docs_embedder.py --base=nomic-ai/nomic-embed-text-v1.5 --train=/tmp/stagec_smoke.jsonl --steps=100 --out=hf:vtarsh/pay-com-docs-embed-v0 --dry-run`
- exit: 0
- stdout:
  ```
  DRY RUN: would train on 10 pairs, base=nomic-ai/nomic-embed-text-v1.5, steps=100, out_kind=hf, target=vtarsh/pay-com-docs-embed-v0, nomic_prefix=True
  ```
- stderr: (empty)
- validations:
  - exit 0 ✔
  - 10 pairs (matches Step 1 output size) ✔
  - `nomic_prefix=True` ✔ (default — matches prod serving path `search_query:` / `search_document:`)
  - `out_kind=hf`, `target=vtarsh/pay-com-docs-embed-v0` ✔
  - `load_pairs` wraps both query+positive with the prefix, in-place, for training distribution parity
- verdict: **PASS**
- note: `--dry-run` intentionally skips the `HF_TOKEN` check, which lives inside `train()`. Real Stage C will need `HF_TOKEN` exported on the pod (or passed via `--env HF_TOKEN=... --hold` when starting); otherwise `train()` raises `RuntimeError: HF_TOKEN missing — required for hf: push` BEFORE any heavy model load (fail-fast wired correctly, see train_docs_embedder.py:85-86).

---

## Step 6: cost + outcome report

- estimated cost of real Stage C: `(60/60) * 0.34 = $0.34` for 60m RTX 4090 Secure (capped at `--spending-cap=5`). Cost guard would check `today_spend + 0.34 ≤ daily_cap=$5`. On a cold account (no pods yet) `today_spend=0`, so the start is allowed.
- actual $ spent in this dry-run: **$0.00** (no pod created).

---

## Bonus validations

### B1: `pod_lifecycle.py --list` on a live account

- cmd: `source ~/.runpod/credentials && python3.12 scripts/runpod/pod_lifecycle.py --list`
- exit: 0
- stdout: (empty — account has 0 pods, expected for cold start)
- stderr: (empty)
- verdict: **PASS** — the loop over `list_pods()` is a no-op when the list is empty, so 0 pods produces clean empty output.

### B2: `cost_guard.py --check 0.50`

- cmd: `source ~/.runpod/credentials && python3.12 scripts/runpod/cost_guard.py --check 0.50`
- exit: 0
- stdout: `OK: $0.50 run within caps (daily=$5.0, single=$5.0)`
- verdict: **PASS** — 0.50 < single=5 AND (0 + 0.50) < daily=5, so allowed. Confirms real Stage C estimate of $0.34-0.68 comfortably passes.

### B3: hardcoded `NVIDIA GeForce RTX 4090` string in tests

- cmd: `grep -rn "NVIDIA GeForce RTX 4090" tests/ scripts/`
- matches: only in `scripts/runpod/pod_lifecycle.py` (the docstring and `GPU_PRESETS` — the source of truth) plus its `.pyc` cache.
- tests: zero hits. Tests do not duplicate the magic string — they reference via `GPU_PRESETS["rtx4090"]["id"]` or parametrize over the dict.
- verdict: **PASS** — no fragile string coupling.

---

## Summary

**Stage C is RUNNABLE — 4/4 Mac-executable steps PASS, 3/3 bonus checks PASS.**

- Steps 1, 2, 3, 5: PASS
- Step 4 (upload): N/A — requires live pod (expected)
- Bonus B1 (--list), B2 (cost_guard), B3 (no fragile GPU string): PASS
- Zero dollars spent.

### Gotchas surfaced that are worth documenting in NEXT_SESSION_PROMPT before a live Stage C

1. `HF_TOKEN` must be set in the pod environment BEFORE training — either via `pod_lifecycle.py --start ... --env HF_TOKEN=...` (needs a flag passthrough — not currently wired; see `cmd_start` which has no `env` CLI arg), or exported in the pod's SSH session before invoking `train_docs_embedder.py`. Fail-fast is in place in `train()` so no GPU time is wasted if missing. **Minor gap**: `cmd_start` hardcodes `env={}` in `start_pod(...)`, so the CLI cannot forward `HF_TOKEN` today. Either (a) add `--env KEY=VAL` repeatable arg to pod_lifecycle CLI, or (b) document "export HF_TOKEN on the pod over SSH, then run train_docs_embedder.py" in the runbook.
2. 3 `payper-new-docs` files missing from `db/knowledge.db` — not blocking at subset=10, but for Stage D `--full` (target ~88 rows, was 91 pre-skip) the coverage gap is ~3.3%. Verify before Stage D whether those files genuinely don't exist in the live docs repo or are a chunker regression.
3. The prep JSONL is prefix-free (correct); prefixes are applied at train load time. This asymmetry is load-bearing — if anyone edits either side without updating the other, training and serving distributions will diverge silently (the exact bug `train_docs_embedder.py:34-35` warns about).

No FAIL. No DEGRADED. Chain is wired end-to-end; only unknowns are server-side behaviors of `idleTimeoutInMin` / `terminationTime` in the POST body and actual GPU-time training throughput, neither of which can be tested without a live pod.
