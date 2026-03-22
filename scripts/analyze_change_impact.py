#!/usr/bin/env python3
"""Change Impact Analyzer: file change -> exact consumers that call the changed method."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".pay-knowledge")) / "db" / "knowledge.db"

PROVIDER_SERVICE_METHODS = {
    "sale",
    "payout",
    "refund",
    "authorization",
    "completion",
    "cancellation",
    "initialize",
    "verification",
}


def get_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


class FileClassification:
    """Classified file with repo, type, and extracted metadata."""

    def __init__(self, repo: str, file_type: str, file_path: str, **kwargs: str | None) -> None:
        self.repo = repo
        self.file_type = file_type  # "method", "activity", "lib", "route", "other"
        self.file_path = file_path
        self.method: str | None = kwargs.get("method")
        self.provider: str | None = kwargs.get("provider")
        self.lib_name: str | None = kwargs.get("lib_name")


def classify_file(file_path: str) -> FileClassification:
    """Classify a file path into type and extract relevant metadata."""
    parts = file_path.strip("/").split("/")
    repo = parts[0] if parts else file_path
    rel_parts = parts[1:]  # path within repo

    # Strip extension for name extraction
    def strip_ext(name: str) -> str:
        for ext in (".js", ".ts", ".mjs", ".mts"):
            if name.endswith(ext):
                return name[: -len(ext)]
        return name

    # methods/*.js → method consumer lookup
    if len(rel_parts) >= 2 and rel_parts[0] == "methods":
        return FileClassification(repo, "method", file_path, method=strip_ext(rel_parts[-1]))

    # activities/{provider}/... → webhook flow + provider repo lookup
    if len(rel_parts) >= 2 and rel_parts[0] == "activities":
        provider = rel_parts[1]
        return FileClassification(repo, "activity", file_path, provider=provider)

    # libs/*.js → shared utility
    if len(rel_parts) >= 1 and rel_parts[0] == "libs":
        return FileClassification(repo, "lib", file_path, lib_name=strip_ext(rel_parts[-1]))

    # src/routes/*.js → API route
    if len(rel_parts) >= 2 and rel_parts[0] == "src" and rel_parts[1] == "routes":
        return FileClassification(repo, "route", file_path)

    # Fallback: try to extract a method name for backward compat
    method = None
    if rel_parts and (rel_parts[-1].endswith(".js") or rel_parts[-1].endswith(".ts")):
        method = strip_ext(rel_parts[-1])
    return FileClassification(repo, "other", file_path, method=method)


def extract_repo_and_method(file_path: str) -> tuple[str, str | None]:
    """Extract repo name and method name from a file path like grpc-apm-trustly/methods/verification.js.

    Kept for backward compatibility — delegates to classify_file for method files.
    """
    cl = classify_file(file_path)
    return cl.repo, cl.method


def find_direct_consumers(conn: sqlite3.Connection, repo: str, method: str | None) -> list[dict]:
    """Find repos that directly call a method on this repo via grpc_method_call edges."""
    consumers = []
    if method:
        # Look for calls matching the method name in the detail field
        # Detail format: "service-name::methodName" or similar
        rows = conn.execute(
            """SELECT source, target, detail FROM graph_edges
               WHERE target = ? AND edge_type = 'grpc_method_call' AND detail LIKE ?""",
            (repo, f"%{method}%"),
        ).fetchall()
        for r in rows:
            consumers.append(
                {
                    "caller": r["source"],
                    "target": r["target"],
                    "detail": r["detail"],
                }
            )
    if not consumers and method:
        # Also check if this repo is called indirectly via gateway routing
        # e.g., callers -> grpc-payment-gateway::method -> runtime_routing -> this repo
        rows = conn.execute(
            """SELECT source, target, detail FROM graph_edges
               WHERE target = 'grpc-payment-gateway' AND edge_type = 'grpc_method_call'
               AND detail LIKE ?""",
            (f"%{method}%",),
        ).fetchall()
        for r in rows:
            consumers.append(
                {
                    "caller": r["source"],
                    "target": "grpc-payment-gateway",
                    "detail": r["detail"],
                    "indirect": True,
                }
            )
    return consumers


def find_webhook_connections(conn: sqlite3.Connection, repo: str, provider: str | None = None) -> list[dict]:
    """Find webhook dispatch and handler edges related to a repo or provider."""
    results: list[dict] = []

    # Direct edges where this repo is source or target
    # If provider specified, filter by detail to avoid showing ALL 59 webhook edges
    if provider:
        rows = conn.execute(
            """SELECT source, target, edge_type, detail FROM graph_edges
               WHERE (source = ? OR target = ?) AND edge_type IN ('webhook_dispatch', 'webhook_handler')
               AND (detail = ? OR detail IS NULL)""",
            (repo, repo, provider),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT source, target, edge_type, detail FROM graph_edges
               WHERE (source = ? OR target = ?) AND edge_type IN ('webhook_dispatch', 'webhook_handler')""",
            (repo, repo),
        ).fetchall()
    for r in rows:
        results.append(
            {
                "source": r["source"],
                "target": r["target"],
                "edge_type": r["edge_type"],
                "detail": r["detail"],
            }
        )

    # If we have a provider name, also look for edges matching that provider
    if provider:
        rows = conn.execute(
            """SELECT source, target, edge_type, detail FROM graph_edges
               WHERE detail = ? AND edge_type IN ('webhook_dispatch', 'webhook_handler')""",
            (provider,),
        ).fetchall()
        for r in rows:
            entry = {
                "source": r["source"],
                "target": r["target"],
                "edge_type": r["edge_type"],
                "detail": r["detail"],
            }
            if entry not in results:
                results.append(entry)

    return results


