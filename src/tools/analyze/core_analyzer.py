"""CORE task analyzer — sections for cross-cutting platform tasks."""

from __future__ import annotations

import re

from src.config import CO_CHANGE_RULES, DOMAIN_PATTERNS
from src.graph.queries import bfs_dependents

from .base import _KEYWORD_STOP_WORDS, AnalysisContext
from .classifier import TaskClassification


def run_core_analysis(ctx: AnalysisContext, classification: TaskClassification) -> str:
    """Run CORE-specific analysis sections. Returns combined markdown."""
    primary = classification.domain.split("+")[0]
    # Also run for PI tasks with secondary core-* domain but no provider detected
    # (e.g., "pi+core-dispute" for chargebacks911 integration)
    has_secondary_core = any(d.startswith("core-") for d in classification.domain.split("+")[1:])
    if (
        not primary.startswith("core-")
        and primary not in ("bo", "hs", "unknown")
        and not (has_secondary_core and not ctx.provider)
    ):
        return ""

    output = ""
    output += _section_domain_repos(ctx, classification)
    output += _section_cascade(ctx, classification)
    output += _section_bulk_migration(ctx)
    output += _section_provider_fanout(ctx)
    output += _section_function_search(ctx)
    output += _section_keyword_scan(ctx, classification)
    return output


def run_co_occurrence(ctx: AnalysisContext) -> str:
    """Run co-occurrence boost for ANY domain (PI, CORE, BO, HS). Called from orchestrator."""
    return _section_co_occurrence(ctx)


