# RAG Improvement Analysis — Proactive Task Pattern Extraction

> **Context:** This document is a handoff from a deep analysis session.
> It contains ground truth data extracted from PI-60 (Payper Interac eTransfer) and cross-provider comparison.
> Goal: help the RAG development session understand what's missing and what to improve.

---

## 1. Problem Statement

Current RAG (pay-knowledge) is good at:
- Static dependency graphs (repo → repo)
- Co-change patterns from task history
- Gotchas and provider docs
- Task metadata (description, plan, decisions)

Current RAG is **missing**:
- **Implementation trace flow** — the actual file-by-file execution path (not dependency graph)
- **Churn prediction** — which files get revised most during review cycles
- **Review pattern extraction** — what themes reviewers consistently catch
- **Implementation order** — what to build first, second, third
- **Cross-task pattern synthesis** — what's common across PI-40, PI-54, PI-56, PI-60

The difference: RAG knows "these repos are connected" but doesn't know "build map-response.js last because it gets rewritten 6 times during review."

---

## 2. PI-60 Ground Truth: Git History Analysis

### 2.1 Repos and Branches

| Repo | Branch | PI-60 Commits |
|------|--------|---------------|
| grpc-apm-payper | feature/pi_60 | 12 commits |
| workflow-provider-webhooks | feature/pi_60 | 7 commits |
| express-webhooks | feature/pi_60 | 2 commits |
| grpc-providers-features | feature/pi_60 | 3 commits |
| grpc-providers-credentials | feature/pi_60 | 1 commit |
| **Total** | | **25 commits** |

### 2.2 Implementation Timeline (Phases)

**Phase 1: Scaffold (2026-03-24 morning, ~11:40)**
All 5 repos committed within seconds — coordinated initial push.

| Repo | What |
|------|------|
| grpc-apm-payper | Core provider: deleted 13 card-payment files (-698 lines), kept initialize/sale/refund |
| workflow-provider-webhooks | Webhook activities: parse-payload, compose-response, handle-activities (+178 lines) |
| express-webhooks | Webhook route: +2 lines (payper entry in provider routes) |
| grpc-providers-features | Feature flags: +28 lines in seeds.cql |
| grpc-providers-credentials | Credentials: migration, config, data-mapper, validation (+39 lines) |

**Phase 2: Tests (2026-03-24 afternoon)**
- grpc-apm-payper: 26 unit tests (+745 lines)
- workflow-provider-webhooks: 12 unit tests (+313 lines)
- Small fixes: failureMessage metadata, processor_transaction_id

**Phase 3: Review Round 1 fixes (2026-03-25)**
- **Architectural pivot:** `approved` status doesn't mean approved — means "request accepted, pending bank"
- Two-level webhook status map (tx_action → status → internal status)
- IP whitelist verification added
- Async refund via signalAsyncProcessingWorkflow
- Dead code removed (compose-response, save-webhook-as-request-log)
- 8 commits across 2 repos

**Phase 4: Bug fixes between rounds (2026-03-26)**
- Proto field naming: `consumerDetails` → `consumer`
- Proto version pinning: ^1.28.0
- processorTransactionId placement fix
- 3 commits in grpc-apm-payper

**Phase 5: Review Round 2 fixes (2026-04-03)**
- Action-aware status mapping (different semantics per tx_action)
- Error code mapping module (entirely new, +171 lines → +245 rewrite)
- Fraud codes: AB (acquirer blocked) per Notion
- Expired handling + finalize in webhooks
- 5 commits across 2 repos

### 2.3 File Churn Map (grpc-apm-payper)

| File | Commits Touched | % of Total | Pattern |
|------|----------------|------------|---------|
| libs/map-response.js | 6/12 | 50% | **Highest churn** — central mapping, target of both review rounds |
| libs/statuses-map.js | 3/12 | 25% | Revised: flat → per-action mapping |
| libs/error-code-mapping.js | 3/12 | 25% | **Late addition** — added in round 2, then rewritten |
| methods/initialize.js | 2/12 | 17% | Relatively stable |
| methods/sale.js | 2/12 | 17% | Relatively stable |
| methods/refund.js | 1/12 | 8% | Most stable handler |

**Key insight:** Response mapping and status/error mapping account for 75% of revisions. Handlers are stable after initial scaffold.

---

## 3. PI-60 Ground Truth: PR Review Analysis

### 3.1 Review Metrics

