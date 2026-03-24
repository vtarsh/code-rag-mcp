---
paths:
  - "**/grpc-apm-*/**"
  - "**/grpc-providers-*/**"
  - "**/workflow-provider-*/**"
  - "**/src/**"
---

# Impact Audit Rules

## Severity Criteria

| Severity | Definition | Examples |
|----------|-----------|----------|
| CRITICAL | Breaks core functionality, data corruption, payment failure | Wrong transaction ID mapping, missing required API field |
| HIGH | Degraded functionality observable in production | Silent error swallowing that loses payment status |
| MEDIUM | Edge case that could cause issues under specific conditions | Unmapped status code with safe fallback to DECLINED |
| LOW | Code quality, nice-to-have improvements | Missing unit tests, extra status mappings, cleanup |

**Key rule**: If the fallback behavior is safe (e.g., unknown status -> DECLINED), it's MEDIUM at most, not HIGH.

## Verification Protocol (5 checks before flagging)

Before flagging ANY issue:

1. **Check provider docs** — does the provider actually send this field/status/error format? (`ls profiles/pay-com/docs/providers/{provider}/`)
2. **Check platform generic handling** — does the platform already handle this generically? (expiration workflows, generic callbacks, proto enums)
3. **Check if it exists already** — verify types/schemas/routes actually need changes before claiming they're missing
4. **Check sandbox evidence** — has this been observed in sandbox/webhook.site testing? If not, defer to e2e.
5. **Check responsibility boundary** — is this backend, frontend, or infra responsibility?

## Scope Rules

### Missing repos — verify before flagging
- Generic APM callback routes already exist — don't flag express-api-callbacks as missing
- Proto enums for payment methods may already include the new type — check first
- Schema repos are rarely provider-specific — verify actual need

### Tests
- Missing tests are valid but LOW severity for initial PR
- Don't flag test absence as HIGH for initial implementation

### Webhooks
- Check if provider documents webhook behavior before assuming patterns
- Refund flows may be synchronous — don't assume webhook-driven without checking docs

## Anti-Patterns (from PI-60 calibration)

1. Assuming API error format without checking docs (e.g., `message?.[0]?.error` "dead code" that wasn't)
2. Flagging generic platform concerns as provider-specific issues
3. Overstating severity of edge cases with safe fallbacks
4. Claiming repos are missing when generic routes already handle the case
5. Flagging theoretical issues not observed in sandbox
6. Confusing backend vs frontend responsibility (e.g., redirect flag)
7. Assuming webhook-driven flows without verifying provider supports webhooks for that operation
8. Flagging status mapping gaps when sandbox never produces those statuses
