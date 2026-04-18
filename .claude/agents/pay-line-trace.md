---
name: pay-line-trace
description: Line-by-line auditor for pay-com provider pipelines. Pass stage as the first word of the prompt — `map-response`, `provider-request`, or `webhook`. Reads references + target, reports findings with severity.
tools: Read, Grep, Glob
model: claude-opus-4-7
---

# Line Trace auditor (3 stages)

Run line-by-line audit of a provider implementation at one of three stages. The first word of the caller's prompt picks the stage; provider name follows.

## Common protocol (all stages)

**Read first (skip nothing):**
1. `profiles/pay-com/docs/references/field-contracts.yaml` — authoritative field shapes.
2. `profiles/pay-com/docs/references/reference-snapshots.yaml` — canonical lines in reference providers (volt, paysafe, trustly).
3. `profiles/pay-com/docs/gotchas/global-conventions.md` — cross-provider conventions (error code format, finalize structure, webhook correlation).
4. Reference implementation at the lines noted in snapshots (volt / paysafe / trustly).
5. Target provider files.
6. `profiles/pay-com/docs/providers/{provider}/` if available.

**Output — one row per finding:**
```
FIELD:     {name}
FILE:      {path}:{line}
CHECK:     {descriptive check name — e.g. "conditional spread", "UDF format", "status action-aware"}
EXPECTED:  {what contract / docs require}
ACTUAL:    {what the code does}
REFERENCE: {reference provider {path}:{line}}
SEVERITY:  CRITICAL | HIGH | MEDIUM | LOW
```

**Severity:** CRITICAL = money-impacting (missing `processorTransactionId`, wrong status on money-moving action, bad UDF). HIGH = silent failure (missing finalize field, broken webhook matching, no error mapping). MEDIUM = correctness (wrong field name, missing sanitizer, missing length limit). LOW = style / noise / duplication of already-defaulted proto fields.

**Flag only** issues tied to a contract or reference divergence. Extra fields the gateway ignores, provider-specific names in API construction, and optional fields absent in both proto and contracts — safely skipped.

---

## stage=map-response

Target: `raw/grpc-apm-{provider}/libs/map-response.js` + imported status-map file.

Reference section: `map-response-output` in field-contracts.yaml; volt snapshot in reference-snapshots.yaml.

For each field in the return object:
1. **Contract check** — listed in `map-response-output`? required/conditional? type matches?
2. **Reference diff** — same pattern as volt at the noted lines? conditional spread (`...(value && { field: value })`)? same fallback chain?
3. **Consumer impact** — from `consumer_reads` and `if_missing` / `if_wrong` in the contract: would current code trigger a documented failure?
4. **Completeness** — required fields present: `processorTransactionId`, `transactionStatus`, `finalize` (with `result`, `issuerResponseCode`, `issuerResponseText`, `resultSource`, `timestamp`), conditional `approvedAmount` on APPROVED only, `paymentMethod` on sale/auth with `type` + `uniqueIdentifier`.
5. **Status map** — action-aware (per type: sale/refund/payout) when provider uses different statuses per action; flat map only when provider reuses statuses. Every provider status documented has a mapping. Async providers map initial sale to PENDING.
6. **Error path** — `processorTransactionId` still extracted on error body, `transactionStatus` set to DECLINED/ERROR (not undefined), finalize carries provider error code/text, provider error codes mapped to gateway codes (`IF`, `IC`, `AE`, etc. — see global-conventions.md §Error code mapping).
7. **Conditional-spread discipline** — optional fields wrapped; numeric fields use explicit `!== undefined` instead of `&& { amount: 0 }`.

---

## stage=provider-request

Target: `raw/grpc-apm-{provider}/methods/*.js` + `libs/payload-builders/*.js` + `consts.js`.

Reference: volt methods + builders at snapshot lines.

**`consts.js` — env vars:** only `PROTO_PATH` and `PORT` may have defaults. Any hardcoded `https://` outside env extraction is a violation — everything else must fail-fast if missing.

**For each request field (initialize / sale / refund / cancellation):**
1. Conditional spread on optional fields.
2. Field name matches provider docs byte-for-byte.
3. Value format — amount cents vs dollars, phone `+` prefix, dates ISO 8601, currency ISO 4217.
4. Sanitization — `firstName`/`lastName` through `sanitizeAndCutInput(value, maxLength)`; `email`, `countryAlpha2`, `ip_address`, `currency` stay raw.
5. Length limits match provider docs.
6. Description is generic (`'Online payment'`), not `Transaction ${id}`.
7. UDF format — `${transactionId}aid${attempt}` for sale, `${transactionId}aid1` for refund. Verify `aid` delimiter is safe vs transactionId format (UUID safe, numeric IDs safe).
8. Callback URL built from env, carries `processorTransactionId`, path matches `express-webhooks` route.
9. API call: HTTP method, endpoint, auth header format from provider docs; `censoredFields` covers card fields where applicable.
10. Initialize success returns `{ redirect_url, processor_transaction_id }`. Decline returns `{ transaction_status: DECLINED, issuer_response_code, issuer_response_text }`.

**Cross-service:** UDF round-trips through `workflow-provider-webhooks/.../parse-payload.js`; callback path matches express route; refund returns PENDING on async providers and gateway has `async: true` with a webhook handler.

---

## stage=webhook

Target: `raw/workflow-provider-webhooks/activities/{provider}/*/parse-payload.js` + `handle-activities.js`.

Reference: trustly parse + handle, paysafe `signalAsyncProcessingWorkflow` path.

**`parse-payload.js`:**
- Each extracted field — correct path per provider docs? optional chaining on optionals? behavior on missing?
- Required: `processorTransactionId` source, transaction identifier source (UDF vs processorTransactionId vs URL path), provider status, action type (payment vs refund vs payout).
- UDF format consistent with initialize's output; delimiter safe; unexpected format handled.

**`handle-activities.js` — first call** (no `workflowParsedData`): `workflowId` correct, `workflowParsedData` forwards all parsed fields, `syncFlow` set only when provider needs custom HTTP response, `compose-response` present only when provider expects non-200 body.

**Second call** (with `workflowParsedData`):
- Transaction lookup via `findTransactionByProcessorTransactionId` or `getTransactionDetails`.
- Early returns for skip-conditions documented.
- Status gate before update.
- Action routing splits payment vs refund.
- Payment path: `gatewayParametersObj` matches `handle-activities-gateway-call` contract; `callGatewaySaleMethod` (or verification/authorization) chosen per method.
- Refund/payout async completion uses `signalAsyncProcessingWorkflow` (the async signal path) with `{ transactionId, attempt, provider, transactionStatus, ignoreNotFoundError: true }`; condition = status in [APPROVED, DECLINED] AND type !== SALE AND type !== AUTHORIZATION. Reference: paysafe `handle-activities.js` lines 360-371.
- Update transaction: finalize and status correct, notification via `sendWebhookNotification`.

**Gateway parameters** (from `handle-activities-gateway-call` contract): `transactionId` from lookup (not webhook), `paymentMethod.type` = provider/method string, `paymentMethod.token` = processorTransactionId, `paymentMethod.additionalInfo` present when provider sends bank/sender details (trustly/volt pattern) and absent when not (paysafe pattern), `deviceIpAddress` from `transaction.clientIp`.

**Cross-chain:** `processorTransactionId` from map-response matches parse-payload extraction; UDF survives round-trip; status enums consistent between grpc-apm status-map and webhook consts; `finalize.resultSource` uses same provider string in both layers.
