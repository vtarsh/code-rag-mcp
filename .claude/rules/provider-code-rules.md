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

## 4. No Env Var Defaults

```js
// WRONG:
const { API_URL = 'https://sandbox.provider.com' } = process.env

// RIGHT:
const { API_URL } = process.env
```

Only `PROTO_PATH` and `PORT` have defaults. All URLs, keys, and tokens must be explicit — no fallback values.

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
