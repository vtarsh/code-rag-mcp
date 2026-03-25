---
paths:
  - "**/grpc-apm-*/**"
  - "**/grpc-providers-*/**"
  - "**/workflow-provider-*/**"
  - "**/providers/**"
---

# Provider Code Rules

Rules for writing provider integration code. Derived from 38 PR review comments.
Apply whenever touching grpc-apm-*, workflow-provider-webhooks, grpc-providers-*.
See also: `lessons-active.md` (rules 16-20: impact audit calibration), `provider-docs-first.md` (doc research protocol).

## 1. Never Send Undefined Values

All payload fields MUST be conditional — never send `undefined` to a provider API.

```js
// WRONG:
{ phone, email, first_name }

// RIGHT:
{ ...(phone && { phone }), ...(email && { email }), ...(firstName && { first_name: firstName }) }
```

Use sanitization helpers from `@pay-com/providers-common` for user inputs (phone, email, name fields).

`sanitizeAndCutInput(value, maxLength)` from `@pay-com/providers-common/libs` does: trim + normalize unicode (ÀàÉé→AaEe) + cut to max length.

**What to sanitize** (based on real provider usage):
- **Always**: `firstName`, `lastName` — sanitize with provider's max length (or 255 default)
- **Per provider need**: `addressLine`, `city`, `state`, `zip` — card providers (nuvei, paysafe, stripe) sanitize these; APM providers (volt, ppro, trustly) usually don't
- **Never sanitize**: `email` (own format), `countryAlpha2` (2-letter ISO), `ip_address`, `currency`
- **Phone**: separate logic — check `+` prefix if provider requires it, not via sanitize

Reference: volt (APM, minimal — name only), nuvei (card, extensive — all billing fields), paysafe (card, all + country).

## 2. Reference Existing Providers

Before implementing any pattern, find an existing provider that does the same thing:

- Input sanitization -> reference volt
- Payment method format -> reference paysafe (`interac:{email}`)
- Webhook handling -> reference paynearme or ppro
- Status mapping -> reference the provider most similar to yours

Never invent a new pattern when a proven one exists in the codebase.

## 3. Status Mapping = Action + Status

Status is NEVER a simple 1:1 map. It depends on:

- Current action (sale/refund/verification/payout)
- Provider status value
- Whether flow is sync or async

Refunds are usually ASYNC — don't return APPROVED immediately.
Map to PENDING and let the webhook signal completion.

```js
// WRONG: flat status map
const STATUS_MAP = { completed: 'APPROVED', failed: 'DECLINED' }

// RIGHT: action-aware mapping
const STATUS_MAP = {
  sale:   { completed: 'APPROVED', pending: 'PENDING' },
  refund: { completed: 'APPROVED', pending: 'PENDING', initiated: 'PENDING' },
}
```

**How to determine mappings**: Check provider's status flow documentation.
- For **sale**: look at "Ending Combinations" (final states: approved, declined, expired)
- For **refund**: look at "Initial Combinations" (refund is async — best case = PENDING on our side, completed comes via webhook later)
- Always check provider docs for the **complete status list** before mapping — don't guess.

## 4. No Env Var Defaults

```js
// WRONG:
const { API_URL = 'https://sandbox.provider.com' } = process.env

// RIGHT:
const { API_URL } = process.env
```

Only `PROTO_PATH` and `PORT` have defaults. All URLs, keys, and tokens must be explicit — no fallback values.

**Why**: If env var is not set, service should FAIL immediately — not silently work against sandbox in production. The `undefined` URL will cause fetch to crash, which is the correct behavior. Dev/local environments set these via `.env` file or K8s config.

## 5. Version Alignment

Package version must be providers-proto version minus 1.
If providers-proto is at 1.29, set package to 1.28.
First release will bump to 1.29 and match.

## 6. Response Must Include processorTransactionId

Every response from `map-response.js` must include `processorTransactionId`.
Also include error details if available in the provider response body.

```js
// Always present in mapped response:
{
  processorTransactionId: response.id || response.transaction_id,
  ...(response.error && { errorMessage: response.error.message }),
}
```

## 7. Transaction Reference Format

Always: `${transactionId}aid${attempt}`
For refunds (no attempt available): `${transactionId}aid1`
Pass reference on completion, refund, and cancellation calls.

