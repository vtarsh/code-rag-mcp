# Chunking Strategy Investigation

**Date:** 2026-05-16  
**Scope:** `src/index/builders/code_chunks.py`, SQLite `chunks` table, `profiles/pay-com/eval/jira_eval_clean.jsonl`  
**Goal:** Determine whether the chunking strategy is losing gold-standard information.

---

## 1. Chunking Logic Summary

- **Files:** `src/index/builders/code_chunks.py` (JS/TS smart chunking), `src/index/builders/_common.py` (constants)
- **MAX_CHUNK:** 4,000 characters
- **MIN_CHUNK:** 50 characters
- **Smart chunking (JS/TS):** Regex-based boundary detection for functions, classes, exports, decorators, routes, and methods. Segments are non-overlapping.
- **Oversized segments:** If a segment exceeds `max_lines=200` (or MAX_CHUNK=4000 after repo-prefix), it is split into `_part1`, `_part2`, etc. at blank lines or hard-cut.
- **Fallback (non-JS/TS):** Line-count splitting at top-level declarations, also non-overlapping.
- **No overlap:** There is zero overlap between consecutive chunks. A clean cut is made at the boundary.

---

## 2. Eval Data Characteristics

- **Queries:** 665 Jira tickets
- **Expected paths per query:** avg 16.0 (min 3, max 184)
- **Unique gold files:** 5,293 (repo, path) pairs
- **Granularity:** Gold is **file-level**, not function-level. The eval only checks whether the correct *file* appears in top-K results.
- **Coverage:** 100% of gold files (5,293/5,293) are present in the `chunks` index.

---

## 3. Chunk Distribution in the Index

| Metric | Value |
|--------|-------|
| Total chunks (`chunks_content`) | 81,087 |
| Chunks with `chunk_meta` | 53,832 |
| Avg chunks per file (with meta) | 3.63 |
| Max chunks in a single file | 327 (`libs-types/proto/protos/common.proto`) |

### Eval-File Chunk Counts
- **Single-chunk files:** 4,484 / 5,293 (84.7%)
- **Multi-chunk files:** 809 / 5,293 (15.3%)
- **Files with `_part` suffix:** 133
- **Files with >5 chunks:** 313

### Chunk Size Distribution (code-related file types only)
| Size Range | Count | Notes |
|------------|-------|-------|
| < 100 chars | 1,619 | Tiny fragments |
| < 200 chars | 9,640 | Likely noise |
| < 500 chars | 35,713 | Many small callbacks |
| 3,500–4,000 chars | 524 | Near truncation boundary |
| 3,900–4,000 chars | 87 | Effectively at MAX_CHUNK limit |
| > 4,000 chars | 974 | Must be non-code (docs/proto) |

---

## 4. Hypothesis Testing

### H1: "If a function spans 5,000 chars, it's split into two chunks. The query might match the second chunk but the gold points to the first (or vice versa)."

**Verdict: PARTIALLY TRUE, but mitigated by file-level gold.**

- 133 eval files are split into `_part1`, `_part2`, etc.
- Examples:
  - `backoffice-web/src/Components/ErrorFallback/ErrorFallbackSVG.tsx` → 4 parts (~15.5k total, each ~4k)
  - `backoffice-web/src/App/Router.tsx` → preamble split into `_part1` (4,039) + `_part2` (2,868)
  - `hosted-fields/apps/hosted-fields/src/utils/i18n/i18n.ts` → 14 parts
- Because gold is **file-level**, as long as *any* chunk from the file ranks in top-K, the hit counts as correct.
- **However**, ranking can suffer:
  - The CrossEncoder reranker scores individual chunks. If the query references a concept in `_part2` but the function signature (critical context) is in `_part1`, the reranker has less signal.
  - FTS5 `snippet()` only shows the matched chunk. A user/agent seeing `_part3` of a 14-part file gets no indication that parts 1–2 and 4–14 exist.
  - The `chunk_type` label (e.g., `code_function:handleApmCreate_part2`) does not link back to `part1`.

### H2: "MIN_CHUNK=50 creates many tiny useless chunks that dilute the index."

**Verdict: CONFIRMED.**

- 9,640 chunks are <200 characters (11.9% of all chunks).
- Many are misdetected "method" boundaries from inline callback invocations.
- **Example:** `backoffice-web/.../use-merchant-pricing-logic.ts` contains **4 separate `code_method:setIsEditing` chunks**:
  - `setIsEditing(false)` inside `useCreatePricingMutation` callback
  - `setIsEditing(false)` inside `useUpdatePricingMutation` callback
  - `setIsEditing(false)` inside `useRemovePricingMutation` callback
  - `setIsEditing(false)` inside `handleClose` function
- The regex `^(?:async\s+)?(?!if|for|...)(\w+)\s*\([^)]*\)\s*\{?\s*$` treats any indented line that looks like a function call as a **method boundary**, even when it is just a setter invocation inside a React hook callback.
- **Impact:** These tiny chunks:
  - Inflate the candidate pool with low-signal fragments.
  - Can outrank larger, more meaningful chunks if the query happens to match the short snippet exactly.
  - Waste embedding/RAM (every chunk gets a vector).

