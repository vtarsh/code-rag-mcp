# Causal trace analysis — n=665 rerank ON vs OFF arm flips

> **Scope:** root-cause "why this task flipped" analysis for 11 highest-magnitude
> flips between baseline (rerank ON) and rerank-OFF arms on the v2 steps-to-find
> bench (full n=665, RunPod RTX 4090, 2026-05-20). Documents the cascading
> mechanism explaining the stratum-level finding "rerank STRONGLY HELPS BO,
> HURTS CORE".
>
> **Why this file exists:** so we don't re-run per-task trace analysis on the
> same flips when the underlying code/data is unchanged. See invalidation
> conditions at bottom.

## Reformulation policy (recap)

Both arms use the SAME deterministic reformulation policy: extract compound
identifiers (camelCase/PascalCase/snake_case ≥8 chars) from top-K NEW snippets,
slide-window (next query = base + last-step tokens, NOT accumulated). The ONLY
difference between arms is whether `hybrid_rerank.rerank()` reorders the top-K
candidates before content-token extraction. Different ordering → different
top-1-NEW file → different tokens extracted → cascading divergence.

## Tasks analyzed (11)

| Task | Strata | n_exp | ON tr | OFF tr | Δ | Winner |
|---|---|---|---|---|---|---|
| BO-1491 | BO | 5 | 0.80 | 0.00 | −0.80 | ON |
| BO-1358 | BO | 7 | 0.71 | 0.00 | −0.71 | ON |
| BO-928  | BO | 3 | 1.00 | 0.33 | −0.67 | ON |
| BO-1588 | BO | 3 | 0.67 | 0.00 | −0.67 | ON |
| BO-1109 | BO | 3 | 0.67 | 0.00 | −0.67 | ON |
| CORE-2522 | CORE | 3 | 0.33 | 1.00 | +0.67 | OFF |
| CORE-2328 | CORE | 3 | 0.00 | 0.67 | +0.67 | OFF |
| CORE-2122 | CORE | 6 | 0.17 | 0.67 | +0.50 | OFF |
| CORE-2492 | CORE | 2 | 0.00 | 0.50 | +0.50 | OFF |
| CORE-2170 | CORE | 10 | 0.20 | 0.70 | +0.50 | OFF |
| CORE-2349 | CORE | 3 | 0.00 | 0.33 | +0.33 | OFF |

## Pattern: BO wins (rerank-ON) — 5 examples

### BO-1491 "Prevent Duplicate Individual Relations"
- ON step-2 tokens: `relatedEntityId IndividualRelationObject` (precise UI/domain).
  Cascade → found 4/5 GT files in steps 1-4.
- OFF step-2 tokens: `residentialAddress dateOfBirth` (drifted to generic
  Individual fields). Then `beneficialOwner`. Never recovered. 0 GT found.

### BO-1358 "Add contractAgreementDate Under Merchant Application Review Section"
- ON step-3 → `BackofficePermissions declineReason` → 5/7 GT found.
- OFF step-3 → `DisputeStatus objectId` → drifted to dispute domain (wrong
  task). Then `pre_arbitration resolved_rapid_dispute_resolution`. 0 GT found.

### BO-928 "Alert Management - No assignee"
- ON step-2 → `RiskAlertStatus merchantId` → 1/3 found at step 1.
  step-3 → `riskAlert statusList` → 3/3 found by step 4.
- OFF step-2 → `merchantId companyId` (generic, off-topic). Stuck for 2 steps.
  Eventually found 1/3 at step 5.

### BO-1588 "Add settlement_account Option to LogicFieldsValueFieldType"
- ON step-2 → `enumType settlementAccounts` → step-3 →
  `CreateSettlementAccountDisablingOptionsObject` → 2/3 found.
- OFF reformulations all stayed in `companyId settlementAccounts NexusGenEnums`
  pool. Never found target UI page. 0 GT.

