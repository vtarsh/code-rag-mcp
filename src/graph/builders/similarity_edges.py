"""Pre-compute similar_repo edges based on shared deps and file structure."""

import json
import sqlite3


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
