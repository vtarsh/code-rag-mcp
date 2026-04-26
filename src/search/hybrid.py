"""Hybrid search — RRF fusion of FTS5 + vector results + CrossEncoder reranking.

Pipeline:
  1. FTS5 keyword search (2x weight, 100 candidates, NO per-repo cap)
  2. LanceDB vector search (50 candidates)
  3. RRF (Reciprocal Rank Fusion) to merge both lists
  4. CrossEncoder reranker (70% rerank + 30% RRF) for final ordering

Per-repo diversity is NOT applied here — candidates must survive fusion
and reranking on merit. Diversity capping happens only at the
presentation layer (search tool output) to control output size.
"""

from __future__ import annotations

import logging
import os
import re

from src.config import (
    CI_PENALTY,
    CODE_FACT_BOOST,
    CODE_FACT_INJECT_WEIGHT,
    DICTIONARY_BOOST,
    DOC_PENALTY,
    ENV_VAR_BOOST,
    GOTCHAS_BOOST,
    GUIDE_PENALTY,
    KEYWORD_WEIGHT,
    PROVIDER_PREFIXES,
    REFERENCE_BOOST,
    RERANK_POOL_SIZE,
    RRF_K,
    TEST_PENALTY,
)
from src.container import db_connection, get_reranker
from src.search.code_facts import code_facts_search, fetch_chunks_for_files
from src.search.env_vars import env_var_search
from src.search.fts import fts_search
from src.search.vector import vector_search

# Cross-provider fan-out (2026-04-23): eliminates provider-swap reformulation
# chains. 82% of reformulation chains end with identical result_len and 56% of
# transitions are pure provider token swaps (nuvei -> payper -> volt). When the
# query matches {provider} {topic_verb}, we pull top-1 analogous chunk from each
# sibling provider repo and prepend a grouped header.
#
# Provider list: hard-coded to top-10 payment providers. PROVIDER_PREFIXES from
# conventions.yaml tells us WHERE to look (repo prefix), but the actual provider
# names are encoded in the repo names themselves — not independently exposed.
# Keeping this list small and explicit is safer than scanning the repo index at
# import time (which would need a DB connection).
_KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {
        "payper",
        "nuvei",
        "trustly",
        "volt",
        "ppro",
        "paynearme",
        "aeropay",
        "fonix",
        "paysafe",
        "worldpay",
    }
)

# Topic verbs that indicate an operation which is implemented per-provider.
# Must appear adjacent to a provider token for fan-out to trigger.
_TOPIC_VERBS: frozenset[str] = frozenset(
    {
        "payout",
        "refund",
        "sale",
        "webhook",
        "initialize",
        "dispatch",
        "activities",
        "signature",
        "credentials",
        "idempotency",
    }
)

# Max sibling providers returned (spec: up to 6 siblings).
_MAX_SIBLINGS: int = 6


def _detect_provider_topic(query: str) -> tuple[str, str] | None:
    """Return (provider, topic_verb) if the query contains both tokens, else None.

    Both must appear in the query; order does not matter so "nuvei payout" and
    "payout handle-activities nuvei" both trigger fan-out. Case-insensitive,
    word-boundary matched to avoid false positives on substrings.

    When a query contains multiple valid topic tokens (e.g. "nuvei payout
    handle-activities.js" has both `payout` and `activities`), we preserve the
    token order from the original query so tests and the resulting FTS query
    line up with user intent — the leftmost verb wins.
    """
    if not query:
        return None
    token_list = [t.lower().strip(".,;:!?") for t in re.split(r"[\s/\-_.]+", query) if t]
    token_set = set(token_list)
    provider = next((t for t in token_list if t in _KNOWN_PROVIDERS), None) or next(
        (p for p in _KNOWN_PROVIDERS if p in token_set), None
    )
    topic = next((t for t in token_list if t in _TOPIC_VERBS), None) or next(
        (t for t in _TOPIC_VERBS if t in token_set), None
    )
    if provider and topic:
        return provider, topic
    return None


def _sibling_provider_repos(active_provider: str) -> list[tuple[str, str]]:
    """Return up to _MAX_SIBLINGS (sibling_name, repo_name) pairs.

    Uses PROVIDER_PREFIXES from conventions.yaml to build candidate repo names.
    Skips the active provider and the 4th-prefix "mpi" (historically only 3D
    Secure, not payment methods). Output is deterministic (sorted siblings).
    """
    if not PROVIDER_PREFIXES:
        # Fallback for profiles that don't declare prefixes — use the two most
        # common layouts so the fan-out still fires on pay-com-like orgs.
        prefixes = ("grpc-apm-", "grpc-providers-")
    else:
        prefixes = tuple(PROVIDER_PREFIXES[:2])

    siblings = sorted(p for p in _KNOWN_PROVIDERS if p != active_provider)
    out: list[tuple[str, str]] = []
    for sib in siblings:
        for prefix in prefixes:
            out.append((sib, f"{prefix}{sib}"))
        if len({s for s, _ in out}) >= _MAX_SIBLINGS:
            break
    return out


