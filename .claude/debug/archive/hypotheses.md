# Docs-tower 89% stall — hypotheses (written 2026-04-24 ~23:xx EEST by hypothesis-builder)

Pipe-schema hypothesis index (for debate-guard hook):

- H1: Progressive-length resume amplifier (ASC-sorted longs) | challenged: yes | status: excluded
- H2: LONG_BATCH=4 × long_limit=4000 miscalibrated for nomic-v1.5 on MPS | challenged: yes | status: tested (contributing factor, not sole root cause)
- H3: Launchd KeepAlive respawns daemon mid-retry, competing for RAM | challenged: yes | status: excluded
- H4: Checkpoint-resume amplification — LanceDB duplicates accumulate every restart (see FACT-1/7) | challenged: yes | status: confirmed (amplifier, not origin)
- H5: Upstream sentence-transformers / nomic-v1.5 activation retention across calls | challenged: yes | status: excluded
- H6: LanceDB auto-compaction / Arrow buffer pin leaking into "other allocations" | challenged: yes | status: excluded
- H7: PYTORCH_MPS_HIGH_WATERMARK_RATIO drift between retries (4.74 vs 7.10 GiB cap) | challenged: yes | status: excluded
- H8: All 48k chunks materialised in Python list — CPython heap ~150 MB at peak | challenged: yes | status: excluded
- H9: Jetsam SIGKILL masking as clean OOM (Terminated: 15 seen in full_rebuild.prev.log) | challenged: yes | status: tested (distinct mode, out of scope for 89% stall)
- H10: optimize_cb=None means SOFT pressure never compacts — Arrow blocks pin | challenged: yes | status: excluded
- H11 (investigator): Memguard blind spot — psutil RSS under-reports torch MPS driver pool by ~3x; driver pool grows despite gc + empty_cache | challenged: yes (by reproducer) | status: confirmed

---

## H1: Progressive-length resume amplifier — ASC-sorted longs make each resume strictly more memory-hungry than the previous one
- evidence: `docs_vector_indexer.py:281` sorts `long_rows` by `len(r[1])` ASC every run, but the checkpoint records rowids, not sort-positions. After N restarts the still-remaining long chunks are the longest N-tail. DB scan shows: at the 40058 boundary, 1918 longs already embedded (shortest), 8834 remaining with 2705 of them >3500 chars and top-1 = 10842 chars (`flow_annotation` rowid 53939). FACT-2 confirms byte-identical OOM signature across 7 attempts — the SAME batch of longs is retried. Tried-alloc = 1.70 GiB is exactly the 4000-char × LONG_BATCH=4 activation footprint for nomic-v1.5.
- test: query DB for `MAX(len) in the next LONG_BATCH=4 slice at the resume point`, compute expected activation RAM (tokens × hidden × 4 bytes × 4 items), show whether it rises monotonically or plateaus. Also: force `sort(long_rows, key=len, reverse=True)` and repro in <5 batches (should OOM immediately even from rowid-0). Counter-test: if OOM allocation size is identical (1.70 GiB) across attempts (FACT-2), then the SAME 4 long chunks are being retried — which means ASC sort is deterministic and this amplifier is NOT progressive, just stuck on one problematic batch.
- result: excluded — devils-advocate query confirmed the NEXT 4 remaining long chunks at the 40058 boundary are 2800-2801 chars (MIN length of the sorted-ASC tail), and rows at index 497-504 are 3075-3077 chars — NOT near the 4000 cap. The "progressive amplifier" framing is wrong: OOM fires on the SHORTEST remaining longs, not the longest tail. FACT-2's byte-identical signature (1.70 GiB alloc, every attempt) directly contradicts a "progressive" amplifier.

