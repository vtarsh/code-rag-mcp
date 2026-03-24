#!/usr/bin/env python3
"""
Build dependency graph from extracted artifacts.

Parses:
  1. ENV configs — *_GRPC_URL vars → service-to-service gRPC calls
  2. Scoped npm deps — package.json dependencies → npm package edges
  3. Proto imports — proto file imports → proto dependency edges
  4. K8s configs — service names, env vars pointing to other services

Output: graph_nodes + graph_edges tables in knowledge.db
"""

import json
import os
import re
import sqlite3
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


def init_graph_tables(conn: sqlite3.Connection):
    """Create graph tables if they don't exist."""
    conn.executescript("""
        DROP TABLE IF EXISTS graph_edges;
        DROP TABLE IF EXISTS graph_nodes;

        CREATE TABLE graph_nodes (
            name TEXT PRIMARY KEY,
            type TEXT,           -- grpc-service-js, temporal-workflow, library, etc.
            grpc_host TEXT,      -- e.g. core-merchants.grpc.example.com
            proto_package TEXT   -- e.g. provider, core.merchants
        );

        CREATE TABLE graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,       -- repo that depends/calls
            target TEXT NOT NULL,       -- repo being depended on/called
            edge_type TEXT NOT NULL,    -- grpc_call, npm_dep, proto_import, k8s_env
            detail TEXT,               -- extra info (env var name, package name, etc.)
            UNIQUE(source, target, edge_type, detail)
        );

        CREATE INDEX idx_edges_source ON graph_edges(source);
        CREATE INDEX idx_edges_target ON graph_edges(target);
        CREATE INDEX idx_edges_type ON graph_edges(edge_type);
        CREATE INDEX IF NOT EXISTS idx_edges_source_type ON graph_edges(source, edge_type);
        CREATE INDEX IF NOT EXISTS idx_edges_target_type ON graph_edges(target, edge_type);
    """)
    conn.commit()


def populate_nodes(conn: sqlite3.Connection):
    """Create a node for every repo."""
    repos = conn.execute("SELECT name, type FROM repos").fetchall()
    for r in repos:
        conn.execute("INSERT OR IGNORE INTO graph_nodes (name, type) VALUES (?, ?)", (r[0], r[1]))
    print(f"  Nodes: {len(repos)}")


def parse_grpc_url_edges(conn: sqlite3.Connection):
    """Parse *_GRPC_URL env vars to find service-to-service gRPC calls.

    Pattern: CORE_MERCHANTS_GRPC_URL=core-merchants.grpc.example.com
    → source repo calls grpc-core-merchants (or similar)
    """
    edges = []

    # Get all env_config chunks
    rows = conn.execute("SELECT repo_name, content FROM chunks WHERE chunk_type = 'env_config'").fetchall()

    # Also check code_file chunks for consts.js patterns
    code_rows = conn.execute(
        "SELECT repo_name, content FROM chunks WHERE (chunk_type = 'code_file' OR chunk_type = 'code_function') AND content LIKE '%GRPC_URL%'"
    ).fetchall()

    all_rows = list(rows) + list(code_rows)

    # Build a lookup: grpc hostname prefix → repo name
    # e.g., "core-merchants" → "grpc-core-merchants"
    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    # hostname patterns:
    # core-merchants.grpc.example.com → target is "grpc-core-merchants"
    # providers-credentials.grpc.example.com → target is "grpc-providers-credentials"
    grpc_url_pattern = re.compile(r'(\w+_GRPC_(?:URL|HOST))\s*[=:]\s*[\'"]?([a-z][\w-]+)\.grpc\.', re.IGNORECASE)

    # Also match plain hostname assignments
    grpc_url_pattern2 = re.compile(
        r'(\w+_GRPC_(?:URL|HOST))\s*[=:]\s*[\'"]?([a-z][\w-]+?)(?:\.grpc|[\'"\s,]|$)', re.IGNORECASE
    )

    # Pattern to extract service name from env var name:
    # CORE_MERCHANTS_GRPC_URL → core-merchants → grpc-core-merchants
    env_var_name_pattern = re.compile(r"(\w+?)_GRPC_(?:URL|HOST)\s*[=:]", re.IGNORECASE)

    for row in all_rows:
        source = row[0]
        content = row[1]

        # Strategy 1: parse hostname from URL value (works when URL has real hostname)
        for pattern in [grpc_url_pattern, grpc_url_pattern2]:
            for match in pattern.finditer(content):
                env_var = match.group(1)
                hostname = match.group(2)

                if hostname in ("0.0.0.0", "localhost", "127.0.0.1"):
                    continue

                target_candidates = [f"grpc-{hostname}", hostname]
                target_candidates.extend(f"{p}{hostname}" for p in PROVIDER_PREFIXES)

                target = None
                for candidate in target_candidates:
                    if candidate in repo_names and candidate != source:
                        target = candidate
                        break

                if target and source != target:
                    edges.append((source, target, "grpc_call", env_var))

        # Strategy 2: derive service name from env var NAME itself
        # CORE_MERCHANTS_GRPC_URL → core-merchants → grpc-core-merchants
        # VAULT_CARDS_GRPC_URL → vault-cards → grpc-vault-cards
        for match in env_var_name_pattern.finditer(content):
            var_prefix = match.group(1)  # e.g., "CORE_MERCHANTS"
            # Convert CORE_MERCHANTS → core-merchants
            service_name = var_prefix.lower().replace("_", "-")

            target_candidates = [f"grpc-{service_name}", service_name]
            target_candidates.extend(f"{p}{service_name}" for p in PROVIDER_PREFIXES)

            target = None
            for candidate in target_candidates:
                if candidate in repo_names and candidate != source:
                    target = candidate
                    break

            if target and source != target:
                edges.append((source, target, "grpc_call", var_prefix + "_GRPC_URL"))

    # Deduplicate
    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)
    print(f"  gRPC call edges: {len(unique_edges)}")


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


def _classify_npm_dep(pkg_name: str, target: str | None) -> str:
    """Classify npm dependency into subtypes for better graph signal.

    Returns edge_type: npm_dep_proto | npm_dep_tooling | npm_dep
    """
    # Check against known sets
    clean_name = pkg_name
    for prefix in ("node-libs-", "libs-"):
        if clean_name.startswith(prefix):
            clean_name = clean_name[len(prefix) :]

    if clean_name in _PROTO_PACKAGES or pkg_name in _PROTO_PACKAGES:
        return "npm_dep_proto"
    if clean_name in _TOOLING_PACKAGES or pkg_name in _TOOLING_PACKAGES:
        return "npm_dep_tooling"
    if target and any(target.startswith(p) for p in [*PROTO_REPOS, "node-libs-envoy-proto"]):
        return "npm_dep_proto"
    if target and any(target.startswith(p) for p in ("eslint-", "commitlint-", "tailwind-", "prettier-")):
        return "npm_dep_tooling"

    return "npm_dep"


