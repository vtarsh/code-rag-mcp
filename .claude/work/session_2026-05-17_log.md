# Session Log — 2026-05-17 — Eval Combinations Run

## Results Summary

| # | Combination | hit@10 | Config | Notes |
|---|-------------|--------|--------|-------|
| 1 | **EXP2 rerun 3** (benchmark) | **63.91%** | EXP2 enabled | First stable full run, 665/665 queries, 32m20s |
| 2 | **True baseline** (no EXP2) | **64.06%** | CODE_RAG_REPO_PREFILTER_BOOST=1.0 | Daemon was restarted properly. +0.15pp vs EXP2 (noise) |
| 3 | **EXP2 + camelCase** | **64.36%** | +CODE_RAG_USE_CAMELCASE_EXPAND=1 | Daemon was NOT restarted on first attempt (stale process reused). After fix: +0.30pp vs baseline (minimal) |
| 4 | **EXP2 + dict hints** | RUNNING | +CODE_RAG_USE_DICT_RERANK_HINTS=1 | Started 2026-05-17 12:42, ETA ~13:17 |

## Key Findings
- **EXP2 (repo prefilter boost)** gives negligible effect on Jira eval: +0.15pp vs baseline (64.06% vs 63.91%)
- **camelCase expansion** gives negligible effect: +0.30pp vs baseline (64.36% vs 64.06%)
- **Routing fix** (return False from _query_wants_docs) was already in effect for all runs above
- Biggest projected lift is still **docs tower disable** — not yet tested

## Issues & Fixes Applied

### Issue 1: Stale daemon between runs
**Problem:** Old daemon process persisted between eval runs, causing new evals to use old config.
- EXP2 rerun 3 → baseline: daemon NOT restarted. Baseline result = 63.91% (actually repeated EXP2)
- baseline → camelCase: daemon NOT killed properly. Eval used stale daemon.
**Fix:** Added explicit `kill -9` for daemon PID before starting new daemon.

### Issue 2: eval_jira_daemon.py silently drops errors
**Problem:** On exception, script `continue`s without recording error, but divides by total rows.
**Fix needed:** Record errors in output and compute hit rate over evaluated count only.

### Issue 3: RunPod setup failure
**Problem:** `nohup` did not persist build_vectors.py process on RunPod. Process died after SSH disconnect.
**Root cause:** Unknown — possibly process crashed before writing log.

## RunPod Connections

### Pod 1 (n2568uzm6665qa) — ACTIVE
- **Status:** RUNNING
- **IP:** 103.196.86.92
- **SSH Port:** 19991
- **Cost:** $0.69/hr
- **SSH Config:**
```
Host runpod-1
  HostName 103.196.86.92
  User root
  Port 19991
  IdentityFile ~/.ssh/runpod_key
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
```
- **Data uploaded:** knowledge.db, models, eval, code
- **Vectors generation:** FAILED — build_vectors.py did not persist
- **Action needed:** Regenerate vectors or upload pre-built archive

### Pod 2 (y7gmfg5o3n6jbx) — TERMINATED
- **Status:** TERMINATED (by us due to slow upload)
- **IP:** 213.181.111.2 (was)
- **SSH Port:** 54063 (was)

### Attempted Pods (failed creation)
- 3 pods failed with HTTP 500 on creation (RunPod API limit)

## Commands Reference

### Start daemon with config
```bash
export CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp
export ACTIVE_PROFILE=pay-com
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export CODE_RAG_IDLE_UNLOAD_SEC=0
export CODE_RAG_REPO_PREFILTER_BOOST=1.4  # or 1.0 for baseline
export CODE_RAG_USE_CAMELCASE_EXPAND=1     # optional
export CODE_RAG_USE_DICT_RERANK_HINTS=1    # optional
python3 daemon.py > logs/daemon_NAME.log 2>&1 &
```

### Run eval
```bash
python3 scripts/eval/eval_jira_daemon.py \
  --eval /Users/vaceslavtarsevskij/.code-rag-mcp/profiles/pay-com/eval/jira_eval_clean.jsonl \
  --out bench_runs/NAME.json \
  --limit 10 2>&1 | tee bench_runs/NAME.log
```

### Check daemon health
```bash
curl -s http://127.0.0.1:8742/health | python3 -m json.tool
```

