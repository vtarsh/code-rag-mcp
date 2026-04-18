"""Shared state and helpers for graph builders.

Loads conventions ONCE so every builder module can import the same constants.
"""

import json
import os
import re
from pathlib import Path

BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = BASE_DIR / "db" / "knowledge.db"
RAW_PATH = BASE_DIR / "raw"

_profile = os.getenv("ACTIVE_PROFILE", "")
if not _profile:
    _ap = BASE_DIR / ".active_profile"
    _profile = _ap.read_text().strip() if _ap.exists() else ""
_profile_config = BASE_DIR / "profiles" / _profile / "config.json" if _profile else None
_legacy_config = BASE_DIR / "config.json"
_config_path = _profile_config if (_profile_config and _profile_config.exists()) else _legacy_config
_config = json.loads(_config_path.read_text()) if _config_path.exists() else {}
_org = _config.get("org", "my-org")
NPM_SCOPE = _config.get("npm_scope", f"@{_org}")
GRPC_DOMAIN_SUFFIX = _config.get("grpc_domain_suffix", "")

# Load conventions for provider prefixes, webhook repos, etc.
import yaml  # noqa: E402

_conv_path = BASE_DIR / "profiles" / _profile / "conventions.yaml" if _profile else None
_conv = yaml.safe_load(_conv_path.read_text()) if (_conv_path and _conv_path.exists()) else {}
PROVIDER_PREFIXES: list[str] = _conv.get("provider_prefixes", ["grpc-apm-", "grpc-providers-"])
WEBHOOK_REPOS: dict[str, str] = _conv.get("webhook_repos", {})
GATEWAY_REPO: str = _conv.get("gateway_repo", "")
PROTO_REPOS: list[str] = _conv.get("proto_repos", ["providers-proto"])
FEATURE_REPO: str = _conv.get("feature_repo", "")
CREDENTIALS_REPO: str = _conv.get("credentials_repo", "")
MANUAL_EDGES: dict[str, list[dict]] = _conv.get("manual_edges", {})
PKG_RESOLUTION_PREFIXES: list[str] = _conv.get(
    "pkg_resolution_prefixes", ["", "grpc-", "grpc-core-", "node-libs-", "libs-"]
)
PKG_RESOLUTION_SUFFIXES: list[str] = _conv.get("pkg_resolution_suffixes", [])
PKG_RESOLUTION_MAP: dict[str, str] = _conv.get("pkg_resolution_map", {})


EXTRACTED_PATH = BASE_DIR / "extracted"

# Well-known protobuf types to skip when tracking usage
_WELL_KNOWN_TYPES = {
    "Timestamp",
    "Duration",
    "Any",
    "Empty",
    "Struct",
    "Value",
    "ListValue",
    "FieldMask",
    "BoolValue",
    "BytesValue",
    "DoubleValue",
    "FloatValue",
    "Int32Value",
    "Int64Value",
    "StringValue",
    "UInt32Value",
    "UInt64Value",
    "NullValue",
}

# Generic message names too common to be useful for usage tracking
_GENERIC_MESSAGE_NAMES = {
    "ID",
    "Request",
    "Response",
    "Error",
    "Status",
    "Result",
    "Empty",
    "Params",
    "Options",
    "Config",
    "Data",
    "Item",
    "List",
}

# Tooling packages that don't indicate real service dependencies
_TOOLING_PACKAGES = {
    "eslint-config",
    "eslint-config-react",
    "eslint-config-next",
    "commitlint-config",
    "tailwind-config",
    "prettier-config",
    "icons",
    "components",
    "js",
    "tsconfig",
}

# Proto/types packages that indicate contract dependencies
_PROTO_PACKAGES = {
    "providers-proto",
    "types",
    "envoy-proto",
}

# Proto field/message regex patterns
_MESSAGE_RE = re.compile(r"message\s+(\w+)\s*\{", re.DOTALL)
_FIELD_RE = re.compile(
    r"(?:repeated\s+|optional\s+|required\s+)?"
    r"(?:map\s*<\s*\w+\s*,\s*\w+\s*>|[\w.]+)\s+"
    r"(\w+)\s*=\s*\d+",
)
_SERVICE_START_RE = re.compile(r"service\s+(\w+)\s*\{")
_RPC_RE = re.compile(r"rpc\s+(\w+)\s*\(([\w.]+)\)\s*returns\s*\(([\w.]+)\)")
_PACKAGE_RE = re.compile(r"package\s+([\w.]+)\s*;")