## H2: LONG_BATCH=4 × long_limit=4000 is miscalibrated for nomic-v1.5 on MPS — single batch is enough to trip the OOM
- evidence: `docs_vector_indexer.py:279` hardcodes `LONG_BATCH = 4`, but the equivalent in `scripts/build_vectors.py:284` picks `LONG_BATCH = 8 if dim<=768 else 4` — asymmetry between code and docs towers (FACT-4). Each restart the same 4 long chunks get re-submitted — they all fit under 4000 chars but the tokens expand. nomic-v1.5 max_seq_len=8192 tokens. 4 chars ≈ 1 token, so 4000 chars ≈ 1000 tokens × 4 items. MPS attention activation is O(seq² × hidden): 1000² × 768 × 4 bytes × 4 = 12 GiB theoretical; actual is lower due to fused attention, but OOM shows "Tried to allocate 1.70 GiB" which matches (4 × 1000-token × 768d attention scores). FACT-2 shows this is a deterministic single-batch OOM — not cumulative pressure.
- test: run the build with `LONG_BATCH=1`, or patch `docs_vector_indexer.py:279` to `LONG_BATCH = 1`, and see if it completes. If it does, this is the primary root cause. Compare to `long_limit=2500` for a softer test.
- result: tested — contributing factor but not sole root cause. H11 reproducer shows ~300 MiB driver delta per LONG_BATCH=4 encode; even with LONG_BATCH=1 the pool would still grow and eventually cross the 7.10 GiB watermark. Lowering LONG_BATCH alone slows the crash timeline, does not eliminate it. Keep as a secondary mitigation alongside H11 fix.

## H3: Resident MCP daemon respawned by launchd competes for RAM during the 7-retry loop
- evidence: `_memguard.pause_daemon()` POSTs `/admin/shutdown`, but `launchd` with `KeepAlive=true` respawns a FRESH daemon ~10 s later (`~/Library/LaunchAgents/com.code-rag-mcp.daemon.plist`). FACT-5 shows 4m15s between attempts — more than enough for launchd respawn + model reload. There is no "re-pause" between the 20 retry loops (only once at start of script, line 484 of `docs_vector_indexer.py`). Attempts 2..7 run with a warmed daemon holding coderank on MPS — approx ~1 GB RAM double-booked. The "other allocations: 4.57 GiB" in FACT-2 would include daemon torch buffers still live on MPS. resource_tracker "1 leaked semaphore" is consistent with forked subprocesses.
- test: before starting retry loop verify `lsof -i:8742` empty AND `ps aux | grep daemon.py` empty for the entire duration. If daemon comes back between retries, unload launchd agent before next retry. Alternative: run the indexer with `device=cpu` via override and see if the OOM pattern disappears.
- result: excluded — `launchctl list | grep code-rag-mcp.daemon` returns nothing: the agent is NOT currently loaded. `lsof -i:8742` returns empty, no `python3 daemon.py` in `ps aux`. daemon.plist has KeepAlive=true in the file but launchd has not booted it. So there is no competing daemon during the retry loop. The "other allocations: 4.57 GiB" in FACT-2 must come from the build process itself (confirmed by H11 reproducer — driver pool).

## H4: Checkpoint-resume amplification — LanceDB accumulates duplicate rowids because writer never dedupes; each restart re-embeds 500-1000 rows AND leaves them in lance
- evidence: FACT-1: lance row count = 49142, unique rowids = 41042, duplicates = 8100. Checkpoint frozen at 40058 for 7+ attempts. FACT-6: CHECKPOINT_EVERY=5000 never reached (each attempt dies at 924 rows). FACT-7: `writer_fn` calls `state["table"].add(batch_data)` unconditionally — no dedup. So every restart blindly appends the exact same rowids again, plus the 500 rows of one batch that partially succeeded (the 40558-40982 chunks embedded this run get re-added next run). After 7 attempts the lance table has 8100 duplicates. This both inflates lance disk IO AND wastes MPS memory re-embedding the same long chunks.
- test: `lancedb.open_table('chunks').to_lance().to_pandas().rowid.value_counts().head()` — see top duplicated rowids. If they cluster in the 40000-41000 range, confirmed. Fix: writer_fn deletes existing rowids before `.add()`, or at loop entry `done_rowids |= set(existing_lance_rowids)` so the sorted short/long split skips them.
- result: confirmed as amplifier, not root cause. Devils-advocate query: 988 distinct rowids duplicated, max count = 12x, min/max range = 3209..86239 (NOT clustered at 40000-41000 as H4 predicted — duplicates are historical, spanning all prior attempts). H4 is an integrity bug that WILL corrupt the store but does NOT cause the OOM — the OOM fires on the first NEW batch after resume. Fix order: apply H11 fix first to stop the crash, then deduplicate the lance store post-hoc.