## Remaining Combinations to Test
1. EXP2 + camelCase + dict (combined)
2. EXP2 + no docs tower (CODE_RAG_DISABLE_DOCS_TOWER=1) — biggest projected lift
3. All-in (EXP2 + camelCase + dict + no docs tower)

## Memory Watch
- Daemon RSS: ~850MB (normal for model)
- Eval RSS: ~15MB (HTTP client)
- System free: ~15-29% (stable)
- No memory pressure issues observed

## Time Log
- 11:26 — True baseline eval started
- 12:03 — True baseline finished: 64.06%
- 12:05 — EXP2 + camelCase eval started (stale daemon bug)
- 12:07 — Bug discovered, daemon restarted
- 12:08 — EXP2 + camelCase eval restarted (correct daemon)
- 12:37 — EXP2 + camelCase finished: 64.36%
- 12:38 — EXP2 + dict eval started (stale daemon bug AGAIN)
- 12:41 — Bug discovered, killed all daemons
- 12:42 — EXP2 + dict eval restarted (correct daemon, PID 1486)

## RunPod Update — Vectors Generation Fixed
- 12:45 — Installed `screen` on pod
- 12:46 — Started build_vectors.py in detached screen session `1114.vectors`
- Process: PID 1118, CPU 226%, running stable
- Log: /workspace/code-rag-mcp/build_vectors.log
- ETA: ~30-60 minutes for code tower vectors generation

## Next Steps
1. Wait for vectors generation on RunPod
2. Once ready: start daemon on RunPod, run 2-3 eval combinations there
3. Mac runs remaining combinations in parallel

## Eval Results Update
- 13:11 — EXP2 + dict eval finished: hit@10 = 63.76% (-0.30pp vs baseline)
- Finding: Dictionary hints HURT performance slightly
- Next: Testing EXP2 + no docs tower (biggest projected lift)

## RunPod Upload Retry
- 13:12 — Started vectors upload WITHOUT rsync compression
- File: db/vectors.lance.coderank.tar.gz (6.3GB)
- Expected time on gigabit: ~1-2 minutes

## Code Change: CODE_RAG_DISABLE_DOCS_TOWER
- Added env var check in src/search/hybrid.py line ~450
- When CODE_RAG_DISABLE_DOCS_TOWER=1, always uses code tower (model_key=None)
- Syntax verified OK

## Current Status
- 13:23 — EXP2 + no docs tower eval STARTED (daemon PID 5434)
- RunPod upload: FAILED multiple times (rsync issues, file never created on pod)
- RunPod pod n2568uzm6665qa still RUNNING ($0.69/hr)

## BREAKTHROUGH: EXP2 + no docs tower
- 13:25 — First 50 queries: 34/50 = 68.00%
- This is +3.6pp above baseline (64.06%) on first 50 queries!
- If trend holds, projected full run: ~67-68% hit@10
- This would be the biggest single improvement found so far

## Upload Status
- SCP upload started at 13:24, still in progress
- CPU usage low (1.6%), may take several minutes

## RunPod Terminated
- 13:30 — Terminated pod n2568uzm6665qa due to upload failures
- Upload methods tried: rsync with compression, rsync without compression, scp without compression
- All failed to create file on pod within 5 minutes
- Root cause: Unknown, possibly RunPod inbound bandwidth limit or large file handling issue
- Decision: Focus remaining eval on Mac only

## Current Eval Status
- EXP2 + no docs tower: 150/665 (64.67% current)
- Projected final: ~65-66% (trending upward from 62% dip)
- ETA: ~20 minutes

## New RunPod Pod: rn548uysxoa9nu
- IP: 203.57.40.127:10036
- SSH Host: runpod-2
- Setup complete: knowledge.db, models, eval, code uploaded and extracted
- build_vectors.py STARTED: PID 553, CPU 267%
- Log: /workspace/code-rag-mcp/build_vectors.log (buffered, may appear later)

## Mac Eval Status
- EXP2 + no docs tower: 200/665 (67.00%)
- Trend: climbing! Started at 68%, dipped to 62%, now back to 67%
- ETA: ~15 minutes

## EXP2 + no docs tower — EXCELLENT PROGRESS
- 300/665: 68.67% (trending UP!)
- Projection: ~67-68% final hit@10
- This would be +3-4pp above baseline — biggest improvement found!

## RunPod Issue (rn548uysxoa9nu)
- build_vectors.py crashed again immediately (no log, no vectors dir)
- Same symptom as previous pod — likely import error
- Pod still RUNNING ($0.69/hr) but not productive