def _cross_provider_fanout(query: str, limit_per_sibling: int = 1) -> tuple[str, str] | tuple[None, None]:
    """Build a cross-provider sibling header.

    Returns (header_text, topic_verb) on hit, (None, None) when the query does
    not match the {provider} {topic_verb} pattern or no sibling chunks exist.

    For each sibling repo we fire a lightweight FTS5 query for the topic verb
    filtered to that repo. Top-1 hit per sibling is included, capped at
    _MAX_SIBLINGS unique sibling providers.
    """
    hit = _detect_provider_topic(query)
    if hit is None:
        return None, None
    active, topic = hit

    sibling_pairs = _sibling_provider_repos(active)
    lines: list[str] = []
    seen_providers: set[str] = set()
    for sib_name, repo_name in sibling_pairs:
        if sib_name in seen_providers:
            continue
        if len(seen_providers) >= _MAX_SIBLINGS:
            break
        try:
            rows = fts_search(topic, repo=repo_name, limit=limit_per_sibling)
        except Exception:
            rows = []
        if not rows:
            continue
        top = rows[0]
        snippet = re.sub(r">>>|<<<", "", top.snippet or "")[:200]
        lines.append(f"  - **{top.repo_name}** | `{top.file_path}` ({top.file_type}/{top.chunk_type})\n    {snippet}")
        seen_providers.add(sib_name)

    if not lines:
        return None, None

    header = f"## Cross-provider siblings for '{topic}'\n\n" + "\n\n".join(lines) + "\n"
    return header, topic


# A/B investigation env gates (post-P0a hybrid eval found 103 tickets lose GT
# repos vs fts_only+fallback). Default 0 = production behaviour unchanged.
# Set =1 in eval runs to isolate whether penalties or code_facts/env_vars are
# responsible for the regression.
_DISABLE_PENALTIES = os.getenv("CODE_RAG_DISABLE_PENALTIES", "0") == "1"
_DISABLE_CODE_FACTS = os.getenv("CODE_RAG_DISABLE_CODE_FACTS", "0") == "1"

# P10 Phase 1 (2026-04-25): canary gate to skip CrossEncoder reranking on
# doc-intent queries. Validated +1.5pp R@10 / +3.1pp hit@10 / -160ms p50 on
# eval-v3-n200 (see `.claude/debug/p10-quickwin-report.md`). Default 0 keeps
# production behaviour byte-for-byte identical; set =1 for canary deploys.
# Read at call-time (not module-import-time) so tests / canary toggles can flip
# the var without re-importing the module.

_logger = logging.getLogger(__name__)

# File types considered "documentation-like" — penalized unless query asks for docs.
# Matches user spec (P4.1): doc/task/gotchas/reference. Extended with dictionary,
# provider_doc, and flow_annotation because in practice these are derived-knowledge
# chunks that dominate keyword matches for code queries but are not production code.
_DOC_FILE_TYPES: frozenset[str] = frozenset(
    {"doc", "docs", "task", "gotchas", "reference", "dictionary", "provider_doc", "flow_annotation"}
)

# Regex patterns for path-based classification. Compiled once at import.
_TEST_PATH_RE = re.compile(r"(?:\.spec\.(?:js|ts|tsx|jsx)$|\.test\.(?:js|ts|tsx|jsx|py)$|_test\.py$|/tests?/)")
_GUIDE_PATH_RE = re.compile(
    r"(?:/AI-CODING-GUIDE\.md$|/CLAUDE\.md$|/README\.md$|^AI-CODING-GUIDE\.md$|^CLAUDE\.md$|^README\.md$)",
    re.IGNORECASE,
)
# P1c 2026-04-22: CI/deploy yaml files are neither docs nor service code. v8
# FT reranker systematically surfaces them on short repo queries (e.g. "ach
# provider service integration repo" -> 5x workflow-ach-*::ci/deploy.yml).
# Treat them as doc-level noise on code-intent queries.
_CI_PATH_RE = re.compile(
    r"(?:^|/)(?:ci/deploy\.ya?ml|k8s/\.github/workflows/)",
    re.IGNORECASE,
)

# Query keywords that disable penalties (user explicitly asked for docs/tests
# or for a named doc artifact). P1c 2026-04-22: extended with doc-artifact
# tokens (checklist/framework/matrix/severity/sandbox/overview/reference/rules)
# after Opus-judge pass showed 11 of 19 base-win pairs failed on these tokens
# because v8's doc-penalty demoted the exact doc file the user asked for.
#
# P8 2026-04-25 (V4): added Tier-1 strong markers `gotcha(s)` and `how to`.
# `gotcha(s)` is the explicit name of the doc folder (`docs/gotchas/`); `how to`
# is an unambiguous question marker. Both override `_CODE_SIG_RE` rejection.
_DOC_QUERY_RE = re.compile(
    r"\b("
    r"test|tests|spec|specs|"
    r"docs?|documentation|readme|guide|guides|tutorial|"
    r"checklist|framework|matrix|severity|sandbox|overview|reference|rules|"
    r"gotcha|gotchas|how\s+to"
    r")\b",
    re.IGNORECASE,
)


_CODE_SIG_RE = re.compile(
    r"(?:\b[a-z][a-zA-Z0-9]*\([^)]*\)|"
    r"\b[A-Z][A-Z0-9_]{2,}\b|"
    r"[a-z]+_[a-z_]+|"
    r"\.(?:js|ts|py|go|proto)\b)"
)
_REPO_TOKEN_RE = re.compile(
    r"\b(?:grpc-|express-|next-web-|workflow-|k8s-)[a-z0-9-]+\b",
    re.IGNORECASE,
)


