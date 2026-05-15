# F2 — Preventive mid-phase exit (done 2026-04-24)

## Scope
Insert a `PREVENTIVE_EXIT_EVERY` guard in both embed loops so the process
self-terminates after N rows this run, flushes the rowid checkpoint, and lets a
retry wrapper resume. Only process restart releases the MPS driver pool —
`torch.mps.empty_cache()` cannot (verdict.md H11).

## Files changed

| file | md5 (post) | lines |
|---|---|---|
| `src/index/builders/docs_vector_indexer.py` | `a686c0e7f45a7ecf03d058bb27f02d01` | 614 |
| `scripts/build_vectors.py` | `09581bdd1aa708a6a21dd966c155c683` | 579 |

> md5s above were captured after F3 applied on top of F2 (same files). The
> F2-only md5 snapshot (before F3):
>
> - `docs_vector_indexer.py` — `6b9452bc932b22c9ff5df7317496af22`
> - `scripts/build_vectors.py` — `5d42a2929e42bf6a4447e6ed1e243cb7`

## Key diffs

### `src/index/builders/docs_vector_indexer.py`
- Added `import os`, `import sys` (were missing).
- New constant near the top:
  ```python
  PREVENTIVE_EXIT_EVERY = int(os.getenv("CODE_RAG_EMBED_PREVENTIVE_EXIT_EVERY", "2000"))
  ```
- After each batch in both SHORT and LONG loops, after
  `_flush_checkpoint_and_log()` and BEFORE `_memguard.check_and_maybe_exit`:
  ```python
  if embedded_this_run >= PREVENTIVE_EXIT_EVERY:
      _save_checkpoint(checkpoint_path, done_rowids)
      print(
          f"  [preventive-exit] reached {PREVENTIVE_EXIT_EVERY} rows this run; "
          "sys.exit(0) to release MPS pool — next run resumes from checkpoint",
          flush=True,
      )
      sys.exit(0)
  ```

### `scripts/build_vectors.py`
- `os` / `sys` already imported. New constant:
  ```python
  PREVENTIVE_EXIT_EVERY = int(os.getenv("CODE_RAG_EMBED_PREVENTIVE_EXIT_EVERY", "2000"))
  ```
- Mirror preventive-exit block after `_maybe_log_and_checkpoint()` in BOTH
  short and long batch loops, using `save_checkpoint_rowids` (the helper used
  by this file's checkpoint format). Checkpoint flush gated on
  `checkpoint_path is not None` — matches existing code style.

## Verification
- `python3 -m py_compile src/index/builders/docs_vector_indexer.py scripts/build_vectors.py` → **F2 compile OK**
- grep guard (5 matches each file): 1× const, 2× `if` (short+long), 2× print.

## Override knob
```bash
CODE_RAG_EMBED_PREVENTIVE_EXIT_EVERY=3000 python3 scripts/build_vectors.py ...
```