def parse_npm_dep_edges(conn: sqlite3.Connection):
    """Parse scoped npm dependencies from repos table."""
    edges = []
    scope_prefix = f"{NPM_SCOPE}/"

    repos = conn.execute("SELECT name, org_deps FROM repos WHERE org_deps IS NOT NULL").fetchall()
    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    for row in repos:
        source = row[0]
        deps = json.loads(row[1]) if row[1] else []

        for dep in deps:
            pkg_name = dep.replace(scope_prefix, "") if dep.startswith(scope_prefix) else dep

            # Try to find matching repo (ordered by likelihood)
            target_candidates = [
                pkg_name,
                f"grpc-{pkg_name}",
                f"grpc-core-{pkg_name}",
                f"node-libs-{pkg_name}",
                f"libs-{pkg_name}",
            ]
            target_candidates.extend(f"{p}{pkg_name}" for p in PROVIDER_PREFIXES)

            target = None
            for candidate in target_candidates:
                if candidate in repo_names and candidate != source:
                    target = candidate
                    break

            # Classify dependency type for better signal-to-noise
            edge_type = _classify_npm_dep(pkg_name, target)
            edges.append((source, target or f"pkg:{dep}", edge_type, dep))

    # Upgrade npm_dep → grpc_client_usage for known gRPC consumer repos.
    # Only specific consumer patterns (graphql, express-api-*, backoffice-web) that
    # import @pay-com/core-X as gRPC client stubs deserve this upgrade.
    # This is narrower than upgrading ALL npm_dep to grpc-* targets (which caused regression).
    _grpc_consumer_prefixes = ("graphql", "express-api-", "backoffice-web", "next-web-")
    upgraded = []
    for i, (source, target, etype, detail) in enumerate(edges):
        if (
            etype == "npm_dep"
            and target
            and target.startswith("grpc-")
            and any(source.startswith(p) or source == p.rstrip("-") for p in _grpc_consumer_prefixes)
        ):
            edges[i] = (source, target, "grpc_client_usage", detail)
            upgraded.append((source, target))

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)

    resolved = len([e for e in unique_edges if not e[1].startswith("pkg:")])
    unresolved = len(unique_edges) - resolved
    print(
        f"  npm dep edges: {len(unique_edges)} ({resolved} resolved, {unresolved} unresolved, {len(upgraded)} upgraded to grpc_client_usage)"
    )


def parse_k8s_env_edges(conn: sqlite3.Connection):
    """Parse K8s deployment configs for service references."""
    edges = []

    rows = conn.execute(
        "SELECT repo_name, content FROM chunks WHERE file_type = 'k8s' AND content LIKE '%GRPC%'"
    ).fetchall()

    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    grpc_ref_pattern = re.compile(r'value:\s*[\'"]?([a-z][\w-]+)\.grpc\.', re.IGNORECASE)

    for row in rows:
        source = row[0]
        content = row[1]

        for match in grpc_ref_pattern.finditer(content):
            hostname = match.group(1)
            target_candidates = [f"grpc-{hostname}", hostname]

            for candidate in target_candidates:
                if candidate in repo_names and candidate != source:
                    edges.append((source, candidate, "k8s_env", hostname))
                    break

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)
    print(f"  K8s env edges: {len(unique_edges)}")


def parse_proto_import_edges(conn: sqlite3.Connection):
    """Parse proto imports to find proto-level dependencies."""
    edges = []

    rows = conn.execute("SELECT repo_name, content FROM chunks WHERE chunk_type = 'proto_header'").fetchall()

    import_pattern = re.compile(r'import\s+"([^"]+)"')

    for row in rows:
        source = row[0]
        content = row[1]

        for match in import_pattern.finditer(content):
            imported = match.group(1)
            # Track proto imports as edges
            # Common: "types/protos/common.proto" → depends on libs-types
            # "google/protobuf/*" → skip (standard)
            if imported.startswith("google/"):
                continue

            if "types/protos/" in imported:
                edges.append((source, f"pkg:{NPM_SCOPE}/types", "proto_import", imported))
            elif "providers.proto" in imported:
                edges.append((source, PROTO_REPOS[0] if PROTO_REPOS else "providers-proto", "proto_import", imported))
            else:
                edges.append((source, f"proto:{imported}", "proto_import", imported))

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)
    print(f"  Proto import edges: {len(unique_edges)}")


def parse_webhook_edges(conn: sqlite3.Connection):
    """Parse webhook routing to find provider↔webhook service connections.

    Three webhook layers:
      1. express-webhooks: HTTP ingress, routes via webhook.bind(null, 'provider')
      2. workflow-provider-webhooks: Temporal dispatch, provider[name](params)
      3. express-api-callbacks: APM redirects, ?provider=X

    Creates edges:
      - express-webhooks → workflow-provider-webhooks (webhook_dispatch)
      - workflow-provider-webhooks → grpc-apm-{provider} (webhook_handler)
      - express-api-callbacks → grpc-apm-{provider} (callback_handler)
    """
    edges = []
    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    # 1. Parse webhook dispatch repo routes
    _wh_dispatch = WEBHOOK_REPOS.get("dispatch", "express-webhooks")
    _wh_handler = WEBHOOK_REPOS.get("handler", "workflow-provider-webhooks")
    route_rows = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND (file_path LIKE '%routes%' OR file_path LIKE '%webhook%')",
        (_wh_dispatch,),
    ).fetchall()

    webhook_providers_express = set()
    bind_pattern = re.compile(r"webhook\.bind\s*\(\s*null\s*,\s*['\"](\w+)['\"]")

    for row in route_rows:
        for match in bind_pattern.finditer(row[0]):
            provider = match.group(1)
            webhook_providers_express.add(provider)

    # 2. Parse webhook handler repo: provider handler map
    handler_rows = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND "
        "(file_path LIKE '%run-activities%' OR file_path LIKE '%activities/index%')",
        (_wh_handler,),
    ).fetchall()

    webhook_providers_workflow = set()
    # Patterns: handleTrustly, ...trustly, require('./activities/trustly/...')
    require_pattern = re.compile(r"require\s*\(\s*['\"]\.*/activities/(\w+)")
    handler_map_pattern = re.compile(r"(\w+)\s*:\s*handle\w+")

    for row in handler_rows:
        content = row[0]
        for match in require_pattern.finditer(content):
            webhook_providers_workflow.add(match.group(1))
        for match in handler_map_pattern.finditer(content):
            webhook_providers_workflow.add(match.group(1))

    # Also scan activity directories — each subdirectory is a provider
    activity_dirs = conn.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_path LIKE '%activities/%/%.js'",
        (_wh_handler,),
    ).fetchall()

    for row in activity_dirs:
        # activities/trustly/webhook/handle-activities.js → trustly
        parts = row[0].split("/")
        for i, part in enumerate(parts):
            if part == "activities" and i + 1 < len(parts):
                provider = parts[i + 1]
                if provider not in ("index", "libs", "utils", "helpers", "common"):
                    webhook_providers_workflow.add(provider)

    # 3. Parse express-api-callbacks
    callback_rows = conn.execute("SELECT content FROM chunks WHERE repo_name = 'express-api-callbacks'").fetchall()

    callback_providers = set()
    # Pattern: case 'ideal': or provider === 'volt'
    case_pattern = re.compile(r"case\s+['\"](\w+)['\"]")
    provider_eq_pattern = re.compile(r"provider\s*===?\s*['\"](\w+)['\"]")

    for row in callback_rows:
        content = row[0]
        for match in case_pattern.finditer(content):
            callback_providers.add(match.group(1))
        for match in provider_eq_pattern.finditer(content):
            callback_providers.add(match.group(1))

    # Build edges
    all_providers = webhook_providers_express | webhook_providers_workflow | callback_providers

    # Dispatch repo forwards to handler repo
    if _wh_dispatch in repo_names and _wh_handler in repo_names:
        edges.append((_wh_dispatch, _wh_handler, "webhook_dispatch", "all-providers"))

    for provider in all_providers:
        # Find the provider repo
        target = None
        for prefix in PROVIDER_PREFIXES:
            candidate = f"{prefix}{provider}"
            if candidate in repo_names:
                target = candidate
                break

        # dispatch → handler (per provider)
        if provider in webhook_providers_express and _wh_handler in repo_names:
            edges.append((_wh_dispatch, _wh_handler, "webhook_dispatch", provider))

        # handler → provider repo
        if provider in webhook_providers_workflow and target:
            edges.append((_wh_handler, target, "webhook_handler", provider))

        # express-api-callbacks → provider repo (callback)
        if provider in callback_providers and target:
            edges.append(("express-api-callbacks", target, "callback_handler", provider))

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)

    dispatch_count = len([e for e in unique_edges if e[2] == "webhook_dispatch"])
    handler_count = len([e for e in unique_edges if e[2] == "webhook_handler"])
    callback_count = len([e for e in unique_edges if e[2] == "callback_handler"])
    print(
        f"  Webhook edges: {len(unique_edges)} (dispatch: {dispatch_count}, handler: {handler_count}, callback: {callback_count})"
    )
    print(
        f"  Providers found: {len(all_providers)} ({len(webhook_providers_express)} express, {len(webhook_providers_workflow)} workflow, {len(callback_providers)} callback)"
    )