# P8 2026-04-25 (V4): repo-overview pattern. 28 OOB queries are bare repo-tokens
# (e.g. `grpc-providers-features`, optionally `... repo|repository`); all
# hand-classified as doc-intent (repo overview).
_REPO_OVERVIEW_RE = re.compile(
    r"^\s*(?:grpc-|express-|next-web-|workflow-|k8s-|backoffice-)[a-z0-9-]+"
    r"(?:\s+(?:repo|repository))?\s*$",
    re.IGNORECASE,
)

# P8 2026-04-25 (V4): provider-only short query. Bare provider name (single
# token) signals provider-overview / docs intent — captures queries like
# `trustly`, `nuvei` that the absence heuristic rejected on tok_lt2.
_PROVIDER_ONLY_RE = re.compile(
    r"^\s*(?:nuvei|trustly|payper|volt|ppro|paynearme|aeropay|fonix|paysafe|"
    r"worldpay|skrill|aircash|okto|interac|neosurf|rapyd|epay|fortumo)\s*$",
    re.IGNORECASE,
)

# P8 2026-04-25 (V4): Tier-3 concept-doc keywords. Strong doc-intent markers
# that the absence heuristic missed when paired with code-style tokens. Routed
# to docs UNLESS _STRICT_CODE_RE also matches (mined code-flow blocklist below).
_CONCEPT_DOC_RE = re.compile(
    r"\b(apm|tokenizer|vault|sepa|voucher|"
    r"integrate|integration|integrations|"
    r"provider\s+integration|how\s+does|how\s+is|pattern|repo|repository)\b",
    re.IGNORECASE,
)

# P8 2026-04-25 (V4): mined "definitely-code" markers. Blocks Tier-3 routing so
# concept words paired with explicit code-flow tokens (e.g. `doNotExpire APM`,
# `paynearme methods/sale.js`) keep going to the code tower. Blocklist tokens
# observed exclusively in code-intent labeled queries (n=50 + held-out 30).
_STRICT_CODE_RE = re.compile(
    r"(?:\.(?:js|ts|tsx|jsx|py|go|proto)\b|"  # file extension
    r"\b[a-z][a-zA-Z]{8,}[A-Z][a-zA-Z]+\b|"  # long camelCase like internalMetadata
    r"(?:[a-z]+_[a-z_]+\s+){1,}[a-z]+_[a-z_]+|"  # 2+ snake_case in sequence
    r"\b(?:doNotExpire|signalWithStart|activateWorkflow|destructure|"
    r"udf|destination_data|process-initialize-data|seeds\.cql|"
    r"call-providers-initialize|paymentMethodType|sourceDataType|"
    r"reusablePayouts|notificationType|companyId|transactionId|"
    r"WITHDRAW_REQUEST|WITHDRAW_ORDER|EXPIRED|updateTransaction|"
    r"PAYMENT_METHODS|PROVIDER_TRANSACTION|FF3Cipher|accountNumber|"
    r"clientIp|ip_address|clientUniqueId|TransactionID)\b)",
)


def _query_wants_docs(query: str) -> bool:
    """Doc-intent classifier with V4 router extension (P8 2026-04-25).

    Decision order:
      1. Tier-1 STRONG_DOC trigger (gotcha/how-to + existing doc tokens) → True.
      2. Tier-2 repo-overview anchor (bare repo-token, optionally + repo/ory) → True.
      3. Tier-2 provider-only anchor (bare provider name) → True.
      4. Tier-3 concept-doc keyword AND no strict-code blocklist hit → True.
      5. Code signature OR repo token present → False (legacy code-intent).
      6. Absence heuristic: 2..15 tokens → True.
      7. Otherwise → False.

    V4 expected effect on prod traffic: +394 OOB queries route to docs
    (+12.9pp). Held-out smoke: 0 IN→OUT flips, 0 code mis-routes, 11 OUT→IN
    flips (7 doc + 4 ambiguous).
    """
    if _DOC_QUERY_RE.search(query or ""):
        return True
    if not query:
        return False
    # Tier-2: repo-overview / provider-only anchors run BEFORE code_sig
    # rejection so bare repo-tokens (`grpc-providers-features`) and bare
    # provider names (`trustly`) route to docs instead of being eaten by
    # _REPO_TOKEN_RE / tok_lt2.
    if _REPO_OVERVIEW_RE.search(query):
        return True
    if _PROVIDER_ONLY_RE.search(query):
        return True
    # Tier-3: concept-doc keyword wins UNLESS a strict-code marker also fires.
    # Keeps "Okto Cash APM integrations" as docs while keeping
    # "doNotExpire APM session workflow" as code.
    if _CONCEPT_DOC_RE.search(query) and not _STRICT_CODE_RE.search(query):
        return True
    if _CODE_SIG_RE.search(query) or _REPO_TOKEN_RE.search(query):
        return False
    tokens = query.split()
    return 2 <= len(tokens) <= 15


