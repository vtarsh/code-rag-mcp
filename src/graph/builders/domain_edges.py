"""Domain registry, flow annotation, redirect and URL reference edges."""

import re
import sqlite3
from pathlib import Path

from ._common import BASE_DIR, _load_domain_map, _resolve_domain_to_repo, _resolve_url_to_repo


def parse_domain_registry_edges(conn: sqlite3.Connection):
    """Create domain_serves edges from docs/domain_registry.yaml.

    For each domain entry, creates:
    - domain_serves edge: repo → domain (the repo serves this domain)
    - domain_reference edges: referencing_repo → serving_repo (who references the domain)
    """
    registry_file = BASE_DIR / "docs" / "domain_registry.yaml"
    if not registry_file.is_file():
        print("  No domain_registry.yaml found, skipping")
        return

    try:
        import yaml

        data = yaml.safe_load(registry_file.read_text())
        entries = data.get("domains", [])
    except ImportError:
        # Fallback: parse without PyYAML
        entries = _parse_domain_registry_simple(registry_file)

    edges: list[tuple[str, str, str, str]] = []
    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}

    for entry in entries:
        domain = entry.get("domain", "")
        repo = entry.get("repo", "")
        if not domain or not repo:
            continue

        # domain_serves: repo serves this domain
        if repo in known_repos:
            edges.append((repo, f"domain:{domain}", "domain_serves", domain))

        # domain_reference: repos that reference this domain → the serving repo
        for ref_repo in entry.get("referenced_by", []):
            if ref_repo in known_repos and repo in known_repos and ref_repo != repo:
                edges.append((ref_repo, repo, "domain_reference", domain))

    if edges:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            edges,
        )

    real_edges = [e for e in edges if not e[1].startswith("domain:")]
    print(f"  Domain registry: {len(edges)} edges ({len(real_edges)} repo-to-repo references)")


def _parse_domain_registry_simple(registry_file: Path) -> list[dict]:
    """Fallback parser for domain_registry.yaml without PyYAML."""
    text = registry_file.read_text()
    entries: list[dict] = []
    current: dict = {}

    for line in text.splitlines():
        m = re.match(r'\s+-\s+domain:\s+"(.+)"', line)
        if m:
            if current:
                entries.append(current)
            current = {"domain": m.group(1), "referenced_by": []}
            continue
        m = re.match(r"\s+repo:\s+(\S+)", line)
        if m and current:
            current["repo"] = m.group(1)
        m = re.match(r'\s+description:\s+"(.+)"', line)
        if m and current:
            current["description"] = m.group(1)
        m = re.match(r"\s+-\s+(\S+)\s*#?", line)
        if m and current and "referenced_by" in current and m.group(1) not in ("domain:", "repo:", "description:"):
            ref = m.group(1)
            if not ref.startswith('"') and not ref.startswith("domain:"):
                current["referenced_by"].append(ref)

    if current:
        entries.append(current)

    return entries


def parse_flow_annotation_edges(conn: sqlite3.Connection):
    """Create graph edges from flow annotation YAML files.

    Parses docs/flows/*.yaml and creates edges for:
    - grpc_call steps: source → target (flow_step edge)
    - redirect steps: source → target_repo (flow_redirect edge)
    - dispatch steps: source → target (flow_dispatch edge)
    """
    flows_dir = BASE_DIR / "docs" / "flows"
    if not flows_dir.is_dir():
        print("  No flows/ directory found, skipping")
        return

    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}
    edges: list[tuple[str, str, str, str]] = []

    for flow_file in sorted(flows_dir.glob("*.yaml")):
        source_repo = flow_file.stem
        if source_repo not in known_repos:
            continue

        text = flow_file.read_text()

        # Extract target repos from flow annotations
        for line in text.splitlines():
            stripped = line.strip()

            # target: "repo-name"
            m = re.match(r'target:\s+"?([a-z0-9-]+)"?', stripped)
            if m:
                target = m.group(1)
                # Handle template patterns like grpc-apm-{provider}
                if "{" in target:
                    continue  # Skip parameterized targets
                if target in known_repos:
                    edges.append((source_repo, target, "flow_step", "flow annotation"))

            # target_repo: "repo-name"
            m = re.match(r'target_repo:\s+"?([a-z0-9-]+)"?', stripped)
            if m:
                target = m.group(1)
                if target in known_repos:
                    edges.append((source_repo, target, "flow_redirect", "flow annotation"))

            # from: "repo-name" (for multi-hop flows)
            m = re.match(r'from:\s+"?([a-z0-9-]+)"?', stripped)
            if m:
                from_repo = m.group(1)
                if from_repo in known_repos:
                    # Look for the next target in nearby lines
                    pass  # Handled by target: on the same flow step

            # target_repos map entries: key: "repo-name"
            m = re.match(r'([a-z_]+):\s+"?([a-z0-9-]+)"?$', stripped)
            if (
                m
                and m.group(2) in known_repos
                and m.group(1)
                not in (
                    "type",
                    "name",
                    "entry",
                    "file",
                    "handler",
                    "method",
                    "action",
                    "condition",
                    "description",
                    "target",
                    "target_repo",
                )
            ):
                edges.append((source_repo, m.group(2), "flow_redirect", f"via {m.group(1)}"))

    if edges:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            edges,
        )

    print(f"  Flow annotations: {len(edges)} edges from {len(list(flows_dir.glob('*.yaml')))} files")


