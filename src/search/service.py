"""Search MCP tools — search().

Public MCP tool function registered with FastMCP.
"""

from __future__ import annotations

import os
import re

from src.cache import cache_key, cache_or_compute
from src.config import (
    CO_CHANGE_RULES,
    CREDENTIALS_REPO,
    FEATURE_REPO,
    GATEWAY_REPO,
    INFRA_REPOS,
    PROTO_REPOS,
    PROVIDER_PREFIXES,
    SHARED_FILES,
    WEBHOOK_REPOS,
)
from src.container import require_db
from src.feedback import log_search
from src.formatting import strip_repo_tag
from src.search.fts import expand_query, expand_query_dictionary
from src.search.hybrid import _KNOWN_PROVIDERS, hybrid_search
from src.search.suggestions import format_no_results

# 2026-04-27: env-gated `expand_query` — default OFF after meta-debate showed
# the glossary expansion regresses jira hit@10 by -9.71pp (W2-curated bench)
# and v2 hit@10 by -6.81pp (W2-curated bench), regardless of curation effort.
# Set `CODE_RAG_USE_EXPAND_QUERY=1` to re-enable for A/B or future glossary
# rebuild via Doc2Query. See `.claude/debug/current/meta-converged.md`.
_USE_EXPAND_QUERY = os.getenv("CODE_RAG_USE_EXPAND_QUERY", "1") == "1"
# FIX-G (2026-05-19): entity-boost collapse guard. The forensic found prod-bug/
# error queries ("Prod bug UNKNOWN: ...") degenerate — preprocess_query extracts
# a single entity and the whole query is replaced by it (FTS query = "UNKNOWN").
# V2 only entity-boosts when >=3 entities survive, so a 1-entity extraction
# never collapses the query. Env-gated, default OFF.
_QUERY_V2 = os.getenv("CODE_RAG_QUERY_V2", "1") == "1"  # enabled 2026-05-19
# Coverage hint: when the result list hits the requested `limit` and the pool
# holds more, tell the (agent) caller it was truncated so it can re-query wider
# instead of silently losing recall on broad/multi-file tasks.
_COVERAGE_HINT = os.getenv("CODE_RAG_COVERAGE_HINT", "1") == "1"

# 2026-05-18: Frontend/backend query intent routing.
# When a query smells like UI work (contains frontend keywords), boost
# frontend repos so backoffice-web / hosted-files surface above backend API.
_FRONTEND_KEYWORDS = frozenset(
    {
        # Pure UI/UX terms — these strongly indicate frontend work
        "component",
        "button",
        "modal",
        "tab",
        "page",
        "form",
        "ui",
        "style",
        "css",
        "tsx",
        "layout",
        "icon",
        "tooltip",
        "dropdown",
        "menu",
        "nav",
        "sidebar",
        "header",
        "table",
        "card",
        "input",
        "checkbox",
        "radio",
        "select",
        "filter",
        "toggle",
        "accordion",
        "carousel",
        "image",
        "chart",
        "render",
        "display",
        "click",
        "event",
        "handler",
        "animation",
        "hover",
        "focus",
        "scroll",
        "drag",
        "dashboard",
        "wizard",
        "backoffice",
        # Plurals (word-boundary "merchant" does NOT match "merchants")
        "merchants",
        "buttons",
        "tabs",
        "pages",
        "forms",
        "tables",
        "cards",
        "inputs",
    }
)
_FRONTEND_REPOS = frozenset(
    {
        "backoffice-web",
        "hosted-fields",
        "space-web",
        "components",
        "paypass-web",
        "checkout-web",
        "microfrontends-web",
        "next-web-transaction-drilldown",
        "next-web-alternative-payment-methods",
        "next-web-authorizing-transactions",
        "next-web-balance",
        "next-web-checkout",
        "next-web-decline-recovery",
        "next-web-dynamic-currency-converter",
        "next-web-partial-approval",
        "next-web-pay-with-bank",
        "next-web-payment-methods-configurations",
        "next-web-risk-rules",
        "next-web-settlement-drilldown",
    }
)
_FRONTEND_BOOST = float(os.getenv("CODE_RAG_FRONTEND_BOOST", "1.3"))