def find_activity_impact(conn: sqlite3.Connection, provider: str) -> dict:
    """Find impact of changing an activities file for a given provider."""
    result: dict = {"provider": provider, "apm_repo": None, "webhook_chain": []}

    # Find the corresponding APM/provider repo
    for prefix in ("grpc-apm-", "grpc-providers-", "grpc-card-"):
        candidate = f"{prefix}{provider}"
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM graph_edges WHERE source = ? OR target = ?",
            (candidate, candidate),
        ).fetchone()["cnt"]
        if count > 0:
            result["apm_repo"] = candidate
            break

    # Find webhook dispatch chain: express-webhooks -> workflow-provider-webhooks (for this provider)
    rows = conn.execute(
        """SELECT source, target, edge_type FROM graph_edges
           WHERE detail = ? AND edge_type IN ('webhook_dispatch', 'webhook_handler')
           ORDER BY edge_type""",
        (provider,),
    ).fetchall()
    for r in rows:
        result["webhook_chain"].append(
            {
                "source": r["source"],
                "target": r["target"],
                "edge_type": r["edge_type"],
            }
        )

    return result


def find_repo_dependents(conn: sqlite3.Connection, repo: str) -> list[dict]:
    """Find repos that depend on this repo (npm_dep, grpc_call, grpc_client_usage)."""
    rows = conn.execute(
        """SELECT DISTINCT source, edge_type, detail FROM graph_edges
           WHERE target = ? AND edge_type IN ('npm_dep', 'grpc_call', 'grpc_client_usage', 'grpc_method_call')
           ORDER BY edge_type, source""",
        (repo,),
    ).fetchall()
    return [{"source": r["source"], "edge_type": r["edge_type"], "detail": r["detail"]} for r in rows]


def find_route_callers(conn: sqlite3.Connection, repo: str) -> list[dict]:
    """Find upstream callers of an express service (repos that call it via grpc or http)."""
    rows = conn.execute(
        """SELECT DISTINCT source, edge_type, detail FROM graph_edges
           WHERE target = ? AND edge_type IN ('grpc_call', 'grpc_client_usage', 'grpc_method_call')
           ORDER BY source""",
        (repo,),
    ).fetchall()
    return [{"source": r["source"], "edge_type": r["edge_type"], "detail": r["detail"]} for r in rows]


def find_express_routes(conn: sqlite3.Connection, repo: str) -> list[str]:
    """Find express routes defined by a repo."""
    rows = conn.execute(
        """SELECT detail FROM graph_edges
           WHERE source = ? AND edge_type = 'express_route'
           ORDER BY detail""",
        (repo,),
    ).fetchall()
    return [r["detail"] for r in rows]


def find_gateway_fanout(conn: sqlite3.Connection, method: str) -> list[str]:
    """For ProviderService methods, find all provider repos routed from gateway."""
    rows = conn.execute(
        """SELECT DISTINCT target FROM graph_edges
           WHERE source = 'grpc-payment-gateway' AND edge_type = 'runtime_routing'
           ORDER BY target""",
    ).fetchall()
    return [r["target"] for r in rows]


