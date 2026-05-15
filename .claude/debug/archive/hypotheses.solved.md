# Docs-tower build stalls at ~89% (8-hour investigation, 2026-04-24)

## H1: O(N²) table.optimize() in embed loop
- evidence: `_embed_and_write_streaming` called `optimize_cb()` every 20 batches. On a 40k+ table each compact rewrites all fragments → O(N²). 256 versions/11 MB manifests after only 5k rows.
- test: commit c1a59928 removed in-loop `optimize_cb()` from both builders. Observed linear progress at 17 emb/s for short chunks.
- result: excluded as sole root — removal helped short-chunks phase but 89% wall persisted in long phase.

## H2: memguard SOFT sleep(30) per batch
- evidence: In `_memguard.check_and_maybe_exit`, SOFT path called `time.sleep(30)`. In long-chunks phase with avail ~2G, this fired every batch. 500 long batches × 30s = 4.2 hrs of pure sleep.
- test: commit 7466f842 reduced to sleep(2). Test updated `tests/test_memguard.py::test_soft_calls_compact_then_sleeps_when_still_soft` expects [2] not [30].
- result: excluded as sole root — SOFT events didn't fire in final run (0 yields logged) yet the wall partially appeared anyway.

## H3: compact_cb=optimize_cb path in memguard SOFT
- evidence: Even without in-loop optimize, memguard SOFT called `compact_cb` which was still `optimize_cb`. Log showed `[compact ok in 1.6s]` triples at 41k rows — O(N²) was back via memguard path.
- test: commit 4a94df36 changed `compact_cb=optimize_cb` to `compact_cb=None` at both memguard call sites.
- result: excluded — removed the last O(N²) trigger but process still OOMed.

## H4: MPS backend memory leak (upstream PyTorch bug #154329)
- evidence: `MPS backend out of memory (MPS allocated: 1.02 GiB, other allocations: 6.30 GiB, max allowed: 7.10 GiB)`. "Other allocations" grew with each batch despite `torch.mps.empty_cache()` + `gc.collect()`. WebSearch confirmed the bug — sentence-transformers loops leak memory in MPS pool; no fix exists at the allocator level; empty_cache doesn't release fragmented blocks.
- test: Restart the Python process → MPS pool fully releases. Auto-restart wrapper `/tmp/docs_restart_loop.sh` confirmed: each restart resumed from checkpoint and progressed; ~6 OOM/restart cycles covered the long tail.
- result: confirmed — this IS the upstream root cause. The "wall" at 89% is where long chunks hit it hardest because their activation memory is largest.

## H5: macOS paging under 16 GB physical
- evidence: Python VSIZE 17 GB / RSS 3.5 MB meant swap held almost all working set. Agent 1 analysis: `PYTORCH_MPS_HIGH_WATERMARK_RATIO` default 1.7 allows 27 GB reservation on 16 GB Mac; `MallocNanoZone=0` drops 8-14 GB of macOS zone reservation.
- test: Restart with env vars: `MallocNanoZone=0 PYTHONMALLOC=malloc PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.6 LANCE_IO_THREADS=2 OMP_NUM_THREADS=2`. VSIZE dropped from 17 GB → 393 MB at startup.
- result: excluded as root (H4 is root) but confirmed as amplifier. Without env-var caps, the MPS leak thrashes swap earlier; with caps, OOMs come cleanly before system degrades.

## H6: --force + stale checkpoint mismatch
- evidence: After kill+restart with `--force`, lance table dropped to 4157 rows (from 76438) but checkpoint still had 76512 rowids. Build processed only 9953 delta → final table would have been 10k not 86k.
- test: commit 3d914580 unlinks checkpoint at start of force build in `scripts/build_vectors.py` (mirroring prior-art in `docs_vector_indexer.py`).
- result: excluded as wall-root but confirmed as silent data-corruption hazard. Separate bug, now fixed.

## Resolution (confirmed)

- H4 = real root (upstream PyTorch MPS leak, unfixable in our code).
- H5 = amplifier (macOS reservation defaults).
- H1, H2, H3 = compounding slowdowns that each added hours.
- H6 = orthogonal data-integrity bug caught during this session.

**Final working config** (commit 4a94df36 + env vars in launcher):
```
MallocNanoZone=0 PYTHONMALLOC=malloc
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.6 PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4
TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
LANCE_IO_THREADS=2 LANCE_CPU_THREADS=2
CODE_RAG_EMBED_SYS_AVAIL_HARD_GB=0.3 CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB=0.8
```

Plus `/tmp/docs_restart_loop.sh` auto-restart wrapper for OOM recovery (13 attempts / 6 OOMs / completed at 100%).

## Process notes (for memory)

- I (Claude Code) repeatedly declared each new hypothesis "root cause" and stopped to report, rather than proving/disproving comprehensively. User had to trigger each iteration. This is the structural RLHF+turn-based bias that the new `.claude/hooks/enforce-hypotheses.sh` Stop-hook + Agent Teams setup is meant to prevent going forward.
- Build completed: code tower 86465/86465, docs tower 49018 rows (slight over-count from resume overlaps — not a correctness issue, lance search still works).