# 2026-05-22: Task-hint repo boost (Option A).
# Reranker is the binding constraint on H5 BO-* tasks where the intent
# classifier mis-routes "Audits export csv" / "Add Statuses to Onboarding
# Pricing" to the backend branch (the keywords "csv" / "pricing" win over
# absent FE keywords), and GT in `backoffice-web` sinks to rank 14-25. Local
# A/B (bench_runs/improve/agent_task_prefix/) shows a 2.0x FE-repo boost
# lifts 3/5 H5 tasks (BO-1078, BO-1433, BO-1579) into top-10 while leaving
# 3/3 BO controls (BO-1041, BO-1042, BO-1266) byte-identical. BO-941 with
# GT split across FE+BE regresses because boosting FE demotes its BE GT
# (graphql), so callers must own the FE-vs-BE call — we ONLY apply when
# explicitly hinted via the `task_hint` argument; the keyword-derived
# classifier is left untouched. Env-gated, default OFF.
_TASK_HINT_BOOST_DEFAULT = float(os.getenv("CODE_RAG_TASK_HINT_BOOST", "2.0"))
_VALID_TASK_HINTS = frozenset({"frontend", "backoffice", "backend"})

# 2026-05-22: Auto-detect task hint from JIRA-style prefix in the query.
# When CODE_RAG_AUTO_TASK_HINT=1, search_tool() will regex-scan the query
# for "BO-XXXX" / "PI-XXXX" / "HS-XXXX" and infer task_hint accordingly
# (BO/HS → "frontend", PI → "backend", CORE → no bias). The task ID is
# stripped from the query string before downstream FTS/vector calls to
# avoid noise tokens. Explicit caller-supplied task_hint always wins.
# Env-gated default OFF — opt-in until pod-bench validates net delta on
# realistic MCP usage patterns (callers like Claude orchestrator that
# prepend "BO-1579: " to JIRA task search queries).
_AUTO_TASK_HINT = os.getenv("CODE_RAG_AUTO_TASK_HINT", "0") == "1"
_TASK_ID_RE = re.compile(r"\b(PI|BO|HS|PAY|FE|BE|INF|CORE)-?(\d+)\b", re.IGNORECASE)
_TASK_PREFIX_BIAS = {
    "PI": "backend",
    "BO": "frontend",
    "HS": "frontend",
    "FE": "frontend",
    "BE": "backend",
    # CORE / PAY / INF intentionally NOT mapped — they span FE+BE in pay-com
    # and would mis-route per [[project-h5-fix-refuted-2026-05-22]] BO-941
    # multi-repo GT pattern (mixed-domain → leave task_hint=None).
}


