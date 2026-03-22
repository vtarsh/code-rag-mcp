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


def extract_repo_and_method(file_path: str) -> tuple[str, str | None]:
    """Extract repo name and method name from a file path like grpc-apm-trustly/methods/verification.js."""
    parts = file_path.strip("/").split("/")
    repo = parts[0] if parts else file_path
    method = None
    if len(parts) >= 2 and parts[-1].endswith(".js"):
        method = parts[-1].replace(".js", "")
    elif len(parts) >= 2 and parts[-1].endswith(".ts"):
        method = parts[-1].replace(".ts", "")
    return repo, method


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


def analyze_file(conn: sqlite3.Connection, file_path: str) -> None:
    """Analyze impact of changing a single file."""
    repo, method = extract_repo_and_method(file_path)

    print(f"\n\U0001f4c4 {file_path}")
    if method:
        print(f"   Method: {method}")
    else:
        print(f"   Repo: {repo}")
        print("   (Could not extract method name from path)")
        return

    is_provider_repo = (
        repo.startswith("grpc-apm-") or repo.startswith("grpc-providers-") or repo.startswith("grpc-card-")
    )
    is_gateway = repo == "grpc-payment-gateway"
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
            # Show first few, then count
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