def find_gateway_callers(conn: sqlite3.Connection, method: str) -> list[dict]:
    """Find who calls grpc-payment-gateway for a specific method."""
    rows = conn.execute(
        """SELECT source, detail FROM graph_edges
           WHERE target = 'grpc-payment-gateway' AND edge_type = 'grpc_method_call'
           AND detail LIKE ?""",
        (f"%{method}%",),
    ).fetchall()
    return [{"caller": r["source"], "detail": r["detail"]} for r in rows]


def find_proto_impact(conn: sqlite3.Connection, method: str, repo: str | None = None) -> dict | None:
    """Check if this method is defined in a proto service.

    Prioritizes providers-proto for provider repos/gateway, then falls back to other protos.
    """
    rows = conn.execute(
        """SELECT source, target, detail FROM graph_edges
           WHERE edge_type = 'proto_service_def' AND detail LIKE ?""",
        (f"%{method}%",),
    ).fetchall()

    is_provider_context = repo and (
        repo.startswith("grpc-apm-")
        or repo.startswith("grpc-providers-")
        or repo.startswith("grpc-card-")
        or repo == "grpc-payment-gateway"
    )

    # Sort: providers-proto first if in provider context
    candidates = []
    for r in rows:
        methods_str = r["detail"]
        svc_match = re.search(r"svc:[\w.]+\.(\w+)", r["target"] or "")
        service_name = svc_match.group(1) if svc_match else r["target"]
        methods_list = [m.strip() for m in methods_str.split(",")]
        if method in methods_list:
            candidates.append((r, service_name, methods_list))

    # Prefer providers-proto for provider/gateway repos
    if is_provider_context:
        candidates.sort(key=lambda c: 0 if c[0]["source"] == "providers-proto" else 1)

    for r, service_name, methods_list in candidates:
        proto_repo = r["source"]
        dep_count = conn.execute(
            """SELECT COUNT(DISTINCT source) as cnt FROM graph_edges
               WHERE target = ? AND edge_type = 'npm_dep_proto'""",
            (proto_repo,),
        ).fetchone()["cnt"]
        return {
            "proto_repo": proto_repo,
            "service_name": service_name,
            "all_methods": methods_list,
            "dep_count": dep_count,
        }
    return None


def get_repo_methods(conn: sqlite3.Connection, repo: str) -> list[str]:
    """Get all method files for a repo by checking code_facts and graph edges."""
    methods = set()

    # From grpc_method_call edges where this repo is called
    rows = conn.execute(
        """SELECT DISTINCT detail FROM graph_edges
           WHERE target = ? AND edge_type = 'grpc_method_call'""",
        (repo,),
    ).fetchall()
    for r in rows:
        detail = r["detail"] or ""
        # Extract method name from "service-name::methodName"
        if "::" in detail:
            methods.add(detail.split("::")[-1])

    # From proto_service_def if this repo defines services
    rows = conn.execute(
        """SELECT detail FROM graph_edges
           WHERE source = ? AND edge_type = 'proto_service_def'""",
        (repo,),
    ).fetchall()
    for r in rows:
        detail = r["detail"] or ""
        # Remove the svc: prefix part if present
        for m in detail.split(","):
            m = m.strip()
            if m and not m.startswith("svc:") and not m.startswith("("):
                methods.add(m)

    # Check if it's a provider repo -> it implements ProviderService methods
    is_provider = (
        conn.execute(
            """SELECT COUNT(*) as cnt FROM graph_edges
           WHERE target = ? AND source = 'grpc-payment-gateway' AND edge_type = 'runtime_routing'""",
            (repo,),
        ).fetchone()["cnt"]
        > 0
    )

    if is_provider:
        methods.update(PROVIDER_SERVICE_METHODS)

    return sorted(methods)


def _extract_provider_from_repo(repo: str) -> str | None:
    """Extract provider name from a repo name like grpc-apm-trustly -> trustly."""
    for prefix in ("grpc-apm-", "grpc-providers-", "grpc-card-"):
        if repo.startswith(prefix):
            return repo[len(prefix) :]
    return None