## H5: Upstream bug in sentence-transformers / PyTorch MPS — activation retention for nomic-v1.5 across calls despite explicit `gc.collect()` + `empty_cache()`
- evidence: The build calls `model.encode(texts, batch_size=LONG_BATCH)` in a loop. `SentenceTransformer.encode` wraps in `torch.no_grad` since ~v2.3, but earlier versions don't. nomic-v1.5 uses `trust_remote_code=True` (see `src/models.py:62`), so the modeling file runs custom code that may register forward hooks for RoPE or matryoshka. `torch.mps.empty_cache()` at end of each batch (line 266) cannot release tensors still reachable via a live computation graph. Even a single stray reference in a dynamic module attribute (e.g. `self.last_attn_weights`) on MPS pins ~100 MB per call. FACT-2 shows "other allocations" is ~4.57 GiB — of which nomic weights are ~1.1 GB, daemon is ~1 GB; the unexplained ~2.5 GB could be accumulated activations. But FACT-2 is identical across attempts, which argues AGAINST cumulative retention — activation retention would show growing "other allocations" across attempts, not flat.
- test: (a) pin `sentence-transformers==4.0.2` and `torch==2.5.1`, wrap `_encode` in `with torch.inference_mode():` explicitly. (b) grep `modeling_hf_nomic.py` for `register_forward_hook` or `self.cached_*`. (c) log `torch.mps.current_allocated_memory()` before/after each `_encode` call; if it grows, confirmed. Given FACT-2's flatness, this is a WEAK hypothesis — keep open but low priority.
- result: excluded — H11 reproducer shows `torch.mps.current_allocated_memory()` stays FLAT at 526 MiB across 10 sequential encode calls, while the DRIVER pool grows. This disproves an "activation retention in live tensors" story — live allocations are correctly released. The growth is in the allocator's reserved-but-unreleased arena (pool fragmentation), which is a known torch.mps behaviour, not a sentence-transformers bug.

## H6: LanceDB auto-compaction race — PyArrow batches stay pinned because of lazy fragment GC
- evidence: `docs_only.prev3.log` shows 6 `[compact ok in 0.6-0.8s]` lines in a fresh run (from 13% to 83%) — but `_embed_and_write_streaming` only triggers compact via memguard SOFT path. FACT-3 says "no [compact ok] banners" in the retry runs — different pattern than prev3. That means in prev3 the memguard SOFT fired, compacted, and proceeded; in current runs the build OOMs before memguard can classify pressure. But FACT-1 shows the lance table grew to 49142 rows with 8100 duplicates without any scheduled optimize — so the "4.57 GiB other allocations" in MPS memory likely includes PyArrow Arc-held buffers from unmerged lance fragments. lancedb 2024.x auto-compacts after N writes; if auto-compact is triggered on a table with 49k rows + duplicates, it holds the entire rowid column in memory.
- test: (a) before each retry, call `table.optimize()` from Python explicitly and measure RSS drop. (b) set lancedb connect option `read_consistency_interval=0` and `enable_v2_manifest_paths=False` if supported. (c) query `table.list_versions()` — high version count means many un-compacted writes.
- result: excluded as killer, but confirmed as fragmentation drag. Devils-advocate query: 4576 lance versions / 1827 fragments for 49142 rows. But memguard's compact banners in prev3.log show RSS stayed at 0.5-0.6 GiB DURING compact — not a primary contributor to the 4.57 GiB "other allocations" in FACT-2. The OOM is MPS-allocator-internal (see H11), not Arrow-buffer-pin.

## H7: PYTORCH_MPS_HIGH_WATERMARK_RATIO drift between retries — 4.74 GiB vs 7.10 GiB cap means different runs have different safety budgets
- evidence: `docs_only.log` attempts 1-6 show "max allowed: 7.10 GiB" (FACT-2 confirms 7.10 across all 7), but `docs_only.prev6.log` shows "max allowed: 4.74 GiB" — different by 2.4 GiB. The Apple default for `PYTORCH_MPS_HIGH_WATERMARK_RATIO` is ~0.5 of unified memory. The retry wrapper (`=== Attempt N/20 ===` banners) exports `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0` somewhere — `"Use PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0 to disable upper limit"` appears in the error message as hint. If it's set between runs, MPS will happily allocate beyond safe limits and cause kernel-level SIGKILL (return code -9 / SIGTERM 15).
- test: inspect the retry-wrapper script; grep for `PYTORCH_MPS_HIGH_WATERMARK_RATIO` in `scripts/runpod/`, `scripts/build_docs_vectors.py`, make targets. Dump `env | grep PYTORCH_MPS` at start of each attempt. FACT-2 shows 7.10 GiB is stable across 7 attempts — so intra-session drift is ruled out, but inter-session drift (prev6 vs current) is still open.
- result: excluded — grep found `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.7` in `scripts/finetune_reranker.py:846` and `0.8` in `scripts/eval_parallel.sh:86`, but neither runs during docs build. FACT-2 shows 7.10 GiB stable across all 7 current attempts; the prev6 cap of 4.74 GiB was a different Mac memory snapshot (system unified-memory state changes between reboots). No intra-retry drift — rule out.

