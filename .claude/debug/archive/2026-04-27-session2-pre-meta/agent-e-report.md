# Agent E — glossary surgery report

Applied min-viable surgical edits to `profiles/pay-com/glossary.yaml` per Agent C analysis at `.claude/debug/current/w2-rootcause.md`.

## Hashes

- **Pre-edit md5**: `62ffa1cd56c44f53acd2efb46a6fd349` (127 lines)
- **Post-edit md5**: `717156f46ee647b0fcb9e49c7a7898ba` (126 lines)
- `git diff` empty (file is gitignored — confirmed via direct read).

## Surgical edits applied

### 1. DELETE `nuvei` (line 34)

- **Before**: `nuvei: "nuvei"`
- **After**: (entry removed)
- Rationale: self-expansion `a→a` adds no information; only re-weights IDF on FTS5 OR-mode. Caused 9 flips per Agent C.

### 2. TRIM `webhook` (line 62 → 61)

- **Before**: `webhook: "webhook callback notification async DMN express-webhooks workflow-provider-webhooks"`
- **After**: `webhook: "webhook callback notification"`
- Dropped: `async`, `DMN`, `express-webhooks`, `workflow-provider-webhooks` (these matched Stripe/Volt/temporal code paths and dominated doc-intent ranking). Caused 14 flips.

### 3. TRIM `amount` (line 65 → 64)

- **Before**: `amount: "amount conversion minor units smallest unit cents exponent formatAmountFromExponent unFormatAmount formatAmountToSmallestUnit unFormatAmountFromSmallestUnit formatPrecision formatToExponent parseFloat decimal"`
- **After**: `amount: "amount currency conversion"`
- Dropped: 17 camelCase JS function names + `minor units smallest unit cents exponent`. Caused 5 flips by guaranteeing JS-utility files outrank doc pages.

### 4. TRIM `payout` + `payouts` (lines 72-73 → 71-72)

- **Before**:
  - `payouts: "payout settlement disbursement withdraw cash-out cashout payouts-and-funding"`
  - `payout: "payout settlement disbursement withdraw cash-out cashout"`
- **After**:
  - `payouts: "payout payouts"`
  - `payout: "payout payouts"`
- Dropped: `settlement`, `disbursement`, `withdraw`, `cash-out`, `cashout`, `payouts-and-funding`. Caused 7 flips by matching generic payout code routes (`routes/payout.js`).

### 5. TRIM `currency` (line 124 → 123)

- **Before**: `currency: "fx exchange-rate currency-code"`
- **After**: `currency: "currency-code"`
- Dropped: `fx`, `exchange-rate` (compounded with `amount` co-fire to add 21 tokens). Caused 4 flips.

### 6. TRIM `retry` (line 56 → 55)

- **Before**: `retry: "retry backoff timeout retryable nonRetryable temporal"`
- **After**: `retry: "retry backoff retryable"`
- Dropped: `temporal`, `timeout`, `nonRetryable`. Caused 6 flips by anchoring to `temporal-workflows.md` noise.

### 7. TRIM `refund` + `refunds` (lines 58, 71 → 57, 70)

- **Before**:
  - `refund: "refund void cancel reversal chargeback dispute"`
  - `refunds: "refund void cancel reversal chargeback dispute"`
- **After**:
  - `refund: "refund refunds void cancel reversal"`
  - `refunds: "refund refunds void cancel reversal"`
- Dropped: `chargeback`, `dispute` cross-references. Caused 5 flips by pulling off-topic dispute UI / paynearme reverse-payment to top.

## Smoke test output

Command:

```bash
CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 -c "
from src.search.fts import expand_query
for q in ['webhook signature', 'aircash refund method', 'trustly payment retry', 'amount conversion currency', 'nuvei addUPOAPM']:
    print(repr(q), '->', repr(expand_query(q)))
"
```

Output:

```
'webhook signature' -> 'webhook signature webhook callback notification'
'aircash refund method' -> 'aircash refund method refund refunds void cancel reversal'
'trustly payment retry' -> 'trustly payment retry retry backoff retryable'
'amount conversion currency' -> 'amount conversion currency amount currency conversion currency-code'
'nuvei addUPOAPM' -> 'nuvei addUPOAPM'
```

### Verification of surgery

| query | noise removed |
|-------|---------------|
| `webhook signature` | no longer adds `async DMN express-webhooks workflow-provider-webhooks` |
| `aircash refund method` | no longer adds `chargeback dispute` |
| `trustly payment retry` | no longer adds `temporal timeout nonRetryable` |
| `amount conversion currency` | no longer adds 17 JS function names + `minor units` cluster |
| `nuvei addUPOAPM` | no expansion at all (self-expansion entry deleted) |

All 5 sentinel queries from Agent C's flip-list show clean expansion now. Token blow-up reduced from `+9.4 tokens / 1.26× growth` to `+3 tokens or fewer` for all sentinel cases.

## Out-of-scope notes

- Per spec "DO NOT modify entries OTHER than the 7 listed above": left untouched
  - `webhooks` (plural of webhook) — still has full noisy expansion. Spec explicitly enumerated `webhook` (singular). If `webhooks`-trigger flips persist on next bench, recommend a follow-up edit with explicit user approval.
  - `dispute` / `disputes` / `chargeback` — still cross-reference each other (Agent C noted `dispute` itself caused 1 flip — kept per spec).
- All provider-name entries (`silverflow`, `stripe`, etc.) preserved.
- File line count: 127 → 126 (one entry deleted, others trimmed in place).

## Final state

- **md5**: `717156f46ee647b0fcb9e49c7a7898ba`
- **lines**: 126
- **path**: `/Users/vaceslavtarsevskij/.code-rag-mcp/profiles/pay-com/glossary.yaml`
- No bench re-run (per spec). No other files touched.
