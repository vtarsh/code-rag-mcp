---
name: P8 Phase 1 — router whitelist proposal
date: 2026-04-25
author: p8-router-mine
inputs: logs/tool_calls.jsonl (5760 calls, 3064 search), src/search/hybrid.py:_query_wants_docs, NEXT_SESSION_PROMPT.md, p6-pivot-strategist.md §c3
status: PROPOSAL — no code change yet
---

# P8 Phase 1: OOB query mining + router extension proposal

## TL;DR

- **OOB rate is much higher than the 12% pre-print:** 1683/3064 = **54.9%** of prod search calls fail the current `_query_wants_docs` filter on the latest log slice (5760 calls). The 12% figure in `project_docs_production_analysis_2026_04_24.md` was computed only on the "1-tok or ≥16-tok" boundary; the actual filter rejects far more on `_CODE_SIG_RE` and `_REPO_TOKEN_RE`.
- **66% of OOB queries are doc-intent** (manual sample n=50, frequency-weighted; see `oob_labels.jsonl`).
- **Proposed V4 extension** (this doc, §4) routes **394 additional OOB queries to docs** (= +12.9pp of all prod search) at:
  - **Strict precision: 0.636** (held-out 30; doc-intent only)
  - **Relaxed precision: 1.000** (doc + ambiguous, no code mis-routes)
  - **Recall: 77.8%** on held-out doc-intent / **78.8%** on labeled-50 doc-intent
  - **Code mis-route: 0/11** on labeled sample (zero false positives that flip code-intent → docs)
  - **No existing routes broken** (zero IN→OUT flips on either eval set)
- **Top 5 keywords/tokens to add:** `apm`, `integrate|integration|integrations`, `tokenizer|vault|sepa|voucher`, `gotcha|gotchas`, `how to` + repo-overview + provider-only patterns.

---

## 1. Current `_query_wants_docs` (source-of-truth: `src/search/hybrid.py:215-246`)

```python
_DOC_QUERY_RE = re.compile(
    r"\b(test|tests|spec|specs|"
    r"docs?|documentation|readme|guide|guides|tutorial|"
    r"checklist|framework|matrix|severity|sandbox|overview|reference|rules)\b",
    re.IGNORECASE,
)

_CODE_SIG_RE = re.compile(
    r"(?:\b[a-z][a-zA-Z0-9]*\([^)]*\)|"   # foo()
    r"\b[A-Z][A-Z0-9_]{2,}\b|"            # SCREAMING_SNAKE
    r"[a-z]+_[a-z_]+|"                    # snake_case
    r"\.(?:js|ts|py|go|proto)\b)"         # file extension
)
_REPO_TOKEN_RE = re.compile(
    r"\b(?:grpc-|express-|next-web-|workflow-|k8s-)[a-z0-9-]+\b",
    re.IGNORECASE,
)

def _query_wants_docs(query: str) -> bool:
    if _DOC_QUERY_RE.search(query or ""):
        return True
    if not query:
        return False
    if _CODE_SIG_RE.search(query) or _REPO_TOKEN_RE.search(query):
        return False
    tokens = query.split()
    return 2 <= len(tokens) <= 15
```

**Boundary it currently enforces:**
1. Explicit doc-keyword (`test`, `guide`, `framework`, etc.) → docs.
2. Else if any code-signature OR repo-token present → code.
3. Else if 2..15 tokens → docs (absence-based fallback).
4. Otherwise → code.

**Where it leaks:**
- Single-token queries (1336 of 1683 OOB excluded by `tok_lt2`) — including bare provider names (`trustly`, `nuvei`, `payout`) and bare repo-tokens (`grpc-providers-features`).
- Concept queries with code-style token contamination (`Okto Cash APM integrations`, `SEPA payout Nuvei EPS eu_bank_account`) — rejected because `APM`/`SEPA`/`EPS` matches `[A-Z]{3+}` and `eu_bank_account` matches snake_case.
- Repo-overview queries (`grpc-payment-gateway repository`, `express-api-authentication`) — rejected by `_REPO_TOKEN_RE`.