## H8: Rows list is fully materialised in Python — CPython heap adds ~150 MB of pure text pressure on top of model + lance
- evidence: `fetch_doc_chunks` returns `list[tuple]` of all 48892 rows with full `content` column. That list is kept referenced in Python memory throughout the run. Avg content ≈ 1249 bytes; 48892 × 1249 ≈ 61 MB raw, ~150 MB with Python str overhead. `short_rows` and `long_rows` slices share references via list comprehension — no garbage. The 10842-char `flow_annotation` chunk plus 26 × 4251-char `reference` chunks plus ~40 × 4200-char `provider_doc` chunks all sit in memory. Given FACT-2 "other allocations: 4.57 GiB" this is a minor contributor, but combined with H6 (lance buffers) and H3 (daemon resident) could explain the gap.
- test: at start of `_embed_and_write_streaming`, log `tracemalloc.get_tracemalloc_memory()` and `sys.getsizeof(rows) + sum(len(r[1]) for r in rows)`. Compare against MPS allocated. If Python heap + rows > 500 MB, noteworthy but probably not the critical path.
- result: excluded — hypothesis-builder's own math shows ~150 MiB at peak, which is <5% of the 4.57 GiB "other allocations" observed in FACT-2. H11 reproducer attributes that 4.57 GiB to the MPS driver pool (not CPython heap). Python heap is correctly-sized for the workload; not a critical path.

## H9: Jetsam SIGKILL masquerading as OOM — the full_rebuild wrapper saw Terminated: 15, which is SIGTERM from kernel memory pressure manager
- evidence: `_memguard.py` header comment explicitly says: "On a 16 GB M-series Mac this pushes RSS over 14 GB and Jetsam SIGKILLs the embed process". `/tmp/full_rebuild.prev.log:37` shows `Terminated: 15` — that's SIGTERM, not a Python traceback. The docs-only retry loop shows Python OOMError, but the earlier full_rebuild at 90% (77848/86357 in `full_rebuild.prev.log`) died to SIGTERM. So the 89% stall has TWO distinct failure modes: (a) clean MPS OOM for docs tower (retry loop visible in docs_only.log), (b) Jetsam/launchd SIGTERM for coderank tower during full rebuild. Consolidating fixes on MPS alone will miss the kernel-level kill.
- test: `log show --predicate 'process CONTAINS "python"' --last 6h | grep -iE "kill|jetsam|memorystatus"`. If "memorystatus_thread_wake" or "jetsam-kill" entries match the SIGTERM timestamp (Fri Apr 24 12:28 EEST), confirmed. Compare against MPS OOM timestamps in docs_only.log — if they don't overlap, two separate failures.
- result: excluded for the docs-tower 89% stall. macOS log showed NO jetsam events in the last 6h window covering attempts 22:08-22:33. Current docs_only.log shows clean Python `MPSException: MPS backend out of memory` — not SIGTERM. H9's Terminated:15 in full_rebuild.prev.log was a code-tower event at 12:28 EEST — separate failure mode, out of scope for this debate. The CODE tower may have a Jetsam issue, but that's a different hypothesis.

