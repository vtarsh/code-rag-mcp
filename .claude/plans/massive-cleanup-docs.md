# Docs Cleanup Findings

> Scope: `profiles/pay-com/docs/` (~4,000 files, ~90MB total)
> Generated: 2026-05-16

---

## Duplicates

### Exact duplicate files (byte-identical)

| File A | File B | Size |
|--------|--------|------|
| `providers/plaid/llms-full.txt.md` | `providers/plaid/docs_llms-full.txt.md` | 5.3M each |
| `providers/plaid/llms.txt.md` | — | 293K (also likely duplicated elsewhere) |
| `providers/braintree/braintree_files_Centinel…pdf.md` | `providers/braintree/files-Centinel…pdf.md` | 84K each |
| `providers/nuvei/comments_feed.md` | `providers/nuvei/comments-feed.md` | 85K each |
| `providers/payper/reference_webhook-1.md` | `providers/payper-new/reference_webhook-1.md` | identical |
| `providers/payper/reference_gateway-response-variables.md` | `providers/payper-new/reference_gateway-response-variables.md` | identical |
| `providers/checkout/payments.md` | `providers/checkout/docs_payments.md` | identical (9 such pairs) |
| `providers/checkout/support.md` | `providers/checkout/docs_support.md` | identical |
| `providers/checkout/legal_rdr-terms.md` | `providers/checkout/legal-rdr-terms.md` | identical |
| `providers/braintree/braintree_docs_guides_overview.md` | `providers/braintree/guides-overview.md` | identical (3 braintree pairs) |
| `providers/braintree/community-blog.md` | `providers/braintree/community_blog.md` | identical |
| `providers/volt/docs_global-api.md` | `providers/volt/global-api.md` | identical (2 volt pairs) |
| `providers/paylands/batch-operations.md` | `providers/paylands/docs_batch-operations.md` | identical (5 paylands pairs) |
| `providers/bankingcircle/_openapi/*.json` | `providers/bankingcircle/swagger/*.json` | 16 identical JSON pairs (~5.5M total) |

### Near-duplicate / overlapping content

- `gotchas/global-conventions.md` (30KB) vs `gotchas/errors-and-throw-policy.md` (30KB): Both extensively document `InvalidDataError`, soft-decline vs throw policy, and boundary-wrap patterns. ~15+ shared concepts with near-identical phrasing.
- `gotchas/error-code-mapping.md` (18KB) vs `gotchas/global-conventions.md`: Overlap on `issuerResponseCode` taxonomy, legacy v1 mapper behavior, and `metadata.failureCode` usage.
- `gotchas/adapter-file-structure.md` (18KB) vs `references/provider-setup-recipe.md`: Both describe boilerplate repo setup, `consts.js` cleanup, and proto-dep pitfalls.
- `references/trace-chains.yaml` (30KB) vs `references/field-contracts.yaml` (24KB): Both contain provider contract metadata; possible schema consolidation.
- `notes/_moc/*.md` (11 files): Auto-generated reverse indexes that duplicate the `related:` frontmatter links already present in source files.

---

## Stale Info

### Deprecated services still referenced

- `gotchas/grpc-apm-paypal.md`: States `express-webhooks-paypal` is "deprecated; DevOps to remove deployment." The doc still carries legacy F4 no-op verification details that are irrelevant post-PI-8.
- `gotchas/grpc-providers-credentials.md`: Documents dual-PR workflow for `latest-1-4-1a8daee` (Scylla) vs `main` (Postgres). Includes sunset condition: "rule sunsets when `latest-1-4-1a8daee` stops receiving merges." As of 2026-05-12 it was still active, but this doc will become entirely stale after cutover.
- `gotchas/source-flags-lifecycle.md`: References "PI-65 in progress (unmerged `feature/pi_65` branch — no production)". If PI-65 has merged, this section is stale.

### Stale / abandoned provider docs

| Provider | Files | Why stale |
|----------|-------|-----------|
| `providers/iris/` | 1 (`index.md`) | Zero references in gotchas/references/flows. No `grpc-apm-iris` repo mentions outside `errors-and-throw-policy.md` (as a past example). |
| `providers/ilixium/` | 1 (`index.md`) | Same — orphaned provider doc. |
| `providers/neteller/` | 1 (`integration-guide.md`) | Referenced only once in `errors-and-throw-policy.md` as a throw-pattern example. No active integration docs. |
| `providers/rtp/` | 3 files | Tiny set; may be a stub that never grew. |
| `providers/libra/` | 4 files (2 PDF + 2 MD) | 1.6M of bank API docs; zero cross-references in core docs. |
| `providers/stripe-cashapp/` | 30 files | Only referenced in `references/test-credentials/stripe-cashapp-flow.md` and `references/impact-audit-catalog.md` (as broken). No gotchas, no flows. Likely dead integration. |
| `providers/payper-new/` | 78 files | Duplicates 2 files with `providers/payper/`; name suggests migration artifact. |

### Stale MOCs

- `notes/_moc/*.md` (11 files): Last modified 2026-05-01 (~15 days ago). `_housekeeping_report.md` flags them as "stale >14d" with high inbound-link counts, suggesting they are auto-generated reverse indexes that drift out of sync.