def analyze_file(conn: sqlite3.Connection, file_path: str) -> None:
    """Analyze impact of changing a single file."""
    cl = classify_file(file_path)
    repo = cl.repo

    print(f"\n\U0001f4c4 {file_path}")
    print(f"   Repo: {repo}")
    print(f"   File type: {cl.file_type}")

    is_provider_repo = (
        repo.startswith("grpc-apm-") or repo.startswith("grpc-providers-") or repo.startswith("grpc-card-")
    )
    is_webhook_repo = repo in ("workflow-provider-webhooks", "express-webhooks")
    is_gateway = repo == "grpc-payment-gateway"

    if cl.file_type == "method":
        _analyze_method_file(conn, cl, is_provider_repo, is_gateway)
    elif cl.file_type == "activity":
        _analyze_activity_file(conn, cl)
    elif cl.file_type == "lib":
        _analyze_lib_file(conn, cl, is_provider_repo)
    elif cl.file_type == "route":
        _analyze_route_file(conn, cl)
    else:
        _analyze_other_file(conn, cl, is_provider_repo, is_webhook_repo)

    # Webhook flow for provider and webhook repos — skip if activity file already showed chain
    if cl.file_type != "activity" and (is_provider_repo or is_webhook_repo):
        provider_name = cl.provider or _extract_provider_from_repo(repo)
        wh_connections = find_webhook_connections(conn, repo, provider_name)
        if wh_connections:
            print("\n   Webhook flow:")
            for wh in wh_connections:
                arrow = "->" if wh["edge_type"] == "webhook_dispatch" else "<-"
                print(f"     {wh['source']} {arrow} {wh['target']} [{wh['edge_type']}] ({wh['detail']})")


def _analyze_method_file(
    conn: sqlite3.Connection,
    cl: FileClassification,
    is_provider_repo: bool,
    is_gateway: bool,
) -> None:
    """Analyze a methods/*.js file (existing logic)."""
    repo = cl.repo
    method = cl.method
    print(f"   Method: {method}")

    if not method:
        return

    is_provider_method = method in PROVIDER_SERVICE_METHODS

    # Check proto interface
    proto_info = find_proto_impact(conn, method, repo)
    if proto_info and is_provider_method:
        print(f"   Interface: {proto_info['service_name']} (proto)")

    # Direct consumers
    consumers = find_direct_consumers(conn, repo, method)
    if consumers:
        has_direct = any(not c.get("indirect") for c in consumers)
        has_indirect = any(c.get("indirect") for c in consumers)

        if has_direct:
            print("\n   Direct consumers (grpc_method_call):")
            for c in consumers:
                if not c.get("indirect"):
                    print(f"     -> {c['caller']} calls {c['detail']}")

        if has_indirect:
            print("\n   Indirect consumers (via grpc-payment-gateway):")
            for c in consumers:
                if c.get("indirect"):
                    print(f"     -> {c['caller']} calls {c['detail']}")
    else:
        print("\n   Direct consumers: (none found)")

    # For provider repos: gateway always routes to them
    if is_provider_repo and is_provider_method:
        print("\n   Gateway routing:")
        print(f"     -> grpc-payment-gateway calls {method} (via runtime_routing)")

        # Show who calls gateway for this method
        gw_callers = find_gateway_callers(conn, method)
        if gw_callers:
            print("\n   Upstream callers (via gateway):")
            for gc in gw_callers:
                print(f"     -> {gc['caller']} calls {gc['detail']}")

    # For gateway methods: show fan-out to all providers
    if is_gateway and is_provider_method:
        providers = find_gateway_fanout(conn, method)
        if providers:
            print("\n   Gateway fan-out (ProviderService method):")
            print(f"     All providers implementing {method}():")
            display = providers[:8]
            remaining = len(providers) - len(display)
            print(f"       {', '.join(display)}{'...' if remaining > 0 else ''}")
            if remaining > 0:
                print(f"       (and {remaining} more providers)")
            print(
                f"     \u26a0\ufe0f  If you changed the {method} CONTRACT (params/return), these {len(providers)} providers may need updates"
            )

    # Proto impact
    if proto_info:
        print("\n   Proto impact:")
        print(f"     {method} is defined in {proto_info['proto_repo']} {proto_info['service_name']}")
        if proto_info["dep_count"] > 0:
            print(
                f"     Changing signature affects: {proto_info['dep_count']} repos depending on {proto_info['proto_repo']}"
            )

    # Warning for direct consumers
    direct_count = len([c for c in consumers if not c.get("indirect")])
    indirect_count = len([c for c in consumers if c.get("indirect")])
    total = direct_count + indirect_count
    if is_provider_repo and is_provider_method:
        gw_callers = find_gateway_callers(conn, method)
        total += len(gw_callers)
    if total > 0 and not is_gateway:
        print(f"\n   \u26a0\ufe0f  {total} consumer(s) MUST be checked if you changed the method signature")


