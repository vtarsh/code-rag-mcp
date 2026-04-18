"""npm dependency edges with subtype classification."""

import json
import sqlite3

from ._common import (
    _PROTO_PACKAGES,
    _TOOLING_PACKAGES,
    NPM_SCOPE,
    PROTO_REPOS,
    PROVIDER_PREFIXES,
)


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