# P10 Phase A2-revise (2026-04-25 late): stratum-gated rerank-skip — INVERTED.
#
# Original A2 (2026-04-26 stratum map) was inverted vs true reranker behavior.
# v2 LLM-calibrated eval (10 Opus agents, ~2200 judgments, n=192 across 10
# strata in `profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl`) revealed the
# correct direction. Per-stratum R@10 deltas (A2 with skip-on-OFF vs full
# rerank-on baseline):
#
#   reranker HURTS (skip → larger R@10 lift):
#     webhook +3.35pp, trustly +2.68pp, method +1.30pp, payout +1.11pp
#
#   reranker HELPS (must keep rerank to avoid regression):
#     nuvei -7.58pp, aircash -8.78pp, refund -14.51pp
#
#   flat / small loss → conservative KEEP rerank:
#     interac 0.00, provider -0.24pp, tail -1.96pp
#
# `tail` is the catch-all (no stratum tokens match) and resolves via the
# `_detect_stratum() → None → KEEP rerank` fallback below. Provider-specific
# OFF tokens are checked first so a query mentioning both `trustly` and
# `provider` lands in OFF (where the calibrated eval shows it belongs).
_DOC_RERANK_OFF_STRATA: frozenset[str] = frozenset(
    {
        "webhook",
        "trustly",
        "method",
        "payout",
    }
)
_DOC_RERANK_KEEP_STRATA: frozenset[str] = frozenset(
    {
        "nuvei",
        "aircash",
        "refund",
        "interac",
        "provider",
    }
)

# Provider/topic token map — case-insensitive substring match on the query.
# OFF strata are checked first (provider-specific tokens like `trustly` are
# more selective than generic ones like `provider`/`psp`), so a query
# mentioning both lands in the OFF set.
_STRATUM_TOKENS: dict[str, tuple[str, ...]] = {
    "trustly": ("trustly",),
    "webhook": ("webhook", "callback", "notification"),
    "method": ("method",),
    "payout": ("payout",),
    "nuvei": ("nuvei",),
    "aircash": ("aircash",),
    "refund": ("refund", "chargeback"),
    "interac": ("interac", "etransfer", "e-transfer"),
    "provider": ("provider", "psp"),
}

_STRATUM_CHECK_ORDER: tuple[str, ...] = (
    # OFF first — provider-specific tokens are more selective than generic
    # OFF tokens (`method`, `payout`) and the KEEP set's `provider`/`psp`.
    "trustly",
    "webhook",
    "method",
    "payout",
    # KEEP after — provider-specific KEEP names (`nuvei`, `aircash`) before
    # the generic `refund`/`interac`/`provider` tokens.
    "nuvei",
    "aircash",
    "refund",
    "interac",
    "provider",
)

# Sanity invariant: every stratum that has a token map must be classified as
# either OFF or KEEP. Catches typos / split-brain config at import time.
assert set(_STRATUM_TOKENS.keys()) == (_DOC_RERANK_OFF_STRATA | _DOC_RERANK_KEEP_STRATA), (
    "Stratum token map must cover all OFF and KEEP strata exactly. "
    f"missing={(_DOC_RERANK_OFF_STRATA | _DOC_RERANK_KEEP_STRATA) - set(_STRATUM_TOKENS.keys())}, "
    f"extra={set(_STRATUM_TOKENS.keys()) - (_DOC_RERANK_OFF_STRATA | _DOC_RERANK_KEEP_STRATA)}"
)


def _detect_stratum(query: str) -> str | None:
    """Detect the eval-v3-n200 stratum a query maps to via token presence.

    Returns the first stratum whose tokens appear in `query` (lowercased,
    substring match). Order = OFF strata first (provider-specific names are
    more selective), then KEEP strata. Returns None when no stratum token
    matches, in which case the caller falls back to the conservative default
    (run reranker — preserves current production behavior).
    """
    if not query:
        return None
    q = query.lower()
    for stratum in _STRATUM_CHECK_ORDER:
        if any(tok in q for tok in _STRATUM_TOKENS[stratum]):
            return stratum
    return None


def _should_skip_rerank(query: str, is_doc_intent: bool) -> bool:
    """Stratum-gated rerank-skip decision.

    Default behavior:
      - non-doc-intent → False (run reranker, existing behavior preserved)
      - env `CODE_RAG_DOC_RERANK_OFF=1` → True (kill-switch back-compat with
        the P10 Phase 1 quickwin; disables reranker for ALL doc-intent queries)
      - doc-intent + stratum in OFF set → True (reranker hurts these strata)
      - doc-intent + stratum in KEEP set → False (reranker helps these strata)
      - doc-intent + unknown stratum → False (conservative fallback: reranker
        runs on queries the gate can't classify)
    """
    if not is_doc_intent:
        return False
    if os.getenv("CODE_RAG_DOC_RERANK_OFF", "0") == "1":
        return True
    stratum = _detect_stratum(query)
    if stratum is None:
        return False
    return stratum in _DOC_RERANK_OFF_STRATA


def _classify_penalty(file_type: str, file_path: str) -> float:
    """Return the penalty delta (in normalized score units) for a result.

    Priority order (strongest penalty wins):
      1. Guide-like paths (AI-CODING-GUIDE.md / CLAUDE.md / README.md) -> GUIDE_PENALTY
      2. Test paths (*.spec.js, *.test.py, /tests/...) -> TEST_PENALTY
      3. CI yaml paths (ci/deploy.yml, k8s/.github/workflows/*) -> CI_PENALTY
         (stronger than DOC_PENALTY — v8 surfaces 5+ CI files on short repo
         queries, and DOC_PENALTY=0.15 was insufficient on pair #2).
      4. Doc-ish file_type (doc, task, gotchas, reference) -> DOC_PENALTY
    Returns 0.0 for production code (unchanged).

    Eval A/B: CODE_RAG_DISABLE_PENALTIES=1 short-circuits to 0.0 so penalties
    can be isolated as a cause when hybrid-mode eval drops GT repos.
    """
    if _DISABLE_PENALTIES:
        return 0.0
    path = file_path or ""
    if _GUIDE_PATH_RE.search(path):
        return GUIDE_PENALTY
    if _TEST_PATH_RE.search(path):
        return TEST_PENALTY
    if _CI_PATH_RE.search(path):
        return CI_PENALTY
    if (file_type or "") in _DOC_FILE_TYPES:
        return DOC_PENALTY
    return 0.0


