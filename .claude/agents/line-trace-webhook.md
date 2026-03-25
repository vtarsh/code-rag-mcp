---
paths:
  - "**/workflow-provider-webhooks/activities/**/parse-payload*"
  - "**/workflow-provider-webhooks/activities/**/handle-activities*"
---

# Line Trace: Webhook Handler

Agent prompt for line-by-line auditing of parse-payload.js and handle-activities.js.

## Before Starting

Read IN ORDER:
1. Contract: field-contracts.yaml → `parse-payload-output` and `handle-activities-gateway-call`
2. Reference parse: trustly parse-payload.js (lines from reference-snapshots.yaml)
3. Reference handle: trustly handle-activities.js (lines from reference-snapshots.yaml)
4. Reference async: paysafe handle-activities.js → signalAsyncProcessingWorkflow lines
5. Target parse: raw/workflow-provider-webhooks/activities/{provider}/*/parse-payload.js
6. Target handle: raw/workflow-provider-webhooks/activities/{provider}/*/handle-activities.js
7. Provider docs: profiles/pay-com/docs/providers/{provider}/ (webhook payload format)

## parse-payload.js — Line by Line

For EACH field extracted from webhook body:

### 1. Field Extraction
- Is the path correct? (check provider docs for actual webhook payload structure)
- Is optional chaining used for optional fields?
- What happens if field is missing? (silent undefined? crash?)

### 2. Required Fields Check
- processorTransactionId — extracted? from which field?
- Transaction identifier — how is original transaction found? (UDF? processorTransactionId? URL path?)
- Provider status — extracted and mapped?
- Action/method type — determined? (payment vs refund vs payout)

### 3. UDF Parsing (if applicable)
- Is UDF format consistent with what initialize.js sends?
- What delimiter is used? Is it safe? (e.g., 'aid' in transactionId could break split)
- What happens if UDF format is unexpected?

## handle-activities.js — Line by Line

### First Call (no workflowParsedData)
For EACH field in the return object:
1. workflowId — constructed correctly?
2. workflowParsedData — all fields from parse-payload forwarded?
3. syncFlow — set? (only if provider needs custom HTTP response body)
4. response — compose-response present? (only if provider expects non-200 body)

### Second Call (with workflowParsedData)
Line by line through the processing logic:

1. **Transaction lookup** — findTransactionByProcessorTransactionId or getTransactionDetails?
2. **Early returns** — what statuses/conditions skip processing?
3. **Status check** — is current transaction status checked before updating?
4. **Action routing** — separate handling for payment vs refund?
5. **Payment action**:
   - gatewayParametersObj — all fields from contract present?
   - callGatewaySaleMethod (or verification/authorization) — correct method?
6. **Refund action**:
   - signalAsyncProcessingWorkflow (NOT callGatewayRefundMethod)?
   - Or updateTransaction directly?
7. **Update transaction** — finalize object correct? Status correct?
8. **Notification** — sendWebhookNotification called?

### Gateway Parameters Object
Check against field-contracts.yaml handle-activities-gateway-call:
- transactionId — from transaction lookup (NOT from webhook directly)
- paymentMethod.type — correct provider/method string
- paymentMethod.token — processorTransactionId or equivalent
- paymentMethod.additionalInfo — present if provider sends bank account / sender details (trustly/volt pattern); absent if not needed (paysafe pattern)
- deviceIpAddress — from transaction.clientIp
- siteId, merchantId, companyId — from transaction

### Refund Signal Check
For async refund/payout completion, verify:
- signalAsyncProcessingWorkflow called (NOT callGatewayRefundMethod)
- Parameters: transactionId, attempt, provider name, transactionStatus, ignoreNotFoundError: true
- Condition: status in [APPROVED, DECLINED] AND type !== SALE AND type !== AUTHORIZATION
- Reference: paysafe handle-activities.js lines 360-371

## Cross-Chain Verification

After both files audited:
1. Does processorTransactionId in grpc-apm map-response match what parse-payload expects?
2. Does UDF from initialize match what parse-payload extracts?
3. Are status enum values consistent between grpc-apm status-map and webhook consts?
4. Does paymentMethod.token in gateway call match what grpc-apm returns as processorTransactionId?
5. Are finalize.resultSource values consistent (same provider string in grpc-apm and webhook handler)?

## Output Format

For each finding:
```
Field: {field name}
File: {file}:{line}
Issue: {what is wrong}
Reference: {how trustly/paysafe does it}
Chain impact: {what breaks downstream}
Severity: CRITICAL/HIGH/MEDIUM/LOW
```

Severity guide:
- CRITICAL: transaction lost, payment stuck, money impact (missing processorTransactionId, wrong status mapping, missing gateway call)
- HIGH: data corruption, silent failure (wrong finalize fields, missing notification, broken UDF parsing)
- MEDIUM: incomplete data, degraded experience (missing optional fields, wrong resultSource)
- LOW: style inconsistency, non-functional gap (naming conventions, missing optional chaining on safe paths)
