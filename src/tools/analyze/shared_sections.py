"""Shared sections that run for ALL task types (provider and non-provider)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.config import GATEWAY_REPO, PROTO_REPOS
from src.formatting import strip_repo_tag

from .base import _KEYWORD_STOP_WORDS, AnalysisContext, extract_task_id, fts_queries
from .github_helpers import find_task_branches, find_task_prs
from .method_helpers import check_method_exists


def section_gotchas(ctx: AnalysisContext) -> str:
    """Section 0: Surface curated domain knowledge."""
    queries = fts_queries(ctx.provider, ctx.words)
    if not queries:
        return ""

    seen_snippets: set[str] = set()
    results = []
    for q in queries:
        try:
            rows = ctx.conn.execute(
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


def section_existing_tasks(ctx: AnalysisContext) -> str:
    """Section 0.5: Surface existing task documents."""
    queries = fts_queries(ctx.provider, ctx.words)
    if not queries:
        return ""

    seen_snippets: set[str] = set()
    results = []
    for q in queries:
        try:
            rows = ctx.conn.execute(
                "SELECT repo_name, chunk_type, snippet(chunks, 0, '>>>', '<<<', '...', 30) as snippet "
                "FROM chunks WHERE chunks MATCH ? AND file_type = 'task' "
                "AND chunk_type != 'task_progress' ORDER BY rank LIMIT 5",
                (q,),
            ).fetchall()
            for row in rows:
                if ctx.provider and ctx.provider not in row["repo_name"]:
                    continue
                snip = row["snippet"][:300]
                if snip not in seen_snippets:
                    seen_snippets.add(snip)
                    results.append(row)
        except Exception:
            continue

    if not results:
        return ""

    output = "## 📋 Existing Task Documents\n\n"
    output += "_Found related task context from previous work._\n\n"
    for row in results[:6]:
        snip = strip_repo_tag(row["snippet"])
        output += f"**{row['repo_name']}** ({row['chunk_type']}): {snip}\n\n"
    return output


def section_task_patterns(ctx: AnalysisContext) -> str:
    """Section 0.6: Surface learned patterns from task history."""
    output_parts: list[str] = []
    pattern_repos: list[tuple[str, str, int]] = []

    try:
        tables = {r[0] for r in ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "task_patterns" not in tables:
            return ""
        patterns = ctx.conn.execute(
            "SELECT pattern_type, missed_repo, trigger_repos, occurrences, confidence "
            "FROM task_patterns ORDER BY occurrences DESC"
        ).fetchall()
        if not patterns:
            return ""
    except Exception:
        return ""

    # Similar tasks from FTS (exclude self-match to prevent data leakage)
    similar_tasks: list[dict] = []
    current_task_id = extract_task_id(ctx.description)
    try:
        if "task_history_fts" in tables:
            search_terms = []
            if ctx.provider:
                search_terms.append(ctx.provider)
            for w in ctx.words:
                if len(w) > 5 and w not in _KEYWORD_STOP_WORDS:
                    search_terms.append(w)
            if search_terms:
                fts_query = " OR ".join(search_terms[:5])
                rows = ctx.conn.execute(
                    """SELECT t.ticket_id, t.summary, t.repos_changed, t.files_changed, t.pr_urls
                       FROM task_history_fts fts
                       JOIN task_history t ON t.id = fts.rowid
                       WHERE task_history_fts MATCH ?
                       ORDER BY rank LIMIT 6""",
                    (fts_query,),
                ).fetchall()
                for r in rows:
                    # Skip self-match (same ticket ID)
                    if current_task_id and r[0].lower() == current_task_id:
                        continue
                    # Extract repo names from PR URLs
                    pr_repos: list[str] = []
                    if r[4]:
                        try:
                            pr_urls = json.loads(r[4])
                            for url in pr_urls:
                                m = re.search(r"github\.com/[^/]+/([^/]+)/pull", url)
                                if m:
                                    pr_repos.append(m.group(1))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    similar_tasks.append(
                        {
                            "ticket": r[0],
                            "summary": r[1],
                            "repos": json.loads(r[2]) if r[2] else [],
                            "files": json.loads(r[3]) if r[3] else [],
                            "pr_repos": list(dict.fromkeys(pr_repos)),  # dedupe, preserve order
                        }
                    )
                similar_tasks = similar_tasks[:3]
    except Exception:
        pass

    if similar_tasks:
        output_parts.append("## Historical Task Patterns\n")
        output_parts.append("_Based on similar past tasks, these repos/files were involved:_\n")
        existing_finding_repos = {r for _, r, *_ in ctx.findings}
        for t in similar_tasks:
            repos_str = ", ".join(f"**{r}**" for r in t["repos"][:8])
            if len(t["repos"]) > 8:
                repos_str += f" (+{len(t['repos']) - 8} more)"
            output_parts.append(f"**{t['ticket']}** — {t['summary']}\n  Repos: {repos_str}\n")

            # PR URL signal: repos extracted from PR URLs of similar past tasks
            pr_repos = t.get("pr_repos", [])
            if pr_repos:
                pr_repos_str = ", ".join(f"**{r}**" for r in pr_repos[:8])
                output_parts.append(f"  PR repos: {pr_repos_str}\n")
                for repo in pr_repos:
                    if repo not in existing_finding_repos:
                        ctx.findings.append(("pr_url_signal", repo, "high"))
                        existing_finding_repos.add(repo)

            # Similar-task boost: if past task shares ≥3 repos with current findings,
            # inject its other repos as findings (high confidence of same scope)
            overlap = existing_finding_repos & set(t["repos"])
            if len(overlap) >= 3:
                for repo in t["repos"]:
                    if repo not in existing_finding_repos:
                        ctx.findings.append(("similar_task", repo, "medium"))
                        existing_finding_repos.add(repo)

    # Upstream caller patterns
    upstream_patterns = [p for p in patterns if p[0] == "upstream_caller" and p[3] >= 5]
    if upstream_patterns:
        if not output_parts:
            output_parts.append("## Historical Task Patterns\n")
        output_parts.append("\n### ⚡ Main Flow Repos (frequently missed)\n")
        output_parts.append("_These repos are part of the main request flow and were missed in many past tasks:_\n")
        for p in upstream_patterns[:6]:
            output_parts.append(f"- **{p[1]}** — missed in {p[3]} past tasks (avg confidence {p[4]:.0%})\n")
            pattern_repos.append((p[1], "upstream_caller", p[3]))

    # Co-occurrence patterns
    co_patterns = [p for p in patterns if p[0] == "co_occurrence"]
    relevant_co: list[str] = []
    for p in co_patterns:
        trigger_repos = json.loads(p[2]) if isinstance(p[2], str) else p[2]
        missed = p[1]
        trigger_words = set()
        for repo in trigger_repos:
            trigger_words.update(repo.lower().split("-"))
        is_relevant = (ctx.provider and ctx.provider in trigger_words) or bool(ctx.words & trigger_words)
        if is_relevant:
            relevant_co.append(
                f"When changing **{', '.join(trigger_repos[:3])}** → also check **{missed}** ({p[3]} past occurrences)"
            )
            pattern_repos.append((missed, "co_occurrence", p[3]))

    if relevant_co:
        if not output_parts:
            output_parts.append("## Historical Task Patterns\n")
        output_parts.append("\n### 🔗 Co-occurrence Patterns\n")
        output_parts.append("_Repos frequently missed together in past tasks:_\n")
        for p in relevant_co[:5]:
            output_parts.append(f"- {p}\n")

    # Cluster patterns
    cluster_patterns = [p for p in patterns if p[0] == "cluster"]
    relevant_clusters: list[str] = []
    for p in cluster_patterns:
        paired_with = json.loads(p[2]) if isinstance(p[2], str) else p[2]
        known = {r for r, _, _ in pattern_repos}
        if p[1] in known or any(pw in known for pw in paired_with):
            relevant_clusters.append(f"**{p[1]}** + **{', '.join(paired_with)}** — co-missed in {p[3]} tasks")

    if relevant_clusters:
        output_parts.append("\n### 📦 Gap Clusters (repos missed together)\n")
        for c in relevant_clusters[:5]:
            output_parts.append(f"- {c}\n")

    # Inject pattern repos into findings
    existing_finding_repos = {r for _, r, *_ in ctx.findings}
    added = 0
    for repo, _reason, occurrences in pattern_repos:
        if repo not in existing_finding_repos and occurrences >= 5:
            ctx.findings.append(("pattern", repo, "medium"))
            existing_finding_repos.add(repo)
            added += 1

    if added:
        output_parts.append(f"\n_Added {added} pattern-based repos to completeness checklist._\n")

    if output_parts:
        output_parts.append("\n")
    return "".join(output_parts)


def section_file_patterns(ctx: AnalysisContext) -> str:
    """Section 0.7: File-level patterns — hub files, directory pairs, cross-repo files."""
    try:
        tables = {r[0] for r in ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "file_patterns" not in tables:
            return ""
    except Exception:
        return ""

    output_parts: list[str] = []
    relevant_terms = set(ctx.words)
    if ctx.provider:
        relevant_terms.add(ctx.provider)

    # Hub files
    hub_files = ctx.conn.execute(
        "SELECT source, occurrences FROM file_patterns WHERE pattern_type = 'hub_file' ORDER BY occurrences DESC"
    ).fetchall()
    relevant_hubs = []
    for row in hub_files:
        source = row[0]
        source_lower = source.lower()
        if any(t in source_lower for t in relevant_terms if len(t) > 3):
            relevant_hubs.append((source, row[1]))
    if relevant_hubs:
        output_parts.append("## 📁 File-Level Patterns\n")
        output_parts.append("### Hub Files (changed in many tasks)\n")
        for path, occ in relevant_hubs[:5]:
            output_parts.append(f"- `{path}` — changed in {occ} tasks\n")

    # Directory pairs
    dir_pairs = ctx.conn.execute(
        "SELECT source, target, occurrences, confidence FROM file_patterns "
        "WHERE pattern_type = 'dir_pair' AND occurrences >= 3 ORDER BY occurrences DESC"
    ).fetchall()
    relevant_dirs = []
    for row in dir_pairs:
        src, tgt, occ, conf = row
        src_lower, tgt_lower = src.lower(), tgt.lower()
        if any(t in src_lower or t in tgt_lower for t in relevant_terms if len(t) > 3):
            relevant_dirs.append((src, tgt, occ, conf))
    if relevant_dirs:
        if not output_parts:
            output_parts.append("## 📁 File-Level Patterns\n")
        output_parts.append("\n### Directory Pairs (co-changed)\n")
        for src, tgt, occ, conf in relevant_dirs[:6]:
            output_parts.append(f"- `{src}` ↔ `{tgt}` — {occ} tasks ({conf:.0%} confidence)\n")

    # Cross-repo file pairs
    cross_files = ctx.conn.execute(
        "SELECT source, target, occurrences, confidence FROM file_patterns "
        "WHERE pattern_type = 'cross_repo_file' AND occurrences >= 3 ORDER BY occurrences DESC"
    ).fetchall()
    relevant_cross = []
    for row in cross_files:
        src, tgt, occ, conf = row
        src_lower, tgt_lower = src.lower(), tgt.lower()
        if any(t in src_lower or t in tgt_lower for t in relevant_terms if len(t) > 3):
            relevant_cross.append((src, tgt, occ, conf))
    if relevant_cross:
        if not output_parts:
            output_parts.append("## 📁 File-Level Patterns\n")
        output_parts.append("\n### Cross-Repo File Pairs\n")
        for src, tgt, occ, conf in relevant_cross[:5]:
            output_parts.append(f"- `{src}` → `{tgt}` — {occ} tasks ({conf:.0%} confidence)\n")

    if output_parts:
        output_parts.append("\n")
    return "".join(output_parts)


def section_proto(ctx: AnalysisContext) -> str:
    """Section 2: Check proto contract for available RPC methods."""
    proto_repo = PROTO_REPOS[0] if PROTO_REPOS else ""
    output = f"## 2. Proto Contract ({proto_repo or 'N/A'})\n\n"
    if proto_repo:
        proto_service = ctx.conn.execute(
            "SELECT content FROM chunks WHERE repo_name = ? AND chunk_type = 'proto_service'",
            (proto_repo,),
        ).fetchall()
        if proto_service:
            proto_methods: set[str] = set()
            for row in proto_service:
                for match in re.finditer(r"rpc\s+(\w+)", row["content"]):
                    proto_methods.add(match.group(1))
            output += f"Available RPC methods: {', '.join(sorted(proto_methods))}\n\n"
            for word in ctx.words:
                matching = [m for m in proto_methods if word in m.lower()]
                if matching:
                    output += f"  `{word}` matches proto method: **{', '.join(matching)}**\n"
            output += "\n"
        ctx.findings.append(("proto", proto_repo, "high"))
    return output


def section_gateway(ctx: AnalysisContext) -> str:
    """Section 4: Check payment gateway methods."""
    output = "## 4. Payment Gateway\n\n"
    if not GATEWAY_REPO:
        return output
    gateway_methods = ctx.conn.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method'",
        (GATEWAY_REPO,),
    ).fetchall()
    if gateway_methods:
        method_names = [Path(m["file_path"]).stem for m in gateway_methods]
        output += f"**{GATEWAY_REPO}** methods: {', '.join(method_names)}\n"
        matching_methods = [m for m in method_names if m.lower() in ctx.words]
        if matching_methods:
            output += f"  Task-relevant methods: **{', '.join(matching_methods)}**\n"
        output += "\n"
        ctx.findings.append(("gateway", GATEWAY_REPO, "high"))
    return output


def section_methods(ctx: AnalysisContext) -> tuple[str, set[str], dict[str, dict]]:
    """Section 6: Check if gRPC methods exist in provider/gateway repos."""
    output = "## 6. Code Analysis (method existence)\n\n"
    known_methods_rows = ctx.conn.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE file_type = 'grpc_method'"
    ).fetchall()
    known_method_names = {Path(r["file_path"]).stem.lower() for r in known_methods_rows}
    task_methods: set[str] = ctx.words & known_method_names

    method_status: dict[str, dict] = {}
    for ftype, rname, *_conf in ctx.findings:
        if ftype in ("provider", "gateway"):
            for method in task_methods:
                result = check_method_exists(rname, method, ctx.conn)
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


def section_github(ctx: AnalysisContext) -> tuple[str, dict[str, list[dict]], dict[str, list[str]]]:
    """Section 7: Search GitHub for branches/PRs matching task ID."""
    output = "## 7. GitHub Activity\n\n"
    task_id = extract_task_id(ctx.description)

    all_repos = list({rname for _, rname, *_ in ctx.findings} | {"e2e-tests"})

    pr_data: dict[str, list[dict]] = {}
    branch_data: dict[str, list[str]] = {}

    if not task_id:
        output += "No task ID detected. Add a task ID (e.g., 'PI-54') for PR/branch scanning.\n\n"
        return output, pr_data, branch_data

    output += f"**Task ID detected**: `{task_id}`\n\n"
    branch_data = find_task_branches(all_repos, task_id)
    pr_data = find_task_prs(all_repos, task_id)

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


_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

_FTYPE_LABELS = {
    "provider": "Implement method handler",
    "proto": "Proto contract",
    "webhook": "Webhook activity",
    "gateway": "Gateway routing",
    "pattern": "Pattern-based (historically missed)",
    "similar_task": "Similar past task",
    "bulk_migration": "Bulk migration",
    "npm_dep_scan": "npm dependency",
    "repo_ref": "Repo name in description",
    "domain_template": "Domain template",
    "co_change_rule": "Co-change rule (always changes together)",
    "pr_url_signal": "PR URL from similar task",
    "domain": "Domain service",
    "cascade": "Cascade dependency",
    "downstream": "Downstream dependency",
    "reverse_cascade": "Reverse cascade (called by found repo)",
    "keyword": "Keyword match",
    "co-occurrence": "Co-occurrence",
    "universal": "Frequently changed",
    "fanout": "Provider fan-out",
    "function": "Function reference",
}


def section_completeness(
    ctx: AnalysisContext,
    task_methods: set[str],
    method_status: dict[str, dict],
    pr_data: dict[str, list[dict]],
    branch_data: dict[str, list[str]],
) -> str:
    """Section 8: Build completeness checklist from all findings, grouped by confidence."""
    output = "## 8. Completeness Report\n\n"

    # Deduplicate by repo name, keeping highest confidence
    best: dict[str, tuple[str, str, str]] = {}  # repo → (ftype, repo, confidence)
    for ftype, rname, *rest in ctx.findings:
        conf = rest[0] if rest else "medium"
        if rname not in best or _CONFIDENCE_RANK.get(conf, 1) < _CONFIDENCE_RANK.get(best[rname][2], 1):
            best[rname] = (ftype, rname, conf)

    # Build checklist entries: (repo, label, status, reason, confidence)
    checklist: list[tuple[str, str, str, str, str]] = []

    for rname, (ftype, _, conf) in best.items():
        status = "TODO"
        reason = ""

        if ftype == "gateway":
            for method in task_methods:
                key = f"{rname}:{method}"
                ms = method_status.get(key)
                if ms and ms["exists"]:
                    status = "OK"
                    reason = f"`{method}` already implemented"
        elif ftype == "proto":
            for method in task_methods:
                _proto_repo = PROTO_REPOS[0] if PROTO_REPOS else ""
                proto_check = (
                    ctx.conn.execute(
                        "SELECT content FROM chunks WHERE repo_name = ? AND chunk_type = 'proto_service' AND content LIKE ?",
                        (_proto_repo, f"%{method}%"),
                    ).fetchone()
                    if _proto_repo
                    else None
                )
                if proto_check:
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

        label = _FTYPE_LABELS.get(ftype, ftype)
        checklist.append((rname, label, status, reason, conf))

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
    checklist.append(("e2e-tests", "E2E tests", e2e_status, e2e_reason, "high"))

    done = sum(1 for _, _, s, _, _ in checklist if s in ("DONE", "OK"))
    in_progress = sum(1 for _, _, s, _, _ in checklist if s == "IN PROGRESS")
    todo = sum(1 for _, _, s, _, _ in checklist if s == "TODO")
    output += f"**Progress**: {done} done, {in_progress} in progress, {todo} todo (out of {len(checklist)})\n\n"

    # Group by confidence tier
    high_items = [(r, lbl, s, d) for r, lbl, s, d, c in checklist if c == "high"]
    medium_items = [(r, lbl, s, d) for r, lbl, s, d, c in checklist if c == "medium"]
    low_items = [(r, lbl, s, d) for r, lbl, s, d, c in checklist if c == "low"]

    def _render_row(rname: str, label: str, status: str, reason: str, marker: str) -> str:
        icon = {"DONE": "[x]", "OK": "[x]", "IN PROGRESS": "[-]"}.get(status, marker)
        return f"| {icon} **{rname}** | {label} | **{status}** | {reason} |\n"

    if high_items:
        output += "### High Confidence\n\n"
        output += "| Repo | Area | Status | Detail |\n|------|------|--------|--------|\n"
        for r, lbl, s, d in high_items:
            output += _render_row(r, lbl, s, d, "[x]")
        output += "\n"

    if medium_items:
        output += "### Medium Confidence\n\n"
        output += "| Repo | Area | Status | Detail |\n|------|------|--------|--------|\n"
        for r, lbl, s, d in medium_items:
            output += _render_row(r, lbl, s, d, "[?]")
        output += "\n"

    if low_items:
        output += f"<details>\n<summary>Low Confidence ({len(low_items)} repos)</summary>\n\n"
        output += "| Repo | Area | Status | Detail |\n|------|------|--------|--------|\n"
        for r, lbl, s, d in low_items:
            output += _render_row(r, lbl, s, d, "[ ]")
        output += "\n</details>\n\n"

    return output


def section_ci_risk(ctx: AnalysisContext) -> str:
    """Section 11: CI risk — recent failures in affected repos."""
    has_ci = ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ci_runs'").fetchone()
    if not has_ci:
        return ""

    finding_repos = {rname for _, rname, *_ in ctx.findings}
    if not finding_repos:
        return ""

    risky: list[tuple[str, int, int]] = []
    for repo in sorted(finding_repos):
        row = ctx.conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN conclusion = 'failure' THEN 1 ELSE 0 END) as fails
               FROM ci_runs WHERE repo_name = ?""",
            (repo,),
        ).fetchone()
        if row and row["fails"] and row["fails"] > 0:
            risky.append((repo, row["fails"], row["total"]))

    if not risky:
        return ""

    output = "## 11. CI Risk\n\n"
    risky.sort(key=lambda x: x[1], reverse=True)
    for repo, fails, total in risky:
        pct = fails / total * 100 if total > 0 else 0
        level = "HIGH" if pct > 30 else "MEDIUM" if pct > 15 else "LOW"
        output += f"- **{repo}**: {fails}/{total} runs failed ({pct:.0f}%) — {level} risk\n"
    output += "\n"
    return output
