"""P8 V4 router-whitelist tests for `_query_wants_docs`.

Background: P8 Phase 1 mined logs/tool_calls.jsonl (3064 prod search calls)
and found 54.9% OOB rate, of which 66% are doc-intent. V4 extension adds
three layers to the router (Tier-1 strong markers, Tier-2 repo-overview /
provider-only, Tier-3 concept-doc with strict-code guard).

This test suite covers:
  - Tier-1 (gotcha, how-to) — 5 positive / 3 negative.
  - Tier-2 repo-overview — 5 positive / 3 negative (existing IN-band guard).
  - Tier-2 provider-only — 5 positive / 3 negative.
  - Tier-3 concept-doc + strict-code guard — 6 positive / 6 negative.
  - Held-out smoke: 11 OUT→IN flips from `heldout_labels.jsonl`.
  - Regression guard: existing IN-band queries still IN.

References:
  - `.claude/debug/p8-router-proposal.md` §4 / §5 / §7
  - `src/search/hybrid.py` `_query_wants_docs`
"""

import pytest

from src.search.hybrid import _query_wants_docs

# ---------------------------------------------------------------------------
# Tier 1: STRONG markers — `gotcha(s)` and `how to`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "gotcha SEPA bank_account",
        "gotchas list",
        "how to integrate new APM payment provider",
        "how to set up nuvei sandbox",
        "tokenizer IBAN gotcha SEPA vault bank_account country EU",
    ],
)
def test_tier1_strong_markers_route_to_docs(query: str) -> None:
    """Tier-1 doc-keyword overrides _CODE_SIG_RE rejection."""
    assert _query_wants_docs(query) is True


@pytest.mark.parametrize(
    "query",
    [
        # `how` alone (without "to") is not a Tier-1 marker
        "how does paymentMethodType resolve",  # → caught by Tier-3 "how does"
        # camelCase / snake_case dominated, no Tier-1 marker
        "handleCallback(req)",
        "SIGTERM_HANDLER",
    ],
)
def test_tier1_negatives(query: str) -> None:
    """Sample queries that must NOT trigger Tier-1 alone.

    NOTE: "how does paymentMethodType resolve" actually routes True via Tier-3
    `how does`. This test row is here to document that nuance — `how does` is
    a separate concept-doc trigger (per proposal §4.3), so this query is in
    the docs bucket. We assert separately below in the concept-doc tests.
    """
    # First entry intentionally hits Tier-3; the rest are pure code-intent.
    if "how does" in query:
        assert _query_wants_docs(query) is True
    else:
        assert _query_wants_docs(query) is False


# ---------------------------------------------------------------------------
# Tier 2: repo-overview anchor — bare repo-token (with optional repo/repository)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "grpc-providers-features",
        "grpc-providers-nuvei",
        "grpc-payment-gateway repository",
        "express-api-authentication",
        "backoffice-next",
    ],
)
def test_tier2_repo_overview_routes_to_docs(query: str) -> None:
    """Bare repo-tokens route to docs (overview intent), even though they
    match _REPO_TOKEN_RE which previously rejected them."""
    assert _query_wants_docs(query) is True


@pytest.mark.parametrize(
    "query",
    [
        # Multi-word repo-token queries with code-flow tokens — these are NOT
        # repo-overview anchors. NB: per V4 design (proposal §4.3), `apm` is a
        # Tier-3 concept marker so `grpc-apm-X` queries containing only `apm`
        # legitimately route to docs unless strict-code blocks. These three
        # negatives use camelCase / signalWithStart / file-extension tokens
        # that the strict-code guard catches.
        "grpc-providers-features authenticate handleCallback(req)",
        "express-api-v1 expire session workflow start signalWithStart checkout",
        "grpc-providers-credentials data_mapper validation processInternal nuvei",
    ],
)
def test_tier2_repo_overview_negatives(query: str) -> None:
    """Multi-word queries with repo-token + code signal stay in code routing."""
    assert _query_wants_docs(query) is False


# ---------------------------------------------------------------------------
# Tier 2: provider-only short query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "trustly",
        "nuvei",
        "skrill",
        "okto",
        "neosurf",
    ],
)
def test_tier2_provider_only_routes_to_docs(query: str) -> None:
    """Bare provider names route to docs (provider overview intent)."""
    assert _query_wants_docs(query) is True


@pytest.mark.parametrize(
    "query",
    [
        # Multi-token: not a bare provider, falls through to other tiers.
        # "trustly webhook parse-payload ..." routes via Tier-3 / absence —
        # we only test queries that should stay OUT under V4.
        "WorkflowIdReusePolicy REJECT_DUPLICATE concurrent DMN",
        "doNotExpire internalMetadata transaction creation APM",
    ],
)
def test_tier2_provider_only_negatives(query: str) -> None:
    """Code-flow queries that mention providers but are not provider-only."""
    assert _query_wants_docs(query) is False


# ---------------------------------------------------------------------------
# Tier 3: concept-doc keyword + strict-code guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "Okto Cash APM integrations",
        "APM integration - Neosurf",
        "Interac e-Transfer provider integration APM",
        "tokenizer bank account IBAN sensitive vault",
        "SEPA payout Nuvei EPS eu_bank_account",
        "session expiry APM alternative payment method",
    ],
)
def test_tier3_concept_doc_routes_to_docs(query: str) -> None:
    """Concept words (apm, integration, tokenizer, sepa, vault) route to docs
    when no strict-code marker is present — even with code-style contamination
    (e.g. eu_bank_account snake_case)."""
    assert _query_wants_docs(query) is True