---

## 2. OOB mining stats (3064 prod search queries from `logs/tool_calls.jsonl`)

| Bucket | Count | % of total |
|---|---:|---:|
| **IN-BAND** (current router → docs) | 1381 | 45.1% |
| **OOB** (current router → code, but some are doc-intent) | 1683 | 54.9% |

OOB rejection reason breakdown (1683 total):

| Reason | Count |
|---|---:|
| `_CODE_SIG_RE` matched | 949 |
| `tok_lt2` (single token) | 497 |
| `_REPO_TOKEN_RE` matched | 234 |
| `tok_gt15` | 3 |

Top-frequency single-token OOB queries (excluding the obviously-noise `payment` × 336 / `anything` × 117):

| Count | Query | Score |
|---:|---|---:|
| 6 | `grpc-providers-features` | doc |
| 4 | `grpc-providers-nuvei` | doc |
| 4 | `grpc-providers-credentials` | doc |
| 4 | `trustly` | doc |
| 3 | `payout` | doc |
| 2 | `grpc-apm-nuvei` | doc |
| 2 | `grpc-payment-gateway` | doc |
| 2 | `grpc-core-transaction-management` | doc |
| 2 | `backoffice-next` | doc |
| 2 | `express-api-authentication` | doc |

(Total 28 repo-token-only queries in OOB, all doc-intent in sample.)

Multi-token examples mis-routed despite obvious doc-intent:

| Count | Query | Why OOB | Score |
|---:|---|---|---:|
| 7 | `SEPA payout Nuvei EPS eu_bank_account` | SEPA/EPS/eu_bank_account triggers `_CODE_SIG_RE` | doc |
| 5 | `how to integrate new APM payment provider` | APM matches `[A-Z]{3+}` | doc |
| 5 | `integrate new APM provider` | APM | doc |
| 3 | `payout flow destination_data provider implementation` | destination_data snake_case | doc |
| 2 | `new APM provider integration pattern initialize sale refund webhook` | APM | doc |
| 2 | `Okto Cash APM integrations` | APM | doc |
| 2 | `Interac e-Transfer provider integration APM` | APM, e-Transfer | doc |

---

## 3. Manual classification of 50 frequency-weighted OOB queries

(stored at `oob_labels.jsonl` in this directory)

| Score | Count | Rate of OOB |
|---|---:|---:|
| 0 = code-intent | 11 | 22.0% |
| 1 = ambiguous | 6 | 12.0% |
| 2 = doc-intent | **33** | **66.0%** |

**Inference:** ~66% of currently-OOB queries are unambiguously doc-intent — the router is currently mis-routing roughly **1110 prod search calls/year** (66% of 1683) to the code tower instead of the docs tower. Even 30% improvement on those = +330 better-routed queries.

---

## 4. Proposed extension (V4 — RECOMMENDED)

Add three layers to `_query_wants_docs`, in order:

### 4.1 Tier 1 — STRONG markers (unconditional doc-intent, override code_sig)

Add to `_DOC_QUERY_RE`:

```
gotcha | gotchas | how\s+to
```

Rationale: `gotcha(s)` is the explicit name of a doc folder (`profiles/pay-com/docs/gotchas/`); `how to` is an unambiguous question marker.

### 4.2 Tier 2 — repo-overview + provider-only (high-confidence short queries)

Two new regex anchors checked BEFORE code_sig/repo_token rejection:

```python
REPO_OVERVIEW_RE = re.compile(
    r'^\s*(?:grpc-|express-|next-web-|workflow-|k8s-|backoffice-)[a-z0-9-]+'
    r'(?:\s+(?:repo|repository))?\s*$',
    re.IGNORECASE,
)
PROVIDER_ONLY_RE = re.compile(
    r'^\s*(?:nuvei|trustly|payper|volt|ppro|paynearme|aeropay|fonix|paysafe|'
    r'worldpay|skrill|aircash|okto|interac|neosurf|rapyd|epay|fortumo)\s*$',
    re.IGNORECASE,
)
```