### H3: "MAX_CHUNK=4000 might be too small for large functions/classes."

**Verdict: CONFIRMED for modern TS/React codebases.**

- 524 code chunks are in the 3,500–4,000 char range, suggesting many files are pushing the limit.
- Large config/form-builder files in `backoffice-web` routinely exceed 4,000 chars:
  - `general-information-form-config.tsx`: `structuresForCountry` split into 2 parts
  - `website-and-processing-info-form-config.tsx`: split into 2 parts
  - `review-details-config.tsx`: split into 2 parts
- **Proto files** are even worse: `libs-types/proto/protos/common.proto` is split into **327 chunks**.
- **Impact:** Functions split mid-logic lose continuity. A query about "the validation at the end of `handleApmCreate`" might match `_part2`, but the variable declarations and early guards are in `_part1`. Without overlap, the reranker cannot see both halves simultaneously.

---

## 5. Duplicate Chunks

Beyond tiny chunks, the boundary regex creates **massive duplication** for certain file patterns:

| File | Chunk Type | Duplicate Count |
|------|------------|-----------------|
| `graphql/src/types/merchant-application.js` | `code_method:definition` | **93** |
| `graphql/src/types/common/common.ts` | `code_method:definition` | 49 |
| `graphql/src/types/transaction.js` | `code_method:definition` | 48 |
| `graphql/src/types/verification-checks.ts` | `code_method:definition` | 47 |

These GraphQL type files likely contain many field definitions that match the `method` regex (e.g., `definition() { ... }` or similar patterns). Having 93 chunks with the identical `chunk_type` in the same file makes the label meaningless and bloats the index.

---

## 6. Overlap Analysis

**Finding: ZERO overlap exists.**

- `_smart_chunk_js` explicitly prevents overlap: `if actual_start < prev_end: actual_start = prev_end`
- Sub-splitting of oversized segments also does not add overlap.
- Fallback `chunk_code` splits at boundaries and starts a new chunk immediately.

**Consequence:** When a function is split at line 200 or char 4000, there is no shared context across the boundary. A concept that spans the split (e.g., a conditional block that starts in `_part1` and ends in `_part2`) is invisible to the reranker in either chunk.

---

## 7. Search-System Implications

The retrieval pipeline (`src/search/hybrid.py`, `src/search/fts.py`) operates at **chunk granularity**:

1. FTS5 searches the `chunks` table (each row = one chunk).
2. Vector search returns individual chunk embeddings.
3. RRF fusion keys results by `rowid` (chunk-level).
4. The CrossEncoder reranker scores each chunk snippet independently.
5. The final output shows `repo | file_path | file_type | chunk_type` for **each chunk**.

Because gold is file-level, a file with 14 chunks gets 14 independent chances to match. This generally **improves recall** but:
- Wastes reranker slots on duplicate/tiny chunks.
- Can confuse the agent/user who sees `code_function:ErrorFallbackSVG_part3` without knowing parts 1–4 exist.
- Increases latency (more vectors, larger candidate pools).

---

## 8. Root-Cause Summary

| Issue | Severity | Evidence |
|-------|----------|----------|
| No overlap between chunks | **Medium** | 133 eval files split into parts; boundary context lost |
| MIN_CHUNK=50 too low | **High** | 9,640 chunks <200 chars; inline callbacks mischunked |
| Regex boundary over-matches | **High** | 93× `definition` duplicates in one file; 4× `setIsEditing` in another |
| MAX_CHUNK=4000 too small | **Medium** | 524 chunks near boundary; large React/proto files split heavily |
| Chunk-level search vs file-level gold | **Low** | Does not cause false negatives, but hurts ranking precision and user comprehension |

---

## 9. Recommendations

1. **Raise MIN_CHUNK to 200–300.** This would eliminate the 9,640 tiny noise chunks and force small segments to merge forward into their parent boundary.
2. **Fix method-boundary regex.** Exclude simple setter/callback invocations (e.g., `setFoo(false)`, `onChange(e)`) from the `method` pattern unless they are actual function definitions.
3. **Add overlap to `_part` splits.** When a segment is sub-split, include the last 200–300 characters of the previous part at the start of the next part. This preserves boundary context for the reranker.
4. **Consider MAX_CHUNK=6000–8000.** Modern TS/React files with JSX, type annotations, and large config objects routinely exceed 4,000 chars. A higher limit would reduce part-splits.
5. **Deduplicate or collapse duplicate chunk_types within a file.** If the same `chunk_type` appears >3 times in one file, consider merging them or appending a counter so the label is meaningful.
6. **Investigate `chunk_meta` gap.** Only 53,832 of 81,087 chunks have `chunk_meta` entries. The missing 27k+ are mostly `provider_doc`, `env_map`, `reference`, etc. If `chunk_meta.total_chunks` is used for any ranking/diversity logic, those chunks are invisible.