def _auto_task_hint_from_query(query: str) -> tuple[str | None, str]:
    """Detect JIRA task prefix in query → (inferred_hint, cleaned_query).

    Returns (None, query) when no recognizable prefix or env is OFF.
    Strips the task ID from the cleaned query so downstream FTS5/vector
    don't waste a token slot on the ID itself.
    """
    if not _AUTO_TASK_HINT or not query:
        return None, query
    m = _TASK_ID_RE.search(query)
    if not m:
        return None, query
    prefix = m.group(1).upper()
    hint = _TASK_PREFIX_BIAS.get(prefix)
    cleaned = _TASK_ID_RE.sub("", query, count=1)
    # Collapse leftover separators (": ", " - ", etc.) and whitespace
    cleaned = re.sub(r"^\s*[:\-,]\s*|\s*[:\-,]\s*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return hint, cleaned


# Step 3-v3 (2026-05-21): default-FE soft boost for generic queries.
# 7/13 FE-bias misses (per .claude/debug/current/misses_slice1.md) had
# queries with NEITHER explicit FE keywords (button, modal, layout) NOR
# explicit BE keywords (resolver, mutation, grpc). Current logic gives
# them no boost → token-poor JSX components lose BM25 to token-dense
# .js siblings. Soft boost (1.2x default, conservatively below the
# 1.3x explicit-FE boost) lifts FE repos when the query is signal-free.
# Env-gated default OFF so we can pod-bench it cleanly vs baseline.
_FE_DEFAULT_BOOST = os.getenv("CODE_RAG_FE_DEFAULT_BOOST", "0") == "1"
_FE_DEFAULT_BOOST_MULT = float(os.getenv("CODE_RAG_FE_DEFAULT_BOOST_MULT", "1.2"))

# Backend repos that don't match the standard prefixes (grpc-, workflow-, express-api-)
# but are still backend/infrastructure repos that should receive backend boosts.
_BACKEND_REPOS = frozenset(
    {
        "graphql",
        GATEWAY_REPO,
        FEATURE_REPO,
        CREDENTIALS_REPO,
    }
)

# Backend signals — when present, the user is almost certainly doing backend work.
_BACKEND_KEYWORDS = frozenset(
    {
        "provider",
        "integration",
        "grpc",
        "microservice",
        "worker",
        "gateway",
        "apm",
        "payout",
        "refund",
        "charge",
        "hold",
        "auth",
        "token",
        "api",
        "dispute",
        "retry",
        "processor",
        "cvv",
        "decline",
        "validation",
        "postgres",
        "postgresql",
        "pg lib",
        "partition",
        "tuple",
        "clearing",
        "timezone",
        "reference",
        "changelogs",
        "bank transfer",
        "routes",
        "services",
        "workflow-tasks",
        "export",
        "csv",
        "pdf",
        "braintree",
        "ecentric",
        "shift4",
        "libra",
        "iris",
        "paypal",
        "silverflow",
        "tabapay",
        "nuvei",
        "trustly",
        "gumballpay",
        "nexi",
        "visa",
        "applepay",
        "worldpay",
        "neosurf",
        "payper",
        "ach",
        "rtp",
        "hubspot",
        "google utils",
        "webhook",
        "callback",
        "sale",
        "reserve",
        "adjustment",
        "reconciliation",
        "3ds",
        "risk engine",
        "pg migration",
        "migration",
        "sandbox",
        "ledger",
        "settlement",
        "routing",
        "routing rule",
        "fee",
        "pricing",
        "quote",
        "batch",
        "sftp",
        "cron",
        "job",
        "kafka",
        "cdc",
        "scylla",
        "clickhouse",
        "snowflake",
        "vault",
        "encryption",
        "decrypt",
        "hash",
        "hmac",
        "jwt",
        "session",
        "cookie",
        "oauth",
        "saml",
        "mfa",
        "2fa",
        "captcha",
        "fraud",
        "aml",
        "kyc",
        "pci",
        "gdpr",
        "hipaa",
        "soc2",
        "iso27001",
        "penetration test",
        "security audit",
        "vulnerability",
        "cve",
        "dependency",
        "package",
        "npm",
        "pip",
        "go mod",
        "maven",
        "gradle",
        "docker",
        "kubernetes",
        "k8s",
        "helm",
        "terraform",
        "ansible",
        "pulumi",
        "aws",
        "gcp",
        "azure",
        "cloudflare",
        "cdn",
        "dns",
        "ssl",
        "tls",
        "certificate",
        "load balancer",
        "reverse proxy",
        "nginx",
        "envoy",
        "istio",
        "linkerd",
        "consul",
        "etcd",
        "zookeeper",
        "redis",
        "memcached",
        "rabbitmq",
        "sqs",
        "sns",
        "eventbridge",
        "lambda",
        "function",
        "serverless",
        "faas",
    }
)

_BACKEND_REPO_PREFIXES = (
    "grpc-",
    "workflow-",
    "express-api-",
    "backend-utils",
    "boilerplate-api-",
    "boilerplate-grpc-",
    "boilerplate-temporal-",
    "boilerplate-node-mali-",
    "boilerplate-node-providers-",
    "boilerplate-node-service",
    "boilerplate-go-grpc-",
)
_FRONTEND_DEMOTE_MULTIPLIER = float(os.getenv("CODE_RAG_FRONTEND_DEMOTE", "0.9"))
_BACKEND_BOOST_MULTIPLIER = float(os.getenv("CODE_RAG_BACKEND_BOOST", "1.05"))


_HIGHLIGHT_RE = re.compile(r">>>|<<<")
# FTS5 truncates the "[Repo: repo-name]" prefix via its ellipsis to leave a
# "...repo-name]" residue at the start of each snippet. strip_repo_tag() only
# handles the full "[Repo: ...]" tag, so we clean the residue separately in
# brief mode where every byte matters.
_REPO_RESIDUE_RE = re.compile(r"^\.\.\.[a-zA-Z0-9_-]+\]\s*")


def _apply_task_hint(
    repo_boost: dict[str, float] | None,
    repo_prefix_boost: dict[str, float] | None,
    task_hint: str | None,
) -> tuple[dict[str, float] | None, dict[str, float] | None]:
    """Apply caller-supplied task_hint override to repo boost maps.

    Returns the (possibly modified) (repo_boost, repo_prefix_boost) tuple.

    Behaviour matrix:
      - task_hint == "frontend" | "backoffice" → multiply every FRONTEND_REPO
        by _TASK_HINT_BOOST_DEFAULT (default 2.0x). Existing classifier values
        for FE repos (e.g. 0.9 demote) are OVERWRITTEN, not multiplied.
      - task_hint == "backend" → multiply every BACKEND_REPO and add prefix
        boosts for backend repo prefixes. Existing FE demote preserved.
      - Anything else (None, "", "unknown", etc.) → no-op.

    Opt-in via explicit caller arg only. No env-gate — the param itself is
    the gate. Callers passing an unrecognised value get a silent no-op.
    """
    if not task_hint or task_hint not in _VALID_TASK_HINTS:
        return repo_boost, repo_prefix_boost

    boost = _TASK_HINT_BOOST_DEFAULT
    rb = dict(repo_boost) if repo_boost else {}
    pb = dict(repo_prefix_boost) if repo_prefix_boost else {}

    if task_hint in {"frontend", "backoffice"}:
        for repo in _FRONTEND_REPOS:
            rb[repo] = boost  # overwrite demote/boost — explicit hint wins
    elif task_hint == "backend":
        for repo in _BACKEND_REPOS:
            if repo:
                rb[repo] = boost
        for prefix in _BACKEND_REPO_PREFIXES:
            pb[prefix] = boost
    return (rb if rb else None), (pb if pb else None)


def _detect_intent_adjustments(
    query: str,
) -> tuple[dict[str, float] | None, dict[str, float] | None, bool, bool]:
    """Detect query intent and return repo adjustment maps.

    Returns:
        (repo_boost, repo_prefix_boost, is_frontend_only, is_backend)

    - repo_boost: exact-repo multipliers (e.g. demote front-end repos)
    - repo_prefix_boost: prefix-based multipliers (e.g. boost grpc-/workflow- repos)
    - is_frontend_only: True when query is purely UI-focused
    - is_backend: True when query contains backend signals
    """
    lower = query.lower()
    has_frontend = False
    for kw in _FRONTEND_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", lower):
            has_frontend = True
            break
    has_backend = False
    for kw in _BACKEND_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", lower):
            has_backend = True
            break

    is_frontend_only = has_frontend and not has_backend
    is_mixed = has_frontend and has_backend

    repo_boost = None
    repo_prefix_boost = None

    if has_backend and not has_frontend:
        # Pure backend query: demote front-end repos, boost backend repos
        repo_boost = {repo: _FRONTEND_DEMOTE_MULTIPLIER for repo in _FRONTEND_REPOS}
        repo_boost.update({repo: _BACKEND_BOOST_MULTIPLIER for repo in _BACKEND_REPOS if repo})
        repo_prefix_boost = {prefix: _BACKEND_BOOST_MULTIPLIER for prefix in _BACKEND_REPO_PREFIXES}
    elif is_frontend_only:
        # Pure frontend query: boost front-end repos
        repo_boost = {repo: _FRONTEND_BOOST for repo in _FRONTEND_REPOS}
    elif is_mixed:
        # Mixed query: apply both frontend boost and backend boost
        # (previously backend boost was suppressed, causing backend misses)
        repo_boost = {repo: _FRONTEND_BOOST for repo in _FRONTEND_REPOS}
        repo_boost.update({repo: _BACKEND_BOOST_MULTIPLIER for repo in _BACKEND_REPOS if repo})
        repo_prefix_boost = {prefix: _BACKEND_BOOST_MULTIPLIER for prefix in _BACKEND_REPO_PREFIXES}
    elif _FE_DEFAULT_BOOST and not has_backend and not has_frontend:
        # Generic query, no FE/BE signal. 7/13 FE-bias zero-recall tasks
        # land here. Apply soft FE boost (default 1.2x) to lift token-poor
        # JSX components against token-dense backend siblings. Env-gated
        # because backend-only repos in this branch get neither boost nor
        # demote — non-zero risk we accidentally lift FE when the GT is in
        # graphql/grpc.
        repo_boost = {repo: _FE_DEFAULT_BOOST_MULT for repo in _FRONTEND_REPOS}

    return repo_boost, repo_prefix_boost, is_frontend_only, has_backend


# Regex patterns for entity extraction in long-query preprocessing.
_FILE_EXT_RE = re.compile(r"\.(ts|tsx|js|go|py|sql)\b", re.IGNORECASE)
_ERROR_CLASS_RE = re.compile(r"\b[A-Z][a-zA-Z]*(?:Error|Exception)\b")
_ALL_CAPS_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_REPO_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9_-]*[a-z0-9]\b")