def rerank(
    query: str,
    results: list[dict],
    limit: int = 10,
    *,
    reranker_override=None,
) -> list[dict]:
    """Rerank search results with the local CrossEncoder provider.

    Takes RRF-fused results and reranks by scoring each snippet
    against the query. Combines: 70% reranker score + 30% normalized RRF score.

    `reranker_override` (P0a): any object with a `rerank(query, documents, limit)`
    method replaces `get_reranker()` for the duration of this call. Used by
    `scripts/eval_finetune.py --use-hybrid-retrieval` to score the same RRF pool
    with an arbitrary CrossEncoder so eval shares the production candidate set.
    """
    if not results or len(results) <= 1:
        return results

    if reranker_override is not None:
        reranker, err = reranker_override, None
    else:
        reranker, err = get_reranker()
    if err or reranker is None:
        return results  # Fallback: return original order

    # Build document strings for reranker
    documents: list[str] = []
    for r in results:
        doc = re.sub(r">>>|<<<|\.\.\.|\[Repo: [^\]]+\]", "", r.get("snippet", ""))
        doc = f"{r['repo_name']} {r['file_path']} {doc}"
        documents.append(doc)

    scores = reranker.rerank(query, documents, limit=limit)

    if not scores:
        return results[:limit]

    # Normalize reranker scores to [0, 1]
    max_score = max(scores) if scores else 1
    min_score = min(scores) if scores else 0
    score_range = max_score - min_score if max_score != min_score else 1

    # Combine: reranker score (70%) + original RRF score (30%)
    max_rrf = max(r["score"] for r in results) if results else 1
    min_rrf = min(r["score"] for r in results) if results else 0
    rrf_range = max_rrf - min_rrf if max_rrf != min_rrf else 1

    # Skip doc/test penalties when the query explicitly asks for them.
    apply_penalties = not _query_wants_docs(query)

    for i, r in enumerate(results):
        rrf_norm = (r["score"] - min_rrf) / rrf_range
        rerank_norm = (scores[i] - min_score) / score_range if i < len(scores) else 0
        r["rerank_score"] = float(scores[i]) if i < len(scores) else 0
        combined = 0.7 * rerank_norm + 0.3 * rrf_norm

        # P4.1: down-weight doc/test/guide chunks so production code ranks higher
        # on code-related queries. Stored on result for observability.
        penalty = _classify_penalty(r.get("file_type", ""), r.get("file_path", "")) if apply_penalties else 0.0
        r["penalty"] = penalty
        r["combined_score"] = combined - penalty

    results.sort(key=lambda x: x["combined_score"], reverse=True)
    return results[:limit]


def _apply_code_facts(
    scores: dict[str, dict],
    query: str,
    repo: str,
    rrf_k: int,
    kw_weight: float,
) -> None:
    """P0c: Fold code_facts_fts hits into the RRF pool.

    Two effects:
      1. Boost — for existing (repo, file) pairs already in the pool, multiply
         the RRF score by CODE_FACT_BOOST. Signals a structural match (e.g. a
         validation guard whose condition contains a query term).
      2. Inject — for (repo, file) pairs NOT in the pool, fetch the first chunk
         from that file and insert it with position-based RRF weight. This is
         the recall-surface part: chunks the keyword/vector search missed but
         code_facts matched.

    P0 (2026-04-22): `scores` is keyed by `f"{source}:{rowid}"` to avoid
    collisions between FTS and vector rowid spaces. `fetch_chunks_for_files`
    returns rowids from the `chunks` (FTS5) table, so injected records use
    the `fts:` prefix to stay coherent if the same chunk surfaces via FTS.
    """
    cf_hits = code_facts_search(query, repo, limit=50)
    if not cf_hits:
        return

    existing_files: set[tuple[str, str]] = {
        (data.get("repo_name", ""), data.get("file_path", "")) for data in scores.values()
    }

    seen_pairs: set[tuple[str, str]] = set()
    ordered_pairs: list[tuple[str, str]] = []
    for hit in cf_hits:
        key = (hit["repo_name"], hit["file_path"])
        if key not in seen_pairs:
            seen_pairs.add(key)
            ordered_pairs.append(key)

    boost_pairs = seen_pairs & existing_files
    missing_pairs = [p for p in ordered_pairs if p not in existing_files]

    if boost_pairs:
        for data in scores.values():
            key = (data.get("repo_name", ""), data.get("file_path", ""))
            if key in boost_pairs:
                data["score"] *= CODE_FACT_BOOST
                if "code_facts" not in data["sources"]:
                    data["sources"].append("code_facts")

    if missing_pairs:
        injected = fetch_chunks_for_files(missing_pairs)
        for rank_idx, chunk in enumerate(injected):
            # chunk["rowid"] comes from the `chunks` (FTS5) table.
            key = f"fts:{chunk['rowid']}"
            if key in scores:
                continue
            rrf_score = (kw_weight * CODE_FACT_INJECT_WEIGHT) / (rrf_k + rank_idx + 1)
            scores[key] = {
                "score": rrf_score,
                "repo_name": chunk["repo_name"],
                "file_path": chunk["file_path"],
                "file_type": chunk["file_type"],
                "chunk_type": chunk["chunk_type"],
                "snippet": chunk["snippet"],
                "sources": ["code_facts"],
            }