def parse_grpc_client_require_edges(conn: sqlite3.Connection):
    """Parse require('@scope/package') in code to find gRPC client usage.

    When code does `require('{NPM_SCOPE}/core-transactions')`, it means
    the repo is calling grpc-core-transactions as a client. This is
    stronger signal than npm_dep because it confirms actual code usage.

    Pattern: require('{NPM_SCOPE}/X') → source calls grpc-X (or grpc-core-X, grpc-apm-X)
    """
    edges = []

    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    # Get code chunks that contain {NPM_SCOPE}/ requires
    rows = conn.execute(
        "SELECT repo_name, content FROM chunks "
        "WHERE chunk_type IN ('code_file', 'code_function') "
        f"AND content LIKE '%require%{NPM_SCOPE}/%'"
    ).fetchall()

    require_pattern = re.compile(rf"""require\s*\(\s*['"]({re.escape(NPM_SCOPE)}/([\w-]+))(?:/[\w-]+)*['"]\s*\)""")

    # Packages that are tooling/utilities, not gRPC service clients
    skip_packages = {
        "tools",
        "common",
        "fetch",
        "cassandra",
        "grpc-tools",
        "temporal",
        "temporal-tools",
        "types",
        "js",
        "eslint-config",
        "eslint-config-react",
        "eslint-config-next",
        "commitlint-config",
        "tailwind-config",
        "prettier-config",
        "icons",
        "components",
        "tsconfig",
    }

    for row in rows:
        source = row[0]
        content = row[1]

        for match in require_pattern.finditer(content):
            full_pkg = match.group(1)
            pkg_name = match.group(2)

            if pkg_name in skip_packages:
                continue

            # Try to resolve to a repo
            target_candidates = [f"grpc-{pkg_name}", f"grpc-core-{pkg_name}", pkg_name, f"node-libs-{pkg_name}"]
            target_candidates.extend(f"{p}{pkg_name}" for p in PROVIDER_PREFIXES)

            target = None
            for candidate in target_candidates:
                if candidate in repo_names and candidate != source:
                    target = candidate
                    break

            if target:
                edges.append((source, target, "grpc_client_usage", full_pkg))

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)
    print(f"  gRPC client usage edges: {len(unique_edges)}")


def parse_grpc_method_call_edges(conn: sqlite3.Connection):
    """Parse method-level gRPC calls from destructured requires and variable calls.

    Patterns detected:
    1. const X = require('{NPM_SCOPE}/pkg') → X.method() calls
    2. const { method1, method2 } = require('{NPM_SCOPE}/pkg') → method imports

    Creates grpc_method_call edges: source → grpc-pkg :: method_name
    This is more granular than grpc_client_usage (which only knows the package).
    """
    repo_set = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    rows = conn.execute(
        "SELECT repo_name, content FROM chunks "
        "WHERE chunk_type IN ('code_file', 'code_function') "
        f"AND content LIKE '%{NPM_SCOPE}/%'"
    ).fetchall()

    # Variable assignment: const X = require('{NPM_SCOPE}/pkg')
    var_require_pat = re.compile(
        rf"(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['\"]"
        rf"{re.escape(NPM_SCOPE)}/([\w-]+)(?:/[\w-]+)*['\"]\s*\)"
    )
    # Destructured: const { m1, m2 } = require('{NPM_SCOPE}/pkg')
    destructured_pat = re.compile(
        rf"(?:const|let|var)\s*\{{([^}}]+)\}}\s*=\s*require\s*\(\s*['\"]"
        rf"{re.escape(NPM_SCOPE)}/([\w-]+)(?:/[\w-]+)*['\"]\s*\)"
    )

    # Packages that are tooling/utilities, not gRPC service clients
    skip_packages = {
        "tools",
        "common",
        "fetch",
        "cassandra",
        "grpc-tools",
        "temporal",
        "temporal-tools",
        "types",
        "js",
        "eslint-config",
        "eslint-config-react",
        "eslint-config-next",
        "commitlint-config",
        "tailwind-config",
        "prettier-config",
        "icons",
        "components",
        "tsconfig",
        "pg",
        "clickhouse",
        "message-queue",
        "kafka",
        "errors",
        "logger",
        "envoy-proto",
    }

    # JS built-in methods to ignore
    skip_methods = {
        "then",
        "catch",
        "finally",
        "bind",
        "call",
        "apply",
        "toString",
        "valueOf",
        "hasOwnProperty",
        "map",
        "filter",
        "reduce",
        "forEach",
        "length",
        "push",
        "pop",
        "shift",
        "keys",
        "values",
        "entries",
    }

    def _resolve_pkg_to_repo(pkg: str, source: str) -> str | None:
        candidates = [f"grpc-{pkg}", f"grpc-core-{pkg}", pkg, f"node-libs-{pkg}"]
        candidates.extend(f"{p}{pkg}" for p in PROVIDER_PREFIXES)
        for candidate in candidates:
            if candidate in repo_set and candidate != source:
                return candidate
        return None

    edges = []

    for repo, content in rows:
        # Pattern 1: variable assignment → method calls
        for m in var_require_pat.finditer(content):
            var_name = m.group(1)
            pkg = m.group(2)
            if pkg in skip_packages:
                continue

            target = _resolve_pkg_to_repo(pkg, repo)
            if not target:
                continue

            method_pat = re.compile(rf"{re.escape(var_name)}\.(\w+)\s*\(")
            for mc in method_pat.finditer(content):
                method = mc.group(1)
                if method not in skip_methods:
                    edges.append((repo, target, "grpc_method_call", f"{pkg}::{method}"))

        # Pattern 2: destructured imports
        for m in destructured_pat.finditer(content):
            methods_raw = m.group(1).split(",")
            pkg = m.group(2)
            if pkg in skip_packages:
                continue

            target = _resolve_pkg_to_repo(pkg, repo)
            if not target:
                continue

            for method_raw in methods_raw:
                method = method_raw.strip().split(":")[0].strip()
                if method and len(method) < 40 and method not in skip_methods:
                    edges.append((repo, target, "grpc_method_call", f"{pkg}::{method}"))

    unique = list(set(edges))
    for e in unique:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)
    print(f"  gRPC method call edges: {len(unique)}")


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


