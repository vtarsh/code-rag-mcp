# Independent investigation (investigator)

Evidence collected (before reading hypotheses.md):

- /tmp/docs_only.log: 7 attempts 1..7/20 in ~25 min window (22:08-22:33). Each retry reads checkpoint=40058 (82%), progresses to ~40558 (+500 rows) then OOMs with "MPS backend out of memory" — private pool tries to allocate 1.70 GiB on top of 4.2-4.7 GiB "other allocations".
- Lance table grows each retry: 42674 → 43598 → 44522 → 45446 → 46370 → 47294 → 48218 (approx +924/attempt), but checkpoint stays pinned at 40058 for entire window.
- CHECKPOINT_EVERY=5000 (docs_vector_indexer.py:49) — but loop only embeds ~500 new rows per attempt before OOM, so checkpoint never gets flushed.
- On the very first attempt (prev4.log) the loop ran much further (30k→40k+) and DID checkpoint at 35056, 40058. Then memguard SOFT→HARD path exited cleanly at 40082. Later attempts never re-trigger the memguard SOFT→HARD path — they die inside torch with a raw MPSException.
- short_rows phase is already drained — log always says "Remaining short: 0" — so 100% of the remaining 8834 rows fall into the long_rows path.
- long_rows batch size LONG_BATCH=4 for the docs tower (docs_vector_indexer.py:279), so ~2200 batches of 4-item long-embedding calls, each with `up to 4000 chars` per item. nomic-embed-text-v1.5 sequence packing → ~16k tokens per batch activation.
- Memguard is configured via env defaults: RSS_HARD=10G, SYS_AVAIL_HARD=0.8G. It watches psutil process RSS + system avail — NOT MPS allocator accounting. On Apple Silicon the MPS private pool is unified-memory but psutil reports only RSS (virtual/resident) — so a bloating MPS pool can crash before memguard sees hard pressure.
- The retry wrapper (prints "Attempt N/20 — lance: X/48892") is NOT in the tree — it's an ad-hoc shell around `build_docs_vectors.py`. So exit codes 1 (attempt 1-7) come from the script's `except Exception as exc: return 1` in build_docs_vectors.py:121-123 after MPS raises.

## Ranked by confidence:

1. **MPS private pool leak across retries inside a single process — NOT released until proc exit, and memguard can't see it** — evidence: each retry prints "other allocations: 4.28-4.66 GiB" even on attempt 1 of that process. A fresh proc should start near zero. My read: the model load alone (nomic-embed-text-v1.5 on MPS) already occupies ~4.5 GiB — that's why every attempt OOMs ~500 rows in. RSS-based memguard thresholds (8G soft / 10G hard) never trip because MPS unified memory may not all count toward psutil RSS; the process looks "healthy" to psutil but torch sees only 1-2 GiB headroom and dies. Rank #1 because it explains (a) why the failure mode is deterministic at almost the same count every attempt and (b) why memguard's SOFT→HARD escalation is not firing. **Test**: `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0 python3 build_docs_vectors.py` and observe whether stall moves past 90%; separately, print `torch.mps.current_allocated_memory()` at start-of-attempt vs peak.

2. **Long-rows batch=4 with length-sorted descending tail — the last 20% are the longest docs, activation memory scales linearly with seq-len so by rowid 40k+ we've hit the length tail** — evidence: `long_rows = sorted(long_rows, key=lambda r: len(r[1]))` sorts ASC, so the LAST batches are the longest. Memguard doesn't trip because it measures wall-clock-smoothed RSS, but a 4-item batch of 4000-char docs blows 1.7 GiB of MPS in one allocation (matches the "Tried to allocate 1.70 GiB on private pool" line). This happens at roughly the same fractional position in the sorted tail every attempt → deterministic stall location. **Test**: change `LONG_BATCH` to 1 or 2 for docs tower, or sort long_rows ascending for the last 20% and see if a full attempt completes.