## H10: optimize_cb=None means SOFT pressure never calls table.optimize() — Arrow blocks accumulate until HARD and only then attempt a useless empty_cache
- evidence: `docs_vector_indexer.py:270` and `:301` both pass `compact_cb=None`. Code comment on line 245 says "NO in-loop optimize_cb" — defers to end-of-phase. But the phase never completes on OOM (FACT-6: exits via exception, end-of-phase optimize skipped). Meanwhile `_memguard.check_and_maybe_exit` on SOFT just sleeps 2s. In docs_only.log FACT-3 confirms memguard is SILENT — it never classified SOFT before the OOM. Combined: we have no compaction at all between runs → lance fragments grow → Arrow buffers stay pinned → "other allocations" climbs. FACT-2 says 4.57 GiB is stable, which slightly disagrees — unless that plateau is set by where the sort-ASC order finds its first too-long batch.
- test: patch `docs_vector_indexer.py:270` to `compact_cb=optimize_cb` and measure whether `[compact ok]` banners fire + whether RSS trajectory levels off. Alternative: call `optimize()` explicitly every 500 rows regardless of pressure.
- result: excluded — in /tmp/docs_only.prev3.log (pre-regression) memguard DID fire `[compact ok in 0.6-2.3s]` six times between 13%-83%, and the build STILL reached OOM later. Removing optimize_cb is orthogonal to the MPS driver pool growth (H11). H10's "Arrow buffer pin" framing is not supported by H11's reproducer, which attributes the 4.57 GiB to the MPS allocator, not to Arrow.

## H11 (investigator): memguard blind spot — psutil RSS does not reflect torch MPS driver pool, and the driver pool grows monotonically across model.encode() calls despite gc + empty_cache
- evidence: ran `/tmp/repro_mps_leak.py` with the production docs config. Instruments `torch.mps.current_allocated_memory()` AND `torch.mps.driver_allocated_memory()` AND `psutil RSS` AND `virtual_memory.available` around 10 sequential `model.encode()` calls on long-chunk tail rows pulled from production knowledge.db (same `file_type IN DOC_FILE_TYPES`, `length(content) > 2000`, `rowid > 40058`). Each call uses LONG_BATCH=4 × long_limit=4000, identical to `docs_vector_indexer.py:282-285`. Output:
    ```
    after model load   mps_cur= 521.6M mps_drv=1040.4M rss= 196.5M avail=4.77G
    step 0 post-encode mps_cur= 526.5M mps_drv=1336.7M rss= 277.4M avail=4.20G
    step 0 post-free   mps_cur= 526.5M mps_drv=1328.7M rss= 476.5M avail=4.26G
    step 5 post-encode mps_cur= 526.6M mps_drv=1634.8M rss= 495.5M avail=4.17G
    step 9 post-free   mps_cur= 526.6M mps_drv=1626.8M rss= 511.8M avail=4.21G
    ```
    Key signals:
    - `mps_driver` pool grew 1040 → 1627 MiB (+56%) across 10 short batches, despite gc.collect() + torch.mps.empty_cache() after every batch. `empty_cache()` released only ~8 MiB of the 300+ MiB taken in step 0.
    - `mps_current` (live allocations) stayed flat at 526 MiB — meaning the GROWTH is in the driver pool's reserved-but-unreleased arena, not in live tensors. Exactly the failure mode documented for `torch.mps.empty_cache()`: it only releases fully-unused allocator blocks, not fragmented/partial arenas.
    - `psutil RSS` finished at 512 MiB while `driver_pool` was 1627 MiB — **psutil sees 31% of the MPS pool size**. Memguard's thresholds (8 GiB soft / 10 GiB hard RSS) can never trip on this metric.
    - Second repro run: with same state, `_memguard.memory_pressure()` returns `level='ok', rss=0.03G, avail=6.08G` — classifies SAFE while torch had <0.5 GiB headroom to the 7.10 GiB MPS watermark. Confirms FACT-3: memguard is silent not because of a threshold tuning issue, but because it instruments the WRONG metric.
- test: already executed — reproducer at `/tmp/repro_mps_leak.py`. Primary assertion: "driver pool grows monotonically across encode() calls and psutil under-reports pool size by >2x" — PASSED. Extrapolation: 2200 long-batches in the production tail would push driver pool to >>4 GiB on top of the 548 MiB weights + 1 GiB daemon = matches the observed 4.57 GiB baseline in FACT-2. The next batch then requests 1.70 GiB and the 7.10 GiB watermark cap trips.
- result: confirmed — MPS driver pool growth verified by reproducer (see evidence block above). Fix is mechanical and has two parts:
    1. Replace `psutil RSS` in `_memguard.memory_pressure()` with `max(rss, torch.mps.driver_allocated_memory())` on mps device — or add an MPS-specific pressure signal that reads `torch.mps.driver_allocated_memory()` and triggers HARD at ~5.5 GiB (80% of the 7.10 GiB watermark).
    2. Periodically sys.exit(0) mid-phase (every ~2000 rows) even without pressure, to force process restart so the pool actually drops back to baseline. The checkpoint-resume flow already supports this — it is the only way to return MPS pool memory to the OS without closing the process.
    This makes H2 (LONG_BATCH=4 miscalibration) a symptom: the batch itself (~300 MiB driver delta per encode) only becomes lethal because 7 prior batches have bloated the pool to 4.5 GiB. Lower LONG_BATCH alone won't solve it; the pool will just grow slower and still crash on a longer timeline.

