"""gRPC edge parsers: URL env vars, client require(), method-level calls."""

import re
import sqlite3

from ._common import NPM_SCOPE, PROVIDER_PREFIXES


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
