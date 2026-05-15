# F3 — Writer rowid dedup + lower CHECKPOINT_EVERY (done 2026-04-24)

## Scope
Kill the appender duplication that left 988 dup rowids (max 12×) in the docs
LanceDB table across today's 7-attempt retry loop (verdict.md H4). Also lower
`CHECKPOINT_EVERY` in the docs indexer from 5000 → 500 because per-run yield is
now often <1000 rows (bounded by F2 preventive exit).

## Files changed / created

| file | md5 (post) | lines |
|---|---|---|
| `src/index/builders/docs_vector_indexer.py` | `a686c0e7f45a7ecf03d058bb27f02d01` | 614 |
| `scripts/build_vectors.py` | `09581bdd1aa708a6a21dd966c155c683` | 579 |
| `scripts/dedup_docs_lance.py` (**new**) | `4cd94b4e986d633332aa95cfe8760775` | 106 |

## Key diffs

### `src/index/builders/docs_vector_indexer.py`
- `CHECKPOINT_EVERY = 5000` → `CHECKPOINT_EVERY = 500`.
- `_open_or_create_writer::writer_fn` now:
  - On the FIRST write of the run, after `db.open_table("chunks")` or
    `db.create_table(...)`, seed `state["known_rowids"]` from the existing
    table via a 3-level fallback chain:
    1. `table.to_lance().to_table().column("rowid").to_pylist()` (cheapest)
    2. `table.to_arrow().column("rowid").to_pylist()`
    3. `list(table.to_pandas()["rowid"])`
  - Subsequent calls: filter `batch_data` to exclude records whose rowid is
    already in the set; when any were filtered, print
    `[writer-dedup] skipped N duplicate rowids in batch`.
  - Add kept rowids to the set after `.add(filtered)`.
- Same logic mirrored in `scripts/build_vectors.py::_open_or_create_writer`.

### `scripts/dedup_docs_lance.py` (new)
- Opens `db/vectors.lance.docs/chunks` (resolves `CODE_RAG_HOME` or `~/.code-rag-mcp`).
- Full scan via `table.to_lance().to_table()`, Counter of rowids → summary of
  duplicates (top 10 offenders, counts, excess row count).
- Interactive `input()` y/n gate. Refuses to act on `n` / `<CR>`.
- Rebuilds keep-set by walking physical (append) order → `keep_by_rowid` is
  overwritten on every occurrence, so the LAST (newest) record wins per rowid.
- Bulk delete via `table.delete("rowid IN (...)")` then re-insert the kept
  newest records. Prints before/after row counts.

## Verification

```bash
cd /Users/vaceslavtarsevskij/.code-rag-mcp
python3 -m py_compile src/index/builders/docs_vector_indexer.py scripts/build_vectors.py scripts/dedup_docs_lance.py
# → F3 compile OK
python3.12 -m pytest tests/test_docs_vector_indexer.py -q
# → 9 passed in 0.43s
python3.12 -m pytest tests/test_memguard.py tests/test_docs_chunks.py tests/test_docs_vector_indexer.py -q
# → 38 passed in 4.32s
```

`_FakeTable` in `tests/test_docs_vector_indexer.py` has no `to_lance()` /
`to_arrow()` / `to_pandas()` methods — all three lambdas in
`_load_known_rowids` raise `AttributeError`, the helper falls through to the
empty-set return, and `writer_fn` proceeds without filtering. Production
LanceDB tables expose all three methods, so the dedup path activates there
(with `to_lance()` hit on the first fallback).

## Runtime usage

```bash
# One-off cleanup of the historical 988-dup buildup
python3 /Users/vaceslavtarsevskij/.code-rag-mcp/scripts/dedup_docs_lance.py
# expects: Total rows 49142 → 48154
```