# Build a static set of known repo names from conventions (no DB access at import time).
def _build_known_repos() -> set[str]:
    repos: set[str] = set()
    for repo in (GATEWAY_REPO, FEATURE_REPO, CREDENTIALS_REPO, *PROTO_REPOS):
        if repo:
            repos.add(repo)
    for wh_repo in WEBHOOK_REPOS.values():
        if wh_repo:
            repos.add(wh_repo)
    for entry in INFRA_REPOS:
        if isinstance(entry, dict):
            r = entry.get("repo", "")
            if r:
                repos.add(r)
    for repo, companions in CO_CHANGE_RULES.items():
        repos.add(repo)
        for c in companions:
            repos.add(c)
    for sf in SHARED_FILES:
        if isinstance(sf, dict):
            path = sf.get("path_pattern", "")
            if path:
                first_part = path.split("/")[0]
                if first_part:
                    repos.add(first_part)
    return repos


_KNOWN_REPOS: set[str] = _build_known_repos()


# Expand provider set with suffixes extracted from infra repos matching provider prefixes.
def _build_known_providers() -> set[str]:
    providers = set(_KNOWN_PROVIDERS)
    for prefix in PROVIDER_PREFIXES:
        for entry in INFRA_REPOS:
            if not isinstance(entry, dict):
                continue
            repo = entry.get("repo", "")
            if repo and repo.startswith(prefix):
                suffix = repo[len(prefix) :]
                if suffix:
                    providers.add(suffix)
    for sf in SHARED_FILES:
        if not isinstance(sf, dict):
            continue
        for item in sf.get("used_by", []):
            if (
                isinstance(item, str)
                and item.islower()
                and "_" not in item
                and item
                not in (
                    "allproviders",
                    "allapmproviders",
                    "allproviderssharingmessage",
                    "perproviderwebhookhandler",
                    "allapmproviderssalemethod",
                    "allapmprovidersrefundmethod",
                    "allapmproviderspayoutmethod",
                    "allapmprovidersresponsemapping",
                    "s2sapmproviders",
                )
            ):
                providers.add(item)
    return providers


