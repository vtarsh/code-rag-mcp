# W2 root-cause analysis — why glossary expansion hurt v2 docs by -19.83pp

Source data:
- `bench_runs/v2_e2e_fts5_only_session2.json` — h@10 = 0.6025
- `bench_runs/v2_e2e_fts5_w2_session2.json` — h@10 = 0.4099
- 161 queries, perfect index alignment, no `id` mismatches

Net effect on v2 docs:
- 38 queries flipped HIT@10 → MISS
- 7 queries recovered MISS → HIT@10
- net = −31 / 161 = **−19.25pp** (matches headline −19.83pp within rounding)

The expansion fires on every query regardless of intent, and the expansion text dominates the FTS5+vector ranking signal whenever the original query is short.

---

## Per-stratum breakdown

| stratum   | total | H→M | M→H | net  |
|-----------|------:|----:|----:|-----:|
| webhook   | 22    | 12  | 0   | −12  |
| payout    | 16    | 9   | 0   | −9   |
| nuvei     | 21    | 5   | 0   | −5   |
| method    | 16    | 2   | 0   | −2   |
| aircash   | 8     | 1   | 0   | −1   |
| trustly   | 4     | 1   | 0   | −1   |
| provider  | 20    | 2   | 1   | −1   |
| refund    | 11    | 3   | 3   | 0    |
| tail      | 34    | 3   | 3   | 0    |
| interac   | 9     | 0   | 0   | 0    |

The damage concentrates in **`webhook` (−12), `payout` (−9), `nuvei` (−5)** — exactly the strata where a single trigger token (`webhook`, `payout`, or `nuvei`) blows up into a long expansion that competes with the rest of the query.

---

## Trigger-token frequency in flipped queries

| key            | flips | expansion (head) |
|----------------|------:|------------------|
| `webhook`      | 14    | webhook callback notification async DMN express-webhooks workflow-provider-webhooks |
| `nuvei`        | 9     | nuvei                            |
| `payout`       | 7     | payout settlement disbursement withdraw cash-out cashout |
| `retry`        | 6     | retry backoff timeout retryable nonRetryable temporal |
| `amount`       | 5     | amount conversion minor units smallest unit cents exponent formatAmountFromExponent unFormatAmount … |
| `refund`       | 5     | refund void cancel reversal chargeback dispute |
| `currency`     | 4     | fx exchange-rate currency-code   |
| `country`      | 2     | geo region locale jurisdiction   |
| `verification` | 2     | verification verify mandate setup_session debitMandateActive |
| `expiry`/`gw`/`apm`/`dispute`/`chargeback`/`underwriting`/`settlement`/`reconciliation`/`risk` | 1 each | … |

The **top-7** triggers (`webhook`, `nuvei`, `payout`, `retry`, `amount`, `refund`, `currency`) account for **50 / 67** trigger occurrences (~75 %). Cap them and most of the regression goes away.

Mean expansion factor on the 38 flipped queries: **+9.4 tokens added to a 8.4-token query (1.26× growth)**, but several queries blow up 2–4×:
- `trustly payment retry` (3 → 9)
- `Trustly refund inline credit debit cancel refund=1 not separate webhook` (11 → 37)
- `aircash refund method` (3 → 9)
- `paynearme dispute chargeback cash refund window finance` (7 → 18)

---

## Top 10 flipped queries (worst regressions)

For each: original Q, expansion text added, what the gold path expected, what the top-3 looked like before vs after.

### #1  `trustly payment retry` (stratum: trustly)
- **+ added**: `retry backoff timeout retryable nonRetryable temporal`
- expected: `docs/providers/trustly/acceptance-testing.md`
- BEFORE top-3: trustly/test-cases.md · trustly/tdd-charge.md · trustly/tdd-charge.md
- AFTER top-3:  references/temporal-workflows.md · references/diagnostic-templates.md · AI-CODING-GUIDE.md
- root cause: **stop-word inflation** — `retry` glossary entry includes `temporal` which pulls in temporal-workflow noise that drowns the trustly-specific docs. 3-token query becomes 9 tokens dominated by temporal/retry noise.

