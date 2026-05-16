# Jira Eval Failure Analysis — baseline_rerun3 (63.91% hit@10)

**Date:** 2026-05-16  
**Eval file:** `profiles/pay-com/eval/jira_eval_clean.jsonl` (665 queries)  
**Baseline:** `bench_runs/baseline_rerun3.json`  
**Miss rate:** 240/665 = 36.1%

---

## Executive Summary

The 36.1% miss rate is driven by **four dominant failure modes**:

1. **Doc-intent misclassification (10.8% of misses)** — Code queries containing words like "integrate", "integration", or "vault" are routed to the docs vector tower and (for trustly/webhook strata) skip reranking entirely.
2. **Ground truth quality issues (~15% of misses)** — Expected paths include CI/k8s files unrelated to the task, or are comically over-broad (e.g. "Update Vite and babel" → 71 paths including `BOSidebar.tsx`).
3. **Query vagueness / semantic gap (~40% of misses)** — Queries like "Task Adjustments and Fixes" or "Refresh Compliance bug" lack specific technical tokens that would match indexed chunks.
4. **Provider integration query pattern mismatch (~12% of misses)** — Provider integration queries (e.g. "nexi provider integrations", "trustly integration") don't trigger cross-provider fanout because they lack a `{topic_verb}` (payout, refund, sale, webhook, etc.).

The remaining ~22% are diverse: specific error-message bugs, migration/refactor tasks with no unique keywords, and cases where the gold file exists in the index but is ranked below position 10.

---

## 1. Doc-Intent Misclassification (26 queries, 10.8% of misses)

### The Problem

The `_query_wants_docs()` classifier in `src/search/hybrid_query.py` uses `_CONCEPT_DOC_RE` which matches:
- `integrate`, `integration`, `integrations`
- `vault`, `apm`, `tokenizer`, `sepa`, `voucher`
- `pattern`, `repo`, `repository`

When these match and no `_STRICT_CODE_RE` token is present, the query is routed to the **docs vector tower only** (`limit=50` instead of `100`). For queries whose stratum is in `_DOC_RERANK_OFF_STRATA` (`trustly`, `webhook`, `method`, `payout`), the **CrossEncoder reranker is completely skipped**.

### Affected Missed Queries

| Query ID | Query | Classified As | Stratum | Actual Intent |
|----------|-------|---------------|---------|---------------|
| BO-1042 | Integrate Payment Methods Configurations Microfrontend into Backoffice | doc-intent | `method` | **Code** — UI integration task |
| PI-12 | nexi provider integrations | doc-intent | `provider` | **Code** — provider integration |
| PI-40 | trustly integration | doc-intent | `trustly` | **Code** — provider integration |
| CORE-2577 | [Webhooks] Add rules related webhook events | doc-intent | `webhook` | **Code** — webhook implementation |
| CORE-2574 | [Vault] Retry DEK decrypt in case of error | doc-intent | None | **Code** — vault service logic |
| CORE-2638 | [Routing] APM Routing Through Risk Engine with payment_method_type | doc-intent | `method` | **Code** — routing logic |
| HS-254 | Okto Cash APM | doc-intent | None | **Code** — APM integration |

**PI-40 (trustly integration)** is a devastating triple failure:
1. "integration" matches `_CONCEPT_DOC_RE` → doc-intent
2. "trustly" matches `_DOC_RERANK_OFF_STRATA` → reranker skipped
3. No `{provider} {topic_verb}` pattern → cross-provider fanout disabled

The expected paths are `grpc-apm-trustly/methods/payout.js`, `grpc-apm-trustly/methods/initialize.js`, etc. — all code files. The docs tower and skipped reranker make this nearly impossible to hit.

### Fix Recommendation

**Immediate (high impact):**
- Remove `integrate`, `integration`, `integrations` from `_CONCEPT_DOC_RE` or require them to be paired with doc-only context (e.g. "integration guide", "integration docs").
- Remove `vault` from `_CONCEPT_DOC_RE` — it is both a doc concept AND a heavily used code repo name (`grpc-vault-dek`).
- For stratum-gated rerank skip: do NOT skip reranker when the query also contains a code-intent repo token (e.g. `grpc-`, `express-`) or a file extension.

**Medium-term:**
- Add a calibration eval specifically for queries that hit `_CONCEPT_DOC_RE` — many are code tasks in disguise.