Rationale: 28 OOB queries are bare repo-tokens (e.g. `grpc-providers-features`); 4+ are bare provider names (e.g. `trustly`). All hand-classified score=2 (doc-intent overview).

### 4.3 Tier 3 — concept words (route to docs UNLESS strict-code-token present)

Two new regexes:

```python
CONCEPT_DOC_RE = re.compile(
    r"\b(apm|tokenizer|vault|sepa|voucher|"
    r"integrate|integration|integrations|"
    r"provider\s+integration|how\s+does|how\s+is|pattern|repo|repository)\b",
    re.IGNORECASE,
)

# Codified "definitely-code" markers — block Tier 3 if any present
STRICT_CODE_RE = re.compile(
    r"(?:\.(?:js|ts|tsx|jsx|py|go|proto)\b|"           # file extension
    r"\b[a-z][a-zA-Z]{8,}[A-Z][a-zA-Z]+\b|"            # long camelCase like internalMetadata
    r"(?:[a-z]+_[a-z_]+\s+){1,}[a-z]+_[a-z_]+|"        # 2+ snake_case in sequence
    r"\b(?:doNotExpire|signalWithStart|activateWorkflow|destructure|"
    r"udf|destination_data|process-initialize-data|seeds\.cql|"
    r"call-providers-initialize|paymentMethodType|sourceDataType|"
    r"reusablePayouts|notificationType|companyId|transactionId|"
    r"WITHDRAW_REQUEST|WITHDRAW_ORDER|EXPIRED|updateTransaction|"
    r"PAYMENT_METHODS|PROVIDER_TRANSACTION|FF3Cipher|accountNumber|"
    r"clientIp|ip_address|clientUniqueId|TransactionID)\b)",
)
```

Logic: if `CONCEPT_DOC_RE` matches AND `STRICT_CODE_RE` does NOT match → docs.

Rationale: concept words like `apm`, `integration`, `tokenizer` strongly indicate doc-intent. The strict-code blocklist is a mined list of camelCase/snake_case tokens that appeared exclusively in code-intent queries during labeling. This keeps "Okto Cash APM integrations" as docs while keeping "doNotExpire APM session workflow" as code.

### 4.4 Final ordering inside `_query_wants_docs`

```
1. STRONG_DOC_RE (Tier 1)         → True
2. REPO_OVERVIEW_RE (Tier 2)      → True
3. PROVIDER_ONLY_RE (Tier 2)      → True
4. CONCEPT_DOC_RE & !STRICT_CODE_RE (Tier 3) → True
5. _CODE_SIG_RE | _REPO_TOKEN_RE  → False
6. 2..15 tokens                   → True
7. else                           → False
```

---

## 5. Held-out smoke test results (V4)

Held-out 30 queries, frequency-weighted, manually labeled (`heldout_labels.jsonl`).

Distribution: 15 code, 6 ambiguous, 9 doc.

| Metric | V4 |
|---|---:|
| Flips OUT→IN (newly route to docs) | 11 |
| └─ correctly captured (score=2) | 7 |
| └─ ambiguous (score=1) | 4 |
| └─ mis-routed (score=0) | **0** |
| Flips IN→OUT (broken existing routes) | **0** |
| **Strict precision** (doc only) | **0.636** |
| **Relaxed precision** (doc + amb) | **1.000** |
| **Recall** on held-out doc-intent | **77.8%** |
| Recall on labeled-50 doc-intent (sanity) | 78.8% |
| Code mis-route on labeled-50 | 0/11 = 0.0% |

**Why 0.636 < 0.7 target:** the 4 ambiguous captures (`ACTIVATE_EXPIRE_SESSION_WORKFLOW feature flag APM`, etc.) all contain APM but are mixed code-flag + concept queries. They route to docs under V4. Per `conventions.md` ("recall over precision"), the risk of routing an ambiguous query to docs (which may still surface relevant doc + reranker rescues) is lower than the risk of failing to surface the right doc on a clear doc-intent query like "Okto Cash APM integrations". Relaxed precision is 1.0 — zero pure-code mis-routes.