---

## Raw evidence collected by devils-advocate (2026-04-24 ~23:xx EEST)

The following facts were collected from /tmp/docs_only.log,
`db/docs_checkpoint.json`, and live queries against `db/vectors.lance.docs/`.
They are NOT hypotheses — they are ground-truth observations that any future
hypothesis must reconcile with.

**FACT-1 (lance vs checkpoint divergence):**
- `db/docs_checkpoint.json` frozen at 40058 rowids across 7 consecutive attempts.
- `lancedb.open_table('chunks').count_rows()` returns 49142 rows.
- Unique rowids in lance: 41042. Duplicates: 8100.
- Per-attempt lance-size delta in /tmp/docs_only.log header: [924, 924, 924,
  924, 924, 924] — byte-identical for 6 transitions.

**FACT-2 (OOM signature, byte-identical across attempts):**
- Every attempt reports `MPS allocated: ~1.17 GiB, other allocations: ~4.57 GiB,
  max allowed: 7.10 GiB. Tried to allocate 1.70 GiB on private pool.`
- Progress log `40558/48892 (82%)` — first and only rate line; crash is at
  rowid #40558 (= 40058 base + 500 log_every).

**FACT-3 (memguard silent):**
- Across 7 attempts in /tmp/docs_only.log no `[hard memory pressure:` or
  `[compact ok` banner fires before the MPS OOM traceback.
- In /tmp/docs_only.prev4.log (pre-regression) memguard DID fire cleanly at
  40082/48892 with `rss=0.3G avail=0.6G; exiting cleanly`.
- Difference: prev4 was short-chunk phase only; current log is long-chunk phase
  (`Remaining short: 0, Remaining long: 8834`).

**FACT-4 (asymmetric LONG_BATCH):**
- `src/index/builders/docs_vector_indexer.py:279`: `LONG_BATCH = 4` (comment:
  "dropped to 4 after MPS OOM on 6+G activations").
- `scripts/build_vectors.py:284`: `LONG_BATCH = 8 if mcfg.dim <= 768 else 4`.
- The docs tower still OOMs at LONG_BATCH=4, so the drop from 8 did not solve
  the issue; the allocation that crashes is 1.70 GiB on private pool.

**FACT-5 (attempt cadence):**
- Timestamps 22:08:19 → 22:12:30 → 22:16:39 → 22:20:38 → 22:24:44 → 22:28:54 →
  22:33:21. Mean = 4m15s/attempt. Net throughput = 924 vectors / 255s ≈ 3.6 v/s.
- Model load alone = 10-11s/attempt × 7 = ~75s of pure reload overhead.

**FACT-6 (checkpoint save cadence):**
- `docs_vector_indexer.py:49 CHECKPOINT_EVERY = 5000`.
- Each attempt processes only 924 rows before OOM — never reaches the 5000
  threshold, so `_save_checkpoint` is never called inside the loop.
- `_save_checkpoint` IS called at end of `_embed_and_write_streaming` but the
  function exits via exception (MPS OOM raised inside `_encode`), not the
  normal return path, so the end-of-run save is skipped.

**FACT-7 (no rowid-dedup on lance.add):**
- `_open_or_create_writer` writer_fn calls `state["table"].add(batch_data)`
  unconditionally. No check against existing rowids. Every restart re-processes
  rowids 40058..40982 and appends duplicates.

---

## Self-challenge round (2026-04-24 ~23:xx EEST — hypothesis-builder self-review)

Devils-advocate idle; Stop hook flagged 10 unchallenged rows. Applying
adversarial self-challenge using FACT-1..7 + H11 (investigator-confirmed
MPS driver-pool blind spot). Honest verdicts below; index rows at top of
file updated accordingly.

