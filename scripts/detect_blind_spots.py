#!/usr/bin/env python3
"""Detect repos that are structurally important but invisible to search.

The "drowning" problem: when 60+ similar repos contain the same keywords,
rare-but-important repos (boilerplate, core libraries, hub services) can
get pushed out of search results entirely.

This script:
1. Identifies "important" repos from the graph (boilerplate, hubs, core libs)
2. Derives natural search queries from each repo's name and content
3. Runs those queries through the search pipeline
4. Reports which important repos never appear in results

This is DIAGNOSTIC only — it doesn't change search behavior. It tells you
where to add PHRASE_GLOSSARY rules or improve documentation.

Usage:
    python3 scripts/detect_blind_spots.py
    python3 scripts/detect_blind_spots.py --verbose
    python3 scripts/detect_blind_spots.py --min-dependents=10
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.bench_utils import (
    get_db,
)
from scripts.bench_utils import (
    run_hybrid_search as _run_hybrid_search_base,
)

_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))


def derive_queries_from_content(repo_name: str, conn) -> list[str]:
    """Fallback: derive search queries from indexed content when name is opaque.

    Extracts distinctive keywords from README/docs and package.json description
    to generate queries a developer would naturally use.
    """
    queries = []

    # Try package.json description first (most concise)
    row = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND file_path LIKE '%package.json%' LIMIT 1",
        (repo_name,),
    ).fetchone()
    if row:
        import re as _re

        desc_match = _re.search(r'"description"\s*:\s*"([^"]+)"', row[0])
        if desc_match:
            desc = desc_match.group(1).lower()
            # Remove generic words, keep domain terms
            stop = {"a", "an", "the", "for", "and", "or", "of", "to", "in", "is", "with", "this", "that", "it"}
            words = [w for w in desc.split() if w not in stop and len(w) > 1]
            if len(words) >= 2:
                queries.append(" ".join(words[:5]))

    # Try README content for key phrases
    row = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND file_path LIKE '%README%' LIMIT 1",
        (repo_name,),
    ).fetchone()
    if row:
        # Extract first meaningful sentence (usually describes what the repo is)
        lines = [ln.strip() for ln in row[0].split("\n") if ln.strip() and not ln.strip().startswith("#")]
        for line in lines[:3]:
            words = line.split()
            if len(words) >= 3:
                queries.append(" ".join(words[:6]).lower().rstrip(".,;:"))
                break

    return queries


def derive_queries(repo_name: str, repo_type: str, conn=None) -> list[str]:
    """Generate natural search queries a developer might use to find this repo.

    Two strategies:
    1. Name-based: derive from repo name structure (fast, works for most repos)
    2. Content-based fallback: extract from indexed content (for opaque names)

    Examples:
      grpc-apm-trustly → ["trustly", "apm trustly", "trustly provider"]
      boilerplate-node-providers-grpc-service → ["provider boilerplate", "new provider service"]
      mali → (name-based fails) → content: ["grpc framework", "lightweight grpc middleware"]
    """
    queries = []

    # Remove common prefixes to get the "domain" part
    name = repo_name
    prefixes = [
        "grpc-",
        "express-",
        "workflow-",
        "node-libs-",
        "boilerplate-",
        "cloudflare-workers-",
        "github-workflows-",
    ]
    domain = name
    for prefix in prefixes:
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
            break

    # Further split compound domains
    # Keep short parts if they're domain-meaningful (s3, pg, db, k8s, ci, 3ds, nt)
    MEANINGFUL_SHORT = {"s3", "pg", "db", "k8s", "ci", "3ds", "nt", "mq", "ws", "fx", "ip", "js", "ts", "go"}
    parts = domain.split("-")
    meaningful = [p for p in parts if len(p) > 2 or p.lower() in MEANINGFUL_SHORT]
    if not meaningful:
        meaningful = [p for p in parts if p]  # keep all if nothing passes filter
    domain_str = " ".join(meaningful)

    if domain_str:
        queries.append(domain_str)

    # Type-specific queries
    if repo_type == "boilerplate":
        if "provider" in repo_name:
            queries.extend(
                [
                    "new provider service",
                    "add method provider",
                    "provider integration setup",
                    "create provider",
                ]
            )
        elif "temporal" in repo_name:
            queries.extend(
                [
                    "new temporal workflow",
                    "create workflow",
                    "workflow boilerplate",
                ]
            )
        elif "grpc" in repo_name:
            queries.extend(
                [
                    "new grpc service",
                    "create service",
                    "service boilerplate",
                ]
            )
        else:
            # Generic boilerplate fallback
            queries.append(f"{domain_str} boilerplate")
    elif repo_type == "library":
        queries.append(f"{domain_str} library")
        if "common" in domain:
            queries.append("shared utilities")
        if "temporal" in domain:
            queries.append("temporal tools")
        if "pg" in parts:
            queries.append("postgres database library")
        if "cache" in domain or "valkey" in domain or "redis" in domain:
            queries.append("cache redis library")
    elif repo_type in ("grpc-service-js", "grpc-service-ts"):
        # Hub services — people search by what they DO
        if "core" in domain:
            queries.append(f"{domain_str} service")
        if "payment" in domain:
            queries.append(f"{domain_str} flow")
        if "settlement" in domain:
            queries.append(f"settlement {domain_str}")
        if "utils" in domain or "s3" in domain:
            queries.append(f"{domain_str} utility service")
    elif repo_type == "node-service":
        if "api" in domain:
            queries.append(f"{domain_str} endpoint")
        if "webhook" in domain:
            queries.append(f"webhook {domain_str}")
        if "vault" in domain:
            queries.append(f"vault {domain_str}")

    # Content-based fallback: when name-based queries are weak
    concept_queries = [q for q in queries if q != repo_name and q != domain_str]
    name_is_opaque = (
        not concept_queries  # no type-specific queries generated
        and (domain == repo_name or len(domain_str.split()) <= 1)  # domain ≈ name or single word
    )
    if name_is_opaque and conn is not None:
        content_queries = derive_queries_from_content(repo_name, conn)
        queries.extend(content_queries)

    # Always include the repo name itself
    queries.append(repo_name)

    return list(dict.fromkeys(queries))  # deduplicate preserving order


def run_hybrid_search(query: str, limit: int = 10) -> set[str]:
    """Run hybrid search, return set of repo names found."""
    return _run_hybrid_search_base(query, limit=limit)["repos"]


def find_important_repos(conn, min_dependents: int = 10) -> list[dict]:
    """Find repos that SHOULD be discoverable: boilerplate, hubs, core libs."""
    important = []

    # 1. All boilerplate repos — they're templates, always important
    for row in conn.execute("SELECT name, type FROM repos WHERE type = 'boilerplate'"):
        important.append(
            {
                "name": row["name"],
                "type": row["type"],
                "reason": "boilerplate (template repo)",
                "priority": "high",
            }
        )

    # 2. Hub repos — many dependents in graph (>= min_dependents)
    hub_rows = conn.execute(
        """
        SELECT target as name, COUNT(DISTINCT source) as dep_count
        FROM graph_edges
        WHERE target NOT LIKE 'pkg:%' AND target NOT LIKE 'proto:%'
        AND target NOT LIKE 'msg:%' AND target NOT LIKE 'svc:%'
        AND target NOT LIKE 'signal:%'
        AND edge_type NOT IN ('proto_message_def', 'proto_service_def', 'proto_message_usage')
        GROUP BY target
        HAVING dep_count >= ?
        ORDER BY dep_count DESC
    """,
        (min_dependents,),
    ).fetchall()

    existing_names = {r["name"] for r in important}
    for row in hub_rows:
        if row["name"] not in existing_names:
            repo_info = conn.execute("SELECT type FROM repos WHERE name = ?", (row["name"],)).fetchone()
            if repo_info:
                important.append(
                    {
                        "name": row["name"],
                        "type": repo_info["type"],
                        "reason": f"hub ({row['dep_count']} dependents)",
                        "priority": "medium" if row["dep_count"] < 20 else "high",
                    }
                )

    # 3. Core libraries (node-libs-* with many users)
    for row in conn.execute("""
        SELECT name, type FROM repos
        WHERE type = 'library' AND name LIKE 'node-libs-%'
    """):
        if row["name"] not in existing_names and row["name"] not in {r["name"] for r in important}:
            dep_count = conn.execute(
                "SELECT COUNT(DISTINCT source) as cnt FROM graph_edges WHERE target = ?", (row["name"],)
            ).fetchone()["cnt"]
            if dep_count >= min_dependents:
                important.append(
                    {
                        "name": row["name"],
                        "type": row["type"],
                        "reason": f"core library ({dep_count} users)",
                        "priority": "medium",
                    }
                )

    return important


def main():
    verbose = "--verbose" in sys.argv
    min_dependents = 10
    for arg in sys.argv:
        if arg.startswith("--min-dependents="):
            min_dependents = int(arg.split("=")[1])

    print("=" * 70)
    print("Blind Spot Detection: Important Repos Missing from Search")
    print("=" * 70)

    conn = get_db()
    start = time.time()

    # Find important repos
    important = find_important_repos(conn, min_dependents)
    print(f"\nFound {len(important)} important repos to check")
    print(f"  (boilerplate + hubs with >= {min_dependents} dependents + core libs)\n")

    # Test each
    blind_spots = []
    visible = []
    untestable = []

    for repo in important:
        queries = derive_queries(repo["name"], repo["type"], conn=conn)
        found_in_any = False
        found_queries = []
        missed_queries = []

        concept_queries = [q for q in queries if q != repo["name"]]

        for q in concept_queries:
            repos_found = run_hybrid_search(q, limit=10)
            if repo["name"] in repos_found:
                found_in_any = True
                found_queries.append(q)
            else:
                missed_queries.append(q)

        # Also check exact name (should always work)
        exact = run_hybrid_search(repo["name"], limit=10)
        exact_found = repo["name"] in exact

        repo["found_by_name"] = exact_found
        repo["found_by_concept"] = found_in_any
        repo["found_queries"] = found_queries
        repo["missed_queries"] = missed_queries
        repo["derived_queries"] = concept_queries

        # Categorize: no concept queries at all = untestable (query generation gap)
        if not concept_queries:
            untestable.append(repo)
        elif not found_in_any:
            blind_spots.append(repo)
        else:
            visible.append(repo)

    elapsed = time.time() - start

    # ============================================================
    # Report
    # ============================================================
    if blind_spots:
        print(f"{'!' * 50}")
        print(f"BLIND SPOTS: {len(blind_spots)} important repos invisible to concept search")
        print(f"{'!' * 50}")

        for bs in sorted(blind_spots, key=lambda x: x["priority"], reverse=True):
            priority_icon = {"high": "!!!", "medium": "!! "}[bs["priority"]]
            name_status = "found" if bs["found_by_name"] else "NOT FOUND"
            print(f"\n  [{priority_icon}] {bs['name']} ({bs['type']})")
            print(f"       Reason important: {bs['reason']}")
            print(f"       By exact name: {name_status}")
            print(f"       Queries tried ({len(bs['derived_queries'])}):")
            for q in bs["derived_queries"]:
                print(f'         - "{q}"')

        print(f"\n{'─' * 50}")
        print("Recommended actions:")
        print("  1. Add PHRASE_GLOSSARY rules in src/config.py for blind spot repos")
        print("  2. Improve CLAUDE.md / README in blind spot repos with searchable terms")
        print("  3. Add benchmark queries for blind spot domains")

    if untestable:
        print(f"\n{'─' * 50}")
        print(f"UNTESTABLE: {len(untestable)} repos — query generation gap (no concept queries derived)")
        for ut in untestable:
            print(f"  {ut['name']:<40} {ut['reason']}")

    if not blind_spots and not untestable:
        print("All important repos are discoverable — no blind spots, no gaps.")

    # Visible repos summary
    if verbose and visible:
        print(f"\n{'─' * 50}")
        print(f"Visible repos ({len(visible)}):")
        for v in visible:
            hit_rate = len(v["found_queries"]) / len(v["derived_queries"]) if v["derived_queries"] else 1.0
            status = "full" if hit_rate == 1.0 else f"{hit_rate:.0%}"
            print(f"  {v['name']:<45} {status:<6} {v['reason']}")

    # Summary stats — breakdown, not a single misleading percentage
    total = len(important)
    tested = len(visible) + len(blind_spots)
    visible_count = len(visible)
    blind_count = len(blind_spots)
    untestable_count = len(untestable)
    testable_visibility = visible_count / tested if tested else 1.0
    coverage = tested / total if total else 1.0

    print(f"\n{'=' * 70}")
    print(f"{'Total important repos':.<40} {total}")
    print(f"{'Tested':.<40} {tested}")
    print(f"{'  Visible':.<40} {visible_count}")
    print(f"{'  Blind spots':.<40} {blind_count}")
    print(f"{'Untestable (query gap)':.<40} {untestable_count}")
    print(f"{'─' * 40}")
    print(f"{'Testable visibility':.<40} {testable_visibility:.1%}")
    print(f"{'Coverage':.<40} {coverage:.1%}")
    print(f"{'Detection time':.<40} {elapsed:.1f}s")
    print(f"{'=' * 70}")

    # Save results
    output_path = _BASE / "blind_spots_results.json"
    with open(output_path, "w") as f:
        json.dump(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "total_important": total,
                "tested": tested,
                "visible": visible_count,
                "blind_spots": blind_count,
                "untestable": untestable_count,
                "testable_visibility": round(testable_visibility, 4),
                "coverage": round(coverage, 4),
                "min_dependents": min_dependents,
                "blind_spot_repos": [
                    {
                        "name": bs["name"],
                        "type": bs["type"],
                        "reason": bs["reason"],
                        "priority": bs["priority"],
                        "missed_queries": bs["missed_queries"],
                    }
                    for bs in blind_spots
                ],
                "untestable_repos": [
                    {
                        "name": ut["name"],
                        "type": ut["type"],
                        "reason": ut["reason"],
                    }
                    for ut in untestable
                ],
                "visible_repos": [
                    {
                        "name": v["name"],
                        "type": v["type"],
                        "reason": v["reason"],
                        "hit_rate": len(v["found_queries"]) / len(v["derived_queries"])
                        if v["derived_queries"]
                        else 1.0,
                    }
                    for v in visible
                ],
            },
            f,
            indent=2,
        )
    print(f"\nResults saved to {output_path}")

    conn.close()


if __name__ == "__main__":
    main()