---

## 2. Ground Truth Quality Issues (~35 queries, ~15% of misses)

### The Problem

A significant fraction of missed queries have `expected_paths` that are clearly wrong, overly broad, or include files completely unrelated to the task description.

### Evidence

**CI/k8s files in unrelated tasks:**

| Query ID | Query | Suspicious Expected Paths |
|----------|-------|---------------------------|
| BO-1171 | Remove record form config type | `backoffice-web/k8s/.github/workflows/deploy_staging.yml` |
| BO-1159 | Compliance Stakeholders - Individual Fields | `backoffice-web/k8s/.github/workflows/deploy_staging.yml` |
| BO-1355 | Support Multiple MCCs per Merchant Application with Scoping | `backoffice-web/k8s/.github/workflows/deploy_staging.yml` |
| PI-40 | trustly integration | `grpc-apm-trustly/k8s/.github/workflows/release-publish-deploy.yml` |
| CORE-1615 | Cache grpc methods result and db requests | `grpc-core-schemas/k8s/.github/workflows/deploy.yml` |

**Comically over-broad ground truth:**

| Query ID | Query | # Paths | Sample of Clearly Wrong Paths |
|----------|-------|---------|------------------------------|
| BO-1108 | Update Vite and babel | 71 | `BOSidebar.tsx`, `BOTable.tsx`, `Router.tsx`, `App.tsx` |
| HS-244 | Visa click to pay | 90 | `AutoHeight.tsx`, `InitDataLoader.tsx`, `MainRouter.tsx` |
| CORE-2581 | Migrate all services to latest pg lib | 178 | Dozens of unrelated service files |
| BO-905 | Add prettier-plugin-tailwindcss | 70 | Random UI components |

**Analysis:** The eval ground truth appears to have been generated by taking all files changed in a Jira ticket's associated PR(s). For tooling updates (Vite, Babel, prettier) or very broad features, this includes every file in the repo that was touched by the PR — even if the search query gives no indication those files are relevant.

### Fix Recommendation

**Ground truth curation (not a search fix):**
- Filter out CI/k8s files from `expected_paths` unless the query explicitly mentions deployment, CI, or k8s.
- Cap `expected_paths` at a reasonable number (e.g., 15-20) and require that paths are semantically related to the query text.
- For tooling updates (Vite, Babel, prettier, eslint), ground truth should only include config files (`vite.config.ts`, `babel.config.js`, `package.json`), not every source file that was reformatted.
- Re-build eval with human-verified expected paths for the top 50 most-path queries.

---

## 3. Query Vagueness / Semantic Gap (~95 queries, ~40% of misses)

### The Problem

Many Jira queries are written for humans (product managers, QA) and lack the specific technical vocabulary that would match code chunks. The FTS5 keyword search (which gets 2x weight) can't match what isn't there. The vector search may semantically map vague terms to unrelated concepts.

### Examples

| Query ID | Query | Why It Misses |
|----------|-------|---------------|
| BO-1051 | Task Adjustments and Fixes | "Adjustments" and "Fixes" are generic; no file names, component names, or function names |
| BO-1018 | Refresh Compliance bug | "Refresh Compliance" is a UI feature label, not a string that appears in code |
| BO-1140 | Research and delete unused code | "Research" and "unused code" are meta-concepts; no specific module mentioned |
| BO-1041 | Refactor update merchant and merchant application | "Refactor" and "update" are generic verbs; 29 expected paths span the entire form system |
| BO-1032 | Refactor fields list component - more generic | "more generic" is a design goal, not a code token |
| BO-1045 | Expose Merchant Drilldown to operations & sales | "Expose" is a permission/visibility concept; the actual code changes are in auth configs |
| BO-1047 | core-transactions findByParams optimisation | "optimisation" is vague; the actual optimization may be in SQL indexes, not visible in code snippets |
| BO-1064 | Merchant > Finance > Rolling Reserves | Page navigation path; actual code uses different naming |

### Why FTS5 Fails

The FTS5 search uses exact token matching. For "Refresh Compliance bug":
- "Refresh" matches thousands of files (React `useEffect` refetches, page refreshes)
- "Compliance" is a huge domain with hundreds of files
- "bug" is a meta-label that almost never appears in code

