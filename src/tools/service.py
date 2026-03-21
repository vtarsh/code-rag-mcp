"""Utility MCP tools — repo_overview, list_repos, health_check, visualize_graph.

These tools provide repo browsing, diagnostics, and visualization.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.cache import get_runtime_stats
from src.config import BASE_DIR, DB_PATH, LANCE_PATH, NPM_SCOPE
from src.container import get_db, is_model_loaded, is_reranker_loaded, require_db


@require_db
def repo_overview_tool(repo_name: str) -> str:
    """Get detailed overview of a specific repo.

    Args:
        repo_name: Exact repo name (e.g., "grpc-apm-trustly", "workflow-provider-webhooks")
    """
    conn = get_db()
    try:
        repo = conn.execute("SELECT * FROM repos WHERE name = ?", (repo_name,)).fetchone()
        if not repo:
            repos = conn.execute("SELECT name FROM repos WHERE name LIKE ? LIMIT 10", (f"%{repo_name}%",)).fetchall()
            if repos:
                return f"Repo '{repo_name}' not found. Did you mean: {', '.join(r['name'] for r in repos)}"
            return f"Repo '{repo_name}' not found."

        deps = json.loads(repo["org_deps"]) if repo["org_deps"] else []
        artifacts = json.loads(repo["artifact_counts"]) if repo["artifact_counts"] else {}

        file_types = conn.execute(
            "SELECT DISTINCT file_type, COUNT(*) as cnt FROM chunks WHERE repo_name = ? GROUP BY file_type",
            (repo_name,),
        ).fetchall()

        short_name = repo_name.replace("grpc-", "").replace("apm-", "").replace("core-", "")
        dependents = conn.execute(
            "SELECT name FROM repos WHERE org_deps LIKE ? AND name != ?", (f"%{short_name}%", repo_name)
        ).fetchall()

        methods = conn.execute(
            "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method'", (repo_name,)
        ).fetchall()

        output = f"# {repo_name}\n\n"
        output += f"**Type**: {repo['type']}\n"
        output += f"**SHA**: {repo['sha'][:8]}...\n\n"

        if artifacts:
            output += "**Artifacts**: " + ", ".join(f"{k}: {v}" for k, v in artifacts.items() if v > 0) + "\n\n"

        if deps:
            output += f"**{NPM_SCOPE} dependencies** ({len(deps)}):\n"
            for d in sorted(deps):
                output += f"  - {d}\n"
            output += "\n"

        if methods:
            output += f"**gRPC methods** ({len(methods)}):\n"
            for m in methods:
                name = Path(m["file_path"]).stem
                output += f"  - {name}\n"
            output += "\n"

        if dependents:
            output += f"**Used by** ({len(dependents)} repos):\n"
            for d in dependents[:15]:
                output += f"  - {d['name']}\n"
            if len(dependents) > 15:
                output += f"  ... and {len(dependents) - 15} more\n"
            output += "\n"

        if file_types:
            output += "**Indexed chunks by type**:\n"
            for ft in file_types:
                output += f"  - {ft['file_type']}: {ft['cnt']}\n"

        return output
    finally:
        conn.close()


@require_db
def list_repos_tool(type: str = "", has_dep: str = "", limit: int = 30) -> str:
    """List repos filtered by type or dependency.

    Args:
        type: Filter by repo type: grpc-service-js, grpc-service-ts, temporal-workflow, library, boilerplate, node-service, ci-actions, gitops
        has_dep: Filter repos that depend on this package
        limit: Max results (default 30)
    """
    conn = get_db()
    try:
        where_clauses: list[str] = []
        params: list[str | int] = []

        if type:
            where_clauses.append("type = ?")
            params.append(type)
        if has_dep:
            where_clauses.append("org_deps LIKE ?")
            params.append(f"%{has_dep}%")

        where = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        params.append(min(limit, 100))

        rows = conn.execute(f"SELECT name, type, org_deps FROM repos{where} ORDER BY name LIMIT ?", params).fetchall()

        if not rows:
            return "No repos found matching criteria."

        count_rows = conn.execute(f"SELECT COUNT(*) as cnt FROM repos{where}", params[:-1]).fetchone()
        total = count_rows["cnt"]

        output = f"Found {total} repos"
        if type:
            output += f" of type '{type}'"
        if has_dep:
            output += f" depending on '{has_dep}'"
        output += f" (showing {len(rows)}):\n\n"

        for r in rows:
            deps = json.loads(r["org_deps"]) if r["org_deps"] else []
            output += f"- **{r['name']}** ({r['type']}) — {len(deps)} org deps\n"

        return output
    finally:
        conn.close()


def health_check_tool() -> str:
    """Return a diagnostic report on the knowledge base: database, vector store,
    models, graph, and consistency status. Takes no arguments."""

    lines = ["=== Knowledge Base Health Check ==="]
    chunk_count = 0
    last_build: str | None = None

    # --- Database ---
    db_ok = _check_database(lines)
    if db_ok:
        chunk_count, last_build = db_ok

    # --- Vector store ---
    vector_count = _check_vector_store(lines)

    # --- Models ---
    lines.append(f"Reranker:      {'loaded' if is_reranker_loaded() else 'not loaded (lazy)'}")
    lines.append(f"Vector Model:  {'loaded' if is_model_loaded() else 'not loaded (lazy)'}")

    # --- Consistency ---
    _check_consistency(lines, chunk_count, vector_count)

    # --- Graph ---
    _check_graph(lines)

    # --- Runtime stats ---
    _append_runtime_stats(lines)

    # --- Index freshness ---
    if last_build:
        _append_index_freshness(lines, last_build)

    lines.append("=" * 36)
    return "\n".join(lines)


def _check_database(lines: list[str]) -> tuple[int, str | None] | None:
    """Check database health. Returns (chunk_count, last_build) or None."""
    if not DB_PATH.exists():
        lines.append("Database:      NOT AVAILABLE")
        return None

    size_mb = DB_PATH.stat().st_size / (1024 * 1024)
    lines.append(f"Database:      OK ({size_mb:.1f} MB)")

    try:
        conn = get_db()
    except Exception as e:
        lines.append(f"  Error reading DB: {e}")
        return None

    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        chunk_count = 0
        repo_count = 0
        file_count = 0
        last_build: str | None = None

        if "build_info" in tables:
            info = dict(conn.execute("SELECT key, value FROM build_info").fetchall())
            last_build = info.get("last_build")
            chunk_count = int(info.get("total_chunks", 0))
            repo_count = int(info.get("total_repos", 0))
            file_count = int(info.get("total_files", 0))

        if not chunk_count and "chunks" in tables:
            chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if not repo_count and "repos" in tables:
            repo_count = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]

        lines.append(f"  Last build:  {last_build or 'unknown'}")
        lines.append(f"  Chunks:      {chunk_count:,}")
        lines.append(f"  Repos:       {repo_count:,}")
        lines.append(f"  Files:       {file_count:,}")
        return chunk_count, last_build
    except Exception as e:
        lines.append(f"  Error reading DB: {e}")
        return None
    finally:
        conn.close()


def _check_vector_store(lines: list[str]) -> int:
    """Check vector store health. Returns vector count."""
    if not LANCE_PATH.exists():
        lines.append("Vector Store:  NOT AVAILABLE")
        return 0

    lance_size = sum(f.stat().st_size for f in LANCE_PATH.rglob("*") if f.is_file())
    size_mb = lance_size / (1024 * 1024)
    lines.append(f"Vector Store:  OK ({size_mb:.1f} MB)")
    try:
        import lancedb

        db = lancedb.connect(str(LANCE_PATH))
        tbl = db.open_table("chunks")
        vector_count = tbl.count_rows()
        lines.append(f"  Vectors:     {vector_count:,}")
        return vector_count
    except Exception as e:
        lines.append(f"  Error reading vectors: {e}")
        return 0


def _check_consistency(lines: list[str], chunk_count: int, vector_count: int) -> None:
    """Append consistency check results."""
    if chunk_count and vector_count:
        if chunk_count == vector_count:
            lines.append(f"Consistency:   OK (chunks == vectors: {chunk_count:,})")
        else:
            lines.append(f"Consistency:   MISMATCH (chunks={chunk_count:,}, vectors={vector_count:,})")
    elif chunk_count or vector_count:
        lines.append(f"Consistency:   PARTIAL (chunks={chunk_count:,}, vectors={vector_count:,})")
    else:
        lines.append("Consistency:   N/A (no data)")


def _check_graph(lines: list[str]) -> None:
    """Check graph tables health."""
    if not DB_PATH.exists():
        lines.append("Graph:         NOT AVAILABLE")
        return

    try:
        conn = get_db()
    except Exception as e:
        lines.append(f"Graph:         ERROR ({e})")
        return

    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "graph_nodes" in tables and "graph_edges" in tables:
            node_count = conn.execute("SELECT COUNT(*) as cnt FROM graph_nodes").fetchone()["cnt"]
            edge_count = conn.execute("SELECT COUNT(*) as cnt FROM graph_edges").fetchone()["cnt"]
            lines.append("Graph:         OK")
            lines.append(f"  Nodes:       {node_count:,}")
            lines.append(f"  Edges:       {edge_count:,}")
        else:
            missing = [t for t in ["graph_nodes", "graph_edges"] if t not in tables]
            lines.append(f"Graph:         NOT AVAILABLE (missing: {', '.join(missing)})")
    except Exception as e:
        lines.append(f"Graph:         ERROR ({e})")
    finally:
        conn.close()


def _append_runtime_stats(lines: list[str]) -> None:
    """Append runtime and cache statistics."""
    stats = get_runtime_stats()
    lines.append(f"\nRuntime:       {stats.uptime_min:.0f} min uptime")
    lines.append(f"Tool calls:    {stats.total_calls}")

    for ts in stats.tool_stats:
        lines.append(f"  {ts.name}: {ts.call_count}x (avg {ts.avg_ms:.0f}ms)")

    if stats.cache_hit_rate is not None:
        lines.append(
            f"Cache:         {stats.cache_hit_rate:.0f}% hit rate ({stats.cache_hits}/{stats.cache_hits + stats.cache_misses})"
        )
    else:
        lines.append("Cache:         no queries yet")


def _append_index_freshness(lines: list[str], last_build: str) -> None:
    """Append index age information."""
    try:
        from datetime import datetime

        build_dt = datetime.fromisoformat(last_build)
        age_hours = (datetime.now() - build_dt).total_seconds() / 3600
        freshness = "fresh" if age_hours < 8 else "stale" if age_hours < 48 else "OLD"
        lines.append(f"Index age:     {age_hours:.0f}h ({freshness})")
    except (ValueError, TypeError):
        pass


@require_db
def diff_provider_config_tool(provider_a: str, provider_b: str) -> str:
    """Compare feature flags and config between two providers from seeds.cql.

    Useful for understanding why a feature works for one provider but not another.

    Args:
        provider_a: First provider name (e.g., "trustly", "epx")
        provider_b: Second provider name (e.g., "paypal", "nuvei")
    """
    conn = get_db()

    def _get_provider_chunk(provider: str) -> str | None:
        row = conn.execute(
            "SELECT content FROM chunks WHERE file_type = 'provider_config' AND content MATCH ? LIMIT 1",
            (f'"{provider}"',),
        ).fetchone()
        if not row:
            return None
        # FTS5 with Row factory — access by column name
        try:
            return row["content"]
        except (IndexError, KeyError):
            return row[0]

    chunk_a = _get_provider_chunk(provider_a)
    chunk_b = _get_provider_chunk(provider_b)

    if not chunk_a and not chunk_b:
        return f"Neither '{provider_a}' nor '{provider_b}' found in provider configs (seeds.cql)"
    if not chunk_a:
        return f"Provider '{provider_a}' not found in seeds.cql. '{provider_b}' exists."
    if not chunk_b:
        return f"Provider '{provider_b}' not found in seeds.cql. '{provider_a}' exists."

    def _parse_features(chunk: str) -> dict:
        features = {}
        for raw_line in chunk.splitlines():
            line = raw_line.strip()
            if "=" in line and raw_line.startswith((" ", "\t")) and "Feature flags:" not in line:
                parts = line.split("=", 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    features[key] = val
        # Also extract payment_method_type
        for line in chunk.splitlines():
            if "payment_method_type:" in line:
                pmt = line.split("payment_method_type:")[1].strip()
                features["_payment_method_type"] = pmt
                break
        return features

    feats_a = _parse_features(chunk_a)
    feats_b = _parse_features(chunk_b)

    all_keys = sorted(set(list(feats_a.keys()) + list(feats_b.keys())))

    lines = [f"## Provider Config Comparison: {provider_a} vs {provider_b}\n"]

    # Payment method types
    pmt_a = feats_a.pop("_payment_method_type", "?")
    pmt_b = feats_b.pop("_payment_method_type", "?")
    lines.append(f"**payment_method_type:** {provider_a}=`{pmt_a}` | {provider_b}=`{pmt_b}`\n")

    # Diff table
    same = []
    diff = []
    only_a = []
    only_b = []

    for key in all_keys:
        if key.startswith("_"):
            continue
        va = feats_a.get(key)
        vb = feats_b.get(key)
        if va and vb:
            if va == vb:
                same.append((key, va))
            else:
                diff.append((key, va, vb))
        elif va and not vb:
            only_a.append((key, va))
        elif vb and not va:
            only_b.append((key, vb))

    if diff:
        lines.append("### Differences")
        lines.append(f"| Feature | {provider_a} | {provider_b} |")
        lines.append("|---------|-------|-------|")
        for key, va, vb in diff:
            marker = " ⚠️" if va != vb else ""
            lines.append(f"| {key} | {va} | {vb} |{marker}")

    if same:
        lines.append(f"\n### Same ({len(same)} features)")
        lines.append(", ".join(f"{k}={v}" for k, v in same))

    if only_a:
        lines.append(f"\n### Only in {provider_a}")
        lines.append(", ".join(f"{k}={v}" for k, v in only_a))

    if only_b:
        lines.append(f"\n### Only in {provider_b}")
        lines.append(", ".join(f"{k}={v}" for k, v in only_b))

    return "\n".join(lines)


def visualize_graph_tool(repo: str = "", edge_type: str = "") -> str:
    """Generate an interactive D3.js graph visualization and open it in the browser.

    Args:
        repo: Optional — focus on a specific repo's neighborhood
        edge_type: Optional — show only a specific edge type
    """
    scripts_dir = BASE_DIR / "scripts"
    cmd = ["python3", str(scripts_dir / "visualize_graph.py"), "--open"]
    if repo:
        cmd.append(f"--repo={repo}")
    if edge_type:
        cmd.append(f"--type={edge_type}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip()
        if result.returncode != 0:
            return f"Error generating graph: {result.stderr}"
        return output
    except Exception as e:
        return f"Error: {e}"
