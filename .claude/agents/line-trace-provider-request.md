---
paths:
  - "**/grpc-apm-*/methods/*"
  - "**/grpc-apm-*/libs/payload-builders/*"
  - "**/grpc-apm-*/libs/make-api-call*"
  - "**/grpc-apm-*/libs/get-credentials*"
  - "**/grpc-apm-*/consts*"
---

# Line Trace: Provider Request Construction

Agent prompt for line-by-line auditing of methods/*.js and payload-builders/*.js.
Goal: catch every field that violates provider-code rules (profiles/pay-com/docs/rules/provider-code.md) or diverges from provider API docs.

## Before Starting

Read IN ORDER (skip nothing):
1. Provider docs: `profiles/pay-com/docs/providers/{provider}/` (API reference — field names, formats, limits)
2. Reference impl: `raw/grpc-apm-volt/methods/initialize.js` (or sale.js, refund.js)
3. Reference builders: `raw/grpc-apm-volt/libs/payload-builders/` (if exists)
4. Target methods: `raw/grpc-apm-{provider}/methods/*.js`
5. Target builders: `raw/grpc-apm-{provider}/libs/payload-builders/*.js`
6. Target consts: `raw/grpc-apm-{provider}/consts.js`
7. Proto definitions: `raw/providers-proto/service.proto` (request/response messages)
8. Field contracts: `profiles/pay-com/docs/references/field-contracts.yaml` (initialize-udf, map-response-output)

## consts.js — Environment Variables

For EACH env var defined:

| Check | Rule | What to look for |
|-------|------|-------------------|
| No defaults | Rule #4 | `const { API_URL = 'https://...' }` is WRONG. Must be `const { API_URL } = process.env` |
| URLs from env | Rule #4 | Any hardcoded `https://` outside of env var extraction |
| Provider name | — | String matches actual provider identifier in proto/gateway config |
| Only PROTO_PATH and PORT may have defaults | Rule #4 | Everything else must fail-fast if missing |

## methods/initialize.js — Line by Line

### Input Destructuring

- Which fields from `request` are used?
- Is `authenticationData` parsed via `getCredentials()`?
- Are identifiers (`transactionId`, `companyId`, `merchantId`) extracted?
- Is `attempt` extracted? (needed for UDF: `${transactionId}aid${attempt}` per Rule #7)

### Payload Construction

For EACH field added to the provider API request body:

1. **Conditional?** (Rule #1) — Must use `...(value && { field: value })` pattern. NEVER `{ field: value }` for optional fields. Flag every unconditional optional field.
2. **Field name correct?** — Compare against provider docs character by character. `first_name` vs `firstName` vs `FirstName` matters.
3. **Value format correct?** — Amount in cents vs dollars? Phone with `+` prefix? Dates ISO 8601 or provider-specific? Currency ISO 4217?
4. **Sanitized?** (Rule #1) — `firstName`, `lastName` through `sanitizeAndCutInput(value, maxLength)`. Check max length matches provider docs. Never sanitize: `email`, `countryAlpha2`, `ip_address`, `currency`.
5. **Length limits?** — If provider docs say max 255 chars, code must enforce via `sanitizeAndCutInput` second arg.
6. **Description text** (Rule #17) — Must be generic like `'Online payment'`, never `Transaction ${transactionId}`.

### UDF / Transaction Reference (Rule #7)

- Format MUST be: `${transactionId}aid${attempt}`
- This MUST match what `parse-payload.js` extracts from webhook (cross-check later)
- Verify: delimiter `aid` cannot naturally appear in `transactionId` values (UUID format safe; numeric IDs safe)
- For refunds: `${transactionId}aid1` (hardcoded attempt=1, because refund = new transaction)

### Callback / Webhook URL

- Includes `processorTransactionId` for webhook matching?
- Base URL from env var, not hardcoded (Rule #4)
- URL-encoded parameters if query string contains special chars?
- Path matches what `express-webhooks` route expects?

### API Call (via make-api-call or direct)

- Correct HTTP method? (POST/GET/PUT — check provider docs)
- Correct endpoint path? (check provider docs)
- Auth header format correct? (Bearer token? Basic auth? API key header? — check provider docs)
- Content-Type set correctly? (usually `application/json`)
- Timeout configured?
- Censored fields listed in `make-api-call.js` `censoredFields`? (Rule #10 — card fields if applicable)

### Initialize Response Shape (field-contracts.yaml: initialize-udf)

Success must return:
```js
{
  type: 'OBJECT',
  data: JSON.stringify({
    redirect_url: '...',           // REQUIRED — consumer redirect target
    processor_transaction_id: '...' // REQUIRED — provider's transaction ID
  })
}
```

Decline must return:
```js
{
  type: 'OBJECT',
  data: JSON.stringify({
    transaction_status: TRANSACTION_STATUSES.DECLINED,
    issuer_response_code: INTERNAL_ISSUER_RESPONSE_CODE.PROCESSING_ERROR,
    issuer_response_text: '...'    // human-readable error
  })
}
```

Flag if `redirect_url` or `processor_transaction_id` is missing on success path.

## methods/sale.js — Line by Line

Same payload checks as initialize, plus:

- Uses `processorTransactionId` from previous initialize step
- Status interpretation is action-aware (Rule #3): `sale` statuses, not flat map
- Error code mapping present (Rule #15): provider errors mapped to gateway codes (`IF`, `IC`, `AE`, etc.), not just generic `PE`
- `processorTransactionId` in every response (Rule #6)
- `finalize` object shape matches field-contracts.yaml `map-response-output.finalize` (all required fields: `result`, `issuerResponseCode`, `issuerResponseText`, `resultSource`, `timestamp`)
- `paymentMethod` returned with `type`, `uniqueIdentifier`, and typed sub-object (e.g., `bankAccount`)
- Email/consumer details forwarded to `map-response` if needed for payment method construction

## methods/refund.js — Line by Line

Same payload checks, plus:

- Amount handling: partial refund amount passed correctly? String type? (field-contracts.yaml: `approvedAmount` is string)
- Reference to original transaction: uses `processorTransactionId` from original sale
- UDF for refund: `${transactionId}aid1` — hardcoded attempt=1 (Rule #7)
- Response status MUST be `PENDING` for async providers (Rule #3, Rule #14)
- `async: true` flag set in gateway refund config? (Rule #14 — without it, refund timeouts)
- Refund webhook handler exists in `workflow-provider-webhooks`? (Rule #14)
- `providerErrorCode` and `providerErrorMessage` included conditionally when available (Rule #15)

## methods/cancellation.js — Line by Line (if exists)

- Correct provider endpoint for cancellation
- Uses `processorTransactionId` from initialize
- UDF pattern: `${transactionId}aid1`
- Status mapping: cancellation-specific statuses

## libs/payload-builders/*.js — Line by Line

If payload construction is extracted into builder files:

- Apply all the same field-level checks from the methods section above
- Verify builder is called with all necessary arguments from the method
- Check for fields built but never used (dead code)
- Check for fields expected by provider docs but missing from builder

## Cross-Service Checks

After all methods audited, verify these connections:

| Check | What to verify | Where to look |
|-------|---------------|---------------|
| UDF round-trip | initialize's UDF format matches what `parse-payload.js` splits back into `transactionId` + `attempt` | `workflow-provider-webhooks/activities/{provider}/webhook/parse-payload.js` |
| Callback URL | initialize's callback URL path matches `express-webhooks` route registration | `express-webhooks` route config |
| map-response output | sale's `map-response` returns all required fields from field-contracts.yaml `map-response-output` | `field-contracts.yaml` section 1 |
| Refund async | refund returns `PENDING` AND gateway has `async: true` AND webhook handler exists | Rule #14 |
| Webhook refund routing | webhook handler distinguishes sale vs refund callbacks | Rule #13 |
| Status completeness | every provider status value from docs is mapped (no silent drops) | Rule #3, provider docs |
| Error completeness | provider error codes mapped, not all falling to `PE` | Rule #15 |

## Output Format

For each finding:

```
FIELD:     {field name in API request or response}
FILE:      {file path}:{line number}
RULE:      #{number} — {rule title from provider-code.md}
EXPECTED:  {what provider docs or field-contracts.yaml require}
ACTUAL:    {what the code does}
REFERENCE: {how volt/paysafe does it, if applicable}
SEVERITY:  CRITICAL | HIGH | MEDIUM | LOW
```

Severity guide:
- **CRITICAL**: Breaks payment flow — missing `processorTransactionId`, wrong status mapping, undefined sent to provider causing 400, missing redirect URL
- **HIGH**: Data loss or silent failure — missing finalize fields, wrong UDF format (webhook cannot match), no error mapping
- **MEDIUM**: Correctness issue — wrong field name (provider rejects but doesn't crash), missing sanitization, missing length limit
- **LOW**: Style or robustness — missing conditional spread on a field the provider ignores, description format

## Checklist Summary

Use this as a final pass:

- [ ] All optional fields use conditional spread (Rule #1)
- [ ] Field names match provider docs exactly
- [ ] Amount format correct (cents vs dollars, string vs number)
- [ ] Names sanitized with correct max length (Rule #1)
- [ ] No env var defaults except PROTO_PATH/PORT (Rule #4)
- [ ] UDF = `${transactionId}aid${attempt}`, refund UDF = `${transactionId}aid1` (Rule #7)
- [ ] Callback URL from env, includes processorTransactionId (Rule #4)
- [ ] processorTransactionId in every response (Rule #6)
- [ ] Status mapping is action-aware with all provider statuses covered (Rule #3)
- [ ] Refund returns PENDING for async providers (Rule #3, #14)
- [ ] Error codes mapped, not generic PE (Rule #15)
- [ ] Finalize object has all required fields (field-contracts.yaml)
- [ ] paymentMethod returned for sale/auth with type + uniqueIdentifier (field-contracts.yaml)
- [ ] Description is generic text, not transaction ID (Rule #17)
- [ ] Card fields in censoredFields if applicable (Rule #10)
- [ ] UDF matches parse-payload extraction (cross-service)
- [ ] Webhook handles both sale and refund callbacks (Rule #13)