### Removed providers still referenced

- `references/do-not-expire-matrix.md` explicitly marks `aircash` and `neosurf` as **REMOVED**, yet their provider directories still exist under `providers/`.

---

## Compression Opportunities

### Oversized files (>50KB)

| File | Size | Suggestion |
|------|------|------------|
| `providers/plaid/llms-full.txt.md` + `docs_llms-full.txt.md` | 5.3M + 5.3M | Delete one; these are raw LLM dumps with minimal curation. Consider deleting both if unused. |
| `providers/plaid/legal.md` | 1.0M | Raw legal text; compress or externalize. |
| `providers/paypal/docs_api_orders_v2.md` | 712K | Large API reference; consider splitting or replacing with OpenAPI link. |
| `providers/nuvei/api_main_reference.md` | 530K | Same — monolithic API doc. |
| `providers/nuvei/documentation_additional-links_api-reference.md` | 527K | Near-duplicate of above? |
| `providers/evo/*.pdf` (6 files) | 2.4M + 2.0M + 980K + 698K + 690K + 576K | PDFs coexist with MD extractions. Keep one format. |
| `references/test-credentials/ecentric-provider.md` | 155K | Contains large inline JS blobs and card data tables. Extract JS to snippets or trim. |

### Fragmented provider docs (many tiny files)

| Provider | Files | Under 2KB | Suggestion |
|----------|-------|-----------|------------|
| `providers/ach/` | 274 | ~199 under 3KB, 70 under 1KB | Merge per-category (accounts, cards, payments, etc.) into single API guides. 274 files → ~15 bundles. |
| `providers/nuvei/` | 530 | 79 under 2KB | Consolidate tiny FAQ/changelog fragments. |
| `providers/bankingcircle/` | 388 | 48 under 2KB | Merge 32 changelog files into single `changelog.md`. Delete `_openapi/` or `swagger/` duplicate dirs. |
| `providers/ecp/` | 267 | — | 11 files named `introduction.md` across subdirs — deduplicate or merge into one overview. |
| `providers/applepay/` | 191 | — | 11 versioned release-note files (version-1 … version-11) — merge into single `release-notes.md`. |
| `providers/ppro/` | 225 | — | Many global-api docs for individual APM methods (alfamart, alipay, amazon-pay…). Could be catalogued more densely. |

### Redundant prose in hand-written docs

- `gotchas/global-conventions.md` (30KB) + `gotchas/errors-and-throw-policy.md` (30KB) + `gotchas/error-code-mapping.md` (18KB): ~78KB total with significant overlap. Consider extracting shared "throw policy" and "error taxonomy" sections into `references/` and linking from gotchas.
- `gotchas/adapter-file-structure.md` (18KB) + `references/provider-setup-recipe.md` (~5KB): Overlap on boilerplate cleanup. Merge recipe into adapter-file-structure or cross-link exclusively.
- `gotchas/processor-token-persistence.md` (27KB): Very long for a single concept. Could be split into vault-write patterns + per-provider examples.

---

## Quick Wins (safe deletions/merges)

1. **Delete 16 identical duplicate pairs in `providers/`** — saves ~15MB immediately:
   - `plaid/llms-full.txt.md` ↔ `plaid/docs_llms-full.txt.md`
   - `bankingcircle/_openapi/*.json` ↔ `bankingcircle/swagger/*.json` (entire `swagger/` dir is redundant)
   - `checkout/*.md` ↔ `checkout/docs_*.md` (9 pairs)
   - `braintree/*` (3 pairs), `volt/*` (2 pairs), `paylands/*` (5 pairs)
   - `nuvei/comments_feed.md` ↔ `nuvei/comments-feed.md`
   - `braintree/*.pdf.md` duplicate naming variants

2. **Delete `providers/payper-new/` or merge into `providers/payper/`** — 78 files, 440K. Only 2 files are confirmed duplicates; the rest may be a stale migration branch.

3. **Delete `notes/_moc/`** — 11 files, 60KB. If auto-generated and already 15 days stale, they add more drift than value. Frontmatter `related:` links in source files already serve the same purpose.

4. **Remove `providers/evo/` PDFs** (6 files, ~5.5M) — MD extractions exist for all of them. PDFs bloat the repo and are not diffable.

5. **Consolidate `providers/ach/` tiny files** — 70 files under 1KB are essentially one-paragraph API endpoint stubs. Merge by category (accounts, cards, payments, lending, etc.) into ~10 composite docs.

6. **Clean up abandoned provider stubs** — `providers/iris/`, `providers/ilixium/`, `providers/neteller/`, `providers/rtp/`, `providers/libra/` are either single-file orphans or unreferenced PDF dumps. Archive or delete unless actively needed.

7. **Remove `providers/aircash/` and `providers/neosurf/`** — Explicitly marked as REMOVED in `do-not-expire-matrix.md`. Directories still exist.

8. **Trim `references/test-credentials/ecentric-provider.md`** — 155KB test-credential file with large inline JS. Extract JS to a separate snippet or trim to essentials.

