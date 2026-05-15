# Verdict — docs-tower 89% stall (debate team debug-89-stall, 2026-04-24)

## Confirmed primary root cause (H11)

**Memguard instruments the wrong metric.** `_memguard.memory_pressure()` reads `psutil.Process().memory_info().rss` to classify pressure. On Apple Silicon / MPS that RSS value under-reports the actual MPS driver pool by ~3×. Torch's private MPS pool grows monotonically across `model.encode()` calls despite `gc.collect()` + `torch.mps.empty_cache()` after each batch — `empty_cache()` returns only fully-unused allocator blocks, not fragmented arenas.

### Reproducer (investigator, `/tmp/repro_mps_leak.py`)

```
after model load   mps_cur= 521.6M mps_drv=1040.4M rss= 196.5M avail=4.77G
step 0 post-encode mps_cur= 526.5M mps_drv=1336.7M rss= 277.4M avail=4.20G
step 0 post-free   mps_cur= 526.5M mps_drv=1328.7M rss= 476.5M avail=4.26G
step 5 post-encode mps_cur= 526.6M mps_drv=1634.8M rss= 495.5M avail=4.17G
step 9 post-free   mps_cur= 526.6M mps_drv=1626.8M rss= 511.8M avail=4.21G
```

- `mps_driver` 1040 → 1627 MiB (+56%) across 10 short batches; `empty_cache()` released only ~8 MiB of the 300+ MiB taken at step 0.
- `mps_current` (live allocations) flat at 526 MiB → growth is in reserved-but-unreleased arena, not live tensors.
- `psutil RSS = 512 MiB` while `driver_pool = 1627 MiB`. Memguard sees 31% of true pool.
- In second repro run, `_memguard.memory_pressure()` returned `level='ok', rss=0.03G, avail=6.08G` while torch had <0.5 GiB headroom to the 7.10 GiB MPS watermark — memguard classifies SAFE while the OOM is imminent.

### Why this matches production

Production OOM trace is byte-identical across 7 retry attempts (FACT-2): `MPS allocated: 1.17 GiB, other allocations: 4.57 GiB, max allowed: 7.10 GiB, Tried to allocate 1.70 GiB`. The 4.57 GiB "other allocations" = nomic weights (~1.1 GB) + daemon residual (~1 GB) + ~2.5 GB of retained MPS driver pool from the sorted-long tail. The next batch requests 1.70 GiB for attention activations of 4 × 1000-token inputs and blows through the cap.

## Confirmed amplifier (H4)

Lance table `db/vectors.lance.docs/chunks` grew to 49142 rows with **988 unique duplicated rowids (max 12×)**, distributed across rowid range 3209..86239 — historical buildup from today's 7-attempt retry loop. Cause: `writer_fn` in `_open_or_create_writer` calls `state["table"].add(batch_data)` with no dedup check against existing rowids. Every restart re-embeds the same rowids (40058..40982) and appends duplicates.

## Ruled out with evidence

| # | Hypothesis | Counter-evidence |
|---|---|---|
| H1 | Progressive-length amplifier (ASC-sorted longs) | At OOM point next long is 3075 chars, not near 4000 max. FACT-2 byte-identical signature → same batch retried, not progressive. |
| H3 | launchd daemon respawn competes for RAM | `launchctl list` shows daemon.plist NOT currently loaded; port 8742 empty. No mid-retry competition. |
| H5 | Sentence-transformers activation retention | FACT-2 flatness argues against — cumulative retention would grow "other allocations" across attempts, but they are identical. |
| H6 | LanceDB fragmentation as killer | 4576 versions / 1827 fragments confirmed but RSS stays 0.5-0.6G during compact — drag, not killer. |
| H7 | MPS_HIGH_WATERMARK_RATIO drift | Stable 7.10 GiB across 7 attempts in current session; 4.74 GiB in prev6 is prior-Mac-state, not intra-session drift. |
| H8 | Python heap (48k rows materialised) | <5% of 4.57 GiB "other allocations"; not critical path. |
| H9 | Jetsam SIGKILL | Valid for full_rebuild.prev.log (code tower), but current docs-tower fails via Python MPSException, not kernel kill. Distinct failure mode, out of scope for 89% docs stall. |
| H10 | `optimize_cb=None` means no compaction → Arrow blocks pin | prev3 log shows compact fired yet still OOMed later — removing optimize_cb is not causal. |
| H2 | LONG_BATCH=4 miscalibrated | Contributing factor, not sole root cause. H11 shows pool growth is primary; lowering LONG_BATCH just slows the inevitable. |