def run_co_change_rules(ctx: AnalysisContext) -> str:
    """Apply high-confidence co-change rules from conventions.yaml.

    After co-occurrence runs, check if any finding repo is a trigger in
    CO_CHANGE_RULES. If so, auto-add the companion repos to findings.
    """
    if not CO_CHANGE_RULES:
        return ""

    finding_repos = {rname for _, rname in ctx.findings}
    added: list[tuple[str, str]] = []  # (companion, trigger)

    for trigger, companions in CO_CHANGE_RULES.items():
        if trigger in finding_repos:
            for companion in companions:
                if companion not in finding_repos:
                    # Verify companion exists in our index
                    exists = ctx.conn.execute("SELECT 1 FROM repos WHERE name = ?", (companion,)).fetchone()
                    if exists:
                        ctx.findings.append(("co_change_rule", companion))
                        finding_repos.add(companion)
                        added.append((companion, trigger))

    if not added:
        return ""

    output = "## Co-change Rules\n\n"
    output += "_High-confidence co-change pairs (no static dependency, derived from task history):_\n\n"
    for companion, trigger in added:
        output += f"  - **{companion}** — always changes with **{trigger}**\n"
    output += "\n"
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
    """Cascade prediction — BFS upstream (who depends on seeds) + downstream (what seeds depend on)."""
    seed_repos = [
        repo
        for repo in classification.seed_repos
        if ctx.conn.execute("SELECT 1 FROM repos WHERE name = ?", (repo,)).fetchone()
    ]

    if not seed_repos:
        return ""

    output = "## Cascade Impact\n\n"
    all_affected: dict[str, tuple[str, str]] = {}  # repo → (via_seed, edge_type)
    seed_set = set(seed_repos)

    # Upstream: repos that depend ON seeds (existing BFS)
    for seed in seed_repos:
        levels = bfs_dependents(ctx.conn, seed, max_depth=2)
        for _level, deps in levels.items():
            for dep_name, edge_type in deps:
                if dep_name not in all_affected and dep_name not in seed_set:
                    all_affected[dep_name] = (seed, edge_type)

    # Downstream: repos that seeds DEPEND ON (shared infrastructure)
    # Walk outgoing edges from seeds, find high-in-degree targets (hub repos)
    downstream_hubs: dict[str, tuple[str, str, int]] = {}  # repo → (via_seed, edge_type, in_degree)
    for seed in seed_repos:
        outgoing = ctx.conn.execute(
            """SELECT DISTINCT target, edge_type FROM graph_edges
               WHERE source = ? AND target NOT LIKE 'pkg:%' AND target NOT LIKE 'proto:%'
               AND target NOT LIKE 'route:%'""",
            (seed,),
        ).fetchall()
        for row in outgoing:
            target = row["target"]
            if target in seed_set or target in all_affected:
                continue
            # Check in-degree (how many repos depend on this target)
            in_degree = ctx.conn.execute(
                "SELECT COUNT(DISTINCT source) as n FROM graph_edges WHERE target = ? AND source NOT LIKE 'pkg:%'",
                (target,),
            ).fetchone()["n"]
            # Hub threshold: at least 5 dependents (commonly shared infrastructure)
            if in_degree >= 5 and target not in downstream_hubs:
                downstream_hubs[target] = (seed, row["edge_type"], in_degree)

    if all_affected:
        output += "_Upstream (repos that depend on seeds):_\n\n"
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

    if downstream_hubs:
        # Filter out pure tooling/infrastructure (eslint, test tools, etc.)
        # Keep only repos with service-relevant edge types
        service_edge_types = {
            "grpc_call",
            "grpc_client_usage",
            "grpc_method_call",
            "npm_dep_proto",
            "proto_service_def",
            "webhook_dispatch",
            "webhook_handler",
            "flow_step",
            "domain_reference",
        }
        relevant_hubs = {
            repo: info for repo, info in downstream_hubs.items() if info[1] in service_edge_types or "msg:" not in repo
        }
        # Filter: exclude tooling repos (eslint, mali, tools, grpc-tools)
        tooling_patterns = {"eslint", "mali", "grpc-tools", "node-libs-grpc"}
        relevant_hubs = {
            repo: info for repo, info in relevant_hubs.items() if not any(tp in repo for tp in tooling_patterns)
        }

        output += "_Downstream (shared infrastructure seeds depend on):_\n\n"
        for repo, (seed, etype, in_deg) in sorted(relevant_hubs.items(), key=lambda x: x[1][2], reverse=True)[:15]:
            ctx.findings.append(("downstream", repo))
            output += f"  - **{repo}** ({in_deg} dependents, via {seed}/{etype})\n"
        output += "\n"

    # Reverse cascade: for each finding repo, walk OUTGOING edges of types that
    # indicate the repo CALLS/HANDLES another repo (e.g., webhook_handler points
    # FROM workflow-provider-webhooks TO grpc-providers-crb). This discovers
    # targets that the forward BFS misses because it only walks incoming edges.
    reverse_edge_types = ("webhook_handler", "grpc_call", "grpc_method_call", "callback_handler")
    finding_repos = {rname for _, rname in ctx.findings} | seed_set | set(all_affected) | set(downstream_hubs)
    reverse_found: dict[str, tuple[str, str]] = {}  # repo → (via_source, edge_type)

    for repo in list(finding_repos):
        try:
            rows = ctx.conn.execute(
                f"""SELECT DISTINCT target, edge_type FROM graph_edges
                    WHERE source = ? AND edge_type IN ({",".join("?" for _ in reverse_edge_types)})
                    AND target NOT LIKE 'pkg:%' AND target NOT LIKE 'proto:%'
                    AND target NOT LIKE 'route:%'""",
                (repo, *reverse_edge_types),
            ).fetchall()
            for row in rows:
                target = row["target"]
                if target not in finding_repos and target not in reverse_found:
                    reverse_found[target] = (repo, row["edge_type"])
        except Exception:
            continue

    if reverse_found:
        output += "_Reverse cascade (targets called/handled by found repos):_\n\n"
        for repo, (via, etype) in sorted(reverse_found.items()):
            ctx.findings.append(("reverse_cascade", repo))
            output += f"  - **{repo}** (via {via}/{etype})\n"
        output += "\n"

    total = len(all_affected) + len(downstream_hubs) + len(reverse_found)
    if total:
        output += f"_Total: {total} repos in cascade._\n\n"
    else:
        output += "No cascade dependencies found.\n\n"
    return output