def parse_proto_field_edges(conn: sqlite3.Connection):
    """Parse .proto files to create message/service definition edges and usage edges.

    Creates three edge types:
      - proto_message_def: repo → msg:Package.MessageName (with field list as detail)
      - proto_service_def: repo → svc:Package.ServiceName (with method list as detail)
      - proto_message_usage: consumer repo → msg:Package.MessageName (TS/JS references)
    """
    msg_def_edges = []
    svc_def_edges = []
    usage_edges = []

    # --- Phase 1: Parse .proto files from extracted repos for definitions ---

    # Collect all message names per package for usage lookups
    # message_name → set of qualified names (package.MessageName)
    message_lookup: dict[str, set[str]] = {}

    proto_file_count = 0
    total_messages = 0
    total_services = 0

    for repo_dir in sorted(EXTRACTED_PATH.iterdir()):
        if not repo_dir.is_dir():
            continue
        repo_name = repo_dir.name

        proto_files = list(repo_dir.rglob("*.proto"))
        if not proto_files:
            continue

        for proto_file in proto_files:
            # Skip vendored google protobuf files
            rel_path = str(proto_file.relative_to(repo_dir))
            if "google/protobuf/" in rel_path or "node_modules/" in rel_path:
                continue

            try:
                parsed = _parse_proto_file(proto_file)
            except Exception:
                continue

            proto_file_count += 1
            package = parsed["package"]

            # Message definitions
            for msg in parsed["messages"]:
                qualified = f"{package}.{msg['name']}" if package else msg["name"]
                fields_str = ", ".join(msg["fields"][:20])  # cap detail length
                if len(msg["fields"]) > 20:
                    fields_str += f" ... (+{len(msg['fields']) - 20} more)"

                msg_def_edges.append(
                    (
                        repo_name,
                        f"msg:{qualified}",
                        "proto_message_def",
                        fields_str or "(empty)",
                    )
                )
                total_messages += 1

                # Register in lookup (unqualified name → qualified name set)
                base_name = msg["name"].split(".")[-1]  # handle nested: Foo.Bar → Bar
                if base_name not in _WELL_KNOWN_TYPES and base_name not in _GENERIC_MESSAGE_NAMES:
                    message_lookup.setdefault(base_name, set()).add(qualified)

            # Service definitions
            for svc in parsed["services"]:
                qualified = f"{package}.{svc['name']}" if package else svc["name"]
                methods_str = ", ".join(m["name"] for m in svc["methods"][:15])
                if len(svc["methods"]) > 15:
                    methods_str += f" ... (+{len(svc['methods']) - 15} more)"

                svc_def_edges.append(
                    (
                        repo_name,
                        f"svc:{qualified}",
                        "proto_service_def",
                        methods_str or "(empty)",
                    )
                )
                total_services += 1

    # --- Phase 2: Find proto message usage in TS/JS code ---

    # Build a regex pattern from all known message names (skip very short/generic ones)
    usable_names = {name for name in message_lookup if len(name) >= 5}
    if usable_names:
        # Sort by length descending so longer names match first
        sorted_names = sorted(usable_names, key=len, reverse=True)
        # Process in batches to avoid regex too-large issues
        batch_size = 200
        name_batches = [sorted_names[i : i + batch_size] for i in range(0, len(sorted_names), batch_size)]

        # Get code chunks from the DB
        code_rows = conn.execute(
            "SELECT repo_name, content FROM chunks "
            "WHERE chunk_type IN ('code_file', 'code_function') "
            "AND length(content) > 50"
        ).fetchall()

        # For each code chunk, find proto message references
        usage_found: dict[tuple[str, str], int] = {}  # (repo, qualified_msg) → count

        for batch in name_batches:
            # Build word-boundary pattern for this batch
            pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in batch) + r")\b")

            for row in code_rows:
                repo_name = row[0]
                content = row[1]

                for match in pattern.finditer(content):
                    msg_name = match.group(1)
                    qualified_names = message_lookup.get(msg_name, set())
                    for qn in qualified_names:
                        key = (repo_name, qn)
                        usage_found[key] = usage_found.get(key, 0) + 1

        # Convert to edges (skip self-references where repo defines the message)
        msg_def_repos = {}  # qualified_msg → defining repo
        for e in msg_def_edges:
            target_msg = e[1]  # msg:package.Name
            msg_def_repos.setdefault(target_msg, set()).add(e[0])

        for (repo_name, qualified_msg), count in usage_found.items():
            target = f"msg:{qualified_msg}"
            # Skip if this repo defines this message (not a cross-repo usage)
            if repo_name in msg_def_repos.get(target, set()):
                continue
            usage_edges.append(
                (
                    repo_name,
                    target,
                    "proto_message_usage",
                    f"refs: {count}",
                )
            )

    # --- Phase 3: Insert all edges ---

    for edges_list in [msg_def_edges, svc_def_edges, usage_edges]:
        unique_edges = list(set(edges_list))
        for e in unique_edges:
            conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)

    unique_msg_defs = len(set(msg_def_edges))
    unique_svc_defs = len(set(svc_def_edges))
    unique_usage = len(set(usage_edges))
    usage_repos = len(set(e[0] for e in usage_edges))

    print(f"  Proto files parsed: {proto_file_count}")
    print(f"  Message definitions: {unique_msg_defs} ({total_messages} total incl. nested)")
    print(f"  Service definitions: {unique_svc_defs} ({total_services} total)")
    print(f"  Message usage edges: {unique_usage} (across {usage_repos} consumer repos)")


def parse_fetch_edges(conn: sqlite3.Connection):
    """Parse {NPM_SCOPE}/fetch calls that reference internal service URLs.

    Pattern: fetch({ url: `${SOME_SERVICE_URL}/...` })
    The env var name hints at the target service.
    """
    edges = []
    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    rows = conn.execute(
        "SELECT repo_name, content FROM chunks "
        "WHERE chunk_type IN ('code_file', 'code_function') "
        "AND content LIKE '%fetch%' AND content LIKE '%_URL%'"
    ).fetchall()

    # Pattern: url variable referencing a service
    url_var_pattern = re.compile(
        r"(?:url|baseUrl|apiUrl)\s*[:=]\s*(?:`\$\{)?(\w+_(?:URL|HOST|BASE_URL))", re.IGNORECASE
    )

    for row in rows:
        source = row[0]
        content = row[1]

        for match in url_var_pattern.finditer(content):
            var_name = match.group(1)

            # Skip external provider URLs (these are outbound, not internal)
            if any(x in var_name.upper() for x in ("PAYSAFE", "STRIPE", "PAYPAL", "ADYEN", "CHECKOUT")):
                continue

            # Try to derive service name from var:
            # PAYMENT_GATEWAY_URL → payment-gateway → grpc-payment-gateway
            service_name = var_name.upper()
            for suffix in ("_BASE_URL", "_API_URL", "_URL", "_HOST"):
                if service_name.endswith(suffix):
                    service_name = service_name[: -len(suffix)]
                    break
            service_name = service_name.lower().replace("_", "-")

            target_candidates = [
                f"grpc-{service_name}",
                f"express-{service_name}",
                f"grpc-core-{service_name}",
                service_name,
            ]

            target = None
            for candidate in target_candidates:
                if candidate in repo_names and candidate != source:
                    target = candidate
                    break

            if target:
                edges.append((source, target, "http_call", var_name))

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)
    print(f"  HTTP/fetch call edges: {len(unique_edges)}")


