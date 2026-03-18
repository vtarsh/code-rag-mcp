"""analyze_task MCP tool — find relevant repos/files/flows for a development task.

Auto-detects provider, checks proto/webhook/gateway, scans GitHub PRs,
generates completeness report.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from src.config import ORG
from src.container import get_db, require_db
from src.formatting import strip_repo_tag

_SAFE_REPO_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


def _validate_repo_name(repo_name: str) -> bool:
    """Validate repo name contains only safe characters."""
    return bool(_SAFE_REPO_NAME.match(repo_name))


def _gh_api(endpoint: str) -> dict | list | None:
    """Call GitHub API via gh CLI. Returns parsed JSON or None on failure."""
    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--paginate"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        logging.warning(f"GitHub API error: {e}")
    return None


def _find_task_branches(repos: list[str], task_id: str) -> dict[str, list[str]]:
    """Search for branches matching task_id in given repos."""
    results: dict[str, list[str]] = {}
    for repo_name in repos:
        if not _validate_repo_name(repo_name):
            continue
        branches = _gh_api(f"repos/{ORG}/{repo_name}/branches")
        if branches and isinstance(branches, list):
            matching = [b["name"] for b in branches if task_id.lower() in b["name"].lower()]
            if matching:
                results[repo_name] = matching
    return results


def _find_task_prs(repos: list[str], task_id: str) -> dict[str, list[dict]]:
    """Search for PRs matching task_id in given repos."""
    results: dict[str, list[dict]] = {}
    for repo_name in repos:
        if not _validate_repo_name(repo_name):
            continue
        prs = _gh_api(f"repos/{ORG}/{repo_name}/pulls?state=all&per_page=30")
        if not prs or not isinstance(prs, list):
            continue
        matching: list[dict] = []
        for pr in prs:
            head_ref = pr.get("head", {}).get("ref", "")
            title = pr.get("title", "")
            if task_id.lower() in head_ref.lower() or task_id.lower() in title.lower():
                pr_info = {
                    "number": pr["number"],
                    "title": pr["title"],
                    "state": pr["state"],
                    "merged_at": pr.get("merged_at"),
                    "branch": head_ref,
                }
                files = _gh_api(f"repos/{ORG}/{repo_name}/pulls/{pr['number']}/files")
                if files and isinstance(files, list):
                    pr_info["files"] = [f["filename"] for f in files]
                matching.append(pr_info)
        if matching:
            results[repo_name] = matching
    return results


def _check_method_exists(repo_name: str, method_name: str, conn) -> dict:
    """Check if a gRPC method already exists in a repo."""
    chunks = conn.execute(
        "SELECT file_path, content FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method' AND file_path LIKE ?",
        (repo_name, f"%{method_name}%"),
    ).fetchall()

    if chunks:
        return {"exists": True, "file_path": chunks[0]["file_path"], "snippet": chunks[0]["content"][:200]}

    registry = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND file_path LIKE '%methods/index%'", (repo_name,)
    ).fetchall()
    if registry:
        for r in registry:
            if method_name.lower() in r["content"].lower():
                return {
                    "exists": True,
                    "file_path": "methods/index.js",
                    "snippet": f"'{method_name}' registered in method index",
                }

    return {"exists": False}


@require_db
def analyze_task_tool(description: str, provider: str = "") -> str:
    """Analyze a development task and find ALL relevant repos, files, and dependencies.

    Args:
        description: Task description (e.g., "implement DirectDebitMandate verification for Trustly")
        provider: Optional provider name to focus on (e.g., "trustly", "paypal")
    """
    conn = get_db()
    try:
        return _analyze_task_impl(conn, description, provider)
    finally:
        conn.close()


def _analyze_task_impl(conn, description: str, provider: str) -> str:
    """Orchestrate 8-section task analysis. Each section is a helper function."""
    output = f"# Task Analysis\n\n**Task**: {description}\n\n"
    findings: list[tuple[str, str]] = []
    words = set(re.findall(r"[a-zA-Z]{3,}", description.lower()))

    # Auto-detect provider from task description
    if not provider:
        provider = _detect_provider(conn, words)

    # Section 0: domain knowledge (show first — most important context)
    output += _section_gotchas(conn, provider, words)

    # Sections 1-5: gather findings
    output += _section_provider(conn, provider, words, findings)
    output += _section_proto(conn, words, findings)
    output += _section_webhooks(conn, provider, findings)
    output += _section_gateway(conn, words, findings)
    output += _section_impact(conn, provider)

    # Section 6: check method existence
    method_output, task_methods, method_status = _section_methods(conn, words, findings)
    output += method_output

    # Section 7: GitHub activity
    github_output, pr_data, branch_data = _section_github(description, findings)
    output += github_output

    # Section 8: completeness report
    output += _section_completeness(conn, findings, task_methods, method_status, pr_data, branch_data)

    return output


def _section_gotchas(conn, provider: str, words: set[str]) -> str:
    """Section 0: Surface curated domain knowledge — show BEFORE code analysis."""
    # Search gotchas chunks for provider and task keywords
    queries = []
    if provider:
        queries.append(f'"{provider}"')
    for w in words:
        if len(w) > 5 and w not in _KEYWORD_STOP_WORDS:
            queries.append(f'"{w}"')

    if not queries:
        return ""

    seen_snippets: set[str] = set()
    results = []
    for q in queries:
        try:
            rows = conn.execute(
                "SELECT repo_name, file_path, snippet(chunks, 0, '>>>', '<<<', '...', 40) as snippet "
                "FROM chunks WHERE chunks MATCH ? AND file_type = 'gotchas' ORDER BY rank LIMIT 5",
                (q,),
            ).fetchall()
            for row in rows:
                snip = row["snippet"][:300]
                if snip not in seen_snippets:
                    seen_snippets.add(snip)
                    results.append(row)
        except Exception:
            continue

    if not results:
        return ""

    output = "## ⚠️ Known Gotchas (from past reviews & production bugs)\n\n"
    output += "_These traps are NOT visible from code — read before coding._\n\n"
    for row in results[:8]:
        snip = strip_repo_tag(row["snippet"])
        output += f"**{row['repo_name']}** (`{row['file_path']}`):\n{snip}\n\n"
    return output


def _detect_provider(conn, words: set[str]) -> str:
    """Auto-detect provider name from task description words."""
    provider_repos = conn.execute(
        "SELECT name FROM repos WHERE name LIKE 'grpc-apm-%' OR name LIKE 'grpc-providers-%'"
    ).fetchall()
    provider_names: set[str] = set()
    for r in provider_repos:
        parts = r["name"].split("-")
        if len(parts) >= 3:
            provider_names.add(parts[-1])
    for p in provider_names:
        if p in words:
            return p
    return ""


_KEYWORD_STOP_WORDS = frozenset(
    {
        "should",
        "which",
        "where",
        "their",
        "about",
        "these",
        "those",
        "would",
        "could",
        "check",
        "start",
        "needs",
    }
)


def _section_provider(conn, provider: str, words: set[str], findings: list[tuple[str, str]]) -> str:
    """Section 1: Find provider service repos and keyword matches."""
    if not provider:
        return ""

    output = f"## 1. Provider: {provider}\n\n"
    for prefix in ["grpc-apm-", "grpc-providers-"]:
        repo_name = f"{prefix}{provider}"
        repo = conn.execute("SELECT * FROM repos WHERE name = ?", (repo_name,)).fetchone()
        if not repo:
            continue

        methods = conn.execute(
            "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method'", (repo_name,)
        ).fetchall()
        method_names = [Path(m["file_path"]).stem for m in methods]
        output += f"**{repo_name}** ({repo['type']})\n  Methods: {', '.join(method_names)}\n\n"
        findings.append(("provider", repo_name))

        for keyword in words:
            if len(keyword) > 4 and keyword not in _KEYWORD_STOP_WORDS:
                matches = conn.execute(
                    "SELECT snippet(chunks, 0, '>>>', '<<<', '...', 20) as snippet FROM chunks WHERE chunks MATCH ? AND repo_name = ? LIMIT 2",
                    (f'"{keyword}"', repo_name),
                ).fetchall()
                if matches:
                    for m in matches:
                        snip = strip_repo_tag(m["snippet"])
                        output += f"  Found `{keyword}`: {snip[:150]}\n"
                    output += "\n"

    return output


def _section_proto(conn, words: set[str], findings: list[tuple[str, str]]) -> str:
    """Section 2: Check proto contract for available RPC methods."""
    output = "## 2. Proto Contract (providers-proto)\n\n"
    proto_service = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = 'providers-proto' AND chunk_type = 'proto_service'"
    ).fetchall()
    if proto_service:
        proto_methods: set[str] = set()
        for row in proto_service:
            for match in re.finditer(r"rpc\s+(\w+)", row["content"]):
                proto_methods.add(match.group(1))
        output += f"Available RPC methods: {', '.join(sorted(proto_methods))}\n\n"
        for word in words:
            matching = [m for m in proto_methods if word in m.lower()]
            if matching:
                output += f"  `{word}` matches proto method: **{', '.join(matching)}**\n"
        output += "\n"
    findings.append(("proto", "providers-proto"))
    return output


def _section_webhooks(conn, provider: str, findings: list[tuple[str, str]]) -> str:
    """Section 3: Find webhook handling for the provider."""
    if not provider:
        return ""

    output = "## 3. Webhook Handling\n\n"
    webhook_chunks = conn.execute(
        "SELECT repo_name, file_path, snippet(chunks, 0, '>>>', '<<<', '...', 25) as snippet "
        "FROM chunks WHERE chunks MATCH ? AND repo_name LIKE '%webhook%' ORDER BY rank LIMIT 10",
        (f'"{provider}"',),
    ).fetchall()
    if not webhook_chunks:
        return output + "No webhook handling found for this provider.\n\n"

    repos_seen: set[str] = set()
    for row in webhook_chunks:
        rname = row["repo_name"]
        if rname not in repos_seen:
            repos_seen.add(rname)
            output += f"**{rname}**\n"
            findings.append(("webhook", rname))
        snip = strip_repo_tag(row["snippet"])
        output += f"  `{row['file_path']}`: {snip[:150]}\n"
    output += "\n"
    return output


def _section_gateway(conn, words: set[str], findings: list[tuple[str, str]]) -> str:
    """Section 4: Check payment gateway methods."""
    output = "## 4. Payment Gateway\n\n"
    gateway_methods = conn.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE repo_name = 'grpc-payment-gateway' AND file_type = 'grpc_method'"
    ).fetchall()
    if gateway_methods:
        method_names = [Path(m["file_path"]).stem for m in gateway_methods]
        output += f"**grpc-payment-gateway** methods: {', '.join(method_names)}\n"
        matching_methods = [m for m in method_names if m.lower() in words]
        if matching_methods:
            output += f"  Task-relevant methods: **{', '.join(matching_methods)}**\n"
        output += "\n"
        findings.append(("gateway", "grpc-payment-gateway"))
    return output


def _section_impact(conn, provider: str) -> str:
    """Section 5: Trace dependency impact for provider repos."""
    output = "## 5. Impact Analysis\n\n"
    if not provider:
        return output

    for prefix in ["grpc-apm-", "grpc-providers-"]:
        repo_name = f"{prefix}{provider}"
        deps = conn.execute(
            "SELECT target, edge_type FROM graph_edges WHERE source = ? AND target NOT LIKE 'pkg:%'", (repo_name,)
        ).fetchall()
        if deps:
            output += f"**{repo_name}** depends on:\n"
            for d in deps:
                output += f"  - {d['target']} ({d['edge_type']})\n"
            output += "\n"
    return output


def _section_methods(conn, words: set[str], findings: list[tuple[str, str]]) -> tuple[str, set[str], dict[str, dict]]:
    """Section 6: Check if gRPC methods exist in provider/gateway repos.

    Returns (output, task_methods, method_status).
    """
    output = "## 6. Code Analysis (method existence)\n\n"
    known_methods_rows = conn.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE file_type = 'grpc_method'"
    ).fetchall()
    known_method_names = {Path(r["file_path"]).stem.lower() for r in known_methods_rows}
    task_methods: set[str] = words & known_method_names

    method_status: dict[str, dict] = {}
    for ftype, rname in findings:
        if ftype in ("provider", "gateway"):
            for method in task_methods:
                result = _check_method_exists(rname, method, conn)
                key = f"{rname}:{method}"
                method_status[key] = result
                status = "EXISTS" if result["exists"] else "MISSING"
                output += f"- `{rname}` → `{method}`: **{status}**"
                if result["exists"]:
                    output += f" ({result.get('file_path', '')})"
                output += "\n"

    if not task_methods:
        output += "No specific method names detected in task description.\n"
    output += "\n"
    return output, task_methods, method_status


def _section_github(
    description: str, findings: list[tuple[str, str]]
) -> tuple[str, dict[str, list[dict]], dict[str, list[str]]]:
    """Section 7: Search GitHub for branches/PRs matching task ID.

    Returns (output, pr_data, branch_data).
    """
    output = "## 7. GitHub Activity\n\n"
    task_id_match = re.search(r"(PI|CORE|PAY|FE|BE|INF)-?\d+", description, re.IGNORECASE)
    task_id = task_id_match.group(0).lower().replace("_", "-") if task_id_match else ""

    all_repos = list({rname for _, rname in findings} | {"e2e-tests"})

    pr_data: dict[str, list[dict]] = {}
    branch_data: dict[str, list[str]] = {}

    if not task_id:
        output += "No task ID detected. Add a task ID (e.g., 'PI-54') for PR/branch scanning.\n\n"
        return output, pr_data, branch_data

    output += f"**Task ID detected**: `{task_id}`\n\n"
    branch_data = _find_task_branches(all_repos, task_id)
    pr_data = _find_task_prs(all_repos, task_id)

    if not branch_data and not pr_data:
        output += f"No branches or PRs found matching `{task_id}` in any repo.\n\n"
        return output, pr_data, branch_data

    output += "### Found activity:\n\n"
    covered_repos: set[str] = set()
    for repo_name in all_repos:
        branches = branch_data.get(repo_name, [])
        prs = pr_data.get(repo_name, [])
        if not branches and not prs:
            continue
        covered_repos.add(repo_name)
        output += f"**{repo_name}**:\n"
        for b in branches:
            output += f"  - Branch: `{b}`\n"
        for pr in prs:
            status = "MERGED" if pr["merged_at"] else pr["state"].upper()
            output += f"  - PR #{pr['number']} [{status}]: {pr['title']}\n"
            if pr.get("files"):
                for f in pr["files"][:8]:
                    output += f"    - {f}\n"
        output += "\n"

    uncovered = set(all_repos) - covered_repos
    if uncovered:
        output += "### No activity found:\n"
        for repo_name in sorted(uncovered):
            output += f"  - **{repo_name}**\n"
        output += "\n"

    return output, pr_data, branch_data


def _section_completeness(
    conn,
    findings: list[tuple[str, str]],
    task_methods: set[str],
    method_status: dict[str, dict],
    pr_data: dict[str, list[dict]],
    branch_data: dict[str, list[str]],
) -> str:
    """Section 8: Build completeness checklist from all findings."""
    output = "## 8. Completeness Report\n\n"
    checklist: list[tuple[str, str, str, str, bool]] = []

    for ftype, rname in findings:
        needs_change = True
        status = "TODO"
        reason = ""

        if ftype == "gateway":
            for method in task_methods:
                key = f"{rname}:{method}"
                ms = method_status.get(key)
                if ms and ms["exists"]:
                    needs_change = False
                    status = "OK"
                    reason = f"`{method}` already implemented"
        elif ftype == "proto":
            for method in task_methods:
                proto_check = conn.execute(
                    "SELECT content FROM chunks WHERE repo_name = 'providers-proto' AND chunk_type = 'proto_service' AND content LIKE ?",
                    (f"%{method}%",),
                ).fetchone()
                if proto_check:
                    needs_change = False
                    status = "OK"
                    reason = f"`{method}` RPC already in proto"

        pr_exists = rname in pr_data
        branch_exists = rname in branch_data
        if pr_exists:
            pr = pr_data[rname][0]
            if pr["merged_at"]:
                status = "DONE"
                reason = f"PR #{pr['number']} merged"
            else:
                status = "IN PROGRESS"
                reason = f"PR #{pr['number']} ({pr['state']})"
        elif branch_exists:
            status = "IN PROGRESS"
            reason = f"Branch `{branch_data[rname][0]}` exists"
        elif not needs_change:
            pass

        label = {
            "provider": "Implement method handler",
            "proto": "Proto contract",
            "webhook": "Webhook activity",
            "gateway": "Gateway routing",
        }.get(ftype, ftype)
        checklist.append((rname, label, status, reason, needs_change))

    # e2e-tests
    e2e_status = "TODO"
    e2e_reason = ""
    if "e2e-tests" in pr_data:
        pr = pr_data["e2e-tests"][0]
        e2e_status = "DONE" if pr["merged_at"] else "IN PROGRESS"
        e2e_reason = f"PR #{pr['number']}"
    elif "e2e-tests" in branch_data:
        e2e_status = "IN PROGRESS"
        e2e_reason = f"Branch `{branch_data['e2e-tests'][0]}`"
    checklist.append(("e2e-tests", "E2E tests", e2e_status, e2e_reason, e2e_status == "TODO"))

    done = sum(1 for _, _, s, _, _ in checklist if s in ("DONE", "OK"))
    in_progress = sum(1 for _, _, s, _, _ in checklist if s == "IN PROGRESS")
    todo = sum(1 for _, _, s, _, _ in checklist if s == "TODO")
    output += f"**Progress**: {done} done, {in_progress} in progress, {todo} todo (out of {len(checklist)})\n\n"

    output += "| Repo | Area | Status | Detail |\n|------|------|--------|--------|\n"
    for rname, label, status, reason, _ in checklist:
        icon = {"DONE": "[x]", "OK": "[x]", "IN PROGRESS": "[-]", "TODO": "[ ]"}.get(status, "[ ]")
        output += f"| {icon} {rname} | {label} | **{status}** | {reason} |\n"
    output += "\n"
    return output