## Proposed fix (three-part mechanical patch)

**F1. Teach memguard about MPS pool (primary).** In `src/index/builders/_memguard.py::memory_pressure`, when `torch.backends.mps.is_available()`, compute:

```python
mps_pool = torch.mps.driver_allocated_memory()  # bytes
mps_watermark = 7.10 * _GIB  # from Apple default or env
effective_rss = max(rss, mps_pool)
# Scale thresholds to MPS watermark on mps devices
mps_soft = int(0.7 * mps_watermark)  # ~5.0 GiB
mps_hard = int(0.85 * mps_watermark)  # ~6.0 GiB
# Apply
```

This requires importing torch at check time (deferred import already used elsewhere in memguard). Skip when device is CPU/CUDA.

**F2. Periodic mid-phase exit (secondary).** In `_embed_and_write_streaming`, every `PREVENTIVE_EXIT_EVERY = 2000` rows processed (or 20 min elapsed, whichever first), call `_save_checkpoint(...)` then `sys.exit(0)` unconditionally. The caller's retry-loop wrapper (`/tmp/docs_restart_loop.sh` or equivalent) resumes from checkpoint. Only process restart returns MPS driver pool to the OS — `empty_cache()` is documented as unable to do this.

**F3. Dedup writer (sanitation).** In `_open_or_create_writer::writer_fn`, before `.add(batch_data)`, on the first call open the existing table (if not `force`) and collect its rowids into a set, then skip any rowid already present. Also add `CHECKPOINT_EVERY` cadence protection: the current 5000 is too coarse given per-run yield is often <1000; drop to 500.

## Reproducer commands

```bash
# Confirm H11 independently
CODE_RAG_HOME=~/.code-rag-mcp python3.12 /tmp/repro_mps_leak.py

# Confirm H4
python3 -c "
import lancedb
t = lancedb.connect('~/.code-rag-mcp/db/vectors.lance.docs'.replace('~','/Users/vaceslavtarsevskij')).open_table('chunks')
df = t.to_lance().to_table().to_pandas()
print(df['rowid'].value_counts().head(10))
print('duplicates:', df['rowid'].duplicated().sum())
print('unique:', df['rowid'].nunique(), 'total:', len(df))
"
```

## Validation plan for the fix

1. Apply F1. Re-run docs build from scratch (`--force`, fresh checkpoint). Expect memguard to fire `[hard memory pressure: mps_drv=~6.0G]` before the OOM-causing batch → clean `sys.exit(0)` → retry wrapper resumes. Completion time: ~3-4 cycles of run→exit→resume covering the long-chunk tail.
2. Apply F3 + run `dedup_orphans.py` one-off to prune 988 dup rowids. Expect table row count to drop from 49142 → 48154.
3. Apply F2 as backup — even if F1 works, F2 prevents the next surprise.
4. Regress: `pytest tests/test_memguard.py` (16 tests green) + add 2 new tests: (a) memory_pressure returns `hard` when `mps_drv > hard_threshold` even if RSS is low; (b) writer_fn skips duplicate rowids.

## Why today's 8-hour session missed this

Every fix iteration today targeted the wrong metric: lowered LONG_BATCH, removed in-loop optimize, shortened memguard sleep, added env vars. None of them touched the core issue — **memguard is blind to MPS pool**. Confirming the blind spot required a reproducer that reads `torch.mps.driver_allocated_memory()` directly; that metric was never instrumented in any of today's patches. The team found it in ~15 min by running a targeted probe script instead of iterating on the build.