**Why `aid1` for refund**: Refund is a separate transaction in Pay.com — not a retry of the original. If refund fails, gateway creates a new refund transaction with a new `transactionId`, not a retry of the old one. So attempt is always 1.

**Where to use**: UDF fields (`udfs`), external reference IDs, `processorTransactionId` mapping — anywhere a unique transaction reference is sent to the provider.

```js
// initialize:
udfs: [`${transactionId}aid${attempt}`]

// refund (no attempt in req):
udfs: [`${transactionId}aid1`]
```

## 8. Remove Boilerplate That Doesn't Apply

- APM providers: delete e2e workflow (card providers only)
- If webhook only needs 200 response: don't add sync handling
- If provider service doesn't read webhooks from loggers-rest: don't save them
- If provider doesn't support a method: mark as X, don't leave stubs

Review every generated file and delete what the specific provider doesn't need.

## 9. Webhook Event Scoping

Handle only the specific events expected for each transaction type.
Don't process all events generically.

Example: verification expects only `cancel` from Trustly — don't handle payment events in verification webhook.

## 10. Card Field Masking

Mask ALL card-related fields in provider request logs:

- `card.number`, `card.cvv`, `card.expirationMonth`, `card.expirationYear`
- `networkToken.number`

Add to `censoredFields` in `make-api-call.js`. Never log raw card data.

## 11. Webhook Verification via IP Whitelisting

When a provider has no signature/HMAC mechanism, use IP whitelisting for webhook authentication.

Check `cf-connecting-ip` or `x-envoy-external-address` headers against the provider's known IP list.

```js
// verify-ip.js
const ALLOWED_IPS = process.env.PROVIDER_WEBHOOK_IPS?.split(',') || [];
const clientIp = req.headers['cf-connecting-ip'] || req.headers['x-envoy-external-address'];
if (!ALLOWED_IPS.includes(clientIp)) throw new Error('Unauthorized webhook source');
```

Reference: payper `verify-ip.js`.

## 12. Webhooks Are Fire-and-Forget

Webhook handlers return `200` immediately. No `compose-response` needed, no `save-webhook-as-request-log` unless the provider service actually reads them back (rare — check first).

The webhook flow is: receive -> validate -> map status -> signal workflow -> return 200.

## 13. Refund Callbacks in Webhooks

Webhooks MUST handle refund callbacks separately from sale callbacks. Route via `providerAction` field in the payload (e.g., `payment` vs `refund`).

- **Sale callbacks**: use standard completion workflow signal
- **Refund callbacks**: use `signalAsyncProcessingWorkflow` to notify the refund workflow

```js
if (providerAction === 'refund') {
  await signalAsyncProcessingWorkflow(/* refund signal */);
} else {
  await completePaymentWorkflow(/* sale signal */);
}
```

## 14. Async Refund Flow Setup

Most APM providers process refunds asynchronously. Two changes required:

1. **grpc-payment-gateway** `refund.js`: set `async: true` for the provider in the refund config variable
2. **workflow-provider-webhooks**: handle refund webhook via `signalAsyncProcessingWorkflow`

Without both, refunds will either timeout (missing async flag) or never complete (missing webhook handler).

## 15. Error Code Mapping

Map provider-specific error codes to gateway error codes. Don't return generic `PE` (Processing Error) for every failure.

Check the provider's error codes documentation and map to the closest gateway error:
- Authentication failures -> `AE` (Authentication Error)
- Insufficient funds -> `IF`
- Invalid card/account -> `IC`
- Provider-specific declines -> map to closest match

```js
const ERROR_MAP = {
  'insufficient_funds': 'IF',
  'invalid_account': 'IC',
  'auth_failed': 'AE',
};
// Fallback to PE only for truly unknown errors
```

Also include `providerErrorCode` and `providerErrorMessage` conditionally when available in the provider's error response.

## 16. Fallback Dates

Always provide a fallback date if the webhook payload doesn't include a timestamp. Use `new Date().toISOString()` as the generated fallback.

```js
const processedAt = webhookPayload.completed_at || new Date().toISOString();
```

Never let a missing date field cause a crash or produce `undefined` in the mapped response.

## 17. Item Description Format

Use generic description text, not transaction-specific identifiers.

```js
// WRONG:
description: `Transaction ${transactionId}`

// RIGHT:
description: 'Online payment'
```

**Why**: Provider-facing descriptions should be human-readable and not leak internal IDs. Some providers display this to the end customer.