_PROVIDER_NAMES: set[str] = _build_known_providers()


def preprocess_query(query: str) -> tuple[str, list[str]]:
    """Extract named entities from a query for boosted search.

    Uses simple regex/heuristic extraction (no ML model):
      - Provider names (case-insensitive, word-boundary matched)
      - File extensions (.ts, .tsx, .js, .go, .py, .sql)
      - Error/exception classes (CamelCase + Error/Exception)
      - Repo names (matched against static set derived from conventions.yaml)
      - ALL_CAPS identifiers (env vars, constants)

    Returns:
        (processed_query, extracted_entities)
        *processed_query* is the space-joined entities if any are found,
        otherwise the original *query* unchanged.
    """
    if not query or not query.strip():
        return query, []

    entities: list[str] = []
    seen: set[str] = set()

    # File extensions — keep the leading dot, lowercased.
    for m in _FILE_EXT_RE.finditer(query):
        ext = m.group(0).lower()
        if ext not in seen:
            seen.add(ext)
            entities.append(ext)

    # Error / exception classes.
    for m in _ERROR_CLASS_RE.finditer(query):
        err = m.group(0)
        if err not in seen:
            seen.add(err)
            entities.append(err)

    # ALL_CAPS identifiers.
    for m in _ALL_CAPS_RE.finditer(query):
        caps = m.group(0)
        if caps not in seen:
            seen.add(caps)
            entities.append(caps)

    # Provider names — case-insensitive, preserve original casing from query when
    # possible so the snippet display matches user intent.
    lower_query = query.lower()
    for provider in _PROVIDER_NAMES:
        if re.search(r"\b" + re.escape(provider) + r"\b", lower_query) and provider not in seen:
            seen.add(provider)
            entities.append(provider)

    # Repo names — match tokens against static known-repo set.
    for m in _REPO_TOKEN_RE.finditer(query):
        token = m.group(0)
        if token in _KNOWN_REPOS and token not in seen:
            seen.add(token)
            entities.append(token)

    if entities:
        return " ".join(entities), entities
    return query, []


