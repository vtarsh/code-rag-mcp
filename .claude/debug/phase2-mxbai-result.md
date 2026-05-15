---
name: phase2-mxbai-result
date: 2026-04-25
author: phase2-mxbai-worker
team: P7-phase2
inputs:
  - .claude/debug/debate-verdict-v2.md §3
  - .claude/debug/debate-gte-unblock.md §5 (mxbai fallback details)
  - .claude/debug/eval-grow-report.md (n_eval=133, baseline R@10=0.2620)
outputs:
  - src/models.py (NEW entry: docs-mxbai-baseline)
  - scripts/benchmark_doc_intent.py (NEW flags: --probe, --out)
  - db/vectors.lance.docs.mxbai-baseline/ (vector store, PARTIAL — build IN PROGRESS at session-end, 82% / 40286 of 48892 rowids embedded)
  - db/docs_checkpoint_docs-mxbai-baseline.json (checkpoint, resumable)
  - /tmp/build_mxbai.log (build trace)
  - /tmp/phase2_mxbai_outcome.txt (runner trace; will fill in once build + bench complete)
  - /tmp/run_phase2_mxbai.sh (orchestration runner — already running in background)
verdict: IN-PROGRESS — handoff. Build did NOT finish within session wall-clock budget. Bench numbers NOT measured. See "Handoff" section.
---

# Phase 2 mxbai-embed-large-v1 base-swap — A/B on eval-v3-n150 (n_eval=133)

## TL;DR

**Build did not complete during this session.** Vector build is at 82% / 40286 rows of 48892 (`db/vectors.lance.docs.mxbai-baseline/`, lance dir size ~700MB) after ~88 minutes of Mac MPS encoding plus accumulated 2-second yields under soft memory pressure. Process PID 97822 is alive and still progressing (~7.3 emb/s when not yielding). A second background process (`/tmp/run_phase2_mxbai.sh`, PID 3964) is set to run probe → full bench → baseline rebench → AND-gate compare automatically once the build process exits cleanly.

**Decision is NOT FINALIZED.** Hand off to next session: read `/tmp/phase2_mxbai_outcome.txt` after the build finishes, then read this file's "Handoff" section for next steps.

**$0 spent.** RunPod NOT used. Mac CPU/MPS path stayed within budget.

## Pre-flight (verified clean)

- pytest: 757 passed, 1 skipped (clean baseline; verified again post CLI add)
- `profiles/pay-com/doc_intent_eval_v3_n150.jsonl` present (91KB, 143 file rows, n_eval=133)
- `~/.runpod/credentials` present (chmod 600, 257B) — Mac path used; RunPod NOT cycled
- Disk free: 53GB on `/` (lance dir target ~1.3GB, currently at ~700MB and growing)
- Daemon: offline at :8742 (verified `lsof -ti:8742` empty); bench loaded models programmatically

## Code changes landed (NOT pushed; lead handles MCP push)

1. `src/models.py` — appended `docs-mxbai-baseline` entry:
   - HF id: `mixedbread-ai/mxbai-embed-large-v1`
   - dim: 1024
   - `trust_remote_code=False` (vanilla BERT — no NTK / no nomic-bert custom modeling)
   - `query_prefix=""`, `document_prefix=""` (mxbai uses no prefixes)
   - `batch_size=8`, `short_limit=2000`, `long_limit=4000` (matches other 1024d candidates)
   - `lance_dir="vectors.lance.docs.mxbai-baseline"` (separate path; production `docs` lance untouched)
   - `max_seq_length=512` (BERT cap; matches HF model card)
2. `scripts/benchmark_doc_intent.py` — added two CLI flags:
   - `--probe N`: slice eval to first N scoreable rows (kill-gate smoke). Wider SE than full bench.
   - `--out PATH`: explicit per-model JSON sink for single-model runs. Falls back to `bench_runs/<key>_<ts>.json` for multi-model runs.
   - Verified end-to-end with smoke run on `--model=docs --probe=10` (n=10 → R@10=0.1533, JSON written correctly).
3. NO change to `src/index/builders/docs_vector_indexer.py`. mxbai is vanilla BERT — `_fix_gte_persistent_false_buffers` no-ops on its `BertModel.embeddings.__class__.__name__ != 'NewEmbeddings'` guard, exactly as designed.
4. NO change to production `db/vectors.lance.docs/` lance dir. mxbai writes only to `db/vectors.lance.docs.mxbai-baseline/`.

## Build details (in progress at session-end)