def _apply_env_vars(scores: dict[str, dict], query: str) -> None:
    """P0c: Boost repos that define UPPERCASE env vars in the query.

    Repo-level signal — lighter than code_facts (file-level). Only fires when
    the query contains at least one UPPERCASE_IDENTIFIER token.
    """
    ev_hits = env_var_search(query, limit=30)
    if not ev_hits:
        return

    ev_repos: set[str] = {hit["repo"] for hit in ev_hits}
    if not ev_repos:
        return

    for data in scores.values():
        if data.get("repo_name", "") in ev_repos:
            data["score"] *= ENV_VAR_BOOST
            if "env_var" not in data["sources"]:
                data["sources"].append("env_var")


def hybrid_search(
    query: str,
    repo: str = "",
    file_type: str = "",
    exclude_file_types: str = "",
    limit: int = 10,
    *,
    reranker_override=None,
    cross_provider: bool = False,
    docs_index: bool | None = None,
) -> tuple[list[dict], str | None, int]:
    """Hybrid search: combine FTS5 keyword + vector similarity via RRF.

    Keyword results get 2x weight because exact term matches are more
    reliable for code search than semantic similarity alone.

    `reranker_override` (P0a): passed through to `rerank()`. Used by
    `eval_finetune.py --use-hybrid-retrieval` so each eval model scores the
    production RRF pool (FTS + vector + code_facts/env_vars + content boosts)
    instead of a detached FTS-only pool.

    `cross_provider` (2026-04-23): when True and the query matches the
    `{provider} {topic_verb}` pattern (e.g. "nuvei payout"), the top-result
    snippet is prefixed with a `## Cross-provider siblings for '{topic}'`
    header listing top-1 analogous chunks from up to 6 sibling provider repos.
    Default False preserves byte-for-byte output.

    `docs_index` (2026-04-23 two-tower): overrides vector-leg routing.
      - None  (default): auto-route by query intent — pure doc-intent hits the
        docs tower only, pure code-intent hits the code tower only, and
        ambiguous / mixed queries fan out to both towers and merge.
      - True  : force the docs tower regardless of intent (debug / eval).
      - False : force the code tower regardless of intent (debug / eval).
      FTS5 is content-agnostic and always runs against the shared chunks pool.

    Returns (ranked_results, vector_error | None, total_candidates).
    """
    K = RRF_K
    KW_WEIGHT = KEYWORD_WEIGHT

    # 1. Keyword search (FTS5) — large pool, no per-repo cap.
    #    P4.2: raised 100→150 to fill rerank pool to ~200 after RRF overlap.
    keyword_results = fts_search(query, repo, file_type, exclude_file_types, limit=150)

    # 2. Vector search — two-tower routing.
    #
    # The vector leg is the only part of the pipeline that changes per tower.
    # We route by query intent (docs vs code) and fan out to both towers for
    # ambiguous queries, then dedupe by rowid before the RRF loop so a chunk
    # that surfaces in both towers contributes one RRF position (not two).
    #
    # Dedupe strategy: same `rowid` across towers means the same row in the
    # SQLite `chunks` table (towers share that table — only the embeddings
    # differ). Keeping the first occurrence preserves the better-ranked
    # position from whichever tower surfaced it first, and matches the existing
    # `if key not in scores` behaviour in the RRF loop. Alternatives considered:
    # (a) summing RRF scores (double-boosts mixed hits — spec calls this out),
    # (b) using a fixed merged position (throws away ranking signal). Dedupe
    # by rowid is the lowest-risk option.
    if docs_index is True:
        vector_results, vec_err = vector_search(query, repo, file_type, exclude_file_types, limit=50, model_key="docs")
    elif docs_index is False:
        vector_results, vec_err = vector_search(query, repo, file_type, exclude_file_types, limit=50)
    else:
        is_doc_intent = _query_wants_docs(query)
        has_code_signal = bool(_CODE_SIG_RE.search(query or "") or _REPO_TOKEN_RE.search(query or ""))
        if is_doc_intent and not has_code_signal:
            # pure doc intent → docs tower only
            vector_results, vec_err = vector_search(
                query, repo, file_type, exclude_file_types, limit=50, model_key="docs"
            )
        elif has_code_signal and not is_doc_intent:
            # pure code intent → code tower only (unchanged legacy path)
            vector_results, vec_err = vector_search(query, repo, file_type, exclude_file_types, limit=50)
        else:
            # mixed / ambiguous → query both towers and merge.
            code_results, code_err = vector_search(query, repo, file_type, exclude_file_types, limit=50, model_key=None)
            docs_results, docs_err = vector_search(
                query, repo, file_type, exclude_file_types, limit=50, model_key="docs"
            )
            # Dedupe by rowid keeping first occurrence (code tower first → its
            # ranking wins on collisions; rationale in block comment above).
            seen_rowids: set = set()
            merged: list[dict] = []
            for vrow in list(code_results) + list(docs_results):
                rid = vrow.get("rowid")
                if rid in seen_rowids:
                    continue
                seen_rowids.add(rid)
                merged.append(vrow)
            vector_results = merged
            vec_err = code_err or docs_err

    # 3. RRF fusion
    #
    # P0 (2026-04-22): `scores` is keyed by `f"{source}:{rowid}"` to prevent
    # collisions between the FTS5 `chunks` table and the LanceDB vector table.
    # The two rowid spaces are independent — `rowid=42` in FTS points to a
    # DIFFERENT chunk than `rowid=42` in vector. Keying by raw int merged them
    # into one corrupted record (keeping whichever hit arrived first for
    # repo/path/snippet and summing both RRF scores).
    #
    # We do NOT attempt to re-merge "same logical chunk" across sources here
    # because chunk identity (repo, file, chunk_type) is not unique — a file
    # often has many chunks of the same chunk_type. The downstream reranker
    # scores by content, so a chunk that surfaces in both sources at distinct
    # keys is ranked consistently by the cross-encoder rather than artificially
    # boosted by RRF-sum tricks.
    scores: dict[str, dict] = {}  # "fts:<rowid>" | "vec:<rowid>" → result dict

    for rank_idx, sr in enumerate(keyword_results):
        key = f"fts:{sr.rowid}"
        rrf_score = KW_WEIGHT / (K + rank_idx + 1)
        if key not in scores:
            scores[key] = {
                "score": 0,
                "repo_name": sr.repo_name,
                "file_path": sr.file_path,
                "file_type": sr.file_type,
                "chunk_type": sr.chunk_type,
                "snippet": sr.snippet,
                "sources": [],
            }
        scores[key]["score"] += rrf_score
        scores[key]["sources"].append("keyword")

    for rank_idx, vrow in enumerate(vector_results):
        key = f"vec:{vrow['rowid']}"
        rrf_score = 1.0 / (K + rank_idx + 1)
        if key not in scores:
            scores[key] = {
                "score": 0,
                "repo_name": vrow["repo_name"],
                "file_path": vrow["file_path"],
                "file_type": vrow["file_type"],
                "chunk_type": vrow["chunk_type"],
                "snippet": vrow.get("content_preview", ""),
                "sources": [],
            }
        scores[key]["score"] += rrf_score
        scores[key]["sources"].append("vector")

    # P0c: wire code_facts_fts — structured facts (schemas, env lookups, guards,
    # retry policies) that chunks_fts can miss. Boost chunks whose (repo, file)
    # match a code_facts hit, and inject a candidate chunk for hits that the
    # keyword/vector pool missed entirely.
    #
    # Eval A/B: CODE_RAG_DISABLE_CODE_FACTS=1 skips both code_facts and env_vars
    # wiring so the hybrid-mode regression on 103 "lost" tickets can be
    # attributed (or not) to these candidate-pool injections.
    if not _DISABLE_CODE_FACTS:
        _apply_code_facts(scores, query, repo, K, KW_WEIGHT)
        # P0c: wire env_vars — UPPERCASE identifiers in the query resolve to the
        # repos where those env vars are defined. Light repo-level boost.
        _apply_env_vars(scores, query)

    # Apply content-type boosts — curated knowledge ranks higher
    TASK_BOOST = {
        "task_decisions": 1.1,
        "task_plan": 1.1,
        "task_api_spec": 1.05,
        "task_gotchas": 1.1,
        "task_description": 1.05,
        "task_metadata": 0.95,
        "task_progress": 0.7,
        "task_section": 1.0,
    }
    for _rid, data in scores.items():
        ft = data.get("file_type", "")
        if ft == "gotchas":
            data["score"] *= GOTCHAS_BOOST
        elif ft == "task":
            data["score"] *= TASK_BOOST.get(data.get("chunk_type", ""), 1.0)
        elif ft == "reference":
            data["score"] *= REFERENCE_BOOST
        elif ft == "dictionary":
            data["score"] *= DICTIONARY_BOOST

    total_candidates = len(scores)

    # Sort by RRF score, take top candidates for reranking.
    # P4.2: widened from `limit*2` to `max(limit*2, RERANK_POOL_SIZE)` so the
    # cross-encoder sees ~200 candidates (was ~20). `max(...)` preserves old
    # behavior when the caller asks for a very large limit.
    rerank_cap = max(limit * 2, RERANK_POOL_SIZE)
    ranked = sorted(scores.values(), key=lambda x: x["score"], reverse=True)[:rerank_cap]

    # P10 Phase A2 (2026-04-26): stratum-gated reranker skip on doc-intent.
    # The production reranker is code-trained and transfers poorly to docs on
    # certain strata (nuvei -10.4pp, aircash -8.7pp, trustly -8.3pp,
    # webhook -5.6pp, refund -4.2pp on eval-v3-n200). On other strata it
    # rescues real wins (interac +14.8pp, provider +6.1pp; LLM-judge G1
    # confirms +18pp direct-rate / +0.67 DCG on hard queries). The gate
    # disables the reranker only on the negative-delta strata.
    #
    # Env `CODE_RAG_DOC_RERANK_OFF=1` is preserved as a kill-switch that
    # forces skip on ALL doc-intent queries — back-compat with P10 Phase 1.
    is_doc_intent = _query_wants_docs(query)
    skip_rerank_for_docs = _should_skip_rerank(query, is_doc_intent)
    if skip_rerank_for_docs:
        stratum = _detect_stratum(query)
        _logger.info(
            "rerank_skipped: doc_intent_query, stratum=%s, query=%r",
            stratum or "kill_switch",
            query,
        )
        ranked = ranked[:limit]
        for r in ranked:
            r["rerank_score"] = 0.0
            r["combined_score"] = r["score"]
            r["penalty"] = 0.0
    else:
        # Rerank with cross-encoder (eval path may override with a specific model)
        ranked = rerank(query, ranked, limit, reranker_override=reranker_override)

    # Cross-provider fan-out (post-rerank). When opted in and the query matches
    # {provider} {topic_verb}, prepend a grouped header with top-1 analogous
    # chunk from up to 6 sibling provider repos. This targets 56% of observed
    # reformulation transitions (provider-swap chains) and 82% of chains that
    # end with identical result_len (user searches in vain). Opt-in so the
    # default output stays byte-for-byte identical for callers that don't want
    # the expansion.
    if cross_provider and ranked:
        header, _topic = _cross_provider_fanout(query)
        if header:
            first = ranked[0]
            first["snippet"] = header + "\n" + (first.get("snippet") or "")
            first["has_cross_provider"] = True

    # Expand top results with sibling chunks for context
    ranked = _expand_siblings(ranked)

    # Inject similar repo annotations
    ranked = _annotate_similar_repos(ranked)

    return ranked, vec_err, total_candidates