def _analyze_activity_file(conn: sqlite3.Connection, cl: FileClassification) -> None:
    """Analyze an activities/{provider}/*.js file."""
    provider = cl.provider
    if not provider:
        print("   (Could not extract provider name from activity path)")
        return

    print(f"   Provider: {provider}")

    impact = find_activity_impact(conn, provider)

    # Show corresponding APM repo
    if impact["apm_repo"]:
        print(f"\n   Provider repo: {impact['apm_repo']}")
        print(f"     Activities in {cl.repo} handle webhooks and call {impact['apm_repo']}")
    else:
        print(f"\n   Provider repo: (no grpc-apm-{provider} or grpc-providers-{provider} found)")

    # Show webhook chain
    if impact["webhook_chain"]:
        print("\n   Webhook chain:")
        for step in impact["webhook_chain"]:
            label = "dispatches to" if step["edge_type"] == "webhook_dispatch" else "handled by"
            print(f"     {step['source']} {label} {step['target']}")
    else:
        print(f"\n   Webhook chain: (no webhook edges found for provider '{provider}')")

    # Show what this activity file's repo depends on
    dependents = find_repo_dependents(conn, cl.repo)
    if dependents:
        # Group by edge type
        by_type: dict[str, list[str]] = {}
        for d in dependents:
            by_type.setdefault(d["edge_type"], []).append(d["source"])
        print(f"\n   Repos that depend on {cl.repo}:")
        for etype, sources in by_type.items():
            print(f"     [{etype}]: {', '.join(sources[:5])}")
            if len(sources) > 5:
                print(f"       (and {len(sources) - 5} more)")


def _analyze_lib_file(conn: sqlite3.Connection, cl: FileClassification, is_provider_repo: bool) -> None:
    """Analyze a libs/*.js file."""
    lib_name = cl.lib_name
    print(f"   Lib: {lib_name}")
    print(f"\n   Shared utility in {cl.repo}")
    print("     Changes may affect ALL consumers of this repo.")

    # Show repos that depend on this repo
    dependents = find_repo_dependents(conn, cl.repo)
    if dependents:
        by_type: dict[str, list[str]] = {}
        for d in dependents:
            by_type.setdefault(d["edge_type"], []).append(d["source"])
        print(f"\n   Repos depending on {cl.repo}:")
        for etype, sources in by_type.items():
            print(f"     [{etype}]: {', '.join(sources[:8])}")
            if len(sources) > 8:
                print(f"       (and {len(sources) - 8} more)")

    # For provider repos, show gateway routing
    if is_provider_repo:
        # provider context for this lib
        print(f"\n   Provider context: {cl.repo}")
        print(f"     This lib is used by methods in {cl.repo}")
        print("     If it changes response mapping/validation, all methods using it are affected")

        # Show which methods this repo implements
        methods = get_repo_methods(conn, cl.repo)
        if methods:
            print(f"     Repo methods: {', '.join(methods)}")

    # Check if lib_name appears in code_facts for other repos (cross-repo references)
    if lib_name:
        rows = conn.execute(
            """SELECT DISTINCT repo_name FROM code_facts
               WHERE (raw_snippet LIKE ? OR file_path LIKE ?) AND repo_name != ?
               LIMIT 10""",
            (f"%{lib_name}%", f"%{lib_name}%", cl.repo),
        ).fetchall()
        if rows:
            print(f"\n   Cross-repo references to '{lib_name}':")
            for r in rows:
                print(f"     -> {r['repo_name']}")


