# Fix-pack summary — docs-tower 89% stall (2026-04-24)

Three-part mechanical fix landed via adversarial Agent Teams debate team
(`debug-89-stall`) in 2 commits. Root cause: memguard was blind to the
MPS driver pool; see `.claude/debug/verdict.md` H11 for the reproducer
and full chain.

## Commits

| Commit | Files | Change |
|---|---|---|
| `18edef2c` | `src/index/builders/_memguard.py`, `tests/test_memguard.py` | **F1**: MPS-aware `memory_pressure()` + 2 new tests |
| `c1eb5217` | `src/index/builders/docs_vector_indexer.py`, `scripts/build_vectors.py`, `scripts/dedup_docs_lance.py` | **F2** (preventive mid-phase exit) + **F3** (writer rowid dedup + CHECKPOINT_EVERY 5000→500 for docs) + dedup helper |

## Md5 verification (local == remote)

| File | md5 |
|---|---|
| `src/index/builders/_memguard.py` | `8fb4b4614c69b6b958ef35ba9bdde9e4` |
| `tests/test_memguard.py` | `9549518c43e848538edd746e0bfc1d60` |
| `src/index/builders/docs_vector_indexer.py` | `a686c0e7f45a7ecf03d058bb27f02d01` |
| `scripts/build_vectors.py` | `09581bdd1aa708a6a21dd966c155c683` |
| `scripts/dedup_docs_lance.py` | `4cd94b4e986d633332aa95cfe8760775` |

## Tests

`python3.12 -m pytest tests/ -q` → **691 passed** in 47.76s (was 689 before F1; +2 new MPS-pool tests).

## What changed

### F1 — memguard sees MPS pool
- `_mps_driver_bytes()` lazy torch import, returns 0 when unavailable.
- `Limits` + `get_limits()` gain `mps_soft_bytes` (env `CODE_RAG_EMBED_MPS_SOFT_GB`, default 5.0) and `mps_hard_bytes` (default 6.0).
- `memory_pressure()` trips hard/soft when MPS pool crosses threshold; returns `effective_rss = max(rss, mps_bytes)`.
- `check_and_maybe_exit()` pressure banner shows `mps=<X>G` when non-zero.
- Existing tests patched with `patch.object(_memguard, "_mps_driver_bytes", return_value=0)` to prevent host pool leaking into assertions.
- New tests: `test_mps_pool_triggers_hard_even_when_rss_low`, `test_mps_pool_soft_intermediate`.

### F2 — preventive mid-phase exit
- Module-level `PREVENTIVE_EXIT_EVERY` (env `CODE_RAG_EMBED_PREVENTIVE_EXIT_EVERY`, default 2000) in both indexers.
- After each batch (short + long): if `embedded_this_run >= PREVENTIVE_EXIT_EVERY` → save checkpoint → `print("[preventive-exit] ...")` → `sys.exit(0)`.
- Caller's retry-loop wrapper resumes from checkpoint. Only process restart returns MPS pool to OS — `empty_cache()` cannot.

### F3 — writer rowid dedup + lower CHECKPOINT_EVERY
- `_open_or_create_writer` state now carries `known_rowids`. First `writer_fn` call loads existing rowids via `to_lance() → to_arrow() → to_pandas()` fallback chain. Subsequent calls filter `batch_data`, log `[writer-dedup] skipped N duplicate rowids`.
- `docs_vector_indexer.py::CHECKPOINT_EVERY` 5000 → 500 (typical per-run yield ~500 in long-chunk phase).
- New `scripts/dedup_docs_lance.py` — one-off cleanup for the 988 unique dup rowids already in the production lance. Interactive y/n. Keeps NEWEST per rowid.

## Validation next steps (manual)

1. Run `python3 scripts/dedup_docs_lance.py` against the live docs lance → expect ~988 dup rowids reported, table shrinks from 49142 to ~48154 rows after confirm.
2. Re-run `python3 scripts/build_docs_vectors.py --force` with existing retry-loop wrapper → expect either:
   - Clean completion if MPS pool stays under 5 GiB (new hard threshold via F1);
   - `[hard memory pressure: ... mps=6.0G]` or `[preventive-exit]` banner followed by `sys.exit(0)` → retry wrapper resumes → converges in 2-4 cycles.
3. pytest full suite stays at 691 passed.

## Credits

| Phase | Teammate | Work |
|---|---|---|
| Debate | `hypothesis-builder` | Silent during task #1 window; recovered later with independent 10-hypothesis list validating H11 via converged analysis |
| Debate | `devils-advocate` | FACT-1..7 raw evidence + counter-evidence for 7 excluded hypotheses |
| Debate | `investigator` | `/tmp/repro_mps_leak.py` reproducer proving H11 (MPS pool blind spot) |
| Impl | `memguard-patcher` | F1 + 2 new tests + `patch.object` hygiene for existing tests |
| Impl | `indexer-patcher` | F2 + F3 + `dedup_docs_lance.py` + fallback chain for older LanceDB versions |
| Orchestration | team-lead | Synthesis into `verdict.md`, commit split, MCP push, md5 verify |

Full verdict: `.claude/debug/verdict.md`.