- **Total inline comments:** ~42 across all repos
- **Review rounds:** 3 (Mar 24, Mar 26-Apr 2, Apr 3)
- **Reviewer:** vboychyk (sole reviewer)
- **PRs:** 5 total, 2 approved (credentials immediately, express-webhooks round 2), 3 still pending

### 3.2 Review Themes (grouped by frequency)

#### Theme 1: ERROR MAPPING (5 comments, most significant)
- Initial: simple/internal error codes used
- Required: map EACH Payper error code to ISO-style issuer response codes
- Reference: `grpc-apm-nuvei/libs/err-code.js`, `node-libs-tools/get-default-issuer-response-codes.ts`
- Quote: "this is not correct. We should map each code to ours"

#### Theme 2: STATUS MAPPING / ASYNC FLOW (5 comments)
- Status depends on BOTH tx_action (sale vs refund) AND status value
- Refund is always async: needs ASYNC_FLOW_PROCESSORS + webhook signal
- Sale uses "Ending Combinations", Refund uses "Initial Combinations"
- Quote: "this is not that simple — it's based on combination of current action + status"

#### Theme 3: RESPONSE FIELDS (7 comments)
- processorTransactionId missing initially
- paymentMethodToken format: `interac:{email}` (ref: paysafe)
- reusablePayments vs reusablePayouts were swapped
- Phone/email from response, not request
- No paymentMethod for refunds

#### Theme 4: WEBHOOK HANDLING (5 comments)
- Boilerplate had unnecessary compose-response and save-webhook-as-request-log
- IP whitelist verification needed (no HMAC available)
- Must handle BOTH sale AND refund notifications
- Finalize needed for both

#### Theme 5: BOILERPLATE CLEANUP (5 comments)
- e2e CI job from card boilerplate — doesn't apply to APMs
- SYNC_WORKFLOWS entry unnecessary (HTTP 200 is default)
- Hardcoded env defaults no longer used
- seeds.cql must match existing one-line format

#### Theme 6: INPUT SANITIZATION (3 comments)
- Reference: volt's sanitize helper
- Phone format: must include `+` prefix
- All user fields conditional

#### Theme 7: METADATA (2 comments)
- failureCode/failureMessage must propagate to metadata

#### Theme 8: FEATURE FLAGS (2 comments)
- seeds.cql formatting must match existing convention
- external_settlement value must match actual provider behavior

### 3.3 Recurring Correction Patterns

1. **Boilerplate assumptions** — card-provider boilerplate has artifacts that don't apply to APMs
2. **Oversimplified mappings** — initial pass always too simple; reviewer pushes for action-aware + ISO codes
3. **Missing async understanding** — refund treated as sync initially
4. **Field precision** — exact field names, conditional presence, which response fields map where
5. **Reference drift** — reviewer consistently points to existing providers as the authority

---

## 4. Cross-Provider Pattern Comparison

### 4.1 Common Skeleton (ALL 7 APM providers)

```
grpc-apm-{provider}/
├── index.js                    # gRPC server (Mali) — identical across providers
├── consts.js                   # ENV config + provider constants
├── methods/
│   ├── index.js                # Barrel export + method aliases
│   ├── initialize.js           # Create payment session
│   ├── sale.js                 # Check status / process sale
│   └── refund.js               # Process refund
├── libs/
│   ├── index.js                # Barrel export
│   ├── get-credentials.js      # Parse authenticationData JSON
│   ├── make-api-call.js        # HTTP wrapper for provider API
│   ├── map-response.js         # Provider response → pay-com format
│   └── payload-builders/
│       ├── index.js
│       └── get-*-payload.js    # Per-method payload builders
└── tests/                      # Mirrors src structure
```

### 4.2 Standard Handler Pattern (8 steps, ALL providers)

```js
module.exports = async ({ req }) => {
  // 1. Destructure identifiers
  // 2. addRequestContext (logging)
  // 3. getCredentials(authenticationData)
  // 4. Build payload via payload builder
  // 5. makeApiCall({ method, path, payload, identifiers, credentials })
  // 6. mapResponse({ response, type, processorTransactionId })
  // 7. Log success
  // 8. Return mapped response
}
```

### 4.3 Provider-Specific Divergence Points

| What Varies | Examples |
|-------------|---------|
| Payload builders | Content varies by provider API shape |
| Error code mapping | Each provider has unique error codes → ISO mapping |
| Status mapping | Provider-specific statuses → TRANSACTION_STATUSES |
| Additional libs | Signing (trustly: RSA-SHA512, volt: RS256 JWS, paynearme: HMAC-SHA256) |
| Method coverage | Some have completion, verification, payout |
| Auth mechanism | Bearer token, HMAC, RSA signatures, OAuth |