def parse_express_routes(conn: sqlite3.Connection):
    """Parse Express route definitions from route index files.

    Creates two edge types:
    - express_route: repo → route:METHOD:/path (what endpoints exist)
    - express_mount: repo → route:USE:/prefix/* (sub-router mounts)

    Sources: route index files from express-* repos.
    Pattern: router.get('/path', ...) or router.use('/prefix', ...)
    """
    edges = []

    # Get route index file chunks from express-* repos
    rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks "
        "WHERE repo_name LIKE 'express-%' "
        "AND file_path LIKE '%routes%index%' "
        "AND file_path NOT LIKE '%validations%'"
    ).fetchall()

    # router.get('/path', ...) or router.post('/path', ...)
    route_pattern = re.compile(r"router\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]")
    # router.use('/prefix', ...) — sub-router mount
    mount_pattern = re.compile(r"router\.use\(\s*['\"]([^'\"]+)['\"]")

    routes_found = 0
    mounts_found = 0

    for row in rows:
        repo = row[0]
        file_path = row[1]
        content = row[2]

        # Determine route prefix from file path
        # routes/charges/index.js → /charges
        # routes/index.js → (root)
        parts = file_path.split("/")
        prefix_parts = []
        in_routes = False
        for p in parts:
            if p == "routes":
                in_routes = True
                continue
            if in_routes and p != "index.js" and p != "index.ts":
                prefix_parts.append(p)
        prefix = "/" + "/".join(prefix_parts) if prefix_parts else ""

        # Parse direct routes
        for match in route_pattern.finditer(content):
            method = match.group(1).upper()
            path = match.group(2)
            full_path = prefix + path
            # Normalize double slashes
            while "//" in full_path:
                full_path = full_path.replace("//", "/")
            if not full_path:
                full_path = "/"

            target = f"route:{method}:{full_path}"
            edges.append((repo, target, "express_route", f"{method} {full_path}"))
            routes_found += 1

        # Parse sub-router mounts
        for match in mount_pattern.finditer(content):
            mount_path = match.group(1)
            full_mount = prefix + mount_path
            while "//" in full_mount:
                full_mount = full_mount.replace("//", "/")

            target = f"route:USE:{full_mount}/*"
            edges.append((repo, target, "express_mount", f"USE {full_mount}/*"))
            mounts_found += 1

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)
    print(f"  Express routes: {routes_found} routes, {mounts_found} mounts ({len(unique_edges)} unique edges)")


def print_summary(conn: sqlite3.Connection):
    """Print graph statistics."""
    nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]

    print("\n=== Graph Summary ===")
    print(f"  Nodes: {nodes}")
    print(f"  Edges: {edges}")

    print("\n  Edge types:")
    for row in conn.execute("SELECT edge_type, COUNT(*) as cnt FROM graph_edges GROUP BY edge_type ORDER BY cnt DESC"):
        print(f"    {row[0]}: {row[1]}")

    # Top connected services
    print("\n  Most depended-on (top 15):")
    for row in conn.execute("""
        SELECT target, COUNT(*) as cnt
        FROM graph_edges
        WHERE target NOT LIKE 'pkg:%' AND target NOT LIKE 'proto:%'
        GROUP BY target
        ORDER BY cnt DESC
        LIMIT 15
    """):
        print(f"    {row[0]}: {row[1]} incoming edges")

    print("\n  Most dependencies (top 10):")
    for row in conn.execute("""
        SELECT source, COUNT(*) as cnt
        FROM graph_edges
        GROUP BY source
        ORDER BY cnt DESC
        LIMIT 10
    """):
        print(f"    {row[0]}: {row[1]} outgoing edges")


