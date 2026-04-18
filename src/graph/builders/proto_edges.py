"""Proto file parsing: imports and message/service/usage edges."""

import re
import sqlite3

from ._common import (
    _GENERIC_MESSAGE_NAMES,
    _WELL_KNOWN_TYPES,
    EXTRACTED_PATH,
    NPM_SCOPE,
    PROTO_REPOS,
    _parse_proto_file,
)


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
