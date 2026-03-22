"""PI (Provider Integration) analyzer — sections specific to provider tasks."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from src.config import GATEWAY_REPO, INFRA_REPOS, PROVIDER_PREFIXES, WEBHOOK_REPOS
from src.formatting import strip_repo_tag

from .base import _KEYWORD_STOP_WORDS, AnalysisContext


def detect_provider(conn: sqlite3.Connection, words: set[str]) -> str:
    """Auto-detect provider name from task description words."""
    if not PROVIDER_PREFIXES:
        return ""
    placeholders = " OR ".join("name LIKE ?" for _ in PROVIDER_PREFIXES)
    params = [f"{p}%" for p in PROVIDER_PREFIXES]
    provider_repos = conn.execute(f"SELECT name FROM repos WHERE {placeholders}", params).fetchall()
    provider_names: set[str] = set()
    for r in provider_repos:
        name = r["name"]
        for prefix in PROVIDER_PREFIXES:
            if name.startswith(prefix):
                provider_names.add(name[len(prefix) :])
                break
    for p in provider_names:
        if p in words:
            return p
    return ""


_BULK_PATTERNS = re.compile(
    r"\b(?:all|every|each|all live|across all)\s+(?:providers?|integrations?|apm)\b",
    re.IGNORECASE,
)


def _is_bulk_provider_task(description: str) -> bool:
    """Detect if task targets all providers rather than a specific one."""
    return bool(_BULK_PATTERNS.search(description))


def section_bulk_providers(ctx: AnalysisContext) -> str:
    """When task targets all providers, list them all via gateway routing."""
    if ctx.provider or not _is_bulk_provider_task(ctx.description):
        return ""
    if not GATEWAY_REPO:
        return ""

    routed = ctx.conn.execute(
        """SELECT DISTINCT target FROM graph_edges
           WHERE source = ? AND edge_type = 'runtime_routing'
           ORDER BY target""",
        (GATEWAY_REPO,),
    ).fetchall()
    if not routed:
        return ""

    output = f"## Bulk Provider Change ({len(routed)} providers)\n\n"
    output += "_Task targets all providers — listing all routed repos:_\n\n"
    for r in routed:
        ctx.findings.append(("provider", r["target"]))
        output += f"  - **{r['target']}**\n"
    output += "\n"
    return output


def section_provider(ctx: AnalysisContext) -> str:
    """Section 1: Find provider service repos and keyword matches."""
    if not ctx.provider:
        return ""

    output = f"## 1. Provider: {ctx.provider}\n\n"
    for prefix in PROVIDER_PREFIXES:
        repo_name = f"{prefix}{ctx.provider}"
        repo = ctx.conn.execute("SELECT * FROM repos WHERE name = ?", (repo_name,)).fetchone()
        if not repo:
            continue

        methods = ctx.conn.execute(
            "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method'", (repo_name,)
        ).fetchall()
        method_names = [Path(m["file_path"]).stem for m in methods]
        output += f"**{repo_name}** ({repo['type']})\n  Methods: {', '.join(method_names)}\n\n"
        ctx.findings.append(("provider", repo_name))

        for keyword in ctx.words:
            if len(keyword) > 4 and keyword not in _KEYWORD_STOP_WORDS:
                matches = ctx.conn.execute(
                    "SELECT snippet(chunks, 0, '>>>', '<<<', '...', 20) as snippet FROM chunks WHERE chunks MATCH ? AND repo_name = ? LIMIT 2",
                    (f'"{keyword}"', repo_name),
                ).fetchall()
                if matches:
                    for m in matches:
                        snip = strip_repo_tag(m["snippet"])
                        output += f"  Found `{keyword}`: {snip[:150]}\n"
                    output += "\n"

    return output


def section_webhooks(ctx: AnalysisContext) -> str:
    """Section 3: Find webhook handling for the provider."""
    if not ctx.provider:
        return ""

    output = "## 3. Webhook Handling\n\n"
    webhook_chunks = ctx.conn.execute(
        "SELECT repo_name, file_path, snippet(chunks, 0, '>>>', '<<<', '...', 25) as snippet "
        "FROM chunks WHERE chunks MATCH ? AND repo_name LIKE '%webhook%' ORDER BY rank LIMIT 10",
        (f'"{ctx.provider}"',),
    ).fetchall()
    if not webhook_chunks:
        return output + "No webhook handling found for this provider.\n\n"

    repos_seen: set[str] = set()
    for row in webhook_chunks:
        rname = row["repo_name"]
        if rname not in repos_seen:
            repos_seen.add(rname)
            output += f"**{rname}**\n"
            ctx.findings.append(("webhook", rname))
        snip = strip_repo_tag(row["snippet"])
        output += f"  `{row['file_path']}`: {snip[:150]}\n"
    output += "\n"
    return output


def section_impact(ctx: AnalysisContext) -> str:
    """Section 5: Trace dependency impact for provider repos."""
    output = "## 5. Impact Analysis\n\n"
    if not ctx.provider:
        return output

    for prefix in PROVIDER_PREFIXES:
        repo_name = f"{prefix}{ctx.provider}"
        deps = ctx.conn.execute(
            "SELECT target, edge_type FROM graph_edges WHERE source = ? AND target NOT LIKE 'pkg:%'", (repo_name,)
        ).fetchall()
        if deps:
            output += f"**{repo_name}** depends on:\n"
            for d in deps:
                output += f"  - {d['target']} ({d['edge_type']})\n"
            output += "\n"
    return output


def section_change_impact(ctx: AnalysisContext) -> str:
    """Section 9: Method-level change impact — who calls provider methods via gRPC."""
    if not ctx.provider:
        return ""

    output = "## 9. Change Impact (Method Consumers)\n\n"
    provider_repos = [rname for ftype, rname in ctx.findings if ftype == "provider"]

    for repo in provider_repos:
        consumers = ctx.conn.execute(
            """SELECT source, detail FROM graph_edges
               WHERE target = ? AND edge_type = 'grpc_method_call'
               ORDER BY source""",
            (repo,),
        ).fetchall()

        if consumers:
            output += f"**{repo}** is called by:\n"
            by_caller: dict[str, list[str]] = {}
            for c in consumers:
                caller = c["source"]
                method = c["detail"] or "unknown"
                by_caller.setdefault(caller, []).append(method)
            for caller, methods in sorted(by_caller.items()):
                output += f"  - **{caller}**: {', '.join(methods)}\n"
            output += "\n"

        if GATEWAY_REPO:
            gateway_routes = ctx.conn.execute(
                """SELECT detail FROM graph_edges
                   WHERE source = ? AND target = ? AND edge_type = 'runtime_routing'""",
                (GATEWAY_REPO, repo),
            ).fetchall()
            if gateway_routes:
                gw_callers = ctx.conn.execute(
                    """SELECT DISTINCT source, detail FROM graph_edges
                       WHERE target = ? AND edge_type = 'grpc_method_call'""",
                    (GATEWAY_REPO,),
                ).fetchall()
                if gw_callers:
                    output += f"**{repo}** via gateway ({GATEWAY_REPO}):\n"
                    for gc in gw_callers[:10]:
                        output += f"  - {gc['source']}: {gc['detail']}\n"
                    output += "\n"

    if WEBHOOK_REPOS:
        dispatch_repo = WEBHOOK_REPOS.get("dispatch", "")
        handler_repo = WEBHOOK_REPOS.get("handler", "")
        if dispatch_repo and handler_repo:
            wh_edges = ctx.conn.execute(
                """SELECT source, target, edge_type FROM graph_edges
                   WHERE detail = ? AND edge_type IN ('webhook_dispatch', 'webhook_handler')
                   ORDER BY edge_type""",
                (ctx.provider,),
            ).fetchall()
            if wh_edges:
                output += f"**Webhook chain** for `{ctx.provider}`:\n"
                for e in wh_edges:
                    arrow = "->" if e["edge_type"] == "webhook_dispatch" else "<-"
                    output += f"  {e['source']} {arrow} {e['target']}\n"
                output += "\n"

    return output


def section_provider_checklist(ctx: AnalysisContext) -> str:
    """Section 10: Infrastructure checklist for provider integrations."""
    if not ctx.provider or not INFRA_REPOS:
        return ""

    output = "## 10. Provider Integration Checklist\n\n"
    finding_repos = {rname for _, rname in ctx.findings}

    for item in INFRA_REPOS:
        repo = item.get("repo", "")
        desc = item.get("description", "")
        if not repo:
            continue

        in_findings = repo in finding_repos
        has_repo = ctx.conn.execute("SELECT 1 FROM repos WHERE name = ?", (repo,)).fetchone()
        if not has_repo:
            continue

        provider_match = ctx.conn.execute(
            "SELECT COUNT(*) as cnt FROM chunks WHERE repo_name = ? AND content LIKE ?",
            (repo, f"%{ctx.provider}%"),
        ).fetchone()["cnt"]

        status = "FOUND" if in_findings or provider_match > 0 else "CHECK"
        marker = "[x]" if status == "FOUND" else "[ ]"
        output += f"- {marker} **{repo}** — {desc}"
        if provider_match > 0 and not in_findings:
            output += f" ({provider_match} references found)"
        output += "\n"

    output += "\n"
    return output