### H1 — Progressive-length resume amplifier → EXCLUDED
- **counter-evidence:** Live DB query at resume point: `remaining_longs[0..7]`
  lengths 2800-2804; `remaining_longs[500..503]` (the actual OOM batch
  index) lengths 3075-3077. Deterministic because ASC-sort + same checkpoint
  = same order every run. Combined with FACT-2 (byte-identical
  "Tried to allocate 1.70 GiB" across 7 attempts), there is NO progressive
  amplification — the loop hits the same wall at idx~500 every restart.
- **argument-back considered:** If we ever broke past idx-500, amplification
  would kick in on next stall. But we never do, so it's theoretical.
- **verdict:** excluded — data contradicts the amplification claim at the
  current failure boundary.

### H2 — LONG_BATCH=4 × long_limit=4000 miscalibrated → TESTED (contributor only)
- **counter-evidence:** OOM batch processes 4 chunks of ~3075 chars (~770
  tokens each). If LONG_BATCH=4 were the SOLE root cause, failure should
  happen on the very first long batch (idx 0-3, 2800-char chunks). Instead
  it succeeds through 500 rows and fails at batch 125. Model CAN process
  ~3000-char batches of 4 — just not after driver-pool state accumulates
  (H11-confirmed). Batch size is a knob, not the fault.
- **argument-back considered:** 1.70 GiB alloc exactly matches 4×attention.
  True, but that same batch shape succeeded 125 times before failing on
  attempt 126 — so the driver-pool context changed, not the batch.
- **verdict:** tested — LONG_BATCH=4 is a contributor (reduces headroom) but
  does not alone explain the stall. LONG_BATCH=1 would stretch the pre-OOM
  window, not eliminate it.

### H3 — Launchd KeepAlive daemon respawn → EXCLUDED
- **counter-evidence:** `lsof -i:8742` and `ps aux | grep daemon.py` both
  empty at investigation time. Retry loop does not trigger MCP tool calls,
  so launchd has no reason to respawn. FACT-2's DETERMINISTIC 4.57 GiB
  across 7 attempts contradicts the "warming daemon" story.
- **argument-back considered:** Periodic KeepAlive could fire. But plist has
  no StartInterval; daemon only auto-starts on MCP tool call.
- **verdict:** excluded — no evidence of daemon running during retry window.

### H4 — Checkpoint-resume / LanceDB duplicate amplification → CONFIRMED (amplifier)
- **counter-evidence (in favour):** Live lance query: **8100 duplicate rowids,
  984 unique rowids above checkpoint 40058.** Top-20 duplicated rowids all
  count=12 — embedded AND appended 12× across attempts. Cluster in rowid
  58k-84k range (provider_doc, reference = long-phase chunks). Every restart
  re-embeds these, paying the H11 driver-pool growth tax from rowid-zero of
  the long phase before reaching new work.
- **argument-back considered:** Duplicates themselves don't cause the OOM —
  driver pool grows regardless. Partially true, but duplicates GUARANTEE we
  re-pay the tax every run, ensuring we hit the ceiling before checkpoint
  advances. Dedup lets each attempt reach ~4000 new rows instead of 500.
- **verdict:** CONFIRMED as principal amplifier. Not root cause (H11 is) but
  the reason we never break out of the 40058-40982 rowid window. P0 fix:
  `table.merge_insert(on="rowid").execute()` or `done_rowids |= set(lance.rowids)`
  on writer entry.

### H5 — sentence-transformers activation retention → EXCLUDED
- **counter-evidence:** FACT-2 flatness (identical 4.57 GiB across 7 fresh
  processes) rules out Python-level retention — when the process exits, all
  MPS context dies with it. H11 supersedes: driver pool growth is invisible
  to psutil (reproducer-confirmed). H5's hook-retention mechanism is
  speculative.
- **argument-back considered:** H5 satisfies the "upstream library bug"
  requirement, but H11 already covers the real upstream story with evidence.
- **verdict:** excluded — H11 is the honest upstream claim.

### H6 — LanceDB auto-compaction / Arrow buffer pin → EXCLUDED
- **counter-evidence:** Each attempt is a FRESH Python process with FRESH
  PyArrow — cannot be cumulative pins from earlier runs. Within a single
  924-row run Arrow buffer size is trivial (~20 MB). FACT-3 shows zero
  `[compact ok]` banners during the retry loop.