def parse_redirect_edges(conn: sqlite3.Connection):
    """Parse static res.redirect(URL) calls → target via domain registry.

    Only handles static URLs (string literals). Data-driven redirects
    (res.redirect(variable)) are covered by flow annotations (2.4).
    """
    # Load domain registry for URL resolution
    domain_map = _load_domain_map()
    if not domain_map:
        print("  No domain registry, skipping redirect edge parsing")
        return

    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}

    # Get code chunks that might contain redirects
    rows = conn.execute(
        "SELECT repo_name, content FROM chunks WHERE language IN ('javascript', 'typescript') "
        "AND (content LIKE '%redirect%' OR content LIKE '%302%')"
    ).fetchall()

    edges: list[tuple[str, str, str, str]] = []
    redirect_pattern = re.compile(r'(?:res\.redirect|redirect)\s*\(\s*[\'"`](https?://[^\'"`]+)[\'"`]')

    for repo, content in rows:
        for match in redirect_pattern.finditer(content):
            url = match.group(1)
            target_repo = _resolve_url_to_repo(url, domain_map)
            if target_repo and target_repo in known_repos and target_repo != repo:
                edges.append((repo, target_repo, "redirect", url))

    unique = list(set(edges))
    if unique:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            unique,
        )
    print(f"  Static redirect edges: {len(unique)}")


def parse_url_reference_edges(conn: sqlite3.Connection):
    """Parse URL patterns in code/config → domain registry → repo.

    Finds internal domain references in code that indicate cross-service communication.
    """
    domain_map = _load_domain_map()
    if not domain_map:
        print("  No domain registry, skipping URL reference parsing")
        return

    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}

    # Get chunks that reference internal service domains
    # Derive search pattern from domain_map keys (e.g., "pay.com", "example.com")
    _domain_suffixes = set()
    for d in domain_map:
        parts = d.split(".")
        if len(parts) >= 2:
            _domain_suffixes.add(".".join(parts[-2:]))
    if not _domain_suffixes:
        print("  No domain suffixes found in registry, skipping URL reference parsing")
        return

    # Build SQL LIKE clauses for each suffix
    like_clauses = " OR ".join(f"content LIKE '%.{s}%'" for s in _domain_suffixes)
    rows = conn.execute(
        f"SELECT repo_name, content FROM chunks WHERE ({like_clauses}) "
        "AND file_type NOT IN ('domain_registry', 'flow_annotation', 'env_map', 'gotchas')"
    ).fetchall()

    edges: list[tuple[str, str, str, str]] = []
    # Build regex pattern from domain suffixes
    _escaped_suffixes = "|".join(re.escape(s) for s in _domain_suffixes)
    url_pattern = re.compile(rf"https?://(\w[\w.-]*\.(?:{_escaped_suffixes}))(?:/\S*)?")

    for repo, content in rows:
        for match in url_pattern.finditer(content):
            domain = match.group(1)
            target_repo = _resolve_domain_to_repo(domain, domain_map)
            if target_repo and target_repo in known_repos and target_repo != repo:
                edges.append((repo, target_repo, "url_reference", domain))

    unique = list(set(edges))
    if unique:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            unique,
        )
    print(f"  URL reference edges: {len(unique)}")