3. **Checkpoint vs LanceDB drift writes duplicate rowids — subsequent searches may be merging two vectors per chunk, but that is a symptom not a cause of OOM** — evidence: checkpoint=40058 but LanceDB=48218. When attempt 8 resumes at 40058 it re-embeds rows already written by attempts 1-7 (since those wrote via streaming writer_fn but checkpoint only flushed at 40058). LanceDB has no rowid uniqueness constraint — `table.add()` on a duplicate rowid just appends. **Not a primary cause of the stall itself** (not OOM-related) but it corrupts the store every retry → ranked here because the bug hunt must account for it when proposing any "clean restart" fix. **Test**: `SELECT rowid, count(*) FROM vectors.lance.docs GROUP BY rowid HAVING count(*)>1 LIMIT 5;`

4. **Env-var defaults wrong for 16GB Mac when daemon respawns mid-run** — evidence: `CODE_RAG_EMBED_RSS_SOFT_GB=8`, `CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB=2` (memguard.py:52-55). After `/admin/shutdown`, launchd restarts daemon with CodeRankEmbed (~1 GB RSS). If launchd respawn happens mid-build, sys avail drops by ~1 GB → memguard should trip SOFT → yield 2s. But in logs we never see a SOFT-pressure line between attempts, only the direct MPS OOM. Daemon plist has KeepAlive throttle of ~10s — it *may* be reloading CodeRankEmbed while the docs build is running. **Test**: `launchctl list | grep code-rag` + check LaunchAgent plist for KeepAlive / ThrottleInterval. Also `lsof -p $(pgrep -f daemon.py)` during a build.

5. **lancedb dataset version fragmentation — each batch.add creates a new fragment; by 40k+ rows we have 10k+ fragments and `table.optimize()` inside memguard takes seconds** — evidence: log shows "[compact ok in 0.6s (rss=0.5G avail=1.9G)]" early, then "[compact ok in 2.3s …]" later in prev3.log. If the lance dir has >8k small fragments, each `writer_fn` open/add touches the manifest. Not directly an MPS issue but it slows throughput enough that psutil avail can't recover between batches. Rank lower because the OOM is the immediate cause — fragmentation is a drag, not a killer. **Test**: `ls db/vectors.lance.docs/_transactions/ | wc -l` and `ls db/vectors.lance.docs/data/ | wc -l`.

6. **Upstream library: sentence-transformers + nomic-embed-text-v1.5 has a known MPS memory retention bug — activations from one `model.encode()` call persist across calls until explicit `mps.empty_cache`** — evidence: memguard.free_memory() DOES call `torch.mps.empty_cache`, but only after `gc.collect()`. The issue: `sentence_transformers` holds references in tokenizer internal caches (e.g. `_tokenizer_state`) that gc.collect won't free because they're reachable from the model object. On MPS this leaks ~50-100 MiB per call → over 2000 long-batch calls compounds to ~1-2 GiB. Nomic v1.5 with `trust_remote_code=True` executes the repo's custom `modeling_hf_nomic.py` which may have its own caching layer. **Test**: instrument `torch.mps.current_allocated_memory()` before and after each long-batch encode; look for monotonic growth. Also try swapping to `docs-gte-large` which uses a different backbone.

7. **Python 3.12 + sentence-transformers 3.x resource-tracker leak — "There appear to be 1 leaked semaphore objects"** — evidence: this warning appears in EVERY single log file at shutdown. Usually a cosmetic PyTorch multiprocessing loader warning, but sometimes indicates a DataLoader worker that's holding GPU refs. If the encode path spins a worker per batch (likely not, batch_size small) or a semaphore for the fork, it may be keeping a file descriptor on a shared memory block that torch maps into MPS. Would explain the 4.66 GiB "other allocations" baseline. Rank lowest because it's warn-only and typically benign. **Test**: set `TOKENIZERS_PARALLELISM=false PYTORCH_MPS_WORKERS=0` and re-run; if the semaphore warning vanishes and the stall moves past 82%, this is contributing.

---