With no distinctive tokens, the RRF score for the true gold files is drowned out by noise.

### Why Vector Search Fails

The vector model (coderank or similar) is trained on code. Vague natural-language descriptions like "Task Adjustments and Fixes" don't have strong vector analogs in the code embedding space. The docs tower might help, but these queries are classified as code-intent (no doc tokens), so they only get the code tower.

### Fix Recommendation

**Query expansion / entity extraction:**
- Use the Jira ticket description/body (not just the title) as the search query. Titles are often too short and vague.
- Extract repo names, file names, and function names from the ticket body/PR description and inject them into the query.
- Expand domain abbreviations using `glossary.yaml` (already done, but may need expansion for UI feature names).

**Hybrid query augmentation:**
- For queries with <3 technical tokens, automatically fall back to a broader search (higher `limit`, both towers) or warn the user that the query is too vague.

---

## 4. Provider Integration Query Pattern Mismatch (~28 queries, ~12% of misses)

### The Problem

Provider integration queries (e.g. "nexi provider integrations", "trustly integration", "nuvei provider") don't trigger the cross-provider fanout because they lack a `{topic_verb}` from `_TOPIC_VERBS`: `payout`, `refund`, `sale`, `webhook`, `initialize`, `dispatch`, `activities`, `signature`, `credentials`, `idempotency`.

The cross-provider fanout is designed for queries like "nuvei payout" or "trustly webhook" — it finds the top hit in the active provider repo and then pulls analogous chunks from sibling providers. But integration queries are about *adding a new provider*, which means the relevant files are in the new provider's repo, credential/config repos, and shared infrastructure — NOT in sibling providers.

### Affected Queries

| Query ID | Query | Expected Repos | Why Fanout Doesn't Help |
|----------|-------|----------------|------------------------|
| PI-12 | nexi provider integrations | `grpc-providers-nexi`, `grpc-providers-credentials` | No topic verb; new provider repo has no siblings |
| PI-39 | nuvei provider | `grpc-providers-nuvei`, `grpc-providers-credentials` | No topic verb; just provider name |
| PI-40 | trustly integration | `grpc-apm-trustly`, `express-api-internal` | "integration" not in `_TOPIC_VERBS` |
| PI-47 | Payhub Integration | `workflow-provider-webhooks`, `grpc-providers-credentials` | No topic verb |
| PI-53 | [APM] - Trustly payouts | `grpc-apm-trustly`, `grpc-payment-gateway` | Has "payouts" but query is `trustly payouts` — actually this SHOULD trigger fanout! Wait, "payouts" is not in `_TOPIC_VERBS` (only "payout"). Let me verify... Yes, `_TOPIC_VERBS` has "payout" not "payouts". So "Trustly payouts" doesn't match. |

### Fix Recommendation

**Immediate:**
- Add "integration", "integrate", "provider" to `_TOPIC_VERBS` or create a separate fanout path for provider-setup queries.
- Normalize topic verbs (stem "payouts" → "payout", "refunds" → "refund") before matching.
- For provider-only queries (bare provider name or "{provider} provider"), inject the provider's repo into the candidate pool with high boost.

**Medium-term:**
- Build a provider-setup recipe/knowledge base: when a query mentions a provider + integration/setup/validation, boost the provider's own repo, the credentials repo, and the features/seeds repo.

---

## 5. Reranker Skip for Trustly/Webhook Strata Hurts Code Queries

### The Problem

The P10 stratum-gated rerank skip (`_should_skip_rerank`) disables the CrossEncoder for doc-intent queries in strata where the reranker was empirically found to hurt:
- `webhook` (+3.35pp lift when skipped)
- `trustly` (+2.68pp lift when skipped)
- `method` (+1.30pp lift when skipped)
- `payout` (+1.11pp lift when skipped)

But this was measured on **doc-intent** queries. When a code query is misclassified as doc-intent (see §1), skipping the reranker is catastrophic — the code-tuned reranker is the main tool that rescues code files from a noisy RRF pool.

### Affected Queries

- `PI-40: trustly integration` — trustly stratum + doc-intent → no reranker
- `CORE-2577: [Webhooks] Add rules related webhook events` — webhook stratum + doc-intent → no reranker
- `BO-1042: Integrate Payment Methods Configurations Microfrontend into Backoffice` — method stratum + doc-intent → docs reranker (L6) instead of code reranker (L12)