def _analyze_route_file(conn: sqlite3.Connection, cl: FileClassification) -> None:
    """Analyze a src/routes/*.js file."""
    print(f"\n   API route file in {cl.repo}")

    # Show defined routes
    routes = find_express_routes(conn, cl.repo)
    if routes:
        print(f"\n   Defined routes ({len(routes)} total):")
        for route in routes[:10]:
            print(f"     {route}")
        if len(routes) > 10:
            print(f"     (and {len(routes) - 10} more)")

    # Show upstream callers
    callers = find_route_callers(conn, cl.repo)
    if callers:
        print("\n   Upstream callers:")
        for c in callers:
            print(f"     -> {c['source']} [{c['edge_type']}]")
    else:
        print("\n   Upstream callers: (none found — may receive external HTTP traffic)")

    # Show downstream dependencies
    downstream = conn.execute(
        """SELECT DISTINCT target, edge_type, detail FROM graph_edges
           WHERE source = ? AND edge_type IN ('grpc_call', 'grpc_client_usage', 'grpc_method_call', 'webhook_dispatch')
           ORDER BY edge_type, target""",
        (cl.repo,),
    ).fetchall()
    if downstream:
        print(f"\n   Downstream services called by {cl.repo}:")
        for d in downstream:
            print(f"     -> {d['target']} [{d['edge_type']}]")


def _analyze_other_file(
    conn: sqlite3.Connection,
    cl: FileClassification,
    is_provider_repo: bool,
    is_webhook_repo: bool,
) -> None:
    """Analyze any other file type — show basic repo dependencies."""
    print(f"\n   General file in {cl.repo}")

    # Show basic repo dependency info
    dependents = find_repo_dependents(conn, cl.repo)
    if dependents:
        by_type: dict[str, list[str]] = {}
        for d in dependents:
            by_type.setdefault(d["edge_type"], []).append(d["source"])
        print(f"\n   Repos depending on {cl.repo}:")
        for etype, sources in by_type.items():
            print(f"     [{etype}]: {', '.join(sources[:8])}")
            if len(sources) > 8:
                print(f"       (and {len(sources) - 8} more)")

    # Show downstream
    downstream = conn.execute(
        """SELECT DISTINCT target, edge_type FROM graph_edges
           WHERE source = ? AND edge_type IN ('grpc_call', 'grpc_client_usage', 'grpc_method_call', 'npm_dep', 'webhook_dispatch')
           ORDER BY edge_type, target""",
        (cl.repo,),
    ).fetchall()
    if downstream:
        by_type_ds: dict[str, list[str]] = {}
        for d in downstream:
            by_type_ds.setdefault(d["edge_type"], []).append(d["target"])
        print(f"\n   Downstream dependencies of {cl.repo}:")
        for etype, targets in by_type_ds.items():
            # Filter virtual nodes
            real = [t for t in targets if not t.startswith("pkg:") and not t.startswith("route:")]
            if real:
                print(f"     [{etype}]: {', '.join(real[:8])}")
                if len(real) > 8:
                    print(f"       (and {len(real) - 8} more)")

    if not dependents and not downstream:
        print("   No dependency edges found for this repo.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze change impact: file changes -> exact consumers")
    parser.add_argument(
        "--files",
        help="Comma-separated file paths (e.g., grpc-apm-trustly/methods/verification.js)",
    )
    parser.add_argument(
        "--repo",
        help="Analyze all methods in a repo",
    )
    parser.add_argument(
        "--pr",
        help="PR URL or comma-separated file list from a PR",
    )
    args = parser.parse_args()

    if not args.files and not args.repo and not args.pr:
        parser.print_help()
        sys.exit(1)

    conn = get_db()

    print("=" * 50)
    print("=== Change Impact Analysis ===")
    print("=" * 50)

    if args.files:
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        for f in files:
            analyze_file(conn, f)

    elif args.repo:
        repo = args.repo
        methods = get_repo_methods(conn, repo)
        if not methods:
            print(f"\nNo methods found for repo: {repo}")
            conn.close()
            return
        print(f"\nRepo: {repo}")
        print(f"Methods found: {', '.join(methods)}")
        for m in methods:
            file_path = f"{repo}/methods/{m}.js"
            analyze_file(conn, file_path)

    elif args.pr:
        # Treat as comma-separated file list (PR URL parsing could be added later)
        files = [f.strip() for f in args.pr.split(",") if f.strip()]
        for f in files:
            analyze_file(conn, f)

    print()
    conn.close()


if __name__ == "__main__":
    main()