def parse_temporal_edges(conn: sqlite3.Connection):
    """Parse Temporal workflow patterns: child workflows, signals, cross-repo workflow imports.

    Patterns:
      - executeChild('workflowName', ...) / startChild('workflowName', ...)
      - startChild(variableName, ...) with taskQueue
      - require('@scope/temporal-tools/workflows/...') — cross-repo workflow import
      - defineSignal('signalName') — signal definitions (stored as node metadata)
      - taskQueue: 'queueName' — links workflow to a task queue
      - activateWorkflow({ workflowName: 'name' }) — starts workflow via grpc-core-workflows
    """
    edges = []
    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    # Get all workflow-related chunks
    rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks WHERE file_type = 'workflow' OR "
        "(file_type IN ('grpc_method', 'library', 'code_file', 'code_function') AND (content LIKE '%executeChild%' OR content LIKE '%startChild%' OR content LIKE '%activateWorkflow%'))"
    ).fetchall()

    # Also get chunks that import from temporal-tools workflows
    import_rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks WHERE content LIKE '%temporal-tools/workflows/%'"
    ).fetchall()

    all_rows = list(rows) + list(import_rows)

    # Patterns
    child_workflow_pattern = re.compile(
        r'(?:executeChild|startChild)\s*\(\s*[\'"](\w[\w-]*)[\'"]',
    )
    task_queue_pattern = re.compile(
        r'taskQueue:\s*[\'"]([a-zA-Z][\w-]*)[\'"]',
    )
    signal_def_pattern = re.compile(
        r'defineSignal\s*\(\s*[\'"](\w+)[\'"]',
    )
    # Cross-repo workflow imports: require('{NPM_SCOPE}/temporal-tools/workflows/some-workflow/workflow')
    workflow_import_pattern = re.compile(
        rf"""require\s*\(\s*['"]({re.escape(NPM_SCOPE)}/temporal-tools/workflows/)([\w-]+)""",
    )
    # activateWorkflow({ workflowName: 'someWorkflow' }) — starts a workflow via grpc-core-workflows
    activate_workflow_pattern = re.compile(
        r"""activateWorkflow\s*\(\s*\{[^}]*workflowName:\s*['"](\w+)['"]""",
        re.DOTALL,
    )

    # Build a mapping: taskQueue name → repo name (heuristic: repo name often matches queue)
    task_queue_to_repo = {}
    for name in repo_names:
        # workflow-settlement-worker → settlementWorker or settlement-worker
        short = name.replace("workflow-", "")
        task_queue_to_repo[short] = name
        # camelCase version: settlement-worker → settlementWorker
        camel = re.sub(r"-(\w)", lambda m: m.group(1).upper(), short)
        task_queue_to_repo[camel] = name

    # Also build: workflow function name → repo name
    workflow_name_to_repo = {}
    for name in repo_names:
        if name.startswith("workflow-"):
            short = name.replace("workflow-", "")
            camel = re.sub(r"-(\w)", lambda m: m.group(1).upper(), short)
            workflow_name_to_repo[camel] = name
            workflow_name_to_repo[short] = name

    seen_signals = {}  # repo → [signal_names]

    # Additional patterns for signal sending and cross-repo activities
    signal_send_pattern = re.compile(
        r'(?:temporal\.signal|\.signal)\s*\(\s*(?:\w+,\s*)?[\'"](\w+)[\'"]',
    )
    # Cross-repo activity imports: require('{NPM_SCOPE}/temporal-tools/activities/some-activity')
    activity_import_pattern = re.compile(
        rf"""(?:require|from)\s*\(?['"]({re.escape(NPM_SCOPE)}/temporal-tools/activities/)([\w-]+)""",
    )
    # proxyActivities with cross-repo type import
    proxy_activity_type_pattern = re.compile(
        rf"""proxyActivities\s*<\s*typeof\s+import\s*\(\s*['"]({re.escape(NPM_SCOPE)}/([\w-]+))""",
    )

    # Also get chunks with signal sending or activity patterns
    signal_send_rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks "
        "WHERE content LIKE '%temporal.signal%' OR content LIKE '%.signal(%'"
    ).fetchall()
    activity_rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks "
        "WHERE content LIKE '%temporal-tools/activities/%' "
        "OR (content LIKE '%proxyActivities%' AND content LIKE '%import%')"
    ).fetchall()

    all_rows = list(set(all_rows + signal_send_rows + activity_rows))

    for row in all_rows:
        source = row[0]
        content = row[2]

        # 1. Child workflow calls
        for match in child_workflow_pattern.finditer(content):
            child_name = match.group(1)
            # Try to resolve to a repo
            target = workflow_name_to_repo.get(child_name)
            if not target:
                # Try with task queue context
                tq_match = task_queue_pattern.search(content)
                if tq_match:
                    tq = tq_match.group(1)
                    target = task_queue_to_repo.get(tq)

            if target and target != source:
                edges.append((source, target, "child_workflow", child_name))
            elif not target:
                # Record as unresolved workflow reference
                edges.append((source, f"workflow:{child_name}", "child_workflow", child_name))

        # 2. Cross-repo workflow imports
        for match in workflow_import_pattern.finditer(content):
            workflow_path = match.group(2)  # e.g., "update-transactions-bulk-with-s3"
            # Try to find target repo
            target = None
            for candidate in [f"workflow-{workflow_path}", workflow_path]:
                if candidate in repo_names:
                    target = candidate
                    break
            # If not a separate repo, it's from temporal-tools itself
            if not target:
                target = "node-libs-temporal-tools"
                if target not in repo_names:
                    target = f"pkg:{NPM_SCOPE}/temporal-tools"
            if target != source:
                edges.append((source, target, "workflow_import", workflow_path))

        # 3. Signal definitions → signal_handler edges
        for match in signal_def_pattern.finditer(content):
            signal_name = match.group(1)
            seen_signals.setdefault(source, []).append(signal_name)
            # Create edge: workflow handles this signal
            edges.append((source, f"signal:{signal_name}", "signal_handler", signal_name))

        # 4. Signal sending → signal_send edges
        for match in signal_send_pattern.finditer(content):
            signal_name = match.group(1)
            edges.append((source, f"signal:{signal_name}", "signal_send", signal_name))

        # 5. Cross-repo activity imports
        for match in activity_import_pattern.finditer(content):
            activity_path = match.group(2)
            target = "node-libs-temporal-tools"
            if target not in repo_names:
                target = f"pkg:{NPM_SCOPE}/temporal-tools"
            if target != source:
                edges.append((source, target, "activity_import", activity_path))

        # 6. proxyActivities with cross-repo type reference
        for match in proxy_activity_type_pattern.finditer(content):
            pkg_name = match.group(2)
            # Find the actual repo for this package
            target_candidates = [pkg_name, f"node-libs-{pkg_name}"]
            target = None
            for candidate in target_candidates:
                if candidate in repo_names:
                    target = candidate
                    break
            if not target:
                target = f"pkg:{match.group(1)}"
            if target != source:
                edges.append((source, target, "activity_import", pkg_name))

        # 7. activateWorkflow({ workflowName: 'X' }) — resolve to target workflow repo
        for match in activate_workflow_pattern.finditer(content):
            wf_name = match.group(1)  # camelCase, e.g. 'collaborationMaster'
            target = workflow_name_to_repo.get(wf_name)
            if not target:
                # Convert camelCase to kebab-case and try workflow-{kebab} variants
                kebab = re.sub(r"([a-z])([A-Z])", r"\1-\2", wf_name).lower()
                for candidate in [f"workflow-{kebab}", f"workflow-{kebab}-processing"]:
                    if candidate in repo_names:
                        target = candidate
                        break
            if target and target != source:
                edges.append((source, target, "temporal_activate", wf_name))
            elif not target:
                edges.append((source, f"workflow:{wf_name}", "temporal_activate", wf_name))

    # Deduplicate edges
    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)

    # Connect signal senders to signal receivers through shared signal names
    signal_edges = 0
    signal_defs = {}  # signal_name → set of defining repos
    for repo, signals in seen_signals.items():
        for sig in signals:
            signal_defs.setdefault(sig, set()).add(repo)

    # Find senders: repos with signal_send edges to signal:X
    signal_senders = {}
    for e in unique_edges:
        if e[2] == "signal_send":
            signal_senders.setdefault(e[3], set()).add(e[0])

    # Create direct sender → handler edges where we can match signal names
    for sig_name, senders in signal_senders.items():
        handlers = signal_defs.get(sig_name, set())
        for sender in senders:
            for handler in handlers:
                if sender != handler:
                    conn.execute(
                        "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
                        (sender, handler, "temporal_signal", sig_name),
                    )
                    signal_edges += 1

    resolved = len(
        [
            e
            for e in unique_edges
            if not e[1].startswith("workflow:") and not e[1].startswith("pkg:") and not e[1].startswith("signal:")
        ]
    )
    unresolved = len([e for e in unique_edges if e[1].startswith("workflow:") or e[1].startswith("pkg:")])
    signal_count = sum(len(v) for v in seen_signals.values())
    activity_count = len([e for e in unique_edges if e[2] == "activity_import"])
    print(f"  Temporal edges: {len(unique_edges)} ({resolved} resolved, {unresolved} unresolved)")
    print(f"  Signal definitions: {signal_count} across {len(seen_signals)} repos")
    print(f"  Signal sender→handler edges: {signal_edges}")
    print(f"  Activity import edges: {activity_count}")


def parse_domain_registry_edges(conn: sqlite3.Connection):
    """Create domain_serves edges from docs/domain_registry.yaml.

    For each domain entry, creates:
    - domain_serves edge: repo → domain (the repo serves this domain)
    - domain_reference edges: referencing_repo → serving_repo (who references the domain)
    """
    registry_file = BASE_DIR / "docs" / "domain_registry.yaml"
    if not registry_file.is_file():
        print("  No domain_registry.yaml found, skipping")
        return

    try:
        import yaml

        data = yaml.safe_load(registry_file.read_text())
        entries = data.get("domains", [])
    except ImportError:
        # Fallback: parse without PyYAML
        entries = _parse_domain_registry_simple(registry_file)

    edges: list[tuple[str, str, str, str]] = []
    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}

    for entry in entries:
        domain = entry.get("domain", "")
        repo = entry.get("repo", "")
        if not domain or not repo:
            continue

        # domain_serves: repo serves this domain
        if repo in known_repos:
            edges.append((repo, f"domain:{domain}", "domain_serves", domain))

        # domain_reference: repos that reference this domain → the serving repo
        for ref_repo in entry.get("referenced_by", []):
            if ref_repo in known_repos and repo in known_repos and ref_repo != repo:
                edges.append((ref_repo, repo, "domain_reference", domain))

    if edges:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            edges,
        )

    real_edges = [e for e in edges if not e[1].startswith("domain:")]
    print(f"  Domain registry: {len(edges)} edges ({len(real_edges)} repo-to-repo references)")


def _parse_domain_registry_simple(registry_file: Path) -> list[dict]:
    """Fallback parser for domain_registry.yaml without PyYAML."""
    text = registry_file.read_text()
    entries: list[dict] = []
    current: dict = {}

    for line in text.splitlines():
        m = re.match(r'\s+-\s+domain:\s+"(.+)"', line)
        if m:
            if current:
                entries.append(current)
            current = {"domain": m.group(1), "referenced_by": []}
            continue
        m = re.match(r"\s+repo:\s+(\S+)", line)
        if m and current:
            current["repo"] = m.group(1)
        m = re.match(r'\s+description:\s+"(.+)"', line)
        if m and current:
            current["description"] = m.group(1)
        m = re.match(r"\s+-\s+(\S+)\s*#?", line)
        if m and current and "referenced_by" in current and m.group(1) not in ("domain:", "repo:", "description:"):
            ref = m.group(1)
            if not ref.startswith('"') and not ref.startswith("domain:"):
                current["referenced_by"].append(ref)

    if current:
        entries.append(current)

    return entries