### #2  `aircash refund method` (stratum: method)
- **+ added**: `refund void cancel reversal chargeback dispute`
- expected: `docs/providers/aircash/refund.md`
- BEFORE: aeropay/transaction-status.md · **aircash/refund.md** (#2 hit) · data-layer.md
- AFTER:  paynearme/post-reverse-payment.md · paynearme/post-reverse-payment.md · libs/transaction-status-mapping.js
- root cause: **wrong glossary entry** — `refund` expansion = "refund void cancel reversal chargeback dispute" pulls paynearme/`reverse-payment` and dispute docs to top, kicking the aircash-specific gold path off the list.

### #3  `Trustly refund inline credit debit cancel refund=1 not separate webhook` (stratum: webhook)
- **+ added**: `refund void cancel reversal chargeback dispute webhook callback notification async DMN express-webhooks workflow-provider-webhooks`
- expected: `docs/GOTCHAS.md` (grpc-apm-trustly section)
- BEFORE: trustly/test-cases.md · GOTCHAS.md · architecture.md
- AFTER:  libs/stripe/index.js · stripe/refund/handle-activities.js · stripe/refund/handle-refund-failed.js
- root cause: **over-expansion** — query already contained `refund` and `webhook`; expansion adds 13 tokens that all match Stripe code paths (DMN, express-webhooks). FTS5 OR-mode floods candidate pool with stripe refund handlers.

### #4  `paynearme dispute chargeback cash refund window finance` (stratum: refund)
- **+ added**: `chargeback cb representation evidence dispute-handling dispute-lifecycle dispute refund void cancel reversal`
- expected: `docs/providers/paynearme/devdocs_docs_chargebacks-and-returns.md`
- BEFORE: proto/protos/common.proto · libs/generate-signature.js · paynearme/agent-interface.md
- AFTER:  libs/get-dispute-select-params.js · tabapay/map-action-to-dispute-status.js · disputes/DisputePage/Evidence/...
- root cause: **wrong glossary entry** — `chargeback`+`dispute`+`refund` triple-fire add 11 redundant dispute synonyms that pull general dispute UI/code rather than paynearme-specific chargebacks-and-returns doc.

### #5  `webhook late received hours days delayed timeout sale completion` (stratum: webhook)
- **+ added**: `webhook callback notification async DMN express-webhooks workflow-provider-webhooks`
- expected: `docs/docs/performance-profile.md` (workflow-worldpay-webhook)
- BEFORE: plaid/webhooks.md · concepts.yaml · **performance-profile.md** (top-3 hit)
- AFTER:  send-webhook-notification.js · send-webhook-notification.js · volt/activities.spec.js
- root cause: **stop-word inflation** — `webhook` adds 7 tokens; `send-webhook-notification.js` matches all of them (workflow + webhook + notification) and dominates the ranker.

### #6  `webhook idempotency duplicate dedup check transaction status before processing already finalized` (stratum: webhook)
- **+ added**: `webhook callback notification async DMN express-webhooks workflow-provider-webhooks`
- expected: `docs/providers/monek/TransactDirect_key-features_idempotency-token.md`
- BEFORE: e2e-script-patterns.md · flutterwave/webhooks.md · architecture.md
- AFTER:  stripe/refund/handle-refund-failed.js · stripe/refund/handle-refund-updated.js · workflow.js
- root cause: **over-expansion** — adds `DMN`, `express-webhooks`, `workflow-provider-webhooks` which match stripe/workflow code paths, overwhelming the monek docs.

### #7  `aircash voucher prepaid initialize refund` (stratum: refund)
- **+ added**: `refund void cancel reversal chargeback dispute`
- expected: `docs/docs/AI-CODING-GUIDE.md` (grpc-apm-aircash)
- BEFORE: methods/initialize.js · methods/refund.js · aeropay/transaction-status.md
- AFTER:  proto/protos/common.proto · nuvei/risk-guide_chargebacks.md · proto/protos/common.proto
- root cause: **wrong glossary entry** — `refund` synonyms pull `chargebacks` and `dispute` docs (nuvei) which have nothing to do with aircash voucher refunds.

### #8  `Nuvei addUPOAPM checksum formula merchantId merchantSiteId clientRequestId timeStamp merchantSecretKey` (stratum: nuvei)
- **+ added**: `nuvei`
- expected: `docs/providers/nuvei/documentation_accept-payment_payment-page_quick-start-for-payment-page.md`
- BEFORE: nuvei/przelewy24.md · withdrawal-dmns.md · vip-preferred-sdk.md
- AFTER:  libs/payload-builders/get-session-token-payload.js · safecharge-client.js · useHostedFields.ts
- root cause: **`nuvei` self-expansion (a→a) tilts FTS5 ranking** — duplicating `nuvei` doubles its IDF weight on tokenization but does not add new info, so the original ranking gets perturbed and code files (which mention `nuvei`/`safecharge`) outrank doc pages.

### #9  `payper payout webhook claimed declined returned` (stratum: payout)
- **+ added**: `payout settlement disbursement withdraw cash-out cashout webhook callback notification async DMN express-webhooks workflow-provider-webhooks`
- expected: `docs/providers/payper/09-postback-notifications.md`
- BEFORE: payper/reference_notification-postback-details.md · payper/reference_direct-deposit.md · GOTCHAS.md
- AFTER:  routes/payout.js · routes/payout.js · aptpay/handle-activities.js
- root cause: **double-trigger over-expansion** — `payout` (6 tokens) + `webhook` (7 tokens) = 13 added tokens swamp the 6-token query, pulling generic `routes/payout.js` to the top.

### #10  `Nuvei payout example response merchantId merchantSiteId userTokenId clientRequestId transactionId transactionStatus` (stratum: payout)
- **+ added**: `nuvei payout settlement disbursement withdraw cash-out cashout`
- expected: `docs/providers/nuvei/documentation_europe-guides_epay-bg.md`
- BEFORE: nuvei/pix.md · nuvei/vip-preferred-sdk.md · nuvei/vip-preferred-sdk.md
- AFTER:  methods/payout.js · nuvei/pay-by-bank.md · utils/nuvei/nuvei-web-client.ts
- root cause: **wrong glossary entry** — `payout` expansion injects `cashout` and `disbursement` and `settlement` which match generic payout code (`methods/payout.js`) and pull a non-target nuvei doc (`pay-by-bank.md`).

---

## Root-cause categories

| category | count | description |
|----------|------:|-------------|
| **(b) Over-expansion** (1.5×+ token blow-up) | 16 | original query short or already domain-specific; expansion >50 % drowns reranker. Examples: #1, #3, #4, #6, #9, #15, #19, #25, #28, #29, #33. |
| **(a) Wrong glossary entries** (synonyms pull off-topic docs/code) | 14 | `refund`→`dispute`/`chargeback`, `payout`→`disbursement`/`cashout`, `chargeback`→`dispute`. Examples: #2, #4, #7, #10, #12, #15, #18, #29, #37. |
| **(c) Stop-word inflation** (high-IDF expansion terms) | 6 | `retry`→`temporal`, `webhook`→`workflow-provider-webhooks`, `amount`→long camelCase function names. Examples: #1, #4 (in part), #5, #16, #34, #36. |
| **(d) Self-expansion `nuvei`→`nuvei`** (duplicates a single token) | 2 | `nuvei: "nuvei"` does NOT add information; only re-weights the term. Combined with FTS5 OR-mode it perturbs ranking. Examples: #8, #30. |

(Categories overlap: many flipped queries were both "over-expansion" AND "wrong synonyms"; primary category counted.)

**Dominant cause: (b) over-expansion + (a) wrong synonyms together = ~30 / 38 (~79 %).**

---

## Recovered queries (counter-examples — what worked)

7 recovered MISS→HIT, all from the `refund`, `audit`, `apm`, `redirect` keys. Pattern: query was originally **off-target** in v2 (FTS5 returned wrong files because the gold doc used a synonym, not the literal term). Examples:
- `attempt increment retry refund duplicate same transactionId different attempt` — `retry`+`refund` combined steered to the right gotcha.
- `paynearme cash voucher initialize redirect` — `redirect`→`callback_url initializeUrl challenge` matched the gold doc that uses `initializeUrl`.
- `audit orchestration methodology` — `audit`→`audit-trail` matched the actual filename.

These recoveries are real — but only **7** vs **38** flips. Net is decisively negative on v2 docs.

---

## Guilty / safe glossary entries — concrete keep/remove/refine list

### REMOVE (high flip rate, no clear recovery wins on v2)

| entry | flips caused | reason |
|-------|-------------:|--------|
| `nuvei: "nuvei"` | 9 | self-expansion does nothing useful; only perturbs FTS5 IDF weighting. Either delete or expand to actually-helpful aliases (`safecharge nuvei-cashier`). |
| `webhook: "webhook callback notification async DMN express-webhooks workflow-provider-webhooks"` | 14 | bare presence of `webhook` in any query (incl. doc-intent ones) injects 7 tokens that match Stripe/Volt/temporal **code** paths. Either remove or strip code-path terms (`DMN`, `express-webhooks`, `workflow-provider-webhooks`) leaving only `webhook callback notification`. |
| `payout: "payout settlement disbursement withdraw cash-out cashout"` | 7 | `disbursement`+`cashout` match generic payout code routes. Either remove or trim to `payout payouts` (just normalize plural). |
| `amount: "amount conversion minor units smallest unit cents exponent formatAmountFromExponent unFormatAmount formatAmountToSmallestUnit unFormatAmountFromSmallestUnit formatPrecision formatToExponent parseFloat decimal"` | 5 | adds 17 camelCase JS function names — guarantees a JS-utility file outranks a doc page. Trim to `amount currency conversion`. |
| `currency: "fx exchange-rate currency-code"` | 4 | usually fires together with `amount` (same query) → 21 added tokens. Compounds the `amount` problem. Either deduplicate against `amount` or remove. |

### REFINE (helpful but too aggressive)

| entry | flips caused | suggested edit |
|-------|-------------:|----------------|
| `retry: "retry backoff timeout retryable nonRetryable temporal"` | 6 | drop `temporal` (anchors to wrong file domain — `temporal-workflows.md`). Keep `retry backoff retryable`. |
| `refund: "refund void cancel reversal chargeback dispute"` | 5 | drop `chargeback dispute` cross-references (they cause off-topic dispute UI to outrank refund docs). Keep `refund refunds void cancel reversal`. |
| `chargeback: "dispute cb"` | 1 | one-way mapping, but together with `refund` triggers 11-token blow-up. Keep but test isolated. |
| `dispute: "chargeback cb representation evidence dispute-handling dispute-lifecycle"` | 1 | only fire when query is short (`dispute X` vs `dispute chargeback X` already containing chargeback). Or strip lifecycle/handling terms. |

### KEEP (low flip rate, observed wins)

- `audit: "audit-log activity history audit-trail compliance change-log"` — recovered #5 cleanly, no flips.
- `apm: "alternative payment method"` — 1 flip vs 1 recovery; helpful for short queries.
- `redirect: "redirect return_url callback_url sca authenticate initializeUrl challenge"` — recovered #6 cleanly.
- `verification: "verification verify mandate setup_session debitMandateActive"` — 2 flips but adds canonical setup_session/mandate aliases (jira benefit likely).
- All the W2-added **provider-name** entries (`silverflow`, `stripe`, `worldpay`, `tabapay`, `crb`, `applepay`, `googlepay`, `paypass`, `passkey`, `cybersource`, `plaid`, `braintree`, `hubspot`) did NOT trigger any flips here (no provider-name token appeared as the trigger key in flipped queries). They likely earn their keep on jira where provider names appear bare in the query and the doc tower needs disambiguation.

---

## Recommendation: intent-gate W2

**Yes — W2 should NOT fire on doc-intent queries (v2 strata: nuvei/aircash/trustly/webhook/refund/payout/method/provider/interac/tail).** Evidence:

1. **Doc-intent collapses** (−19.83pp on v2) but jira-FTS5-only already ships +11.89pp without W2 — so jira alone may not need W2 either; it needs a controlled re-test with W2 ON to confirm any incremental gain.
2. The flips concentrate where the **trigger token IS the search target** (`webhook` query → webhook docs → expansion adds 7 noise tokens that compete with the actual context). On a doc-intent query, the user already typed the canonical term; expansion is anti-helpful.
3. On jira-style queries (multi-keyword, abbreviated, often with bare acronyms like `kyb`, `mfa`, `aml`), expansion is what makes recall move. Provider-name expansion (`silverflow`, `tabapay` ...) and acronym-expansion (`kyb`, `mfa`) are exactly where W2 was designed to help.

**Concrete proposal**: gate `expand_query` on a stratum/intent flag — only call it from the jira benchmark path (`bench_routing_e2e.py` jira mode) and from production when the router classifies query as code/jira-style. Skip it on doc-intent and on v2 docs eval.

Failing that (intent classifier may not be reliable enough), the **min viable cleanup**:
1. Delete `nuvei: "nuvei"` (no info gain).
2. Trim `webhook` to `webhook callback notification`.
3. Trim `amount` to `amount currency conversion`.
4. Drop `temporal` from `retry`.
5. Drop `chargeback dispute` cross-refs from `refund`.

Estimated recovery on v2 from those 5 edits alone: ~20–25 of the 38 flips (the `webhook`/`nuvei`/`payout`/`amount`/`retry` cluster), so **~−6 to −10pp** reduction in regression — still negative but no longer catastrophic. Full repair requires the intent gate.

---

## Files inspected (read-only)

- `/Users/vaceslavtarsevskij/.code-rag-mcp/bench_runs/v2_e2e_fts5_only_session2.json`
- `/Users/vaceslavtarsevskij/.code-rag-mcp/bench_runs/v2_e2e_fts5_w2_session2.json`
- `/Users/vaceslavtarsevskij/.code-rag-mcp/profiles/pay-com/glossary.yaml` (119 entries: 65 pre-existing + 54 W2-added)
- `/Users/vaceslavtarsevskij/.code-rag-mcp/src/search/fts.py` (`expand_query` — `_sanitize_fts_input` strip + OR-mode + glossary lookup)
- `/Users/vaceslavtarsevskij/.code-rag-mcp/.claude/debug/current/agent-w2-report.md`
- 161 v2 eval rows in `/Users/vaceslavtarsevskij/.code-rag-mcp/profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl`

No code or YAML modified. No bench re-run.
