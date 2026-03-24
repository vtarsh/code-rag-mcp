---
paths:
  - "**/grpc-apm-*/**"
  - "**/grpc-providers-*/**"
  - "**/providers/**"
  - "**/docs/providers/**"
---

# Provider Documentation — Always Fetch First

## Rule: Never Assume API Formats

Before making ANY claim about a provider's API response format, error structure, status codes, or capabilities:

1. **Check if docs already exist**: `ls profiles/pay-com/docs/providers/{provider}/`
2. **If docs exist** -> read them before auditing code
3. **If docs DON'T exist** -> ask user for documentation URL before proceeding

For fetching docs, use the `/scrape-docs` skill.

## When to Apply

- **Before ANY impact audit** of a provider task (PI-*)
- **Before suggesting implementation patterns** (polling vs webhook, redirect vs direct)
- **Before claiming error format/status codes** are wrong or missing
- **When user asks about a new provider integration**

## Why This Matters (PI-60 Lesson)

Audit claimed `message?.[0]?.error` was dead code. Reality: Payper returns `message` as array of objects. The audit was WRONG because it assumed the format without checking docs or sandbox.

## Existing Provider Docs (27 providers)

```
profiles/pay-com/docs/providers/
aps, braintree, checkout, credorax, ecp, flutterwave, gumballpay,
monek, neosurf, nexi, nuvei, paylands, paynearme, paypal, payper,
paysafe, plaid, ppro, rapyd, silverflow, stripe, stripe-cashapp,
tabapay-crawl, tabapay-extract, trustly, volt, worldpay
```

Missing docs for active providers: okto, iris, ilixium
