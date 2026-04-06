"""Utility MCP tools — repo_overview, list_repos, health_check, visualize_graph.

These tools provide repo browsing, diagnostics, and visualization.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from src.cache import get_runtime_stats
from src.config import BASE_DIR, DB_PATH, LANCE_PATH, NPM_SCOPE, REPO_NAME_PREFIXES
from src.container import db_connection, is_model_loaded, is_reranker_loaded, require_db


@require_db
def repo_overview_tool(repo_name: str) -> str:
    """Get detailed overview of a specific repo.

    Args:
        repo_name: Exact repo name (e.g., "grpc-apm-trustly", "workflow-provider-webhooks")
    """
    with db_connection() as conn:
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

        short_name = repo_name
        for prefix in REPO_NAME_PREFIXES:
            short_name = short_name.replace(prefix, "")
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


@require_db
def list_repos_tool(type: str = "", has_dep: str = "", limit: int = 30) -> str:
    """List repos filtered by type or dependency.

    Args:
        type: Filter by repo type: grpc-service-js, grpc-service-ts, temporal-workflow, library, boilerplate, node-service, ci-actions, gitops
        has_dep: Filter repos that depend on this package
        limit: Max results (default 30)
    """
    with db_connection() as conn:
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
        with db_connection() as conn:
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
        with db_connection() as conn:
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
    with db_connection() as conn:
        return _diff_provider_config_impl(conn, provider_a, provider_b)


def _diff_provider_config_impl(conn: sqlite3.Connection, provider_a: str, provider_b: str) -> str:
    def _get_provider_chunks(provider: str) -> list[str]:
        """Get ALL provider_config chunks for a provider (may have multiple payment_method_types)."""
        # Sanitize provider name for FTS5 (remove quotes to prevent injection)
        safe_provider = provider.replace('"', "").replace("'", "").strip()
        if not safe_provider:
            return []
        try:
            rows = conn.execute(
                "SELECT content FROM chunks WHERE file_type = 'provider_config' AND content MATCH ?",
                (f'"{safe_provider}"',),
            ).fetchall()
        except Exception:
            return []
        results = []
        for row in rows:
            try:
                content = row["content"]
            except (IndexError, KeyError):
                content = row[0]
            # Verify this chunk is actually for this provider (FTS5 may match partial)
            if f"Provider: {provider}" in content:
                results.append(content)
        return results

    chunks_a = _get_provider_chunks(provider_a)
    chunks_b = _get_provider_chunks(provider_b)

    if not chunks_a and not chunks_b:
        return f"Neither '{provider_a}' nor '{provider_b}' found in provider configs (seeds.cql)"
    if not chunks_a:
        return f"Provider '{provider_a}' not found in seeds.cql. '{provider_b}' has {len(chunks_b)} config(s)."
    if not chunks_b:
        return f"Provider '{provider_b}' not found in seeds.cql. '{provider_a}' has {len(chunks_a)} config(s)."

    def _parse_features(chunk: str) -> dict[str, str]:
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

    lines = [f"## Provider Config Comparison: {provider_a} vs {provider_b}\n"]

    # Handle multiple payment_method_types per provider
    if len(chunks_a) > 1 or len(chunks_b) > 1:
        lines.append(
            f"**{provider_a}** has {len(chunks_a)} config(s), **{provider_b}** has {len(chunks_b)} config(s)\n"
        )

    # Compare each chunk pair (primary comparison: first chunk of each)
    for idx, (ca, cb) in enumerate(
        zip(
            chunks_a + [""] * max(0, len(chunks_b) - len(chunks_a)),
            chunks_b + [""] * max(0, len(chunks_a) - len(chunks_b)),
            strict=False,
        )
    ):
        if not ca or not cb:
            continue

        feats_a = _parse_features(ca)
        feats_b = _parse_features(cb)

        pmt_a = feats_a.pop("_payment_method_type", "?")
        pmt_b = feats_b.pop("_payment_method_type", "?")

        if len(chunks_a) > 1 or len(chunks_b) > 1:
            lines.append(f"\n### Config {idx + 1}: `{pmt_a}` vs `{pmt_b}`")
        else:
            lines.append(f"**payment_method_type:** {provider_a}=`{pmt_a}` | {provider_b}=`{pmt_b}`\n")

        # Diff table for this config pair
        all_keys = sorted(set(list(feats_a.keys()) + list(feats_b.keys())))
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
    cmd = [sys.executable, str(scripts_dir / "visualize_graph.py"), "--open"]
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


@require_db
def search_task_history_tool(query: str, developer: str = "", limit: int = 10) -> str:
    """Search past tasks by description, repos, files, or any keyword.

    Args:
        query: Search query — keywords or natural language question
        developer: Optional - filter by developer name (partial match)
        limit: Max results to return (default 10, max 20)
    """
    if not query.strip():
        return "Error: query cannot be empty"
    limit = min(max(1, limit), 20)

    with db_connection() as conn:
        # FTS5 search over task_history_fts
        sql = """
            SELECT t.ticket_id, t.ticket_type, t.summary, t.developer,
                   t.jira_status, t.repos_changed, t.files_changed, t.pr_urls,
                   t.labels, t.components, t.description
            FROM task_history_fts fts
            JOIN task_history t ON t.id = fts.rowid
            WHERE task_history_fts MATCH ?
        """
        params: list = [query]

        if developer.strip():
            sql += " AND t.developer LIKE ?"
            params.append(f"%{developer.strip()}%")

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        results = conn.execute(sql, params).fetchall()
        if not results:
            return f"No tasks found for: {query}"

        lines = [f"# Task History Search: {query}\n\nFound {len(results)} task(s):\n"]
        for row in results:
            (
                ticket_id,
                ticket_type,
                summary,
                developer_name,
                status,
                repos_json,
                files_json,
                prs_json,
                labels_json,
                components_json,
                description,
            ) = row
            repos = json.loads(repos_json) if repos_json else []
            files = json.loads(files_json) if files_json else []
            prs = json.loads(prs_json) if prs_json else []
            labels = json.loads(labels_json) if labels_json else []

            lines.append(f"## {ticket_id} — {summary}")
            lines.append(f"Type: {ticket_type} | Status: {status} | Developer: {developer_name}")
            if labels:
                lines.append(f"Labels: {', '.join(labels)}")
            if repos:
                lines.append(f"Repos ({len(repos)}): {', '.join(repos)}")
            if files:
                lines.append(f"Files ({len(files)}): {', '.join(files[:20])}")
                if len(files) > 20:
                    lines.append(f"  ... and {len(files) - 20} more")
            if prs:
                lines.append(f"PRs ({len(prs)}): {', '.join(prs)}")
            if description:
                lines.append(f"Description: {description[:300]}...")
            lines.append("")

        return "\n".join(lines)