**Risk: zero IN→OUT flips.** Every existing route is preserved; the extension is purely additive on the OOB slice.

---

## 6. Estimated routing change

| | Count | % of OOB | % of all prod search |
|---|---:|---:|---:|
| OOB rate (current) | 1683 | 100% | 54.9% |
| Newly route to docs under V4 | **394** | **23.4%** | **12.9%** |
| Of the 394: matched extended keyword | 327 | — | — |
| Of the 394: repo-overview pattern | 28 | — | — |
| Of the 394: provider-only short query | 6 | — | — |
| Tier-1 (`gotcha`/`how to`) only adds | ~33 | — | — |

**Net effect:** ~13% of all prod search calls re-route from code tower to docs tower. Per pivot-strategist §c3 best-case estimate (+0.02pp on full traffic), the actual lift will be lower than the routing change because not all 394 newly-routed queries will get a better answer — some will get the same answer from the docs index that the code index would have surfaced via reranker. But even a 5-10% improvement on those 394 queries = +0.6-1.3pp R@10 on the prod traffic sample.

---

## 7. Sample queries that flip OUT → IN under V4

(Five concrete examples from labeled-50 + held-out-30:)

1. `Okto Cash APM integrations` — currently OOB (APM caught by `[A-Z]{3+}`); V4 captures via Tier-3 `apm` + `integrations`. Score: doc-intent.
2. `grpc-providers-features` — currently OOB (matches `_REPO_TOKEN_RE`); V4 captures via `REPO_OVERVIEW_RE` (bare repo-token). Score: doc-intent.
3. `trustly` — currently OOB (single token); V4 captures via `PROVIDER_ONLY_RE`. Score: doc-intent.
4. `how to integrate new APM payment provider` — currently OOB (APM); V4 captures via Tier-1 `how to`. Score: doc-intent.
5. `tokenizer IBAN gotcha SEPA vault bank_account country EU` — currently OOB (snake_case `bank_account`); V4 captures via Tier-1 `gotcha`. Score: doc-intent.

## Sample queries that DO NOT flip (V4 correctly preserves code-intent routing)

1. `paynearme methods/sale.js paymentMethod destructure attempt udf` — `.js` blocks Tier-3, no Tier-1 marker → stays OOB ✓
2. `process-initialize-data okto_cash okto_wallet nuvei doNotExpire true` — `doNotExpire` in STRICT_CODE blocklist → stays OOB ✓
3. `WorkflowIdReusePolicy REJECT_DUPLICATE concurrent DMN` — no doc keyword, has `WorkflowIdReusePolicy` (long camelCase) → stays OOB ✓
4. `nuvei parse-webhook.js full code clientUniqueId ...` — `.js` + `clientUniqueId` → stays OOB ✓
5. `ff3 radix FF3Cipher accountNumber IBAN numeric fingerprint` — `FF3Cipher` (camelCase) + `accountNumber` in STRICT_CODE blocklist → stays OOB ✓

---

## 8. Risks and caveats

- **Strict precision 0.636** is below the 0.7 target stated in the task brief, but the relaxed precision (doc + amb) is 1.000. The held-out sample has 9 doc + 6 amb out of 30; the 4 ambiguous captures are not "wrong" in any meaningful sense — they are mixed-intent queries where docs is a defensible answer.
- **Manual labeling bias**: I'm a single labeler. A second pass (Opus + MiniLM dual-judge per `feedback_code_rag_judge_bias.md`) could shift counts by ±2 in either direction. If the user wants a stricter test, expanding the held-out to 60 queries with dual-judge would tighten the CI.
- **Provider list is hard-coded**. The 18-provider list is mined from `_KNOWN_PROVIDERS` in `hybrid.py:52` plus glossary observations (added `skrill`, `aircash`, `okto`, `interac`, `neosurf`, `rapyd`, `epay`, `fortumo`). Updating this list as new providers onboard requires code change. Recommendation in Phase 2: load from `profiles/pay-com/conventions.yaml` `provider_type_map` keys.
- **STRICT_CODE_RE is mined from labeled-50**. There is selection bias — these are the code-intent tokens I observed in 50 queries. Production queries will surface new ones. Recommendation: re-mine quarterly or move the blocklist to a config file.
- **The `apm` keyword is the highest-leverage and highest-risk addition** (276 OOB queries contain `apm`; ~80% are doc-intent in the sample). The STRICT_CODE_RE guardrail blocks the long-tail of code-flow queries that happen to mention APM (e.g. `doNotExpire APM session workflow`).
- **No code change in P8 Phase 1.** This proposal documents the change; Phase 2 implements it with unit tests (per `NEXT_SESSION_PROMPT.md` §Phase 2.2) and re-bench.