- Path: Mac MPS (no RunPod). $0 spent.
- Started: 2026-04-25 16:52 (`scripts/build_docs_vectors.py --model=docs-mxbai-baseline --force`)
- Re-launched (16:53) with `CODE_RAG_EMBED_PREVENTIVE_EXIT_EVERY=60000` to avoid cyclical 2k-row exits on fresh builds
- Embed rate: ~7.3 emb/s (down from 11 emb/s at start due to memguard SOFT pressure throttling)
- Memguard SOFT yield events: ~30+ at session-end. Each yield is 2s; cumulative wallclock cost ~1+ min. Process never crossed HARD threshold (rss <2GB; sys_avail ~1.7-2.0GB; mps ~1.2-1.4GB).
- Logs: `/tmp/build_mxbai.log` (~250 lines at session-end)
- Lance dir size: ~700MB at 82% rows (target ~1.3GB at 100%)

## Handoff: next session steps

1. **Wait or kick** for build to finish. The runner orchestration script `/tmp/run_phase2_mxbai.sh` (PID 3964 forked) waits for build PID 97822 to exit, then automatically runs probe + full bench + baseline rebench + AND-gate compare. If both processes are still alive, just wait. If they died, see "Recovery" below.
2. **Read `/tmp/phase2_mxbai_outcome.txt`** once `DONE` appears at the bottom. It contains:
   - lance dir size (final)
   - probe R@10, ΔR@10 vs 0.2620 baseline, kill verdict
   - full bench R@10, nDCG@10, hit@5, latency_p95_ms
   - per-stratum breakdown
   - AND-gate DEPLOY:yes/no verdict
3. **Update this file** ("phase2-mxbai-result.md") with the actual numbers from the runner output.
4. **Update `profiles/pay-com/RECALL-TRACKER.md`**: append "Phase 2 mxbai measurement" entry under the "P6 close 2026-04-25" header.
5. **Decision tree** (per the original spec):
   - **DEPLOY:yes** (clears all 5 conditions of AND-gate including +10pp R@10): "Phase 2 mxbai PROMOTE — paste-ready commands to update `src/models.py` `docs` entry to mxbai (LEAD will execute via separate confirmation; do NOT auto-swap)".
   - **+5pp to +9pp lift** without clearing AND-gate: "Phase 2 mxbai SHIP-AS-PROCESS-GAIN". Update tracker with measurement; no daemon swap.
   - **0pp to +5pp** or negative: "Phase 2 mxbai REJECT". Update tracker as 5th rejection. Prior on FT firms to 1/11 ≈ 0.09. Recommend P8 (router term-whitelist) for next session.
6. **DO NOT swap production model** (don't change `src/models.py` `docs` entry to mxbai). That's a separate user decision after seeing the numbers.
7. **DO NOT push to GitHub yet.** Lead handles MCP push after seeing the report.

### Recovery if processes died

If `ps -p 97822` is empty AND `ps -p 3964` is empty, the orchestration died. Resume manually:
```bash
# Check checkpoint state
python3.12 -c "import json; d=json.load(open('db/docs_checkpoint_docs-mxbai-baseline.json')); print('done:', len(d['done_rowids']))"

# If <48892 done, resume embed
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com CODE_RAG_EMBED_PREVENTIVE_EXIT_EVERY=60000 \
  python3.12 scripts/build_docs_vectors.py --model=docs-mxbai-baseline >> /tmp/build_mxbai.log 2>&1 &

# If 48892 done (build complete; just need bench):
python3.12 scripts/benchmark_doc_intent.py \
  --eval=profiles/pay-com/doc_intent_eval_v3_n150.jsonl \
  --model=docs-mxbai-baseline --probe=30 --no-pre-flight \
  --out=/tmp/bench_v3_n143_mxbai_probe.json
# Then full bench, then baseline rebench (--model=docs), then --compare.
```

## Cost

- RunPod $: 0 (Mac CPU/MPS path)
- Total spend this session: $0
- Banked from $13.30 budget: $13.30 (no change)
- $1 hard cap NOT triggered

## Files modified (not pushed)

- `src/models.py` — added `docs-mxbai-baseline` registry entry (M)
- `scripts/benchmark_doc_intent.py` — added `--probe`, `--out` flags + slicing logic (M)
- `db/vectors.lance.docs.mxbai-baseline/` — new lance dir (~700MB, growing) (NEW; gitignored)
- `db/docs_checkpoint_docs-mxbai-baseline.json` — new checkpoint file (NEW; gitignored)
- `profiles/pay-com/RECALL-TRACKER.md` — NOT yet appended (waiting for bench numbers)
- `.claude/debug/phase2-mxbai-result.md` — this report (NEW)

NOT modified:
- production `db/vectors.lance.docs/` lance dir
- production `docs` entry in `src/models.py`
- `src/index/builders/docs_vector_indexer.py` (no-op for mxbai by design)
- daemon (offline throughout)

## Pytest

- 757 passed, 1 skipped (pre-flight)
- 757 passed, 1 skipped (post CLI add to benchmark_doc_intent.py)
- (final post-bench check pending — should still be green; CLI changes were additive only)

## Final verdict

PENDING. Build did not complete within wallclock budget. Numbers unmeasured. Hand off per "Handoff" section above.