def parse_flow_annotation_edges(conn: sqlite3.Connection):
    """Create graph edges from flow annotation YAML files.

    Parses docs/flows/*.yaml and creates edges for:
    - grpc_call steps: source → target (flow_step edge)
    - redirect steps: source → target_repo (flow_redirect edge)
    - dispatch steps: source → target (flow_dispatch edge)
    """
    flows_dir = BASE_DIR / "docs" / "flows"
    if not flows_dir.is_dir():
        print("  No flows/ directory found, skipping")
        return

    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}
    edges: list[tuple[str, str, str, str]] = []

    for flow_file in sorted(flows_dir.glob("*.yaml")):
        source_repo = flow_file.stem
        if source_repo not in known_repos:
            continue

        text = flow_file.read_text()

        # Extract target repos from flow annotations
        for line in text.splitlines():
            stripped = line.strip()

            # target: "repo-name"
            m = re.match(r'target:\s+"?([a-z0-9-]+)"?', stripped)
            if m:
                target = m.group(1)
                # Handle template patterns like grpc-apm-{provider}
                if "{" in target:
                    continue  # Skip parameterized targets
                if target in known_repos:
                    edges.append((source_repo, target, "flow_step", "flow annotation"))

            # target_repo: "repo-name"
            m = re.match(r'target_repo:\s+"?([a-z0-9-]+)"?', stripped)
            if m:
                target = m.group(1)
                if target in known_repos:
                    edges.append((source_repo, target, "flow_redirect", "flow annotation"))

            # from: "repo-name" (for multi-hop flows)
            m = re.match(r'from:\s+"?([a-z0-9-]+)"?', stripped)
            if m:
                from_repo = m.group(1)
                if from_repo in known_repos:
                    # Look for the next target in nearby lines
                    pass  # Handled by target: on the same flow step

            # target_repos map entries: key: "repo-name"
            m = re.match(r'([a-z_]+):\s+"?([a-z0-9-]+)"?$', stripped)
            if (
                m
                and m.group(2) in known_repos
                and m.group(1)
                not in (
                    "type",
                    "name",
                    "entry",
                    "file",
                    "handler",
                    "method",
                    "action",
                    "condition",
                    "description",
                    "target",
                    "target_repo",
                )
            ):
                edges.append((source_repo, m.group(2), "flow_redirect", f"via {m.group(1)}"))

    if edges:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            edges,
        )

    print(f"  Flow annotations: {len(edges)} edges from {len(list(flows_dir.glob('*.yaml')))} files")


def parse_redirect_edges(conn: sqlite3.Connection):
    """Parse static res.redirect(URL) calls → target via domain registry.

    Only handles static URLs (string literals). Data-driven redirects
    (res.redirect(variable)) are covered by flow annotations (2.4).
    """
    # Load domain registry for URL resolution
    domain_map = _load_domain_map()
    if not domain_map:
        print("  No domain registry, skipping redirect edge parsing")
        return

    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}

    # Get code chunks that might contain redirects
    rows = conn.execute(
        "SELECT repo_name, content FROM chunks WHERE language IN ('javascript', 'typescript') "
        "AND (content LIKE '%redirect%' OR content LIKE '%302%')"
    ).fetchall()

    edges: list[tuple[str, str, str, str]] = []
    redirect_pattern = re.compile(r'(?:res\.redirect|redirect)\s*\(\s*[\'"`](https?://[^\'"`]+)[\'"`]')

    for repo, content in rows:
        for match in redirect_pattern.finditer(content):
            url = match.group(1)
            target_repo = _resolve_url_to_repo(url, domain_map)
            if target_repo and target_repo in known_repos and target_repo != repo:
                edges.append((repo, target_repo, "redirect", url))

    unique = list(set(edges))
    if unique:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            unique,
        )
    print(f"  Static redirect edges: {len(unique)}")


def parse_url_reference_edges(conn: sqlite3.Connection):
    """Parse URL patterns in code/config → domain registry → repo.

    Finds internal domain references in code that indicate cross-service communication.
    """
    domain_map = _load_domain_map()
    if not domain_map:
        print("  No domain registry, skipping URL reference parsing")
        return

    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}

    # Get chunks that reference internal service domains
    # Derive search pattern from domain_map keys (e.g., "pay.com", "example.com")
    _domain_suffixes = set()
    for d in domain_map:
        parts = d.split(".")
        if len(parts) >= 2:
            _domain_suffixes.add(".".join(parts[-2:]))
    if not _domain_suffixes:
        print("  No domain suffixes found in registry, skipping URL reference parsing")
        return

    # Build SQL LIKE clauses for each suffix
    like_clauses = " OR ".join(f"content LIKE '%.{s}%'" for s in _domain_suffixes)
    rows = conn.execute(
        f"SELECT repo_name, content FROM chunks WHERE ({like_clauses}) "
        "AND file_type NOT IN ('domain_registry', 'flow_annotation', 'env_map', 'gotchas')"
    ).fetchall()

    edges: list[tuple[str, str, str, str]] = []
    # Build regex pattern from domain suffixes
    _escaped_suffixes = "|".join(re.escape(s) for s in _domain_suffixes)
    url_pattern = re.compile(rf"https?://(\w[\w.-]*\.(?:{_escaped_suffixes}))(?:/\S*)?")

    for repo, content in rows:
        for match in url_pattern.finditer(content):
            domain = match.group(1)
            target_repo = _resolve_domain_to_repo(domain, domain_map)
            if target_repo and target_repo in known_repos and target_repo != repo:
                edges.append((repo, target_repo, "url_reference", domain))

    unique = list(set(edges))
    if unique:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            unique,
        )
    print(f"  URL reference edges: {len(unique)}")


