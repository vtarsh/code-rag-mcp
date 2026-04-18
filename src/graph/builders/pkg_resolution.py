"""Resolve pkg: virtual targets to direct repo edges; build package→repo map."""

import json
import sqlite3

from ._common import (
    NPM_SCOPE,
    PKG_RESOLUTION_MAP,
    PKG_RESOLUTION_PREFIXES,
    PKG_RESOLUTION_SUFFIXES,
)


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


def resolve_pkg_edges(conn: sqlite3.Connection):
    """Resolve pkg: virtual node edges to direct repo edges.

    For each edge with a pkg:@scope/X target, try to find the real repo using:
    1. Explicit overrides from pkg_resolution_map (conventions.yaml)
    2. Prefix matching: {prefix}{pkg_name} (e.g., node-cli-envsub)
    3. Suffix matching: {pkg_name}{suffix} (e.g., microfrontends-web)

    Creates a parallel direct edge (source → real_repo) with the same edge_type
    and detail. The original pkg: edge is kept for provenance.
    """
    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    # Get all edges with pkg: targets
    pkg_edges = conn.execute("SELECT DISTINCT target FROM graph_edges WHERE target LIKE 'pkg:%'").fetchall()

    resolved_count = 0
    edge_count = 0

    for row in pkg_edges:
        pkg_target = row[0]
        # Extract package name: pkg:@pay-com/envsub -> envsub
        pkg_name = pkg_target.split("/")[-1] if "/" in pkg_target else pkg_target.replace("pkg:", "")

        # 1. Check explicit map first
        real_repo = None
        if pkg_name in PKG_RESOLUTION_MAP:
            candidate = PKG_RESOLUTION_MAP[pkg_name]
            if candidate in repo_names:
                real_repo = candidate

        # 2. Try prefix matching
        if not real_repo:
            for prefix in PKG_RESOLUTION_PREFIXES:
                candidate = f"{prefix}{pkg_name}"
                if candidate in repo_names:
                    real_repo = candidate
                    break

        # 3. Try suffix matching
        if not real_repo:
            for suffix in PKG_RESOLUTION_SUFFIXES:
                candidate = f"{pkg_name}{suffix}"
                if candidate in repo_names:
                    real_repo = candidate
                    break

        if not real_repo:
            continue

        resolved_count += 1

        # Create direct edges for all sources that point to this pkg: target
        sources = conn.execute(
            "SELECT source, edge_type, detail FROM graph_edges WHERE target = ?",
            (pkg_target,),
        ).fetchall()

        for src in sources:
            if src[0] != real_repo:  # Don't create self-edges
                conn.execute(
                    "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
                    (src[0], real_repo, src[1], src[2]),
                )
                edge_count += 1

    unresolved = len(pkg_edges) - resolved_count
    print(f"  Resolved {resolved_count}/{len(pkg_edges)} pkg: targets → {edge_count} direct edges added")
    if unresolved:
        # Show which packages couldn't be resolved
        for row in pkg_edges:
            pkg_target = row[0]
            pkg_name = pkg_target.split("/")[-1] if "/" in pkg_target else pkg_target.replace("pkg:", "")
            found = pkg_name in PKG_RESOLUTION_MAP
            if not found:
                for prefix in PKG_RESOLUTION_PREFIXES:
                    if f"{prefix}{pkg_name}" in repo_names:
                        found = True
                        break
            if not found:
                for suffix in PKG_RESOLUTION_SUFFIXES:
                    if f"{pkg_name}{suffix}" in repo_names:
                        found = True
                        break
            if not found:
                print(f"    Unresolved: {pkg_target}")
