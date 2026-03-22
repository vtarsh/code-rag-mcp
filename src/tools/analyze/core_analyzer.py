"""CORE task analyzer — sections for cross-cutting platform tasks."""

from __future__ import annotations

import re

from src.config import DOMAIN_PATTERNS
from src.graph.queries import bfs_dependents

from .base import _KEYWORD_STOP_WORDS, AnalysisContext
from .classifier import TaskClassification


def run_core_analysis(ctx: AnalysisContext, classification: TaskClassification) -> str:
    """Run CORE-specific analysis sections. Returns combined markdown."""
    if not classification.domain.startswith("core-") and classification.domain not in ("bo", "hs", "unknown"):
        return ""

    output = ""
    output += _section_domain_repos(ctx, classification)
    output += _section_cascade(ctx, classification)
    output += _section_co_occurrence(ctx)
    output += _section_provider_fanout(ctx)
    output += _section_keyword_scan(ctx, classification)
    return output


def _section_domain_repos(ctx: AnalysisContext, classification: TaskClassification) -> str:
    """Find repos matching domain patterns + seed repos."""
    output = f"## Domain Analysis: {classification.domain}\n\n"

    if classification.matched_keywords:
        output += f"**Matched keywords**: {', '.join(classification.matched_keywords)}\n\n"

    # Check seed repos exist in our index
    seed_repos_found: list[str] = []
    for repo in classification.seed_repos:
        exists = ctx.conn.execute("SELECT 1 FROM repos WHERE name = ?", (repo,)).fetchone()
        if exists:
            seed_repos_found.append(repo)
            ctx.findings.append(("domain", repo))

    # Also find repos matching domain repo_patterns
    pattern = DOMAIN_PATTERNS.get(classification.domain, {})
    repo_patterns = pattern.get("repo_patterns", [])
    pattern_matched: list[str] = []

    for rp in repo_patterns:
        try:
            rows = ctx.conn.execute("SELECT name FROM repos").fetchall()
            for row in rows:
                name = row["name"]
                if re.match(rp, name) and name not in seed_repos_found:
                    # Only include if task keywords appear in repo content
                    for kw in classification.matched_keywords[:3]:
                        hit = ctx.conn.execute(
                            "SELECT 1 FROM chunks WHERE repo_name = ? AND chunks MATCH ? LIMIT 1",
                            (name, f'"{kw}"'),
                        ).fetchone()
                        if hit:
                            pattern_matched.append(name)
                            break
        except Exception:
            continue

    if seed_repos_found:
        output += "**Seed repos** (domain entry points):\n"
        for repo in seed_repos_found:
            # Get repo type and edge count for context
            info = ctx.conn.execute("SELECT type FROM repos WHERE name = ?", (repo,)).fetchone()
            dep_count = ctx.conn.execute(
                "SELECT COUNT(DISTINCT source) as cnt FROM graph_edges WHERE target = ? AND source NOT LIKE 'pkg:%'",
                (repo,),
            ).fetchone()["cnt"]
            rtype = info["type"] if info else "unknown"
            output += f"  - **{repo}** ({rtype}, {dep_count} dependents)\n"
        output += "\n"

    if pattern_matched:
        output += "**Pattern-matched repos** (content matches task keywords):\n"
        for repo in sorted(set(pattern_matched))[:10]:
            ctx.findings.append(("keyword", repo))
            output += f"  - **{repo}**\n"
        output += "\n"

    if not seed_repos_found and not pattern_matched:
        output += "No domain-specific repos identified.\n\n"

    return output


def _section_cascade(ctx: AnalysisContext, classification: TaskClassification) -> str:
    """Cascade prediction — BFS from seed repos to find affected downstream repos."""
    seed_repos = [
        repo
        for repo in classification.seed_repos
        if ctx.conn.execute("SELECT 1 FROM repos WHERE name = ?", (repo,)).fetchone()
    ]

    if not seed_repos:
        return ""

    output = "## Cascade Impact\n\n"
    output += "_Repos that depend on seed repos (BFS depth 2):_\n\n"

    all_affected: dict[str, tuple[str, str]] = {}  # repo → (via_seed, edge_type)

    for seed in seed_repos:
        levels = bfs_dependents(ctx.conn, seed, max_depth=2)
        for _level, deps in levels.items():
            for dep_name, edge_type in deps:
                if dep_name not in all_affected and dep_name not in seed_repos:
                    all_affected[dep_name] = (seed, edge_type)

    if not all_affected:
        return output + "No cascade dependencies found.\n\n"

    # Group by edge type for readability
    by_edge: dict[str, list[tuple[str, str]]] = {}
    for repo, (seed, etype) in all_affected.items():
        by_edge.setdefault(etype, []).append((repo, seed))

    for etype, repos in sorted(by_edge.items(), key=lambda x: len(x[1]), reverse=True):
        output += f"**{etype}** ({len(repos)} repos):\n"
        for repo, seed in sorted(repos)[:15]:
            ctx.findings.append(("cascade", repo))
            output += f"  - **{repo}** (via {seed})\n"
        if len(repos) > 15:
            output += f"  - ... and {len(repos) - 15} more\n"
        output += "\n"

    output += f"_Total: {len(all_affected)} repos potentially affected._\n\n"
    return output