## Converged with hypotheses.md (devils-advocate evidence):

Hypothesis-builder (task #1) has NOT yet populated real hypotheses — file still
holds the Stop-hook placeholder rows (H1/H2/H3 all flagged "Deferred"). So the
only substantive prior-art I can diff against is devils-advocate's FACT-1..7.

Converged (my independent #N → devils-advocate FACT-M):
- my #1 + #2 → FACT-2 (deterministic OOM, 1.70 GiB alloc, same row every attempt)
- my #3 → FACT-1 + FACT-7 (lance/checkpoint drift, 8100 duplicate rowids, no dedup)
- my analysis of the checkpoint-never-flushes → FACT-6 (CHECKPOINT_EVERY=5000 vs ~500 rows/attempt)
- my #1 claim "memguard can't see MPS pool" → FACT-3 (no SOFT banner fires in current logs vs prev4 which DID fire)
- my #2 (LONG_BATCH=4 still insufficient) → FACT-4 (1.70 GiB allocation exceeds what batch=4 can absorb)

## missed by A (hypothesis-builder gave no real content, and devils-advocate is evidence-only — these angles are absent from hypotheses.md):

- **Upstream library angle** (required by brief). My #6 — sentence-transformers + nomic-embed-text-v1.5's `trust_remote_code=True` path loads `modeling_hf_nomic.py` at runtime; this custom module may retain activation caches independent of gc. No one else raised this. Also my #7 — the "leaked semaphore" warning appears in every log but has not been connected to the MPS retention story.
- **Orchestration contention angle**. My #4 — launchd may respawn the daemon DURING the docs build (KeepAlive throttle ~10s after /admin/shutdown), reloading CodeRankEmbed and silently eating 1 GB. Devils-advocate's evidence is purely build-process-internal and doesn't consider concurrent daemon load.
- **LanceDB fragmentation as throughput-drag**. My #5 — "[compact ok in 0.6s]" → "[compact ok in 2.3s]" is visible in prev3.log, suggests fragments accumulate; relevant because slower writes means the process holds MPS memory longer per row → compounds the MPS retention from #1.
- **Dimensional analysis devils-advocate didn't do**: FACT-2 says "other allocations: ~4.57 GiB" baseline. nomic-embed-text-v1.5 is 137M params × fp32 = 548 MiB for weights. The other ~4 GiB is activations + KV caches + tokenizer buffers. That is NOT normal for a 768d model and is the smoking gun for my #1 (MPS pool not being released between `model.encode()` calls). Devils-advocate stated the symptom; no one explained WHY 4.57 GiB sits resident.
- **Asymmetric with build_vectors.py long-batch math**: FACT-4 notes the docs tower uses LONG_BATCH=4 while scripts/build_vectors.py uses `LONG_BATCH = 8 if mcfg.dim <= 768 else 4` — but BOTH are 768d! So the docs-tower code was hand-tuned AFTER an OOM at 8. The fact that 4 still fails means the real issue is upstream of batch size. Devils-advocate cited the asymmetry but didn't draw this conclusion.

## Agreement with devils-advocate's challenges:

Devils-advocate has not yet issued challenges (H1/H2/H3 in hypotheses.md are all
Stop-hook placeholders marked "excluded — placeholder row"). There is literally
nothing to disagree with yet; challenge phase has not begun.

If/when devils-advocate challenges hypothesis-builder's eventual list, I would
push back on any framing that treats the OOM as a pure memguard tuning issue:
memguard is powerless to prevent this because it instruments `psutil.Process().memory_info().rss`
which does not include MPS unified-memory private-pool allocations. Raising
RSS_HARD_GB from 10 → 14 would not help. The fix is upstream: cap
`torch.mps.current_allocated_memory()` via `PYTORCH_MPS_HIGH_WATERMARK_RATIO`,
OR reduce LONG_BATCH to 1 for docs tower, OR switch device to CPU for the long-chunks
phase. Any "tune memguard" proposal should be excluded.

