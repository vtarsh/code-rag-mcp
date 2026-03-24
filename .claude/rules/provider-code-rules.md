---
paths:
  - "**/grpc-apm-*/**"
  - "**/grpc-providers-*/**"
  - "**/workflow-provider-*/**"
  - "**/providers/**"
---

# Provider Code Rules

Rules for writing provider integration code. Derived from 25 PR review comments.
Apply whenever touching grpc-apm-*, workflow-provider-webhooks, grpc-providers-*.

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