def _section_co_occurrence(ctx: AnalysisContext) -> str:
    """Auto-add repos that co-occur with current findings in task_history (≥50% conditional probability)."""
    try:
        tables = {r[0] for r in ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "task_history" not in tables:
            return ""
    except Exception:
        return ""

    finding_repos = {rname for _, rname in ctx.findings}
    if not finding_repos:
        return ""

    # For each finding repo, check what other repos co-occur in ≥50% of CORE tasks
    import json

    rows = ctx.conn.execute("SELECT repos_changed FROM task_history WHERE ticket_id LIKE 'CORE-%'").fetchall()
    if len(rows) < 5:
        return ""

    # Count how often each repo appears and co-occurs with finding repos
    from collections import Counter

    repo_count: Counter[str] = Counter()
    cooccur_count: Counter[tuple[str, str]] = Counter()  # (finding_repo, other_repo)

    for r in rows:
        task_repos = set(json.loads(r["repos_changed"]) if r["repos_changed"] else [])
        for repo in task_repos:
            repo_count[repo] += 1
        overlap = finding_repos & task_repos
        for f_repo in overlap:
            for other in task_repos:
                if other not in finding_repos:
                    cooccur_count[(f_repo, other)] += 1

    # Find repos with ≥50% conditional probability and ≥3 co-occurrences
    boosted: dict[str, tuple[str, float]] = {}  # repo → (via, probability)
    for (f_repo, other), count in cooccur_count.items():
        if count < 3:
            continue
        prob = count / repo_count[f_repo]
        if prob >= 0.5 and other not in boosted:
            boosted[other] = (f_repo, prob)

    if not boosted:
        return ""

    output = "## Co-occurrence Boost\n\n"
    output += "_Repos that historically co-change with found repos (≥50% probability):_\n\n"

    for repo, (via, prob) in sorted(boosted.items(), key=lambda x: x[1][1], reverse=True)[:12]:
        ctx.findings.append(("co-occurrence", repo))
        output += f"  - **{repo}** — {prob:.0%} when {via} changes\n"
    output += "\n"
    return output


def _section_provider_fanout(ctx: AnalysisContext) -> str:
    """When cascade touches proto/types repos, enumerate all providers via runtime_routing."""
    from src.config import GATEWAY_REPO

    if not GATEWAY_REPO:
        return ""

    finding_repos = {rname for _, rname in ctx.findings}

    # Check if any finding is a proto/types repo that providers depend on
    proto_like = {"providers-proto", "libs-types", "node-libs-providers-common"}
    trigger_repos = finding_repos & proto_like

    if not trigger_repos:
        return ""

    # Get all providers via runtime_routing from gateway
    routed = ctx.conn.execute(
        """SELECT DISTINCT target FROM graph_edges
           WHERE source = ? AND edge_type = 'runtime_routing'
           ORDER BY target""",
        (GATEWAY_REPO,),
    ).fetchall()

    if not routed:
        return ""

    already = finding_repos
    new_providers = [r["target"] for r in routed if r["target"] not in already]

    if not new_providers:
        return ""

    output = f"## Provider Fan-out ({len(new_providers)} providers)\n\n"
    output += f"_Changes to {', '.join(sorted(trigger_repos))} affect all providers via gateway routing:_\n\n"

    for repo in new_providers:
        ctx.findings.append(("fanout", repo))
        output += f"  - **{repo}**\n"
    output += "\n"
    return output


def _section_keyword_scan(ctx: AnalysisContext, classification: TaskClassification) -> str:
    """Broad FTS search across ALL repos for task-specific keywords."""
    # Use keywords that are specific to this task (not generic stop words)
    scan_words = [w for w in ctx.words if len(w) > 5 and w not in _KEYWORD_STOP_WORDS]
    if not scan_words:
        return ""

    # Skip words that are too common (would match too many repos)
    already_found = {rname for _, rname in ctx.findings}

    new_finds: dict[str, list[str]] = {}  # repo → [matched keywords]

    for keyword in scan_words[:8]:
        try:
            rows = ctx.conn.execute(
                "SELECT DISTINCT repo_name FROM chunks WHERE chunks MATCH ? LIMIT 20",
                (f'"{keyword}"',),
            ).fetchall()
            for row in rows:
                repo = row["repo_name"]
                if repo not in already_found:
                    new_finds.setdefault(repo, []).append(keyword)
        except Exception:
            continue

    if not new_finds:
        return ""

    # Sort by number of keyword matches (more matches = more relevant)
    sorted_finds = sorted(new_finds.items(), key=lambda x: len(x[1]), reverse=True)

    # Only show repos with 2+ keyword matches (reduces noise)
    strong_finds = [(repo, kws) for repo, kws in sorted_finds if len(kws) >= 2]

    if not strong_finds:
        return ""

    output = "## Keyword Scan (broad search)\n\n"
    output += "_Repos matching 2+ task keywords (beyond domain/pattern repos):_\n\n"

    for repo, kws in strong_finds[:10]:
        ctx.findings.append(("keyword", repo))
        output += f"  - **{repo}** — matches: {', '.join(kws)}\n"
    output += "\n"

    return output
