# Ground Truth Rubric — Benchmark Q0-Q5

## Q0: Trustly verification with payment_method_type=bank_account

| # | Reference Point | File | Line | Value | Binary |
|---|----------------|------|------|-------|--------|
| 0.1 | payment_method_type = 'trustly' | grpc-providers-features/seeds.cql | 40 | 'trustly' | found/not |
| 0.2 | verification = false in seeds | grpc-providers-features/seeds.cql | 40 | false (18th col) | found/not |
| 0.3 | verificationFeatureFlag check | grpc-payment-gateway/methods/verification.js | 524 | `if (!verificationFeatureFlag)` → UnprocessableEntityError | found/not |
| 0.4 | MIT check: `if (!mit)` | grpc-apm-trustly/methods/initialize.js | 26 | 'Only MIT transactions are allowed' | found/not |
| 0.5 | setup_future_usage='off_session' → mit=true | express-api-v1/src/routes/sessions/setup.js | 269-272 | `mitReason: MIT_REASONS.FIRST, mit: true` | found/not |
| 0.6 | Settlement NOT required for Trustly | seeds.cql: external/internal_settlement=false, settlement_currency_codes=[] | 40 | No settlement refs in grpc-apm-trustly | found/not |
| 0.7 | One merchant sufficient | Not determinable from code alone | — | Tribal knowledge | SKIP |

**Scoring: X/6** (0.7 skipped)

## Q1: EPX feature flags

| # | Reference Point | File | Line | Value | Binary |
|---|----------------|------|------|-------|--------|
| 1.1 | payment_method_type = 'card' | grpc-providers-features/seeds.cql | 21 | 'card' | found/not |
| 1.2 | verification = true | seeds.cql | 21 | true (18th col) | found/not |
| 1.3 | payout = false | seeds.cql | 21 | false (12th col) | found/not |
| 1.4 | payout NOT in methods/index.js | grpc-providers-epx/methods/index.js | 8-14 | 6 methods, no payout | found/not |

**Scoring: X/4** + hallucination penalty (-1 per false claim)

## Q2: Worldpay payout retry

### Layer 1: In-process retry
| # | Reference Point | File | Line | Value | Binary |
|---|----------------|------|------|-------|--------|
| 2.1 | MAX_RETRIES = 5 | grpc-providers-worldpay/methods/payout.js | 28 | 5 | found/not |
| 2.2 | N_RETRIES_WITHOUT_TIMEOUT = 2 | payout.js | 29 | 2 | found/not |
| 2.3 | RETRYABLE_ERRORS content | payout.js | 30 | 'ERROR: Visa API has timed out...' | found/not |
| 2.4 | Backoff formula | payout.js | 158 | `retry <= 2 ? 0 : (retry-2)*100` ms | found/not |

### Layer 2: Temporal workflow
| # | Reference Point | File | Line | Value | Binary |
|---|----------------|------|------|-------|--------|
| 2.5 | maximumAttempts = 9 | workflow-payout-retry/workflow.js | 15-19 | 9 | found/not |
| 2.6 | backoffCoefficient = 1 | workflow.js | 15-19 | 1 (linear) | found/not |
| 2.7 | initialInterval = '1h' | workflow.js | 15-19 | '1h' | found/not |
| 2.8 | Initial sleep('1h') before first attempt | workflow.js | 42 | sleep('1h') | found/not |
| 2.9 | Cancellation → declinePayout in nonCancellable | workflow.js | 61-82 | declinePayout + DECLINED | found/not |

### Trigger
| 2.10 | Trigger: ERROR status + FEATURE_FLAG | grpc-payment-gateway/methods/payout.js | 778-788 | activatePayoutRetryWorkflow() | found/not |

**Scoring: X/10** + hallucination penalty

## Q3: providers-proto dependencies + Initialize

| # | Reference Point | Value | Binary |
|---|----------------|-------|--------|
| 3.1 | Total direct dependents | 63 repos | ±3 tolerance |
| 3.2 | APM repos with Initialize | 24 (all except ach, evo, libra, rtp) | list match |
| 3.3 | Card repos with Initialize | 5 (nuvei, paysafe, storage, stripe, worldpay) | list match |
| 3.4 | Identify node-libs-providers-gateway as caller (not implementor) | Yes | found/not |
| 3.5 | Categorize into APM/Card/Other | Done | found/not |

**Scoring: X/5** + precision on repo counts

## Q4: Add RPC method to grpc-apm-trustly

| # | Reference Point | Source | Binary |
|---|----------------|--------|--------|
| 4.1 | Repo 1: providers-proto (proto definition first) | AI-CODING-GUIDE implied | found/not |
| 4.2 | Repo 2: node-libs-providers-gateway (explicit wrappers) | index.js inspection | found/not |
| 4.3 | Repo 3: grpc-apm-trustly — 6 steps from AI-CODING-GUIDE | AI-CODING-GUIDE.md | found/not |
| 4.3a | Step: Create payload builder in libs/payload-builders/ | AI-CODING-GUIDE | found/not |
| 4.3b | Step: Add validation in libs/validate-payload.js | AI-CODING-GUIDE | found/not |
| 4.3c | Step: Create method handler in methods/ | AI-CODING-GUIDE | found/not |
| 4.3d | Step: Register in methods/index.js (key must match RPC name) | AI-CODING-GUIDE | found/not |
| 4.3e | Step: Add error mappings | AI-CODING-GUIDE | found/not |
| 4.3f | Step: Write tests | AI-CODING-GUIDE | found/not |
| 4.4 | Repo 4: grpc-payment-gateway (routing) | Dependency analysis | found/not |
| 4.5 | Repo 5: workflow-provider-webhooks (if async) | Dependency analysis | found/not |
| 4.6 | Repo 6: grpc-providers-features/seeds.cql (feature flag) | Dependency analysis | found/not |

**Scoring: X/12**

## Q5: E2E testing for APM providers

**⚠️ DISPUTED GROUND TRUTH — see notes below**

| # | Reference Point | Source | Value | Binary |
|---|----------------|--------|-------|--------|
| 5.1 | e2e-tests repo exists | GitHub | Yes | found/not |
| 5.2 | CI dispatch via workflow_dispatch | .github/workflows/run-e2e-api.yml | payment_providers param | found/not |
| 5.3 | LOCAL=TRUE for local testing | .env.example | Required | found/not |
| 5.4 | VPN required (*.grpc.dev.int.pay.com) | .env.example URLs | Internal domains | found/not |
| 5.5 | E2E_PAYMENT_PROVIDERS controls which providers | api/ test files | forEach(provider) | found/not |
| 5.6 | CREDENTIALS in api/consts.js | consts.js | 26 card providers, 0 APM | found/not |
| 5.7 | github-run-e2e-action dispatches workflows | action.yml | workflow_dispatch | found/not |

**RESOLVED:** APM e2e scripts are gitignored, per-developer, stored locally (e.g. grpc-apm-trustly/scripts/).
They are NOT in any repo. Both agents correctly reported "no APM e2e in code" — because it's not in code.
This is TRIBAL KNOWLEDGE. Scoring should reflect that agents correctly described code state.
Additional scoring point: did agent note that custom scripts COULD exist but aren't committed?
After testing, each transaction is linked from backoffice to Jira as verification.

**Scoring: X/7** (pending Q5 ground truth resolution)