def _section_co_occurrence(ctx: AnalysisContext) -> str:
    """Auto-add repos via two data-driven signals from task_history:
    1. Co-occurrence: repos that co-change with findings (≥40% conditional probability)
    2. Universal: repos changed in ≥25% of all CORE tasks (always relevant for CORE)
    """
    try:
        tables = {r[0] for r in ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "task_history" not in tables:
            return ""
    except Exception:
        return ""

    finding_repos = {rname for _, rname in ctx.findings}

    import json
    from collections import Counter

    # Use tasks from same prefix group for co-occurrence (CORE with CORE, BO with BO, etc.)
    from .base import extract_task_id

    task_id = extract_task_id(ctx.description)
    task_prefix = task_id.split("-")[0].upper() if task_id else ""
    if task_prefix and task_prefix in ("CORE", "BO", "HS", "PI"):
        rows = ctx.conn.execute(
            "SELECT repos_changed FROM task_history WHERE ticket_id LIKE ?", (f"{task_prefix}-%",)
        ).fetchall()
    else:
        rows = ctx.conn.execute("SELECT repos_changed FROM task_history").fetchall()
    if len(rows) < 5:
        return ""

    repo_count: Counter[str] = Counter()
    cooccur_count: Counter[tuple[str, str]] = Counter()

    for r in rows:
        task_repos = set(json.loads(r["repos_changed"]) if r["repos_changed"] else [])
        for repo in task_repos:
            repo_count[repo] += 1
        if finding_repos:
            overlap = finding_repos & task_repos
            for f_repo in overlap:
                for other in task_repos:
                    if other not in finding_repos:
                        cooccur_count[(f_repo, other)] += 1

    output_parts: list[str] = []
    boosted: set[str] = set()

    # Signal 1: Universal CORE repos (>25% of all CORE tasks)
    universal_threshold = len(rows) * 0.25
    universal: list[tuple[str, int]] = []
    for repo, cnt in repo_count.most_common():
        if cnt < universal_threshold:
            break
        if repo not in finding_repos:
            universal.append((repo, cnt))
            boosted.add(repo)

    if universal:
        output_parts.append("## Frequently Changed Repos\n\n")
        output_parts.append(f"_Repos changed in ≥25% of CORE tasks ({len(rows)} total):_\n\n")
        for repo, cnt in universal:
            ctx.findings.append(("universal", repo))
            pct = cnt / len(rows) * 100
            output_parts.append(f"  - **{repo}** — {cnt} tasks ({pct:.0f}%)\n")
        output_parts.append("\n")

    # Signal 2: Co-occurrence with findings (≥40% conditional probability, ≥3 tasks)
    # Also bidirectional: if P(found|other) ≥ 0.80 AND count ≥ 4, add other.
    # This catches tight satellites (e.g., apikeys2 always with express-api-v1).
    cooccur_boosted: dict[str, tuple[str, float]] = {}
    if finding_repos:
        for (f_repo, other), count in cooccur_count.items():
            if count < 3 or other in finding_repos or other in boosted:
                continue
            prob_forward = count / repo_count[f_repo]
            prob_reverse = count / repo_count[other] if repo_count[other] > 0 else 0
            if prob_forward >= 0.4 and other not in cooccur_boosted:
                cooccur_boosted[other] = (f_repo, prob_forward)
            elif prob_reverse >= 0.80 and count >= 4 and other not in cooccur_boosted:
                cooccur_boosted[other] = (f_repo, prob_reverse)

    if cooccur_boosted:
        if not output_parts:
            output_parts.append("## Co-occurrence Boost\n\n")
        else:
            output_parts.append("## Co-occurrence Boost\n\n")
        output_parts.append("_Repos that historically co-change with found repos (≥40%):_\n\n")
        for repo, (via, prob) in sorted(cooccur_boosted.items(), key=lambda x: x[1][1], reverse=True)[:12]:
            ctx.findings.append(("co-occurrence", repo))
            boosted.add(repo)
            output_parts.append(f"  - **{repo}** — {prob:.0%} when {via} changes\n")
        output_parts.append("\n")

    return "".join(output_parts)


def _section_bulk_migration(ctx: AnalysisContext) -> str:
    """Detect bulk migration/upgrade tasks and enumerate all service repos.

    When task description matches bulk keywords (e.g., 'migrate', 'audit', 'upgrade')
    and implies cross-service scope, enumerate all repos matching service patterns.
    Configured via conventions.yaml: bulk_keywords + service_repo_patterns.
    """
    from src.config import BULK_KEYWORDS, SERVICE_REPO_PATTERNS

    if not BULK_KEYWORDS or not SERVICE_REPO_PATTERNS:
        return ""

    desc_lower = ctx.description.lower()
    matched_keywords = [kw for kw in BULK_KEYWORDS if kw.lower() in desc_lower]
    if not matched_keywords:
        return ""

    # Enumerate all repos matching service patterns
    all_repos = [r["name"] for r in ctx.conn.execute("SELECT name FROM repos").fetchall()]
    already = {rname for _, rname in ctx.findings}
    new_repos: list[str] = []

    for repo in all_repos:
        if repo in already:
            continue
        for pattern in SERVICE_REPO_PATTERNS:
            if re.match(pattern, repo):
                new_repos.append(repo)
                break

    if not new_repos:
        return ""

    output = "## Bulk Migration Detection\n\n"
    output += f"_Matched keywords: {', '.join(matched_keywords)}_\n"
    output += f"_Enumerating {len(new_repos)} service repos matching configured patterns:_\n\n"

    for repo in sorted(new_repos):
        ctx.findings.append(("bulk_migration", repo))
        output += f"  - **{repo}**\n"
    output += "\n"
    return output


def _section_provider_fanout(ctx: AnalysisContext) -> str:
    """When cascade touches proto/types repos, enumerate all providers via runtime_routing."""
    from src.config import GATEWAY_REPO, PROTO_TRIGGER_REPOS

    if not GATEWAY_REPO:
        return ""

    finding_repos = {rname for _, rname in ctx.findings}

    # Check if any finding is a proto/types repo that providers depend on
    trigger_repos = finding_repos & PROTO_TRIGGER_REPOS

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


def _section_function_search(ctx: AnalysisContext) -> str:
    """Search for function/method names mentioned in description across all repos.

    Detects camelCase (createAuditLog), snake_case (create_audit_log), and
    dotted names (audit.create) in description, then finds all repos that
    reference them. High precision for shared lib bug fixes.
    """
    # Extract function-like names from description
    # camelCase: createAuditLog, handlePayment, getProviderConfig
    camel_funcs = re.findall(r"\b[a-z][a-zA-Z]*[A-Z][a-zA-Z]*\b", ctx.description)
    # snake_case with 2+ parts: create_audit_log
    snake_funcs = re.findall(r"\b[a-z]+(?:_[a-z]+){1,}\b", ctx.description)
    # Also generate shorter camelCase prefixes (createAuditLog → createAudit)
    camel_prefixes = []
    for func in camel_funcs:
        parts = re.findall(r"[A-Z]?[a-z]+", func)
        if len(parts) >= 3:
            # Try prefix without last part (createAuditLog → createAudit)
            prefix = "".join(parts[:-1])
            if len(prefix) >= 8 and prefix != func:
                camel_prefixes.append(prefix)
    # Filter: min 8 chars, not common words
    func_names = [f for f in set(camel_funcs + snake_funcs + camel_prefixes) if len(f) >= 8]

    if not func_names:
        return ""

    already_found = {rname for _, rname in ctx.findings}
    func_repos: dict[str, list[str]] = {}  # repo → [functions found]

    for func in func_names:
        try:
            rows = ctx.conn.execute(
                "SELECT DISTINCT repo_name FROM chunks WHERE chunks MATCH ? LIMIT 50",
                (f'"{func}"',),
            ).fetchall()
            for row in rows:
                repo = row["repo_name"]
                if repo not in already_found:
                    func_repos.setdefault(repo, []).append(func)
        except Exception:
            continue

    if not func_repos:
        return ""

    output = "## Function Reference Search\n\n"
    output += f"_Repos referencing function(s): {', '.join(func_names)}_\n\n"

    # Sort by number of function matches
    sorted_repos = sorted(func_repos.items(), key=lambda x: len(x[1]), reverse=True)

    for repo, funcs in sorted_repos[:20]:
        ctx.findings.append(("function", repo))
        output += f"  - **{repo}** — {', '.join(funcs)}\n"

    output += f"\n_Total: {len(func_repos)} repos reference these functions._\n\n"
    return output


def _section_keyword_scan(ctx: AnalysisContext, classification: TaskClassification) -> str:
    """Broad FTS search across ALL repos for task-specific keywords + compound terms."""
    # Extract compound terms (underscored/hyphenated) from original description
    compound_terms = re.findall(r"[a-zA-Z]+(?:[_-][a-zA-Z]+){1,}", ctx.description)
    # Also extract camelCase compounds
    camel_terms = re.findall(r"[a-z]+(?:[A-Z][a-z]+){1,}", ctx.description)

    # Use single words that are specific to this task
    scan_words = [w for w in ctx.words if len(w) > 5 and w not in _KEYWORD_STOP_WORDS]
    if not scan_words and not compound_terms and not camel_terms:
        return ""

    # Skip words that are too common (would match too many repos)
    already_found = {rname for _, rname in ctx.findings}

    new_finds: dict[str, list[str]] = {}  # repo → [matched keywords]

    # Phase 1: Compound terms (high precision — search as exact phrases)
    for term in compound_terms + camel_terms:
        if len(term) < 8:
            continue
        try:
            rows = ctx.conn.execute(
                "SELECT DISTINCT repo_name FROM chunks WHERE chunks MATCH ? LIMIT 30",
                (f'"{term}"',),
            ).fetchall()
            for row in rows:
                repo = row["repo_name"]
                if repo not in already_found:
                    new_finds.setdefault(repo, []).append(term)
        except Exception:
            continue

    # Phase 2: Repo name matching — lower threshold (4+ chars) since repo names are specific
    all_repo_names = [r["name"] for r in ctx.conn.execute("SELECT name FROM repos").fetchall()]
    name_words = [w for w in ctx.words if len(w) >= 4 and w not in _KEYWORD_STOP_WORDS]
    for keyword in name_words[:12]:
        for rname in all_repo_names:
            if keyword in rname and rname not in already_found:
                new_finds.setdefault(rname, []).append(f"{keyword}(name)")

    # Phase 3: Single keywords in content (higher threshold: >5 chars)
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

    # Repos matching compound terms or repo-name matches count as strong
    all_compounds = set(compound_terms + camel_terms)
    strong_finds = [
        (repo, kws)
        for repo, kws in sorted_finds
        if len(kws) >= 2 or any(kw in all_compounds for kw in kws) or any("(name)" in kw for kw in kws)
    ]

    if not strong_finds:
        return ""

    output = "## Keyword Scan (broad search)\n\n"
    output += "_Repos matching 2+ task keywords (beyond domain/pattern repos):_\n\n"

    for repo, kws in strong_finds[:10]:
        ctx.findings.append(("keyword", repo))
        output += f"  - **{repo}** — matches: {', '.join(kws)}\n"
    output += "\n"

    return output