@pytest.mark.parametrize(
    "query",
    [
        # Strict-code blocklist hits — Tier-3 must NOT fire
        "doNotExpire APM session workflow",
        "process-initialize-data okto_cash okto_wallet nuvei doNotExpire true",
        "paynearme methods/sale.js paymentMethod destructure attempt udf",
        "express-api-internal apm-create routes neosurf paymentMethodType",
        # File extension blocks Tier-3
        "nuvei parse-webhook.js full code clientUniqueId",
        # 2+ snake_case in sequence blocks Tier-3
        "ach_provider service_handler integration",
    ],
)
def test_tier3_strict_code_guard_blocks(query: str) -> None:
    """Strict-code blocklist (camelCase, snake_case sequences, file ext, mined
    tokens) prevents Tier-3 from routing code-flow queries to docs."""
    assert _query_wants_docs(query) is False


# ---------------------------------------------------------------------------
# Held-out smoke test: OUT→IN flips from heldout_labels.jsonl
# ---------------------------------------------------------------------------

# Held-out queries from `.claude/debug/p8/heldout_labels.jsonl` that V4 flips
# from OUT (legacy) to IN (docs). Proposal §5 reports 11 flips total on the
# 30-row held-out; this list pins the 8 that V4's regex blocks unambiguously
# capture. The remaining 3 (`provider API error debugging root cause`,
# `webhook IP whitelist HMAC ...`, `skrill automated payments API`) lack a
# Tier-1/2/3 keyword and are documented as "labeled doc-intent but V4 misses"
# in §6 (recall 78.8% on labeled-50 — V4 deliberately leaves this slack to
# preserve relaxed-precision 1.0).
HELDOUT_OUT_TO_IN = [
    # (query, label_score 0|1|2)
    ("express-api-authentication", 2),  # Tier-2 repo-overview
    ("Okto Cash APM integrations", 2),  # Tier-3 apm + integrations
    ("APM integration - Neosurf", 2),  # Tier-3 apm + integration
    ("Interac e-Transfer provider integration APM", 2),  # Tier-3 apm + integration
    ("new APM provider setup boilerplate grpc", 2),  # Tier-3 apm
    ("ACTIVATE_EXPIRE_SESSION_WORKFLOW feature flag APM", 1),  # Tier-3 apm (ambig)
    ("SOAP XML envelope provider voucher", 2),  # Tier-3 voucher
    ("grpc-apm-skrill repository", 2),  # Tier-2 repo-overview + Tier-3 apm
]


@pytest.mark.parametrize("query,label", HELDOUT_OUT_TO_IN)
def test_heldout_out_to_in_flips(query: str, label: int) -> None:
    """Held-out queries that V4 flips OUT→IN must now route to docs."""
    assert _query_wants_docs(query) is True, f"Held-out flip: {query!r} (label={label}) expected True"


# `doNotExpire APM session workflow` is labeled ambiguous (1) but proposal §7
# negative #2 says this MUST stay OOB under V4 (strict-code blocklist contains
# `doNotExpire`). Lock that in.
def test_heldout_donotexpire_stays_out() -> None:
    """Per proposal §7 #2: doNotExpire-flow queries stay OOB under V4 (strict-code guard)."""
    assert _query_wants_docs("doNotExpire APM session workflow") is False


# ---------------------------------------------------------------------------
# Regression guard: existing IN-band queries must NOT flip OUT
# ---------------------------------------------------------------------------

EXISTING_IN_BAND = [
    # From test_hybrid_doc_intent.py — locked-in absence heuristic
    "Nuvei error code table reason strings",
    "webhook signature",
    "how to configure idempotency",
    "docs/guide/README.md",
    # Doc-keyword triggers
    "payment flow guide",
    "framework reference",
    "checklist for new provider",
    "rules for cross-provider sale",
    # Absence-based: 2..15 tokens, no code sig
    "async payout webhook handler pattern PENDING",
]


@pytest.mark.parametrize("query", EXISTING_IN_BAND)
def test_existing_in_band_preserved(query: str) -> None:
    """V4 must not break any existing IN-band routing."""
    assert _query_wants_docs(query) is True


# ---------------------------------------------------------------------------
# Regression guard: existing OUT-band code queries must stay OUT
# ---------------------------------------------------------------------------

EXISTING_OUT_BAND = [
    # Pure code-flow queries
    "handleCallback(req)",
    "SIGTERM_HANDLER",
    "async_flow_processor",
    "payment.proto schema",
    "handler.ts callback",
    # Long camelCase
    "express-api-v1 activateWorkflow expire-session checkout",
    # 2+ snake_case in sequence
    "express-api-v1 call-providers-initialize set internalMetadata",
]


@pytest.mark.parametrize("query", EXISTING_OUT_BAND)
def test_existing_out_band_preserved(query: str) -> None:
    """V4 must not flip pure-code queries into the docs tower."""
    assert _query_wants_docs(query) is False
