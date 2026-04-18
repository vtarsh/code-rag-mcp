"""Express route definitions and HTTP/fetch call edges."""

import re
import sqlite3


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
