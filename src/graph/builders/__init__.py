"""Build dependency graph orchestrator.

Wires up all parser modules in the order they ran in the original
`scripts/build_graph.py`. Output is identical (same prints, same SQL).
"""

import sqlite3
from pathlib import Path

from ._common import DB_PATH, GATEWAY_REPO, PROVIDER_PREFIXES
from .db import init_graph_tables, populate_nodes, print_summary
from .domain_edges import (
    parse_domain_registry_edges,
    parse_flow_annotation_edges,
    parse_redirect_edges,
    parse_url_reference_edges,
)
from .express_edges import parse_express_routes, parse_fetch_edges
from .grpc_edges import (
    parse_grpc_client_require_edges,
    parse_grpc_method_call_edges,
    parse_grpc_url_edges,
)
from .k8s_edges import parse_k8s_env_edges
from .manual_edges import parse_manual_edges
from .npm_edges import parse_npm_dep_edges
from .pkg_resolution import build_package_repo_map, resolve_pkg_edges
from .proto_edges import parse_proto_field_edges, parse_proto_import_edges
from .similarity_edges import parse_similar_repo_edges
from .temporal_edges import parse_temporal_edges
from .webhook_edges import parse_webhook_edges


def build_graph():
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

        print("\n22. Resolving pkg: virtual node edges...")
        resolve_pkg_edges(conn)

        print("\n23. Building package-to-repo map...")
        build_package_repo_map(conn)

        # Commit all graph data in a single transaction
        conn.commit()

    except Exception:
        conn.rollback()
        conn.close()
        print("\nERROR: Graph build failed — rolled back all changes.")
        raise

    print("\n24. Building env var index...")
    try:
        import importlib.util

        # Resolve scripts/build_env_index.py relative to repo root
        # __file__ = .../src/graph/builders/__init__.py → repo_root = parents[3]
        scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
        spec = importlib.util.spec_from_file_location("build_env_index", scripts_dir / "build_env_index.py")
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


__all__ = ["build_graph"]