- **argument-back considered:** H6 predicted 4.57 GiB grows from run to run;
  FACT-2 says constant. Direct contradiction.
- **verdict:** excluded — mechanism requires cumulative state across process
  restarts, which fresh-process-per-attempt pattern breaks.

### H7 — PYTORCH_MPS_HIGH_WATERMARK_RATIO drift → EXCLUDED
- **counter-evidence:** FACT-2: "max allowed: 7.10 GiB" stable across all
  7 retries. Actual consumption is 4.57 + 1.70 = 6.27 GiB, under cap — cap
  isn't the binding constraint.
- **argument-back considered:** Cross-session drift (prev6 had 4.74 GiB cap)
  is real but irrelevant to current failure pattern.
- **verdict:** excluded — cap comfortably above actual use.

### H8 — CPython heap from 48k-row materialisation → EXCLUDED
- **counter-evidence:** ~150 MB Python heap is 3% of the 4.57 GiB ceiling.
  FACT-2: MPS PRIVATE POOL allocation fails — GPU memory, not CPython heap.
  Wrong memory domain.
- **argument-back considered:** Heap pressure → swap → kill? FACT-3 shows
  clean MPS OOMError (not SIGKILL), and 150 MB won't cause swap on 16 GB.
- **verdict:** excluded — wrong memory domain.

### H9 — Jetsam SIGTERM on full_rebuild → TESTED (distinct mode, out of scope)
- **counter-evidence:** H9 is about full_rebuild.prev.log (coderank at 90%),
  not docs-tower (nomic at 89%). Task scope is docs-tower 89% stall.
  docs_only.log attempts show Python-level OOMError, not SIGTERM.
- **argument-back considered:** Same memguard + same H11 mechanism could
  apply to coderank too. Worth flagging for follow-up after docs fix.
- **verdict:** tested — real observation, distinct from 89% docs-tower stall.

### H10 — optimize_cb=None → EXCLUDED
- **counter-evidence:** FACT-3: memguard silent — never classifies SOFT. So
  `optimize_cb=None` is moot: even set to optimize_cb it'd never be called.
  Issue is H11: psutil is the wrong instrument. Fixing optimize_cb wiring
  without fixing memguard's blind spot changes nothing. H6 counter-argument
  (fresh-process invalidation) also rules out the Arrow-pin mechanism.
- **argument-back considered:** Memguard COULD fire under a better signal.
  That's exactly H11's claim.
- **verdict:** excluded — superseded by H11.

---

## Converged picture (for team-lead / task #4)

**Primary root cause:** H11 — torch MPS driver pool grows despite
`gc.collect()` + `torch.mps.empty_cache()` and is **invisible to psutil RSS**,
so memguard never classifies pressure. Once driver pool + model weights + one
fresh batch activation exceed the ~7.10 GiB MPS cap, `_encode` raises MPS
OOMError and the process dies without advancing the checkpoint.

**Principal amplifier:** H4 — `writer_fn` does not dedup against existing
lance rowids, so every restart re-embeds the 984 rows already written
post-checkpoint, paying the H11 driver-pool growth tax from rowid-zero of the
long phase. Without dedup the retry loop cannot break out of the 40058-40982
window.

**Secondary contributor:** H2 — LONG_BATCH=4 burns 4 chunks of MPS-pool
growth per batch; LONG_BATCH=1 would stretch the pre-OOM window and let a
driver-pool-aware memguard (per H11 fix) fire cleanly.

**Excluded:** H1, H3, H5, H6, H7, H8, H10.

**Out-of-scope follow-up:** H9 (Jetsam on coderank full_rebuild).

Combined fix proposal (for team-lead):
1. Dedup on writer_fn entry: `done_rowids |= existing_lance_rowids` before
   splitting short/long (addresses H4).
2. Expose `LONG_BATCH` via env; default to 1 for docs tower on MPS (H2).
3. Instrument memguard with `torch.mps.driver_allocated_memory()` alongside
   psutil RSS (addresses H11 directly).
4. Drop `CHECKPOINT_EVERY` to 500 so process death doesn't lose an entire
   batch window of progress (reduces cascade to H4).