### 4.4 Reference Map (which provider to look at for what)

| Need | Best Reference | Why |
|------|---------------|-----|
| Simplest APM | grpc-apm-paynearme | 2 methods, minimal complexity |
| Error code mapping | grpc-apm-nuvei/libs/err-code.js | Most complete ISO mapping |
| Sanitize helpers | grpc-apm-volt | sanitizeAndCutInput pattern |
| Payment method token | grpc-apm-paysafe | `{type}:{identifier}` format |
| Webhook handler | grpc-apm-paysafe (in workflow-provider-webhooks) | Clean handle-activities pattern |
| Multi-step flows | grpc-apm-trustly | Completion polling, signature verification |
| OAuth + signing | grpc-apm-volt | RS256 JWS token generation |

---

## 5. MCP RAG (pay-knowledge) Current State Assessment

### 5.1 Consistency Test Results

| Test | Result |
|------|--------|
| Identical keyword query x2 | **Deterministic** — same results |
| Similar keyword query | **Consistent top results** (apm-reference-ranking always first) |
| Vector search | **BROKEN** — Gemini free tier quota exceeded (429 RESOURCE_EXHAUSTED) |
| analyze_task | **Powerful but noisy** — 17 core + 17 related + 64 peripheral for simple APM |
| trace_flow | **Static graph** — shows dependency edges, not implementation flow |

### 5.2 What RAG Returns Well

- **Gotchas** from `docs/GOTCHAS.md` per repo — directly actionable
- **Task metadata** (description, plan, decisions, API spec) — good context
- **Historical task patterns** — which repos were involved in similar past tasks
- **Co-change patterns** — "when X changes, also check Y"
- **File-level hub detection** — "config.js changed in 16 tasks"
- **Cross-repo file pairs** — "express-webhooks/index.js → workflow-provider-webhooks/activities/index.js (100%)"

### 5.3 What RAG Doesn't Return (Gaps)

| Gap | What's Missing | Impact |
|-----|---------------|--------|
| Implementation order | Which files to create first | Developer builds map-response.js first, rewrites it 6 times |
| Churn prediction | Which files will have most revisions | Can't prioritize "get this right first" |
| Review patterns | What reviewer will focus on | Same mistakes repeated across tasks |
| Trace flow (runtime) | Actual gRPC req → handler → libs → HTTP chain | Developer doesn't see the data flow |
| Cross-task synthesis | Common patterns across PI-40/54/56/60 | Each task starts from scratch |
| Error mapping reference | Which existing provider to copy error mapping from | Developer creates from scratch instead of adapting |

### 5.4 Noise Issues

- `analyze_task` returns 64 peripheral repos (npm dependencies) for a simple APM task
- Co-occurrence patterns include unrelated repos (grpc-bank-accounts → grpc-onboarding-entity: 100%)
- npm dependency scan matches generic keywords (`initialize`, `provider`) across entire org
- No confidence threshold filtering — LOW confidence repos mixed with HIGH

---

## 6. Proposed Improvements for RAG

### 6.1 New Data Types to Index

```yaml
# Type: implementation_trace
# Source: extracted from git history of completed tasks
# Purpose: show actual file-level implementation flow

type: implementation_trace
task: PI-60
provider: payper
phases:
  scaffold:
    order: [methods/initialize.js, methods/sale.js, methods/refund.js, libs/make-api-call.js, libs/map-response.js]
    cross_repo: [grpc-providers-credentials, grpc-providers-features, express-webhooks, workflow-provider-webhooks]
  tests:
    order: [tests/methods/*.spec.js, tests/libs/*.spec.js]
  review_fixes:
    high_churn: [libs/map-response.js, libs/statuses-map.js, libs/error-code-mapping.js]
    late_additions: [libs/error-code-mapping.js]
```

```yaml
# Type: review_pattern
# Source: extracted from PR comments
# Purpose: predict what reviewer will focus on

type: review_pattern
task: PI-60
reviewer: vboychyk
themes:
  - name: error_mapping
    frequency: 5
    severity: high
    reference: grpc-apm-nuvei/libs/err-code.js
    quote: "this is not correct. We should map each code to ours"
  - name: status_mapping
    frequency: 5
    severity: high
    reference: null
    quote: "this is not that simple — it's based on combination of current action + status"
  # ... etc
```