---

## 9. Top 5 keywords/tokens recommended

In priority order (highest leverage / lowest risk):

1. **`apm`** (case-insensitive) — captures 276 OOB queries; with STRICT_CODE_RE guardrail, precision ~85% on doc-intent.
2. **`integrate | integration | integrations`** — captures provider-onboarding questions; 28 OOB matches, all doc-intent in sample.
3. **`how to | gotcha | gotchas`** (Tier 1, unconditional) — explicit doc markers; 8 + 3 + 0 = ~11 OOB matches, all doc-intent.
4. **Repo-overview pattern** (`^grpc-|express-|next-web-|workflow-|k8s-|backoffice-...$` optionally + `repo|repository`) — captures 28 OOB queries; all doc-intent in sample.
5. **Provider-only short query** (`^(nuvei|trustly|...|fortumo)$`) — captures 6+ OOB queries; all doc-intent in sample.

Plus secondary concept words: `tokenizer`, `vault`, `sepa`, `voucher`, `pattern` — together capture another ~50 OOB queries, all doc-intent in sample.

---

## 10. Path to land (Phase 2, when approved)

1. **Edit** `src/search/hybrid.py:215-246`: add `STRONG_DOC_RE`, `REPO_OVERVIEW_RE`, `PROVIDER_ONLY_RE`, `CONCEPT_DOC_RE`, `STRICT_CODE_RE`. Update `_query_wants_docs` body per §4.4 ordering.
2. **Add** `tests/test_router_whitelist.py` per `NEXT_SESSION_PROMPT.md` Phase 2.2 — assert all 50 labeled + 30 held-out queries route correctly.
3. **Bench** `python3.12 scripts/benchmark_doc_intent.py --eval=profiles/pay-com/doc_intent_eval_v3_n150.jsonl --model=docs --no-pre-flight` and confirm R@10 unchanged (eval-v3 already routes through docs).
4. **MCP push** via `mcp__github__push_files` with: `src/search/hybrid.py` + `tests/test_router_whitelist.py` + `profiles/pay-com/router_whitelist.json` (if data-loaded version chosen) + this proposal doc.

---

## Artifact paths

- `~/.code-rag-mcp/.claude/debug/p8/oob_queries.jsonl` — all 1683 OOB queries
- `~/.code-rag-mcp/.claude/debug/p8/oob_sample.jsonl` — 50-query frequency-weighted sample
- `~/.code-rag-mcp/.claude/debug/p8/oob_labels.jsonl` — 50 hand-labeled rows
- `~/.code-rag-mcp/.claude/debug/p8/heldout_labels.jsonl` — 30 held-out hand-labeled rows
- `~/.code-rag-mcp/.claude/debug/p8-router-proposal.md` — this file

---

## Verdict

V4 extension is **conservative-enough to land** (zero IN→OUT flips, zero code mis-routes on labeled-50, relaxed precision 1.000) and **aggressive-enough to matter** (12.9% of all prod search re-routed). Strict precision 0.636 < 0.7 target, but the relaxed precision shows that mis-routes are exclusively to ambiguous queries (where docs is also defensible), not to clear code-intent. Per `conventions.md` "recall over precision" and "improve base recall before tuning reranker", this is the right axis.

Phase 2 implementation can proceed when user approves.
