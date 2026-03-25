---
paths:
  - "**/grpc-apm-*/libs/map-response*"
  - "**/grpc-apm-*/libs/status*"
---

# Line Trace: map-response.js

Agent prompt for line-by-line auditing of a provider's response mapping layer.

## Before Starting

Read these files IN ORDER:
1. Contract: `profiles/pay-com/docs/references/field-contracts.yaml` — section `map-response-output`
2. Reference: the volt snapshot from `profiles/pay-com/docs/references/reference-snapshots.yaml` — then read the actual file at the noted lines (`raw/grpc-apm-volt/libs/map-response.js`)
3. Proto: `raw/providers-proto/service.proto` — find SaleResponse, RefundResponse, AuthorizationResponse messages
4. Target: `raw/grpc-apm-{provider}/libs/map-response.js`
5. Status map: `raw/grpc-apm-{provider}/libs/statuses-map.js` (or `status-mappings.js`, `consts.js` — check what the target imports)

## Audit Protocol

For EACH field in the return object of map-response.js:

### 1. Contract Check
- Is this field listed in `field-contracts.yaml` under `map-response-output`?
- Is it marked required or conditional?
- Does the conditionality in code match the contract? (e.g., `paymentMethod` required for sale/auth, NOT for refund)
- Is the type correct? (string, object, enum value)

### 2. Reference Comparison
- Read the SAME field in the volt reference at the exact lines from `reference-snapshots.yaml`
- Is the pattern identical? (conditional spread, fallback chain, type coercion)
- If different — is the difference justified by the provider's API shape?
- Specifically compare:
  - Conditional spread pattern: `...(value && { field: value })` vs bare assignment
  - Fallback chains: `a || b || c` — same number of fallbacks?
  - Wrapping: does volt wrap in a conditional block that the target omits?

### 3. Consumer Impact
- From `field-contracts.yaml`, read the `consumer_reads` and `if_missing` / `if_wrong` fields
- Would the current implementation cause any of the documented failure modes?
- For critical fields (`processorTransactionId`, `transactionStatus`, `finalize`): trace into the consumer file and verify compatibility

### 4. Completeness
Walk through ALL required fields in `field-contracts.yaml` `map-response-output` and verify each is present:

- **processorTransactionId** — present? always returned (even on error)? string type?
- **transactionStatus** — present? sourced from action-aware status map (not hardcoded)?
- **finalize** — present? empty `{}` when PENDING? full object when APPROVED/DECLINED?
  - **finalize.result** — matches transactionStatus?
  - **finalize.issuerResponseCode** — present? meaningful value (not always generic)?
  - **finalize.issuerResponseText** — present? human-readable?
  - **finalize.resultSource** — present? matches provider name string (e.g., `'volt'`, `'trustly'`)?
  - **finalize.timestamp** — present? ISO 8601 format? fallback to `new Date().toISOString()`?
- **approvedAmount** — conditional on APPROVED only? string type? correct amount source?
- **paymentMethod** — present for sale/auth? absent for refund? object with required subfields?
  - **paymentMethod.type** — correct value for this provider (e.g., `'bank_account'`, `'generic'`)?
  - **paymentMethod.uniqueIdentifier** — meaningful value (token, account ID, not empty string)?
- **networkTransactionId** — present if provider supplies it? conditional spread?
- **metadata** — if provider returns error details, are they captured here?

### 5. Status Mappings
Read the status mapping file imported by map-response.js:

- Is it **action-aware**? (separate maps per action type: sale, refund, payout — like volt's `statusMappings[type][providerStatus]`)
- Or is it a **flat map**? (single map for all actions — acceptable only if provider has same statuses across actions, like paysafe)
- Does EVERY provider status from the API docs have a mapping?
- For **async providers** (APM): does the initial sale response map to PENDING (not APPROVED)?
- Are there unknown/unmapped statuses that would silently fall through to ERROR or undefined?
- Does the fallback behavior match volt? (volt: `statusMappings[type][providerStatusToUse] || ERROR`)
- Compare structure with volt reference from `reference-snapshots.yaml` lines noted under `status-mappings.volt`

### 6. Error Handling Path
- What happens when the provider returns an error response?
- Is `processorTransactionId` still extracted (from error body if available)?
- Is `transactionStatus` set to DECLINED or ERROR (not left undefined)?
- Does `finalize` contain the error details (issuerResponseCode, issuerResponseText)?
- Are provider error codes mapped to gateway codes (rule 15 from provider-code-rules)?

### 7. Conditional Spread Discipline
- Are ALL optional fields wrapped in conditional spread: `...(value && { field: value })`?
- No bare assignments that would send `undefined` to the proto serializer?
- Check for subtle bugs: `...(0 && { amount: 0 })` would skip zero amounts — use explicit `!== undefined` check for numeric fields

## Output Format

For each finding:
```
Field: {contract field name}
File: {file}:{line}
Issue: {what is wrong}
Reference: {how volt does it, with file:line}
Consumer: {what breaks, with consumer file:line from field-contracts.yaml}
Severity: CRITICAL/HIGH/MEDIUM/LOW
```

Severity guide:
- **CRITICAL**: missing required field that causes crash or data loss (processorTransactionId, transactionStatus, finalize on non-PENDING)
- **HIGH**: wrong conditionality that causes incorrect behavior (paymentMethod on refund, approvedAmount always present, flat status map on async provider)
- **MEDIUM**: missing optional-but-useful field, wrong fallback, suboptimal pattern
- **LOW**: style difference from reference, extra unused field, missing metadata

## What NOT to Flag
- Extra fields the gateway does not read (harmless, just noise in proto)
- Provider-specific field names in API request construction (expected to differ between providers)
- Missing fields that are optional in both proto AND `field-contracts.yaml` AND not read by any consumer
- Different variable names for the same logical value (e.g., `response.id` vs `response.transactionId` — both valid if they map to the provider's actual transaction ID)
- Status map using a flat structure when the provider genuinely uses the same statuses for all action types (document why it is acceptable)