### Fix Recommendation

- Gate the rerank skip on **both** stratum AND a high-confidence doc-intent signal. A query that merely matches `_CONCEPT_DOC_RE` should not trigger skip — require Tier-1 or Tier-2 doc intent (`_DOC_QUERY_RE`, `_REPO_OVERVIEW_RE`, `_PROVIDER_ONLY_RE`).
- For method stratum: the +1.30pp lift is small; consider removing `method` from `_DOC_RERANK_OFF_STRATA` entirely to avoid false positives.

---

## 6. Specific Error-Message Bug Queries (~15 queries, ~6% of misses)

### The Problem

Some queries are bug reports that quote exact error messages. The error message may not appear in the source code (it's a runtime error from a dependency or database), or it only appears in log files that aren't indexed.

### Examples

| Query ID | Query | Issue |
|----------|-------|-------|
| BO-1016 | Prod bug UNKNOWN: Metadata string value "Error: Code: 60. DB::Exception | Error string is from ClickHouse/DB, not in application code. Expected paths are GraphQL resolvers that handle the data, but the query text doesn't mention GraphQL. |
| BO-1216 | Core Cost - ValidationError: "currency" is not allowed to be empty | The validation error string may appear in Joi/Zod schemas, but "Core Cost" is a UI page name, not a code token. |
| BO-994 | Optimize transactions table - query holds 25s | "25s" is a performance observation, not a code token. The optimization is likely in SQL/indexes, not visible in TS/JS chunks. |
| BO-1303 | Fix Validation and State Handling in Table Filters | "State Handling" is a UI pattern concept, not a specific function name. |

### Fix Recommendation
- For error-message queries: strip the error message and expand to the API/service domain. E.g., "DB::Exception" → "database error handler", "clickhouse".
- Index more structured content: error handler mappings, API response codes, validation schemas.

---

## 7. Build/Tooling Update Queries (~8 queries, ~3% of misses)

### The Problem

Queries about updating build tools (Vite, Babel, graphql-codegen, prettier) often miss because:
1. The config files (`vite.config.ts`, `babel.config.js`) are small and may not chunk well.
2. The query doesn't name the specific config file.
3. Ground truth includes every file reformatted by the tool update, not just config files.

### Examples

| Query ID | Query | Expected Paths |
|----------|-------|---------------|
| BO-1108 | Update Vite and babel | 71 paths — mostly unrelated source files |
| BO-1465 | Update `graphql-codegen` to Latest Version | 3 paths in `next-web-transaction-drilldown` |
| BO-905 | Add prettier-plugin-tailwindcss | 70 paths — mostly source files reformatted |
| BO-1289 | Remove `twin.macro` from Codebase and Dependencies | 18 paths — various UI components |

### Fix Recommendation
- Ground truth curation: only include config and dependency files for tooling updates.
- Search-side: boost `package.json`, `vite.config.*`, `babel.config.*`, `tsconfig.json` when queries mention the tool name.

---

## 8. File Existence Check

All repos referenced in expected paths of missed queries exist in `raw/` (100% repo coverage). Specific files also exist for most queries checked. This means **the gold files are in the index** — the issue is ranking, not coverage.

Example checks:
- `backoffice-web/src/Pages/Merchants/Page/ConfigurationSets/ConfigurationSets.tsx` → EXISTS
- `grpc-providers-nexi/methods/index.js` → EXISTS
- `grpc-apm-trustly/methods/payout.js` → EXISTS
- `grpc-vault-dek/methods/decrypt.js` → EXISTS

Some specific files are missing (e.g., `grpc-providers-nexi/env/consts.js`), but the repos exist and other expected files from those repos are present.

---

## Summary of Actionable Fixes

### P0 — Highest Impact, Easy to Implement

1. **Fix doc-intent misclassification**
   - Remove `integrate`, `integration`, `integrations`, `vault` from `_CONCEPT_DOC_RE` in `src/search/hybrid_query.py`
   - Require stratum-gated rerank skip to also have Tier-1/2 doc intent (not just `_CONCEPT_DOC_RE`)
   - Remove `method` from `_DOC_RERANK_OFF_STRATA` (marginal +1.30pp lift, high false-positive cost)

2. **Add topic verb normalization**
   - Stem `payouts`→`payout`, `refunds`→`refund`, `webhooks`→`webhook` before `_TOPIC_VERBS` matching in `src/search/hybrid_query.py`
   - Add `integration`, `setup`, `validation` to provider fanout triggers

### P1 — High Impact, Medium Effort

3. **Ground truth curation**
   - Filter CI/k8s files from `expected_paths` unless query mentions CI/CD
   - Cap `expected_paths` at ~20 and require semantic relevance
   - Rebuild ground truth for tooling-update tickets (Vite, Babel, prettier) to only include config files
   - Consider rebuilding the entire eval with human-verified expected paths — the current auto-generated ground truth from PR diffs is noisy

4. **Query expansion for vague Jira titles**
   - Use ticket body/PR description as query, not just title
   - Extract file/function names from PR diffs and prepend to query

### P2 — Medium Impact, Higher Effort

5. **Provider-setup recipe boost**
   - When query matches provider + setup/integration/validation, boost the provider repo, credentials repo, and features repo

6. **Error-message query preprocessing**
   - Strip runtime error strings and map to domain concepts using a small error-code dictionary

---

## Appendix: Sample of 25 Missed Queries with Diagnosis

| Query ID | Query | # Paths | Primary Failure Mode |
|----------|-------|---------|---------------------|
| BO-1041 | Refactor update merchant and merchant application | 29 | Vague — generic verbs, no specific tokens |
| BO-1042 | Integrate Payment Methods Configurations Microfrontend into Backoffice | 3 | **Doc-intent misclassification** ("Integrate" → docs tower) |
| BO-1103 | Replace JSON Viewer with Monaco Editor | 16 | Component name mismatch — "JSON Viewer" vs `BOJsonEditor` |
| BO-1244 | Core Documents Reference Migration | 4 | Vague — "Migration" is generic; expected paths are scripts |
| BO-1585 | Export Assessment Form PDF and Attach as Legal Entity Business Document | 44 | Over-broad ground truth + vague query |
| BO-885 | Alert Management - Add Customer ID on the Alerts table | 11 | UI feature name vs code token mismatch |
| CORE-2175 | Adjustments to Transactions | 16 | Vague — "Adjustments" is generic |
| CORE-2361 | ACH and RTP payment method options | 4 | Payment method config — may be in enum/const files not well-indexed |
| CORE-2572 | [API] Retry attempt counter | 5 | "Retry attempt" is generic; gold file is `get-retry-attempt-schema.js` |
| HS-244 | Visa click to pay | 90 | Over-broad ground truth (90 paths!) |
| HS-262 | applpay button fixes | 4 | Typo "applpay" instead of "applepay"; FTS5 can't match |
| HS-273 | Setup Session - allow continue with default | 5 | Vague UI feature description |
| HS-278 | Radio: fix "more apms" button overflow | 5 | "more apms" is UI label, not code token |
| PI-12 | nexi provider integrations | 4 | **Doc-intent misclassification** + no topic verb for fanout |
| PI-13 | Check CVV removal condition in all live providers | 42 | Over-broad ground truth (42 paths across many providers) |
| PI-15 | Validate old integration - gumballpay | 36 | Over-broad ground truth + **doc-intent misclassification** |
| BO-1006 | Merchant Status Button in button | 3 | Vague/nonsensical query |
| BO-1016 | Prod bug UNKNOWN: Metadata string value "Error: Code: 60. DB::Exception | 3 | Error message from dependency, not in app code |
| BO-1018 | Refresh Compliance bug | 22 | Vague — "Refresh Compliance" is UI feature label |
| BO-1032 | Refactor fields list component - more generic | 7 | Vague design goal, not code token |
| BO-1043 | Fix TX table query "orderBy" | 7 | "TX" abbrev not expanded; "orderBy" is generic GraphQL term |
| BO-1045 | Expose Merchant Drilldown to operations & sales | 8 | Permission concept; code changes are in auth files |
| BO-1047 | core-transactions findByParams optimisation | 10 | "optimisation" is vague; likely SQL/index changes |
| BO-1051 | Task Adjustments and Fixes | 26 | Extremely vague — no specific technical terms |
| PI-37 | crb ach payouts provider | 8 | No topic verb match ("payouts" ≠ "payout") + provider pattern |
