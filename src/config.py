"""Configuration loader for code-rag-mcp.

Reads profile-specific data from PROFILE_DIR (glossary, conventions, tuning)
and exposes typed constants for the rest of the codebase.

Profile resolution (in order):
  1. ACTIVE_PROFILE env var
  2. .active_profile file in CODE_RAG_HOME
  3. Fallback to 'example' profile
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# --- Base paths ---
CODE_RAG_HOME: Path = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH: Path = CODE_RAG_HOME / "db" / "knowledge.db"
TASKS_DB_PATH: Path = CODE_RAG_HOME / "db" / "tasks.db"


def _resolve_profile_name() -> str:
    """Resolve the active profile name from env, marker file, or fallback."""
    if env := os.getenv("ACTIVE_PROFILE"):
        return env.strip()
    marker = CODE_RAG_HOME / ".active_profile"
    if marker.exists():
        name = marker.read_text().strip()
        if name:
            return name
    return "example"


ACTIVE_PROFILE: str = _resolve_profile_name()
PROFILE_DIR: Path = CODE_RAG_HOME / "profiles" / ACTIVE_PROFILE


# --- Embedding / reranker model config (from profile) ---
def _load_profile_config() -> dict:
    cfg_path = PROFILE_DIR / "config.json"
    if cfg_path.exists():
        import json

        try:
            return json.loads(cfg_path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


_profile_config: dict = _load_profile_config()

EMBEDDING_PROVIDER: str = _profile_config.get("embedding_provider", "local")
EMBEDDING_MODEL: str = _profile_config.get("embedding_model", "coderank")
RERANKER_MODEL: str = _profile_config.get("reranker_model", "ms-marco-MiniLM-L-6-v2")

# Vector DB path may depend on embedding model (one LanceDB per model family)
try:
    from src.models import get_model_config as _get_model_config

    _mcfg = _get_model_config(EMBEDDING_MODEL)
    VECTOR_DB_PATH: Path = CODE_RAG_HOME / "db" / _mcfg.lance_dir
    EMBEDDING_DIM: int = _mcfg.dim
except Exception:
    # Fallback during early bootstrap (before models module is importable)
    VECTOR_DB_PATH = CODE_RAG_HOME / "db" / "vectors.lance.coderank"
    EMBEDDING_DIM = 768


# --- YAML profile data ---
def _load_yaml(filename: str) -> Any:
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
_expected_top_keys = {
    "infrastructure",
    "domain_patterns",
    "name_patterns",
    "co_occurrence",
    "hub_repos",
    "domain_templates",
    "tuning",
}
_missing = _expected_top_keys - _conventions.keys()
if _conventions and _missing:
    import sys

    print(
        f"[config] Warning: conventions.yaml in profile '{ACTIVE_PROFILE}' is missing top-level keys: {sorted(_missing)}.",
        file=sys.stderr,
    )

# Infrastructure — hard-coded repo classifications for an org
_infrastructure: dict = _conventions.get("infrastructure", {})
GATEWAY_REPOS: set[str] = set(_infrastructure.get("gateways", []))
WORKFLOW_REPOS: set[str] = set(_infrastructure.get("workflows", []))
VAULT_REPOS: set[str] = set(_infrastructure.get("vaults", []))
CALLBACK_REPOS: set[str] = set(_infrastructure.get("callbacks", []))
WEBHOOK_REPOS: set[str] = set(_infrastructure.get("webhooks", []))
PROVIDER_FEATURES_REPO: str = _infrastructure.get("provider_features", "")
PAYMENT_METHODS_FILE: str = _infrastructure.get("payment_methods_file", "")

# Domain patterns — repo name prefixes per domain (PI, CORE, BO, HS, …)
DOMAIN_PATTERNS: dict[str, list[str]] = _conventions.get("domain_patterns", {})
DOMAIN_PREFIXES: list[str] = sorted(DOMAIN_PATTERNS.keys())

# Name patterns — e.g. how APM provider repos look (grpc-apm-*)
NAME_PATTERNS: dict[str, str] = _conventions.get("name_patterns", {})

# Co-occurrence thresholds
_co_occ: dict = _conventions.get("co_occurrence", {})
CO_OCCURRENCE_MIN_TASKS: int = int(_co_occ.get("min_tasks", 3))
CO_OCCURRENCE_MIN_RATIO: float = float(_co_occ.get("min_ratio", 0.4))
BIDIRECTIONAL_MIN_PROB: float = float(_co_occ.get("bidirectional_min_prob", 0.8))
BIDIRECTIONAL_MIN_COUNT: int = int(_co_occ.get("bidirectional_min_count", 4))

# Hub repos — repos appearing in too many tasks get penalized
HUB_REPOS: set[str] = set(_conventions.get("hub_repos", []))
HUB_PENALTY: float = float(_conventions.get("hub_penalty", 0.5))

# Domain templates — pre-built repo sets for each task domain
DOMAIN_TEMPLATES: dict[str, dict] = _conventions.get("domain_templates", {})

# Universal repos — appear in >25% of same-prefix tasks for a given domain
_universal_cfg: dict = _conventions.get("universal_repos", {})
UNIVERSAL_MIN_RATIO: float = float(_universal_cfg.get("min_ratio", 0.25))

# Provider fan-out — how many providers to surface when task matches "all providers" pattern
_provider_fanout: dict = _conventions.get("provider_fanout", {})
BULK_PROVIDER_MAX: int = int(_provider_fanout.get("bulk_max", 12))

# Trace/impact depth limits
_trace_cfg: dict = _conventions.get("trace", {})
TRACE_IMPACT_DEFAULT_DEPTH: int = int(_trace_cfg.get("impact_default_depth", 2))
TRACE_CHAIN_DEFAULT_DEPTH: int = int(_trace_cfg.get("chain_default_depth", 4))

# Tuning — search + rerank weights
_tuning: dict = _conventions.get("tuning", {})

# RRF fusion
RRF_K: int = int(_tuning.get("rrf_k", 60))
KEYWORD_WEIGHT: float = float(_tuning.get("keyword_weight", 2.0))
GOTCHAS_BOOST: float = float(_tuning.get("gotchas_boost", 1.5))
REFERENCE_BOOST: float = float(_tuning.get("reference_boost", 1.3))
DICTIONARY_BOOST: float = float(_tuning.get("dictionary_boost", 1.4))

# --- Rerank penalties (P4.1) — down-weight doc/test chunks for code-related queries ---
# Applied to normalized combined_score AFTER cross-encoder reranking,
# skipped when query itself requests docs/tests/guides.
# Env overrides: CODE_RAG_DOC_PENALTY, CODE_RAG_TEST_PENALTY, CODE_RAG_GUIDE_PENALTY.
DOC_PENALTY: float = float(
    os.getenv("CODE_RAG_DOC_PENALTY", _tuning.get("doc_penalty", 0.15))
)
TEST_PENALTY: float = float(
    os.getenv("CODE_RAG_TEST_PENALTY", _tuning.get("test_penalty", 0.20))
)
GUIDE_PENALTY: float = float(
    os.getenv("CODE_RAG_GUIDE_PENALTY", _tuning.get("guide_penalty", 0.25))
)

# Query cache
CACHE_TTL: int = int(_tuning.get("cache_ttl", 300))
CACHE_MAX: int = int(_tuning.get("cache_max", 64))

# GitHub API helpers
MAX_GITHUB_REPOS: int = int(_tuning.get("max_github_repos", 20))
MAX_WORKERS: int = int(_tuning.get("max_workers", 8))

# --- Provider URL config (optional — per profile conventions if any) ---
PROVIDER_URLS: dict[str, list[str]] = _conventions.get("provider_urls", {})

# --- Function prefixes used by function_search mechanism (generic; refine per profile) ---
_fn_prefixes: dict = _conventions.get("function_prefixes", {})
FUNCTION_PREFIXES: dict[str, list[str]] = {k: list(v) for k, v in _fn_prefixes.items() if isinstance(v, list)}