def _expand_siblings(results: list[dict], max_siblings: int = 2) -> list[dict]:
    """For top results, append prev/next chunks from the same file as context.

    This helps reconstruct function bodies that span multiple chunks.
    Sibling chunks are appended to the snippet text, not added as separate results.
    """
    if not results:
        return results

    try:
        with db_connection() as conn:
            # Check if chunk_meta table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunk_meta'"
            ).fetchone()
            if not table_check:
                return results

            for result in results[:max_siblings]:
                repo = result["repo_name"]
                file_path = result["file_path"]

                # Find all chunks for this (repo, file) with ordering
                rows = conn.execute(
                    """SELECT c.rowid, c.content, cm.chunk_order
                       FROM chunks c
                       JOIN chunk_meta cm ON cm.chunk_rowid = c.rowid
                       WHERE c.repo_name = ? AND c.file_path = ?
                       ORDER BY cm.chunk_order""",
                    (repo, file_path),
                ).fetchall()

                if len(rows) <= 1:
                    continue

                # Find which chunk in the sequence matches our result
                snippet_text = result.get("snippet", "")
                current_order = None
                for row in rows:
                    # Match by content overlap
                    content = row[1] if isinstance(row, tuple) else row["content"]
                    if content and snippet_text and content[:100] in snippet_text[:200]:
                        current_order = row[2] if isinstance(row, tuple) else row["chunk_order"]
                        break

                if current_order is None:
                    continue

                # Collect adjacent chunks
                siblings = []
                for row in rows:
                    order = row[2] if isinstance(row, tuple) else row["chunk_order"]
                    content = row[1] if isinstance(row, tuple) else row["content"]
                    if order == current_order - 1 or order == current_order + 1:
                        siblings.append((order, content))

                if siblings:
                    siblings.sort(key=lambda x: x[0])
                    context_parts = []
                    for order, content in siblings:
                        label = "prev" if order < current_order else "next"
                        # Truncate sibling content to avoid huge results
                        truncated = content[:1000] if len(content) > 1000 else content
                        context_parts.append(f"\n--- [{label} chunk from same file] ---\n{truncated}")

                    result["snippet"] += "".join(context_parts)
                    result["has_siblings"] = True

            return results
    except Exception:
        return results