### BO-1109 "Merchant Drilldown > Finance configurations tab"
- ON → top-1 was MerchantPage.tsx → `BackofficePermissions containedPermissions`
  → 2/3 found at step 1-2.
- OFF → top-1 was a generic FeatureFlag file → `useState FeatureFlagName` →
  cascade into FeatureFlag stack (off-topic). 0 GT.

## Pattern: CORE wins (rerank-OFF) — 6 examples

### CORE-2522 "Add routing_details to schema response"
- ON step-1 → `get-routing-details.js`. step-2 tokens →
  `fromModel fromModelCamelCase` (generic schema-shape tokens). Stuck. 1/3 GT.
- OFF step-1 returned **3 right files immediately**: `get-routing-details.js` +
  `get-underlying-processing-details.js` + later `libs-types/proto/schemas.proto`.
  Rerank had DEMOTED the underlying-processing-details file out of top-3. 3/3 GT.

### CORE-2328 "Drop merchant headers related to tracing"
- ON step-2 → `drilldownData relatedEntityId` (BO-style drilldown tokens —
  wrong domain). Never found tokenize repo. 0 GT.
- OFF step-1 → `cloudflare-workers-tokenize2/libs/clean-external-trace-headers.js`
  (literally the GT file). Then `tokenizationResponse` tokens. 2/3 GT.

### CORE-2122 "Batch processing sub-payments"
- ON found 1 file at step 1 then drifted to `relatedTransaction companyId`
  (generic txn tokens). 1/6 GT.
- OFF kept `requestUlid SubTransaction relatedTransactionId` (workflow-specific
  identifiers). Found 4/6 GT including activities/index.ts and consts.ts.

### CORE-2492 "Transaction partition key: split by worker id"
- ON drifted from `companyId currentDateObj` → `merchantId transactionStatus`
  → `gatewayParams` (gateway domain — wrong!). 0 GT.
- OFF locked onto `transactionIdToKeyMap transactionIdList` (THE key concept).
  Found node-libs-common/src/consts.ts. 1/2 GT.

### CORE-2170 "Legal entity service"
- ON drifted to `businessStructureDetails merchantApplication` (BO underwriting
  domain). Found 2/10 GT.
- OFF stayed in `companyId coreEntities contactInfo applicationStatus`. Found
  7/10 GT including 4 grpc-core-entity methods + service.proto + env/consts.js.

### CORE-2349 "Fix reporting worker to stop retrying on failed grpc client messages"
- ON drifted: `attemptNumber maxAttempts` → `workflowName workflowId` →
  `settlementId merchantId` → `companyId settlementId`. Never converged. 0 GT.
- OFF latched onto `settlementId merchantId` from step 2 onward (stable). Found
  `workflow-reporting-worker/libs/report-upload.js`. 1/3 GT.

## Root cause synthesis

**The reranker `l12-ft-run1` is BO-overfit.** Its training distribution
emphasized BO repos (backoffice-web, graphql, BO-style React UI components).
On CORE tasks (35% of corpus = 236 tasks), it consistently:

1. **Demotes CORE-relevant files** out of the top-K that the agent reads.
   Examples: `get-underlying-processing-details.js` (CORE-2522),
   `clean-external-trace-headers.js` (CORE-2328) were correctly retrieved by
   raw RRF (FTS + vector) but rerank pushed them past rank 3.
2. **Promotes BO-style infrastructure files** instead. The promoted files'
   snippets contain BO-typical identifiers (`drilldownData`,
   `merchantApplication`, `businessStructureDetails`, `BackofficePermissions`,
   `featureFlagName`).
3. **The cascading effect compounds:** content-token extraction pulls these
   BO-style identifiers into the next query → further BO-drift → cascade
   away from CORE-specific repos (`grpc-core-*`, `workflow-*`, `libs-types`,
   `cloudflare-workers-*`).