def parse_similar_repo_edges(conn: sqlite3.Connection):
    """Pre-compute similar_repo edges based on shared npm deps and name similarity.

    Two repos are similar if they share:
    - High overlap in {NPM_SCOPE}/* npm dependencies
    - Similar naming pattern (e.g., next-web-pay-with-bank vs next-web-alternative-payment-methods)

    Creates bidirectional similar_repo edges with a description of the difference.
    """
    # Get all repos with their org_deps
    rows = conn.execute("SELECT name, org_deps FROM repos WHERE org_deps IS NOT NULL AND org_deps != '[]'").fetchall()

    repo_deps: dict[str, set[str]] = {}
    for name, deps_json in rows:
        try:
            deps = json.loads(deps_json) if deps_json else []
            repo_deps[name] = set(deps)
        except (json.JSONDecodeError, TypeError):
            continue

    # Also get file tree info from chunks
    repo_files: dict[str, set[str]] = {}
    file_rows = conn.execute("SELECT DISTINCT repo_name, file_path FROM chunks").fetchall()
    for repo, fpath in file_rows:
        repo_files.setdefault(repo, set()).add(fpath)

    # Define repo groups where similarity is meaningful
    # (only compare within same family to avoid n^2 on 533 repos)
    families: dict[str, list[str]] = {}
    for name in repo_deps:
        # Group by prefix: next-web-*, grpc-apm-*, workflow-*, etc.
        parts = name.split("-")
        if len(parts) >= 2:
            prefix = "-".join(parts[:2])
        else:
            prefix = name
        families.setdefault(prefix, []).append(name)

    edges: list[tuple[str, str, str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for _prefix, repos in families.items():
        if len(repos) < 2 or len(repos) > 20:
            continue  # Skip singletons and huge families (grpc-providers-*)

        for i, repo_a in enumerate(repos):
            deps_a = repo_deps.get(repo_a, set())
            files_a = repo_files.get(repo_a, set())

            for repo_b in repos[i + 1 :]:
                pair = tuple(sorted([repo_a, repo_b]))
                if pair in seen_pairs:
                    continue

                deps_b = repo_deps.get(repo_b, set())
                files_b = repo_files.get(repo_b, set())

                # Compute Jaccard similarity on deps
                if deps_a and deps_b:
                    intersection = deps_a & deps_b
                    union = deps_a | deps_b
                    dep_sim = len(intersection) / len(union) if union else 0
                else:
                    dep_sim = 0

                # Compute file structure similarity
                if files_a and files_b:
                    # Compare file basenames
                    basenames_a = {f.rsplit("/", 1)[-1] for f in files_a}
                    basenames_b = {f.rsplit("/", 1)[-1] for f in files_b}
                    file_intersection = basenames_a & basenames_b
                    file_union = basenames_a | basenames_b
                    file_sim = len(file_intersection) / len(file_union) if file_union else 0
                else:
                    file_sim = 0

                # Combined similarity (weighted)
                combined = 0.6 * dep_sim + 0.4 * file_sim

                if combined > 0.5:  # Threshold
                    seen_pairs.add(pair)
                    detail = f"similarity={combined:.2f} deps={dep_sim:.2f} files={file_sim:.2f}"
                    # Bidirectional
                    edges.append((repo_a, repo_b, "similar_repo", detail))
                    edges.append((repo_b, repo_a, "similar_repo", detail))

    if edges:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            edges,
        )

    print(f"  Similar repo edges: {len(edges) // 2} pairs")


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


def parse_manual_edges(conn: sqlite3.Connection, group: str):
    """Create graph edges from conventions.yaml manual_edges entries.

    Each group (e.g. 'connection_validation', 'merchant_entity') is a list of
    {source, target, edge_type, detail} dicts. Repo name placeholders like
    {gateway_repo} and {feature_repo} are resolved from conventions config.
    """
    edge_defs = MANUAL_EDGES.get(group, [])
    if not edge_defs:
        print(f"  {group} edges: 0 (no config)")
        return

    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}
    edges: list[tuple[str, str, str, str]] = []

    for entry in edge_defs:
        source = _resolve_repo_ref(entry["source"])
        target = _resolve_repo_ref(entry["target"])
        if source in known_repos and target in known_repos:
            edges.append((source, target, entry["edge_type"], entry.get("detail", "")))

    if edges:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            edges,
        )

    print(f"  {group} edges: {len(edges)}")


def build_package_repo_map(conn: sqlite3.Connection):
    """Build a reverse map: {NPM_SCOPE} package → list of repos that use it.

    Creates package_usage chunks in the index so that searching for a package
    name shows which repos have it as a dependency. This helps discover where
    a gRPC client package is available (e.g., "which repos use {NPM_SCOPE}/settlement-account?").
    """
    scope_prefix = f"{NPM_SCOPE}/"
    package_to_repos: dict[str, list[str]] = {}

    repos = conn.execute("SELECT name, org_deps FROM repos WHERE org_deps IS NOT NULL").fetchall()

    for repo_name, deps_json in repos:
        deps = json.loads(deps_json) if deps_json else []
        for dep in deps:
            pkg_short = dep.replace(scope_prefix, "") if dep.startswith(scope_prefix) else dep
            if dep.startswith(scope_prefix):
                package_to_repos.setdefault(pkg_short, []).append(repo_name)

    # Insert as chunks for search
    count = 0
    for pkg, repo_list in sorted(package_to_repos.items()):
        repos_str = ", ".join(sorted(repo_list))
        content = f"Package {NPM_SCOPE}/{pkg} is used by {len(repo_list)} repos: {repos_str}"
        conn.execute(
            "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) VALUES (?, ?, ?, ?, ?, ?)",
            (content, f"pkg-{pkg}", f"package-map/{pkg}", "package_usage", "package_map", ""),
        )
        count += 1

    print(f"  Package-to-repo map: {count} packages indexed")


def main():
    print("Building dependency graph...")
    conn = sqlite3.connect(str(DB_PATH))

    print("\n1. Creating graph tables...")
    init_graph_tables(conn)

    try:
        print("\n2. Populating nodes...")
        populate_nodes(conn)

        print("\n3. Parsing gRPC URL edges...")
        parse_grpc_url_edges(conn)

        print("\n4. Parsing npm dependency edges...")
        parse_npm_dep_edges(conn)

        print("\n5. Parsing K8s env edges...")
        parse_k8s_env_edges(conn)

        print("\n6. Parsing proto import edges...")
        parse_proto_import_edges(conn)

        print("\n7. Parsing Temporal workflow edges...")
        parse_temporal_edges(conn)

        print("\n8. Parsing webhook edges...")
        parse_webhook_edges(conn)

        print("\n9. Parsing gRPC client require() edges...")
        parse_grpc_client_require_edges(conn)

        print("\n10. Parsing gRPC method-level call edges...")
        parse_grpc_method_call_edges(conn)

        print("\n11. Parsing HTTP/fetch call edges...")
        parse_fetch_edges(conn)

        print("\n12. Parsing proto field/message/service edges...")
        parse_proto_field_edges(conn)

        print("\n13. Parsing Express route definitions...")
        parse_express_routes(conn)

        print("\n14. Parsing domain registry edges...")
        parse_domain_registry_edges(conn)

        print("\n15. Parsing flow annotation edges...")
        parse_flow_annotation_edges(conn)

        print("\n16. Parsing static redirect edges...")
        parse_redirect_edges(conn)

        print("\n17. Parsing URL reference edges...")
        parse_url_reference_edges(conn)

        print("\n18. Computing similar repo edges...")
        parse_similar_repo_edges(conn)

        print("\n19. Parsing connection validation edges...")
        parse_manual_edges(conn, "connection_validation")

        print("\n20. Parsing merchant entity edges...")
        parse_manual_edges(conn, "merchant_entity")

        print("\n21. Gateway runtime routing edges...")
        if GATEWAY_REPO:
            known = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}
            provider_repos = [r for r in known if any(r.startswith(p) for p in PROVIDER_PREFIXES)]
            rt_edges = []
            for pr in provider_repos:
                rt_edges.append((GATEWAY_REPO, pr, "runtime_routing", "gateway routes to provider at runtime"))
            if rt_edges:
                conn.executemany(
                    "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?,?,?,?)", rt_edges
                )
            print(f"  Runtime routing: {len(rt_edges)} provider routes")

        print("\n22. Building package-to-repo map...")
        build_package_repo_map(conn)

        # Commit all graph data in a single transaction
        conn.commit()

    except Exception:
        conn.rollback()
        conn.close()
        print("\nERROR: Graph build failed — rolled back all changes.")
        raise

    print("\n23. Building env var index...")
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("build_env_index", Path(__file__).parent / "build_env_index.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        conn.close()  # build_env_index manages its own connection
        mod.build_env_index()
        conn = sqlite3.connect(str(DB_PATH))
    except Exception as e:
        print(f"  Env var index failed: {e}")

    print_summary(conn)

    conn.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