def _annotate_similar_repos(results: list[dict]) -> list[dict]:
    """Check if any result repos have similar_repo edges and add annotations.

    If a repo in results has a similar_repo edge to another repo NOT in results,
    inject an annotation so the user knows about the similar repo.
    """
    if not results:
        return results

    try:
        with db_connection() as conn:
            # Check if graph_edges table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_edges'"
            ).fetchone()
            if not table_check:
                return results

            result_repos = {r["repo_name"] for r in results}

            # Find similar_repo edges for repos in results
            if not result_repos:
                return results

            placeholders = ",".join("?" * len(result_repos))
            similar_rows = conn.execute(
                f"SELECT source, target, detail FROM graph_edges "
                f"WHERE edge_type = 'similar_repo' AND source IN ({placeholders})",
                list(result_repos),
            ).fetchall()

            if not similar_rows:
                return results

            # Group by source repo
            similar_map: dict[str, list[tuple[str, str]]] = {}
            for source, target, detail in similar_rows:
                similar_map.setdefault(source, []).append((target, detail))

            # Annotate results that have similar repos NOT already in results
            for result in results:
                repo = result["repo_name"]
                if repo in similar_map:
                    missing_similar = [
                        (target, detail) for target, detail in similar_map[repo] if target not in result_repos
                    ]
                    if missing_similar:
                        annotations = []
                        for target, detail in missing_similar[:3]:
                            annotations.append(f"{target} ({detail})")
                        result["snippet"] += "\n\n--- Similar repos (may be confused) ---\n" + "\n".join(
                            f"  - {a}" for a in annotations
                        )
                        result["has_similar_repos"] = True

            return results
    except Exception:
        return results