```yaml
# Type: churn_prediction
# Source: aggregated from multiple completed tasks
# Purpose: tell developer which files to invest more time in

type: churn_prediction
task_type: new_apm_provider
sample_size: 4  # PI-40, PI-54, PI-56, PI-60
predictions:
  - file: libs/map-response.js
    avg_revisions: 6
    probability_of_rewrite: 0.85
    recommendation: "Invest extra time. Study reference provider first."
  - file: libs/statuses-map.js
    avg_revisions: 3
    probability_of_rewrite: 0.70
    recommendation: "Always make action-aware from the start."
  - file: libs/error-code-mapping.js
    avg_revisions: 3
    probability_of_rewrite: 0.60
    recommendation: "Will likely be added during review. Proactively create using nuvei as reference."
```

### 6.2 Improvements to Existing Tools

**analyze_task:**
- Add confidence threshold parameter (default: filter out LOW confidence peripheral repos)
- Separate npm dependencies from actual task-relevant repos
- Include implementation_trace if available for similar past tasks
- Include review_patterns if available for similar past tasks
- Add churn_prediction for task type

**trace_flow:**
- Add mode: "implementation" (file-level within repo, not just repo-level graph)
- Show the actual handler → libs → HTTP chain, not just dependency edges

**search:**
- Fix vector search (Gemini quota or switch to local embeddings)
- Add file_type: "implementation_trace", "review_pattern", "churn_prediction"

### 6.3 New Tool Proposals

**extract_task_pattern(task_id, repos[])**
- Reads git history across repos for the task branch
- Extracts implementation phases, churn map, cross-repo coordination
- Generates implementation_trace + churn_prediction documents
- Stores in RAG database

**get_review_insights(task_type OR provider)**
- Returns aggregated review patterns for similar tasks
- Ranked by frequency and severity
- Includes reference implementations per theme

**predict_implementation(task_description)**
- Based on past implementation_traces for similar task types
- Returns recommended implementation order with churn warnings
- Returns review checklist with evidence

---

## 7. Concrete Example: What RAG Should Return for "New APM Provider"

### Current `analyze_task` output (simplified):
```
Repos: 17 core + 17 related + 64 peripheral
Gotchas: 7 from payper
Proto: sale, refund, initialize exist
Checklist: 21 items
```

### Desired enriched output:
```
Repos: 5 essential (ranked by implementation order)
  1. grpc-apm-{provider} — scaffold first
  2. grpc-providers-credentials — same day
  3. grpc-providers-features — same day
  4. express-webhooks — 2 lines
  5. workflow-provider-webhooks — after provider methods work

Implementation trace (from 4 past tasks):
  Day 1 AM: methods/ + libs/ + credentials + features + webhook route
  Day 1 PM: tests (26 unit avg for provider, 12 for webhooks)
  Day 2-3: review fixes (expect 2-3 rounds)

Churn warning:
  HIGH: map-response.js (85% chance of rewrite)
  HIGH: statuses-map.js (70% chance, make action-aware from start)
  MEDIUM: error-code-mapping.js (60% chance of late addition)

Review checklist (from 42 real comments):
  1. Error codes → ISO mapping (ref: nuvei/err-code.js)
  2. Status mapping must be action-aware
  3. Async refund needs ASYNC_FLOW_PROCESSORS
  4. Delete card-provider boilerplate artifacts
  5. Input sanitization with helpers (ref: volt)
  6. IP whitelist OR HMAC for webhooks

Reference providers:
  Error mapping: nuvei | Sanitize: volt | Token format: paysafe
  Simplest APM: paynearme | Webhook handler: paysafe
```

---

## 8. Meta-Analysis: "Hints in Prompt" vs "Learned Patterns"

The current system has knowledge in 3 places:
1. **Claude memory** — static playbooks, manually maintained
2. **MCP RAG** — repo graph + docs, no trace flow
3. **Git history** — ground truth, but requires fresh analysis each time

The problem: Claude's memory contains "rules" like "map errors to ISO codes" — but this is a hint, not a learned pattern. If you remove the memory, Claude makes the same mistakes as PI-60 round 1.

**What "learned" means:**
- Evidence-based: "map-response.js was revised in 6/12 commits across PI-60" (not "be careful with response mapping")
- Quantified: "85% probability of rewrite" (not "this might need changes")
- Referenced: "use nuvei/err-code.js as template" (not "check existing providers")
- Predictive: "for new APM, expect 2-3 review rounds focused on error mapping and status semantics"

The RAG should store these as structured data, not prose. Future sessions query the data and get evidence, not hints.
