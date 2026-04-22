"""Configuration — paths, org settings, domain glossary.

Single source of truth for all config values.
Loads from active profile under profiles/{ACTIVE_PROFILE}/.

Profile resolution order:
1. ACTIVE_PROFILE env var
2. .active_profile file in BASE_DIR
3. Legacy: config.json at root (backward compat)
4. Fallback: "example"
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import yaml

# --- Paths ---
BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
SCRIPTS_DIR = BASE_DIR / "scripts"


# --- Profile resolution ---
def _resolve_profile() -> str:
    """Determine active profile name."""
    # 1. Env var
    if env_profile := os.getenv("ACTIVE_PROFILE"):
        return env_profile

    # 2. .active_profile file
    marker = BASE_DIR / ".active_profile"
    if marker.exists():
        name = marker.read_text().strip()
        if name and (BASE_DIR / "profiles" / name).is_dir():
            return name

    # 3. Legacy: root config.json exists (use "legacy" as marker)
    if (BASE_DIR / "config.json").exists() and not (BASE_DIR / "profiles").is_dir():
        return "__legacy__"

    return "example"


ACTIVE_PROFILE: str = _resolve_profile()

if ACTIVE_PROFILE == "__legacy__":
    # Backward compat: load from root config.json
    PROFILE_DIR = BASE_DIR
    _config_path = BASE_DIR / "config.json"
else:
    PROFILE_DIR = BASE_DIR / "profiles" / ACTIVE_PROFILE
    _config_path = PROFILE_DIR / "config.json"

CONFIG: dict = json.loads(_config_path.read_text()) if _config_path.exists() else {}

# --- Org config ---
ORG: str = CONFIG.get("org", "my-org")
NPM_SCOPE: str = CONFIG.get("npm_scope", f"@{ORG}")
DISPLAY_NAME: str = CONFIG.get("display_name", f"{ORG} Knowledge Base")
GRPC_DOMAIN_SUFFIX: str = CONFIG.get("grpc_domain_suffix", "")

# --- Embedding model ---
EMBEDDING_MODEL_KEY: str = CONFIG.get("embedding_model", os.getenv("CODE_RAG_MODEL", "coderank"))

# --- Reranker model (CrossEncoder; short names auto-prefix "cross-encoder/") ---
RERANKER_MODEL: str = CONFIG.get(
    "reranker_model", os.getenv("CODE_RAG_RERANKER", "cross-encoder/ms-marco-MiniLM-L-6-v2")
)

# --- DB paths (derived from model config) ---
from src.models import get_model_config  # noqa: E402

_model_cfg = get_model_config(EMBEDDING_MODEL_KEY)
DB_PATH = BASE_DIR / "db" / "knowledge.db"
DB_TASKS_PATH = BASE_DIR / "db" / "tasks.db"
LANCE_PATH = BASE_DIR / "db" / _model_cfg.lance_dir


# --- YAML loaders ---
def _load_yaml(filename: str) -> dict | list | None:
    """Load a YAML file from the active profile directory."""
    path = PROFILE_DIR / filename
    if path.exists():
        return yaml.safe_load(path.read_text())
    return None


# --- Domain glossary for query expansion ---
DOMAIN_GLOSSARY: dict[str, str] = _load_yaml("glossary.yaml") or {}

# --- Phrase-aware glossary ---
_raw_phrases = _load_yaml("phrase_glossary.yaml") or []
PHRASE_GLOSSARY: list[tuple[frozenset[str], str]] = [
    (frozenset(entry["tokens"]), entry["expansion"]) for entry in _raw_phrases if isinstance(entry, dict)
]

# --- Conventions (org-specific repo naming & infrastructure) ---
_conventions: dict = _load_yaml("conventions.yaml") or {}

# Validate conventions — warn on missing keys so new profiles get early feedback
if ACTIVE_PROFILE not in ("example", "__legacy__") and _conventions:
    _EXPECTED_KEYS = {"provider_prefixes", "gateway_repo", "webhook_repos", "provider_type_map"}
    _missing = _EXPECTED_KEYS - set(_conventions)
    if _missing:
        logging.warning(f"Profile '{ACTIVE_PROFILE}' conventions.yaml missing keys: {', '.join(sorted(_missing))}")

# Provider repo prefixes — repos matching {prefix}{provider_name}
PROVIDER_PREFIXES: list[str] = _conventions.get("provider_prefixes", [])

# Provider type → repo template (e.g. {"apm": "grpc-apm-{provider}"})
PROVIDER_TYPE_MAP: dict[str, str] = _conventions.get("provider_type_map", {})

# Standard provider service methods
PROVIDER_METHODS: set[str] = set(_conventions.get("provider_methods", []))

# Proto/type definition repos (priority for context_builder)
PROTO_REPOS: list[str] = _conventions.get("proto_repos", [])

# Payment gateway repo
GATEWAY_REPO: str = _conventions.get("gateway_repo", "")

# Webhook infrastructure repos
WEBHOOK_REPOS: dict[str, str] = _conventions.get("webhook_repos", {})

# Feature flags repo
FEATURE_REPO: str = _conventions.get("feature_repo", "")

# Credential management repo
CREDENTIALS_REPO: str = _conventions.get("credentials_repo", "")

# Impact hint patterns for PR checklists
IMPACT_HINTS: list[dict] = _conventions.get("impact_hints", [])

# Infrastructure repos for provider integrations
INFRA_REPOS: list[dict] = _conventions.get("infra_repos", [])

# Infra repo suffixes to exclude from provider detection
INFRA_SUFFIXES: set[str] = set(_conventions.get("infra_suffixes", []))

# Repos that trigger provider fan-out (proto/types/common libs)
PROTO_TRIGGER_REPOS: set[str] = set(_conventions.get("proto_trigger_repos", []))

# Prefixes to strip from repo names for display
REPO_NAME_PREFIXES: list[str] = _conventions.get("repo_name_prefixes", [])

# Bulk migration detection
# Hub penalty config — repos that should not be cascaded through
_hub_penalty: dict = _conventions.get("hub_penalty", {})
HUB_NEVER_CASCADE: set[str] = set(_hub_penalty.get("never_cascade", []))
HUB_SHALLOW_CASCADE: set[str] = set(_hub_penalty.get("shallow_cascade", []))
HUB_DOWNSTREAM_MIN_DEPENDENTS: int = _hub_penalty.get("downstream_min_dependents", 15)

BULK_KEYWORDS: list[str] = _conventions.get("bulk_keywords", [])
SERVICE_REPO_PATTERNS: list[str] = _conventions.get("service_repo_patterns", [])

# High-confidence co-change rules (trigger_repo → [companion_repos])
CO_CHANGE_RULES: dict[str, list[str]] = _conventions.get("co_change_rules", {})

# Shared files — files used by multiple providers/consumers.
# Each entry: {path_pattern, used_by, change_risk, check, convention?}
# Consumed by section_shared_files_warning in analyze_task.
SHARED_FILES: list[dict] = _conventions.get("shared_files", [])

# Domain templates — base repos per domain (auto-suggest)
DOMAIN_TEMPLATES: dict[str, dict] = _conventions.get("domain_templates", {})

# Domain classification patterns for non-PI tasks
DOMAIN_PATTERNS: dict[str, dict] = _conventions.get("domain_patterns", {})

# Async-chain anchor (P4.3) — domain-specific recall boost for async/webhook/payout flows.
# Generic default: empty. Profile-specific (pay-com) declares triggers + repos in
# conventions.yaml → async_chain: { triggers: [...], repos: [...] }. When any
# trigger keyword appears in task description, the listed repos are injected into
# the candidate set with medium confidence so they land in the top-25 tier.
_async_chain: dict = _conventions.get("async_chain", {}) or {}
ASYNC_CHAIN_TRIGGERS: list[str] = list(_async_chain.get("triggers", []))
ASYNC_CHAIN_REPOS: list[str] = list(_async_chain.get("repos", []))

# --- Tuning constants (overridable via conventions.yaml → tuning) ---
_tuning: dict = _conventions.get("tuning", {})

# RRF fusion
RRF_K: int = int(_tuning.get("rrf_k", 60))
KEYWORD_WEIGHT: float = float(_tuning.get("keyword_weight", 2.0))
GOTCHAS_BOOST: float = float(_tuning.get("gotchas_boost", 1.5))
REFERENCE_BOOST: float = float(_tuning.get("reference_boost", 1.3))
DICTIONARY_BOOST: float = float(_tuning.get("dictionary_boost", 1.4))

# P0c: boost chunks whose (repo, file) match a code_facts_fts hit (validation
# guards, joi/zod schemas, env lookups, temporal retries, grpc statuses). These
# are high-signal structural facts that chunks_fts can miss when content
# contains large generic blocks. Default 1.15 = +15% on RRF score.
CODE_FACT_BOOST: float = float(_tuning.get("code_fact_boost", 1.15))

# P0c: weight applied to injected chunks that the chunks_fts pool missed but
# code_facts_fts matched. Weight in (0, 1] acts as position penalty on RRF
# score (fresh candidate gets CODE_FACT_INJECT_WEIGHT / (RRF_K + 1)).
CODE_FACT_INJECT_WEIGHT: float = float(_tuning.get("code_fact_inject_weight", 0.5))

# P0c: boost chunks whose repo appears in env_vars rows matched by an UPPERCASE
# identifier in the query. Lighter than code_fact_boost (repo-level signal
# instead of file-level). Default 1.05 = +5%.
ENV_VAR_BOOST: float = float(_tuning.get("env_var_boost", 1.05))

# --- Rerank pool size (P4.2) — feed more candidates to cross-encoder ---
# Controls pre-rerank cap. Benchmark (rerank_ab_top200.json) showed 50→200
# gives +10 pp r@10 for baseline MiniLM-L-6. Env override: CODE_RAG_RERANK_POOL_SIZE.
RERANK_POOL_SIZE: int = int(os.getenv("CODE_RAG_RERANK_POOL_SIZE", _tuning.get("rerank_pool_size", 200)))

# --- Rerank penalties (P4.1) — down-weight doc/test chunks for code-related queries ---
# Applied to normalized combined_score AFTER cross-encoder reranking,
# skipped when query itself requests docs/tests/guides.
# Env overrides: CODE_RAG_DOC_PENALTY, CODE_RAG_TEST_PENALTY, CODE_RAG_GUIDE_PENALTY.
DOC_PENALTY: float = float(os.getenv("CODE_RAG_DOC_PENALTY", _tuning.get("doc_penalty", 0.15)))
TEST_PENALTY: float = float(os.getenv("CODE_RAG_TEST_PENALTY", _tuning.get("test_penalty", 0.20)))
GUIDE_PENALTY: float = float(os.getenv("CODE_RAG_GUIDE_PENALTY", _tuning.get("guide_penalty", 0.25)))
# P1c 2026-04-22: CI yml (`ci/deploy.ya?ml`, `k8s/.github/workflows/*`) needs a
# stronger penalty than a generic doc — on short repo queries the v8 reranker
# surfaces 5+ identical CI files that out-rank real code; 0.15 was not enough
# to flip the order (validated on pair #2 "ach provider service integration").
CI_PENALTY: float = float(os.getenv("CODE_RAG_CI_PENALTY", _tuning.get("ci_penalty", 0.50)))

# Query cache
CACHE_TTL: int = int(_tuning.get("cache_ttl", 300))
CACHE_MAX: int = int(_tuning.get("cache_max", 64))

# GitHub API helpers
MAX_GITHUB_REPOS: int = int(_tuning.get("max_github_repos", 20))
MAX_WORKERS: int = int(_tuning.get("max_workers", 8))
BATCH_TIMEOUT: int = int(_tuning.get("batch_timeout", 30))
GH_CACHE_TTL: int = int(_tuning.get("gh_cache_ttl", 600))
GH_CACHE_MAX: int = int(_tuning.get("gh_cache_max", 256))

# SQLite pragmas
MMAP_SIZE: int = int(_tuning.get("mmap_size", 268435456))
CACHE_SIZE: int = int(_tuning.get("cache_size", -32000))

# Co-occurrence boost tuning (extracted from core_analyzer.py hardcoded values)
COOCCUR_MIN_COUNT: int = int(_tuning.get("cooccur_min_count", 3))
COOCCUR_FORWARD_PROB: float = float(_tuning.get("cooccur_forward_prob", 0.4))
COOCCUR_REVERSE_PROB: float = float(_tuning.get("cooccur_reverse_prob", 0.80))
COOCCUR_REVERSE_MIN_COUNT: int = int(_tuning.get("cooccur_reverse_min_count", 4))

# Universal repo detection threshold (fraction of tasks)
UNIVERSAL_PCT: float = float(_tuning.get("universal_pct", 0.25))

# --- Graph constants ---
# Meaningful edge types for flow tracing (ordered by signal strength).
# These are generic — not org-specific.
FLOW_EDGE_TYPES: set[str] = {
    "grpc_call",
    "grpc_client_usage",
    "child_workflow",
    "webhook_dispatch",
    "webhook_handler",
    "callback_handler",
    "workflow_import",
    "temporal_signal",
    "domain_reference",
    "flow_step",
    "flow_redirect",
    "url_reference",
    "grpc_method_call",
    "merchant_has",
    "runtime_routing",
    "express_route",
    "temporal_activate",
    "signal_handler",
}

# Pre-defined business flow entry points for trace_chain.
KNOWN_FLOWS: dict[str, list[str]] = _load_yaml("known_flows.yaml") or {}


# --- Structured recipes (evidence-based implementation patterns) ---
def _load_recipes() -> dict[str, dict]:
    """Load recipes from per-recipe YAMLs in recipes/ dir, with fallback to legacy recipes.yaml.

    Preferred layout: profiles/{profile}/recipes/{name}.yaml — one recipe per file.
    Each file may contain either a single recipe (top-level = recipe name) or
    {recipes: {name: ...}} format.
    """
    merged: dict[str, dict] = {}
    recipes_dir = PROFILE_DIR / "recipes"
    if recipes_dir.is_dir():
        for yaml_file in sorted(recipes_dir.glob("*.yaml")):
            if yaml_file.name.startswith("_"):
                continue  # reserved for index/meta files
            try:
                data = yaml.safe_load(yaml_file.read_text())
                if not isinstance(data, dict):
                    continue
                # Accept either {recipes: {...}} or {recipe_name: {...}}
                if "recipes" in data and isinstance(data["recipes"], dict):
                    merged.update(data["recipes"])
                else:
                    merged.update(data)
            except Exception:
                continue
    # Legacy fallback: single recipes.yaml
    if not merged:
        legacy = _load_yaml("recipes.yaml") or {}
        merged = legacy.get("recipes", {})
    return merged


RECIPES: dict[str, dict] = _load_recipes()
