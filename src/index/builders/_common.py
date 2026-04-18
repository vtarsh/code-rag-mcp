"""Shared paths, profile resolution, conventions loading, and constants.

Module-level config is loaded ONCE here and imported by all other builder
modules so behavior matches the original monolithic ``scripts/build_index.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
_PROFILE = os.getenv("ACTIVE_PROFILE", "")
if not _PROFILE:
    _ap = _BASE_DIR / ".active_profile"
    _PROFILE = _ap.read_text().strip() if _ap.exists() else "example"
_PROFILE_DIR = _BASE_DIR / "profiles" / _PROFILE

EXTRACTED_DIR = _BASE_DIR / "extracted"
RAW_DIR = _BASE_DIR / "raw"

# Load conventions
_conv_path = _PROFILE_DIR / "conventions.yaml"
_conv = yaml.safe_load(_conv_path.read_text()) if _conv_path.exists() else {}
FEATURE_REPO: str = _conv.get("feature_repo", "grpc-providers-features")
# Gotchas/flows/domain_registry from profile (fall back to legacy docs/)
GOTCHAS_DIR = (
    _PROFILE_DIR / "docs" / "gotchas"
    if (_PROFILE_DIR / "docs" / "gotchas").is_dir()
    else _BASE_DIR / "docs" / "gotchas"
)
FLOWS_DIR = (
    _PROFILE_DIR / "docs" / "flows" if (_PROFILE_DIR / "docs" / "flows").is_dir() else _BASE_DIR / "docs" / "flows"
)
TASKS_DIR = (
    _PROFILE_DIR / "docs" / "tasks" if (_PROFILE_DIR / "docs" / "tasks").is_dir() else _BASE_DIR / "docs" / "tasks"
)
REFERENCES_DIR = (
    _PROFILE_DIR / "docs" / "references"
    if (_PROFILE_DIR / "docs" / "references").is_dir()
    else _BASE_DIR / "docs" / "references"
)
DICTIONARY_DIR = (
    _PROFILE_DIR / "docs" / "dictionary"
    if (_PROFILE_DIR / "docs" / "dictionary").is_dir()
    else _BASE_DIR / "docs" / "dictionary"
)
PROVIDERS_DIR = _PROFILE_DIR / "docs" / "providers"
_profile_domain_reg = _PROFILE_DIR / "docs" / "domain_registry.yaml"
DOMAIN_REGISTRY_FILE = (
    _profile_domain_reg if _profile_domain_reg.exists() else _BASE_DIR / "docs" / "domain_registry.yaml"
)
DB_DIR = _BASE_DIR / "db"
DB_PATH = DB_DIR / "knowledge.db"
INDEX_FILE = EXTRACTED_DIR / "_index.json"

# Max chunk size in characters
MAX_CHUNK = 4000
MIN_CHUNK = 50