Conversely for BO tasks: rerank `l12-ft-run1` was trained on BO data, so
it correctly prefers BO-domain files. Without rerank, raw RRF pulls in
generic schema/types files that have higher term-frequency on common BO
backend tokens (`merchantId`, `companyId`, `residentialAddress`) but
aren't the right UI components.

## Actionable recommendation

**Stratum-gated rerank-skip for CORE tasks.** Extends the existing
[[project_p10_a2_landed_2026_05_20]] pattern (currently skips rerank on
specific PI sub-strata: nuvei/aircash/trustly/webhook/refund) by adding a
CORE-stratum top-level skip.

Implementation sketch:
1. Detect CORE-stratum at query time (`row['id'].startswith('CORE-')` works
   for eval; for production: keyword-based domain classifier already exists
   in `analyze_task` classifier).
2. Add `_should_skip_rerank` rule: if classifier domain matches `core-*`,
   skip rerank.
3. **Estimated lift on s2f@step5:** roughly +4 hits on CORE → +0.6pp on full
   corpus n_hit (15 of 236 CORE tasks were in the big-flip set; net delta
   was +4 hits in the n=665 aggregate).
4. **Caveat:** rerank-on still wins on some CORE tasks (143 wins for ON vs
   147 for OFF). Net +4 is small. A FINER classifier (e.g. distinguish
   "CORE schema/proto change" vs "CORE business-logic change") might do
   better. Worth measuring before shipping.

## Tasks NOT re-analyzed (lower-magnitude flips)

Below are flips with |Δterminal_recall| < 0.30 that we did NOT investigate
per-task. Still valid for aggregate strata stats; if a fix is shipped that
targets the BO/CORE pattern, these are good follow-up tasks to verify on:

- `bench_runs/improve/s2f_v2_n665_baseline/full_s2f.json`
- `bench_runs/improve/s2f_v2_n665_norerank/full_s2f.json`

Filter: `diff_tr = norerank.terminal_recall - baseline.terminal_recall`,
sort by `abs(diff_tr)`.

## Invalidation conditions

This analysis is VALID as long as ALL of the following remain unchanged.
If any of these change, the per-task root-cause may no longer hold and
re-analysis is required.

| Artifact | SHA / hash | Git ref |
|---|---|---|
| `src/search/hybrid.py` | md5 `063db326bfd1b9a96627517c4eb9aa4f` | `ae3c1e3` |
| `src/search/fts.py` | md5 `e946b3db2c4dc035e9dfa62ec55e5c6b` | `ae3c1e3` |
| `src/search/vector.py` | md5 `835b8c38dbb2213637f18e33b3ae0aea` | `ae3c1e3` |
| `src/search/hybrid_rerank.py` | md5 `cabb9a0894fc2f2b9a704f74bf5b92b5` | `ae3c1e3` |
| `scripts/eval/bench_steps_to_find.py` | md5 `88490083da5b6965814688f8bbf152cf` | `ae3c1e3` |
| `db/knowledge.db` | md5 `81259e981b2efa95e7db75ca67569888` | (not git-tracked) |
| Reranker model | `Tarshevskiy/pay-com-rerank-l12-ft-run1` | HF rev pinned in env |
| Embedding model | `nomic-ai/CodeRankEmbed` | HF rev (default) |
| LanceDB vectors | `db/vectors.lance.coderank/` | unchanged since 2026-04-24 |

**To re-validate this file is current:** run

```bash
git rev-parse HEAD                                       # should match ae3c1e3 or descendant with no s2f-related changes
md5 -q src/search/hybrid.py src/search/fts.py src/search/vector.py \
       src/search/hybrid_rerank.py scripts/eval/bench_steps_to_find.py
md5 -q db/knowledge.db
```

If any hash differs, the cascading-divergence mechanism described here may
have shifted (different reformulation tokens → different cascade) and the
per-task examples MUST be re-run before being cited.

Tasks already analyzed (do NOT re-run unless invalidated): see "Tasks
analyzed" table above.