def _parse_proto_file(filepath: Path) -> dict:
    """Parse a .proto file and extract package, messages, and services.

    Returns dict with keys: package, messages [{name, fields}], services [{name, methods}]
    Handles nested messages by using dot-separated names.
    """
    content = filepath.read_text(errors="replace")

    # Extract package
    pkg_match = _PACKAGE_RE.search(content)
    package = pkg_match.group(1) if pkg_match else ""

    # Extract messages (including nested)
    messages = []
    _extract_messages(content, package, messages)

    # Extract services (use brace counting since service bodies contain {} in rpc lines)
    services = []
    for svc_match in _SERVICE_START_RE.finditer(content):
        svc_name = svc_match.group(1)
        brace_start = svc_match.end() - 1
        depth = 1
        i = brace_start + 1
        while i < len(content) and depth > 0:
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
            i += 1
        svc_body = content[brace_start + 1 : i - 1] if depth == 0 else ""
        methods = []
        for rpc_match in _RPC_RE.finditer(svc_body):
            methods.append(
                {
                    "name": rpc_match.group(1),
                    "input": rpc_match.group(2),
                    "output": rpc_match.group(3),
                }
            )
        services.append({"name": svc_name, "methods": methods})

    return {
        "package": package,
        "messages": messages,
        "services": services,
    }


def _extract_messages(content: str, package: str, result: list, parent_prefix: str = ""):
    """Extract message definitions, handling nested messages via brace counting."""
    pos = 0
    while pos < len(content):
        match = _MESSAGE_RE.search(content, pos)
        if not match:
            break

        msg_name = match.group(1)
        full_name = f"{parent_prefix}{msg_name}" if parent_prefix else msg_name

        # Find matching closing brace via counting
        brace_start = match.end() - 1  # position of opening {
        depth = 1
        i = brace_start + 1
        while i < len(content) and depth > 0:
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
            i += 1

        msg_body = content[brace_start + 1 : i - 1] if depth == 0 else ""

        # Extract fields from this message body (excluding nested message bodies)
        fields = []
        # Remove nested message/enum blocks for field extraction
        clean_body = re.sub(r"(?:message|enum)\s+\w+\s*\{[^}]*\}", "", msg_body)
        for field_match in _FIELD_RE.finditer(clean_body):
            fields.append(field_match.group(1))

        result.append({"name": full_name, "fields": fields})

        # Recurse into nested messages
        _extract_messages(msg_body, package, result, parent_prefix=f"{full_name}.")

        pos = i


def _load_domain_map() -> dict[str, str]:
    """Load domain → repo mapping from domain_registry.yaml (profile-aware)."""
    # Check profile first, then legacy
    _prof_dir = BASE_DIR / "profiles" / _profile if _profile else None
    registry_file = None
    if _prof_dir and (_prof_dir / "docs" / "domain_registry.yaml").is_file():
        registry_file = _prof_dir / "docs" / "domain_registry.yaml"
    elif (BASE_DIR / "docs" / "domain_registry.yaml").is_file():
        registry_file = BASE_DIR / "docs" / "domain_registry.yaml"
    if not registry_file:
        return {}

    mapping: dict[str, str] = {}
    text = registry_file.read_text()
    current_domain = ""

    for line in text.splitlines():
        m = re.match(r'\s+-\s+domain:\s+"(.+)"', line)
        if m:
            current_domain = m.group(1)
        m_repo = re.match(r"\s+repo:\s+(\S+)", line)
        if m_repo and current_domain:
            for env in ["dev", "staging", ""]:
                if env:
                    expanded = current_domain.replace("{env}.", f"{env}.")
                else:
                    expanded = current_domain.replace("{env}.", "")
                mapping[expanded] = m_repo.group(1)

    return mapping


def _resolve_url_to_repo(url: str, domain_map: dict[str, str]) -> str | None:
    """Resolve a full URL to a repo via domain registry."""
    for domain, repo in domain_map.items():
        if domain in url:
            return repo
    return None


def _resolve_domain_to_repo(domain: str, domain_map: dict[str, str]) -> str | None:
    """Resolve a domain name to a repo via domain registry."""
    if domain in domain_map:
        return domain_map[domain]
    # Try without subdomain prefix variations
    for registered, repo in domain_map.items():
        if domain.endswith(registered) or registered.endswith(domain):
            return repo
    return None


def _resolve_repo_ref(name: str) -> str:
    """Resolve {gateway_repo} and {feature_repo} placeholders in manual edge definitions."""
    if name == "{gateway_repo}":
        return GATEWAY_REPO or "grpc-payment-gateway"
    if name == "{feature_repo}":
        return FEATURE_REPO or "grpc-providers-features"
    return name
