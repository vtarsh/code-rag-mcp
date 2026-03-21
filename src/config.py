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
SERVER_NAME: str = CONFIG.get("server_name", "code-knowledge")
DISPLAY_NAME: str = CONFIG.get("display_name", f"{ORG} Knowledge Base")
GRPC_DOMAIN_SUFFIX: str = CONFIG.get("grpc_domain_suffix", "")

# --- Embedding model ---
EMBEDDING_MODEL_KEY: str = CONFIG.get("embedding_model", os.getenv("CODE_RAG_MODEL", "coderank"))

# --- DB paths (derived from model config) ---
from src.models import get_model_config  # noqa: E402

_model_cfg = get_model_config(EMBEDDING_MODEL_KEY)
DB_PATH = BASE_DIR / "db" / "knowledge.db"
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
    "redirect",
    "url_reference",
    "grpc_method_call",
    "merchant_has",
}

# Pre-defined business flow entry points for trace_chain.
KNOWN_FLOWS: dict[str, list[str]] = _load_yaml("known_flows.yaml") or {}