@require_db
def search_tool(
    query: str = "",
    repo: str = "",
    file_type: str = "",
    exclude_file_types: str = "",
    limit: int = 10,
    brief: bool = False,
    cross_provider: bool = False,
    docs_index: bool | None = None,
    task_hint: str | None = None,
) -> str:
    """Search the knowledge base using keyword + semantic hybrid search.

    Args:
        query: Search query — keywords or natural language question
        repo: Optional - filter by repo name (exact or partial match)
        file_type: Optional - filter by type: proto, docs, config, env, k8s, grpc_method, library, workflow, ci, gotchas, reference, dictionary, flow_annotation, task, provider_doc, domain_registry
        exclude_file_types: Optional - comma-separated file types to exclude from results (e.g. "gotchas,task")
        limit: Max results to return (default 10, max 50). The output ends with
            a coverage hint when the limit is hit and the pool holds more —
            re-run with a higher limit for broad/multi-file tasks.
        brief: When True, drop the "Found N of M candidates for 'query'" header
            (re-echoes query), strip >>><<< highlight markers (sub-agents don't
            render), and drop [keyword+vector] source tags. Preserves repo/path/
            file_type/chunk_type/snippet. Default False preserves current output.
        cross_provider: When True and query matches {provider} {operation} pattern,
            also returns top-1 analogous chunk from up to 6 sibling providers —
            eliminates provider-swap reformulation chains. Default False preserves
            current output byte-for-byte.
        docs_index: Debug/eval override for two-tower routing.
            None (default) = auto-route by query intent. True = force docs tower.
            False = force code tower. Operators typically leave this unset.
        task_hint: Optional caller-supplied intent hint. When the query text
            does not carry FE/BE keywords (e.g. JIRA titles like "Audits
            export csv"), pass "frontend" / "backoffice" to boost frontend
            repos (backoffice-web, next-web-*, hosted-fields, ...) or
            "backend" to boost backend repos and prefixes (grpc-, workflow-,
            ...). Default 2.0x multiplier (env: CODE_RAG_TASK_HINT_BOOST).
            Overrides the query-derived classification — use only when you
            know the task domain. Mixed-domain tasks (FE+BE GT both required)
            will see the un-hinted side regress; in that case leave None.
    """
    # Defensive validation: callers sometimes omit `query` entirely (observed 74x
    # KeyError('query') in logs/tool_calls.jsonl before this guard was added).
    # Return a clear error rather than a Python traceback.
    if query is None or not isinstance(query, str) or not query.strip():
        return (
            "Error: 'query' parameter is required and must be a non-empty string. "
            'Example: search(query="payment provider integration")'
        )

    limit = min(max(1, limit), 50)

    # 2026-05-22: Auto-infer task_hint from JIRA-style prefix when the caller
    # didn't pass one explicitly. Strips the task ID from the query so it
    # doesn't pollute FTS tokens. Env-gated: CODE_RAG_AUTO_TASK_HINT=1.
    if task_hint is None:
        inferred, cleaned = _auto_task_hint_from_query(query)
        if inferred is not None:
            task_hint = inferred
            query = cleaned

    expanded = expand_query(query) if _USE_EXPAND_QUERY else query
    if os.getenv("CODE_RAG_USE_DICTIONARY_EXPAND", "0") == "1":
        expanded = expand_query_dictionary(expanded)

    processed_query, entities = preprocess_query(query)
    use_entity_boost = len(query.split()) >= 6 and bool(entities)
    if _QUERY_V2 and len(entities) < 3:
        use_entity_boost = False  # FIX-G: don't collapse the query to <3 entities
    search_query = processed_query if use_entity_boost else expanded

    ck = cache_key(
        "search",
        query=search_query,
        repo=repo,
        file_type=file_type,
        exclude_file_types=exclude_file_types,
        limit=limit,
        brief=brief,
        cross_provider=cross_provider,
        docs_index=docs_index,
        task_hint=task_hint,
    )

    # 2026-05-17: Env-gated default exclude for noisy file types that
    # dominate FTS5 keyword search and degrade code-search quality.
    # package_usage — package-map files with keyword-stuffed metadata.
    # provider_doc  — provider documentation with generic payment terms.
    # dictionary    — glossary files that match almost any query.
    # Eval impact (RunPod jira n=665): +6.47pp hit@10 when excluded.
    default_exclude = os.environ.get("CODE_RAG_DEFAULT_EXCLUDE", "")
    if default_exclude and exclude_file_types:
        exclude_file_types = exclude_file_types + "," + default_exclude
    elif default_exclude:
        exclude_file_types = default_exclude

    repo_boost, repo_prefix_boost, is_frontend_only, _ = _detect_intent_adjustments(query)

    # 2026-05-22: Caller-supplied task_hint overrides keyword-derived
    # classification. Local A/B (bench_runs/improve/agent_task_prefix/):
    # 3/5 H5 BO-* tasks lift into top-10 at 2.0x; 3/3 BO controls unchanged.
    # See _apply_task_hint() docstring for the contract.
    repo_boost, repo_prefix_boost = _apply_task_hint(repo_boost, repo_prefix_boost, task_hint)

    # 2026-05-23: HARD repo filter when query mentions a known provider.
    # Closes the PI-56 failure: "nuvei expired payment handling" gets confused
    # with workflow-dispute-expiration because both contain "expired" + "nuvei".
    # When the query mentions a provider name, restrict pool to provider-touching
    # repos (grpc-apm-{provider}, grpc-providers-{provider}, grpc-connections-{provider},
    # express-webhooks-*, workflow-provider-webhooks, express-api-callbacks).
    # Env-gated default OFF.
    repo_allow_list: set[str] | None = None
    repo_allow_prefixes: tuple[str, ...] = ()
    if os.getenv("CODE_RAG_HARD_FILTER", "0") == "1":
        q_lower = query.lower()
        mentioned_providers = [p for p in _PROVIDER_NAMES if re.search(r"\b" + re.escape(p) + r"\b", q_lower)]
        if mentioned_providers:
            # Build allow set: provider-named repos + workflow/webhook-routing repos
            allow = set()
            for prov in mentioned_providers:
                allow.add(f"grpc-apm-{prov}")
                allow.add(f"grpc-providers-{prov}")
                allow.add(f"grpc-connections-{prov}")
                allow.add(f"express-webhooks-{prov}")
                allow.add(f"workflow-{prov}-webhook")
            # Generic provider routing repos (any provider work touches these)
            allow.update(
                {
                    "workflow-provider-webhooks",
                    "express-webhooks-dispatch",
                    "express-api-callbacks",
                    "grpc-payment-gateway",
                    "grpc-providers-features",
                    "grpc-providers-credentials",
                }
            )
            repo_allow_list = allow
            # Allow these prefixes wholesale (capture sibling provider repos missed above)
            repo_allow_prefixes = tuple(f"grpc-apm-{p}" for p in mentioned_providers)

    def _compute() -> str:
        ranked, vec_err, total_candidates = hybrid_search(
            search_query,
            repo,
            file_type,
            exclude_file_types,
            limit,
            cross_provider=cross_provider,
            docs_index=docs_index,
            entity_boost=1.3 if use_entity_boost else 1.0,
            repo_boost=repo_boost,
            repo_prefix_boost=repo_prefix_boost,
            repo_allow_list=repo_allow_list,
            repo_allow_prefixes=repo_allow_prefixes,
        )

        # Fallback to original query if entity-boosted search returns too few results.
        actual_query = search_query
        if use_entity_boost and len(ranked) < 5:
            ranked, vec_err, total_candidates = hybrid_search(
                expanded,
                repo,
                file_type,
                exclude_file_types,
                limit,
                cross_provider=cross_provider,
                docs_index=docs_index,
                repo_boost=repo_boost,
                repo_prefix_boost=repo_prefix_boost,
                repo_allow_list=repo_allow_list,
                repo_allow_prefixes=repo_allow_prefixes,
            )
            actual_query = expanded

        log_search(
            "search", actual_query, {"repo": repo, "file_type": file_type, "limit": limit}, ranked, total_candidates
        )

        if not ranked:
            context = ""
            if repo:
                context += f"Filter: repo='{repo}'. "
            if file_type:
                context += f"Filter: type='{file_type}'. "
            return format_no_results(query, context.strip())

        results: list[str] = []
        # In brief mode, use a shorter snippet budget — the markers/residue
        # cleanup gives us denser signal per byte, and sub-agents rarely need
        # 300 chars of code context per result to triage relevance.
        snippet_budget = 200 if brief else 300
        for r in ranked:
            snippet = strip_repo_tag(r["snippet"])
            if brief:
                # Strip >>>term<<< highlight markers (sub-agents don't render them)
                # and the "...repo-name]" residue that FTS5 leaves when it
                # truncates the "[Repo: ...]" prefix. Both are pure noise.
                snippet = _HIGHLIGHT_RE.sub("", snippet)
                snippet = _REPO_RESIDUE_RE.sub("", snippet)
                results.append(
                    f"**{r['repo_name']}** | `{r['file_path']}` ({r['file_type']}/{r['chunk_type']})\n"
                    f"  {snippet[:snippet_budget]}\n"
                )
            else:
                sources = "+".join(r["sources"])
                results.append(
                    f"**{r['repo_name']}** | `{r['file_path']}` ({r['file_type']}/{r['chunk_type']}) [{sources}]\n"
                    f"  {snippet[:300]}\n"
                )

        # 2026-05-23: scope warning when top-N spans many distinct repos for a
        # SHORT query (low specificity). Helps the caller spot the PI-56 failure
        # mode: top-3 are all from different repos (no clear winner cluster),
        # the underlying query is vague, and the engineer would benefit from a
        # narrowing hint. Verified 2026-05-23: fires correctly on "settlement"
        # (1 word, 9 repos) and "Nuvei expired payment handling" (4 words, 3
        # distinct top-3 repos), NOT on "paypal webhook signature" (focused,
        # all express-webhooks-paypal). Env-gated default OFF.
        scope_warning = ""
        if os.getenv("CODE_RAG_SCOPE_WARNING", "0") == "1" and len(ranked) >= 5:
            q_words = len(query.split())
            top10_repos = [r.get("repo_name", "") for r in ranked[:10] if r.get("repo_name")]
            top3_distinct = len(set(top10_repos[:3]))
            top10_distinct = len(set(top10_repos))
            # Two trigger conditions:
            #  (a) very short query (≤4 words) AND top-3 all from different repos → vague intent
            #  (b) short-ish query (≤6 words) AND top-10 spans ≥5 distinct repos → genuinely cross-cutting
            fires = (q_words <= 4 and top3_distinct >= 3) or (q_words <= 6 and top10_distinct >= 5)
            if fires:
                seen_order = []
                for r in ranked[:10]:
                    rn = r.get("repo_name", "")
                    if rn and rn not in seen_order:
                        seen_order.append(rn)
                scope_warning = (
                    f"\n⚠ Pool spans {top10_distinct} repos in top-10: {', '.join(seen_order[:5])}"
                    f"{', ...' if len(seen_order) > 5 else ''}. "
                    f"To narrow: add `repo:<name>` filter or include a provider/repo name in the query.\n"
                )

        # Coverage hint — the result list hit the requested limit and the pool
        # holds more; tell the caller so it can widen instead of silently
        # losing recall on broad/multi-file tasks.
        coverage = ""
        if _COVERAGE_HINT and len(ranked) == limit and total_candidates > limit:
            coverage = (
                f"\n↳ limit of {limit} reached — {total_candidates} candidates in the pool. "
                f"If this task spans many files, re-run with a higher `limit` (max 50)."
            )

        if brief:
            # Drop "Found N of M candidates for 'query'" re-echo.
            # Keep the vector-search-unavailable warning when present — it's
            # a quality signal the caller needs, not bloat.
            prefix = ""
            if is_frontend_only:
                prefix = "⚠️ Frontend query detected — search is optimized for backend code, UI results may be incomplete.\n\n"
            if vec_err:
                return (
                    prefix
                    + f"⚠️ Vector search unavailable: {vec_err} (keyword only)\n\n"
                    + "\n".join(results)
                    + coverage
                )
            return prefix + scope_warning + "\n".join(results) + coverage

        header = f"Found {len(ranked)} of {total_candidates} candidates for '{query}'"
        if repo:
            header += f" in repos matching '{repo}'"
        if file_type:
            header += f" (type: {file_type})"
        if vec_err:
            header += " (keyword only)"
            header += f"\n⚠️ Vector search unavailable: {vec_err}"

        return header + scope_warning + coverage + "\n\n" + "\n".join(results)

    return cache_or_compute(ck, _compute)
