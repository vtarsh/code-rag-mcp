"""analyze_task MCP tool — find relevant repos/files/flows for a development task.

Decomposed into modules:
- shared_sections: sections that run for all task types
- pi_analyzer: provider integration specific sections
- github_helpers: GitHub API interaction
- method_helpers: gRPC method existence checks
- base: shared types (AnalysisContext, etc.)
"""

from __future__ import annotations

import re
import sqlite3

import sys

from src.container import db_connection, require_db

from .base import AnalysisContext, Finding
from .github_helpers import clear_gh_cache as _clear_gh_cache

# --- Backward compat re-exports (tests mock these paths) ---
from .github_helpers import find_task_branches as _find_task_branches
from .github_helpers import find_task_prs as _find_task_prs
from .github_helpers import gh_api as _gh_api
from .github_helpers import task_id_matches as _task_id_matches
from .github_helpers import validate_repo_name as _validate_repo_name
from .method_helpers import check_method_exists as _check_method_exists

__all__ = [
    "_analyze_task_impl",
    "_check_method_exists",
    "_clear_gh_cache",
    "_find_task_branches",
    "_find_task_prs",
    "_gh_api",
    "_task_id_matches",
    "_validate_repo_name",
    "analyze_task_tool",
]
from .pi_analyzer import (
    section_bulk_providers,
    section_change_impact,
    section_impact,
    section_provider,
    section_provider_checklist,
    section_webhooks,
)
from .final_ranker import section_final_ranker
from .meta_guard import section_meta_guard
from .recipe_section import section_recipe
from .shared_sections import (
    section_ci_risk,
    section_completeness,
    section_existing_tasks,
    section_file_patterns,
    section_gateway,
    section_github,
    section_gotchas,
    section_methods,
    section_proto,
    section_shared_files_warning,
    section_task_patterns,
)
from src.tools.analyze.investigation_questions import (
    section_investigation_questions,
)

# Note: _BOLD_REPO_RE regex was removed — repo extraction now uses ctx.findings directly.
# benchmark_recall.py still has its own copy for parsing markdown output externally.


def _section_rerank(ctx: AnalysisContext, description: str, output: str) -> str:
    """Run Gemini re-ranker on all repo names collected in ctx.findings."""
    import sys

    try:
        from src.tools.reranker import rerank_repos
    except ImportError:
        print("[reranker] reranker module not available, skipping", file=sys.stderr)
        return ""

    # Extract candidate repos from structured findings (replaces regex on markdown)
    candidates = sorted(ctx.get_unique_repos())
    if not candidates:
        return ""

    try:
        ranked = rerank_repos(ctx.conn, description, candidates)
    except Exception as e:
        print(f"[reranker] Error during re-ranking: {e}", file=sys.stderr)
        return ""

    if not ranked:
        return ""

    section = "\n## Re-ranked Predictions (Gemini)\n\n"
    for entry in ranked:
        repo = entry.get("repo", "")
        conf = entry.get("confidence", "?")
        reason = entry.get("reason", "")
        section += f"- **{repo}** [{conf}] — {reason}\n"
    section += "\n"
    return section


def _inject_domain_template(ctx: AnalysisContext, classification: object) -> str:
    """Auto-add base repos from domain templates when domain is classified."""
    from src.config import DOMAIN_TEMPLATES

    if not DOMAIN_TEMPLATES:
        return ""

    primary = classification.domain.split("+")[0]
    template = DOMAIN_TEMPLATES.get(primary, {})
    base_repos = template.get("base_repos", [])
    if not base_repos:
        return ""

    existing = {f.repo for f in ctx.findings}
    new_repos = []
    for repo in base_repos:
        if repo not in existing:
            exists = ctx.conn.execute("SELECT 1 FROM repos WHERE name = ?", (repo,)).fetchone()
            if exists:
                ctx.findings.append(Finding("domain_template", repo, "high"))
                new_repos.append(repo)

    if not new_repos:
        return ""

    prob = template.get("probability", 0)
    output = f"**Domain template** ({primary}, {prob:.0%} historical): "
    output += ", ".join(f"**{r}**" for r in new_repos) + "\n\n"
    return output


def _extract_repo_refs(ctx: AnalysisContext) -> str:
    """Extract repo names from GitHub URLs and description text matches.

    Finds patterns like github.com/org/repo-name in URLs and also checks
    if multi-word description fragments match known repo names.
    """
    # 1. GitHub URL extraction: github.com/{org}/{repo}
    url_repos = re.findall(r"github\.com/[^/\s]+/([a-z][a-z0-9-]+)", ctx.description, re.IGNORECASE)

    # 2. Repo-name fragment matching: check if description contains known repo names
    all_repos = {r["name"] for r in ctx.conn.execute("SELECT name FROM repos").fetchall()}
    desc_lower = ctx.description.lower()

    matched: set[str] = set()
    for repo in url_repos:
        repo_lower = repo.lower()
        if repo_lower in all_repos:
            matched.add(repo_lower)

    # Check repo names that appear verbatim in description
    for repo in all_repos:
        if len(repo) >= 15 and repo in desc_lower:
            matched.add(repo)
        # Space-to-hyphen: "core configurations" → "grpc-core-configurations"
        elif len(repo) >= 15:
            spaced = repo.replace("-", " ")
            if spaced in desc_lower:
                matched.add(repo)

    # Fuzzy: check if description contains repo name minus trailing 's' or with '.' instead of '-'
    # Catches: "workflow-worldpay-adjustment" → "workflow-worldpay-adjustments"
    #          "update-packages.sh" → "update-packages-script"
    for repo in all_repos:
        if len(repo) < 12:
            continue
        # Strip trailing 's' from repo name for singular match
        repo_singular = repo.rstrip("s") if repo.endswith("s") else repo
        if len(repo_singular) >= 12 and repo_singular in desc_lower:
            matched.add(repo)
        # Check script/file references: "repo-name.sh" or "repo-name.js"
        for ext in (".sh", ".js", ".py", ".ts"):
            file_ref = repo.replace("-script", "") + ext
            if len(file_ref) >= 10 and file_ref in desc_lower:
                matched.add(repo)

    if not matched:
        return ""

    output = ""
    for repo in matched:
        ctx.findings.append(Finding("repo_ref", repo, "high"))
        output += f"**{repo}** referenced in description.\n"
    return output


def _section_npm_dep_scan(ctx: AnalysisContext) -> str:
    """Scan npm_dep edges from found repos for task keyword matches.

    Catches shared libraries like node-libs-common that have provider-specific
    code but aren't found via cascade (too many dependents = deprioritized).
    """
    finding_repos = {f.repo for f in ctx.findings}
    if not finding_repos:
        return ""

    # Collect npm_dep targets from found repos
    dep_repos: dict[str, str] = {}  # dep_repo → via_repo
    for repo in finding_repos:
        try:
            rows = ctx.conn.execute(
                """SELECT DISTINCT target FROM graph_edges
                   WHERE source = ? AND edge_type = 'npm_dep'
                   AND target NOT LIKE 'pkg:%'""",
                (repo,),
            ).fetchall()
            for row in rows:
                target = row["target"]
                if target not in finding_repos and target not in dep_repos:
                    dep_repos[target] = repo
        except Exception as e:
            print(f"[npm_dep_scan] query failed for repo '{repo}': {e}", file=sys.stderr)
            continue

    if not dep_repos:
        return ""

    # Check which dep repos have task keywords in their content
    keywords = [
        w
        for w in ctx.words
        if len(w) >= 4
        and w
        not in {
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
    ]
    if ctx.provider:
        keywords = [ctx.provider] + keywords[:4]
    else:
        keywords = keywords[:5]

    if not keywords:
        return ""

    new_finds: list[tuple[str, str, str]] = []  # (repo, via, keyword)
    for dep_repo, via_repo in dep_repos.items():
        for kw in keywords:
            try:
                hit = ctx.conn.execute(
                    "SELECT 1 FROM chunks WHERE repo_name = ? AND chunks MATCH ? LIMIT 1",
                    (dep_repo, f'"{kw}"'),
                ).fetchone()
                if hit:
                    new_finds.append((dep_repo, via_repo, kw))
                    break
            except Exception as e:
                print(f"[npm_dep_scan] FTS query failed for '{kw}' in '{dep_repo}': {e}", file=sys.stderr)
                continue

    if not new_finds:
        return ""

    output = "## npm Dependency Scan\n\n"
    output += "_Shared libraries with task keyword matches (via npm_dep edges):_\n\n"
    for repo, via, kw in new_finds:
        ctx.findings.append(Finding("npm_dep_scan", repo, "low"))
        output += f"  - **{repo}** — `{kw}` found (dep of {via})\n"
    output += "\n"
    return output


@require_db
def analyze_task_tool(description: str, provider: str = "", rerank: bool = False, exclude_task_id: str = "", final_rank: bool = False) -> str:
    """Analyze a development task and find ALL relevant repos, files, and dependencies.

    Args:
        description: Task description (e.g., "implement DirectDebitMandate verification for Trustly")
        provider: Optional provider name to focus on (e.g., "trustly", "paypal")
        rerank: Set to true to filter predictions via Gemini 3.1 Pro (requires GEMINI_API_KEY)
        exclude_task_id: Optional task ID to exclude from task_history lookups (for blind eval)
        final_rank: Set to true to run precision-oriented LLM pruner on the final
            candidate set (requires GEMINI_API_KEY). Default False until validated.
    """
    with db_connection() as conn:
        return _analyze_task_impl(conn, description, provider, rerank=rerank, exclude_task_id=exclude_task_id, final_rank=final_rank)


def _analyze_task_impl(conn: sqlite3.Connection, description: str, provider: str, *, rerank: bool = False, exclude_task_id: str = "", final_rank: bool = False) -> str:
    """Orchestrate task analysis. Dispatches to shared + domain-specific sections."""
    import sys

    from .classifier import classify_task
    from .core_analyzer import run_co_change_rules, run_co_occurrence, run_core_analysis

    words = set(re.findall(r"[a-zA-Z]{3,}", description.lower()))

    # Classify task into domain
    classification = classify_task(conn, description, provider, words)
    provider = classification.provider

    ctx = AnalysisContext(
        conn=conn,
        description=description,
        words=words,
        provider=provider,
        exclude_task_id=exclude_task_id,
    )

    # Track section failures for end-of-output warning
    failed_sections: list[tuple[str, str]] = []  # (section_name, error_message)

    def _run_section(name: str, func, *args, **kwargs):
        """Run a section function with error capture. Returns result or empty string."""
        try:
            return func(*args, **kwargs)
        except Exception as e:
            failed_sections.append((name, str(e)))
            print(f"[analyze_task] section '{name}' failed: {e}", file=sys.stderr)
            return ""

    # Extract repo names from GitHub URLs and description text
    repo_refs_output = _run_section("repo_refs", _extract_repo_refs, ctx)

    header = f"# Task Analysis\n\n**Task**: {description}\n"
    header += f"**Domain**: {classification.domain}"
    if classification.confidence > 0:
        header += f" ({classification.confidence:.0%} confidence)"
    header += "\n"
    # SUMMARY_PLACEHOLDER will be replaced with tier counts after all sections run
    header += "SUMMARY_PLACEHOLDER\n"

    output = ""

    # Meta-guard: warn if query overlaps heavily with a stored task (first section).
    output += _run_section("meta_guard", section_meta_guard, ctx)

    # SHARED FILE IMPACT: top-priority cross-provider warning when changed files
    # match shared_files patterns (from conventions.yaml). This is the action-forcing
    # signal for review/audit tasks — appears before any data section so the agent
    # sees sibling provider names in its own context.
    output += _run_section("shared_files_warning", section_shared_files_warning, ctx)

    # INVESTIGATION QUESTIONS: Gemini-generated task-specific checks the
    # implementer must answer before writing code. This is the "true
    # proactivity" path — contextual and paraphrase-robust, unlike the
    # keyword-triggered shared_files branch which only matches known words.
    output += _run_section("investigation_questions", section_investigation_questions, ctx)

    if repo_refs_output:
        output += repo_refs_output + "\n"

    # Recipe injection — structured patterns for known task types (early, before other sections)
    output += _run_section("recipe", section_recipe, ctx, classification)

    # Domain template injection — auto-add base repos for the classified domain
    output += _run_section("domain_template", _inject_domain_template, ctx, classification)

    # Shared sections (all task types)
    output += _run_section("gotchas", section_gotchas, ctx)
    output += _run_section("existing_tasks", section_existing_tasks, ctx)
    output += _run_section("task_patterns", section_task_patterns, ctx)
    output += _run_section("file_patterns", section_file_patterns, ctx)

    # PI-specific sections (only when provider detected)
    output += _run_section("provider", section_provider, ctx)
    output += _run_section("bulk_providers", section_bulk_providers, ctx)
    output += _run_section("proto", section_proto, ctx)
    output += _run_section("webhooks", section_webhooks, ctx)
    output += _run_section("gateway", section_gateway, ctx)
    output += _run_section("impact", section_impact, ctx)

    # CORE/BO/HS-specific sections (when not PI)
    output += _run_section("core_analysis", run_core_analysis, ctx, classification)

    # Keyword scan: repo-name matching + broad FTS for task keywords.
    # Originally gated to CORE/BO/HS→PI cross-domain, but PI tasks also benefit
    # (catches provider repos like grpc-providers-paymend from task description).
    from .core_analyzer import _section_keyword_scan

    output += _run_section("keyword_scan", _section_keyword_scan, ctx, classification)

    # npm_dep scan: check npm dependencies of found repos for task keyword matches
    output += _run_section("npm_dep_scan", _section_npm_dep_scan, ctx)

    # Co-occurrence boost (all domains — data-driven from task_history)
    output += _run_section("co_occurrence", run_co_occurrence, ctx)

    # Co-change rules (high-confidence pairs from conventions.yaml)
    output += _run_section("co_change_rules", run_co_change_rules, ctx)

    # Shared analysis sections — these return tuples, handle separately
    task_methods: set[str] = set()
    method_status: dict[str, dict] = {}
    try:
        method_output, task_methods, method_status = section_methods(ctx)
        output += method_output
    except Exception as e:
        failed_sections.append(("methods", str(e)))
        print(f"[analyze_task] section 'methods' failed: {e}", file=sys.stderr)

    pr_data: dict[str, list[dict]] = {}
    branch_data: dict[str, list[str]] = {}
    try:
        github_output, pr_data, branch_data = section_github(ctx)
        output += github_output
    except Exception as e:
        failed_sections.append(("github", str(e)))
        print(f"[analyze_task] section 'github' failed: {e}", file=sys.stderr)

    output += _run_section("completeness", section_completeness, ctx, task_methods, method_status, pr_data, branch_data)

    # PI-specific post-analysis
    output += _run_section("change_impact", section_change_impact, ctx)
    output += _run_section("provider_checklist", section_provider_checklist, ctx)

    # CI risk (all task types)
    output += _run_section("ci_risk", section_ci_risk, ctx)

    # Optional Gemini re-ranking
    if rerank:
        output += _run_section("rerank", _section_rerank, ctx, description, output)

    # Optional precision-oriented LLM pruner: runs AFTER all other sections,
    # rewrites ctx.findings based on evidence + similar-task ground truth.
    # Must run before the summary header is built (summary reads findings).
    if final_rank:
        output += _run_section("final_ranker", section_final_ranker, ctx, classification)

    # Append warning if any sections failed
    if failed_sections:
        section_names = ", ".join(name for name, _ in failed_sections)
        output += "\n---\n"
        output += f"\n**Incomplete analysis**: sections [{section_names}] failed:\n"
        for name, error in failed_sections:
            output += f"  - `{name}`: {error}\n"
        output += "\n"

    # Build confidence tier summary from structured findings
    repos_by_conf = ctx.get_repos_by_confidence()
    n_core = len(repos_by_conf["high"])
    n_related = len(repos_by_conf["medium"])
    n_peripheral = len(repos_by_conf["low"])
    summary = f"**Repos found**: {n_core} core + {n_related} related + {n_peripheral} peripheral"
    header = header.replace("SUMMARY_PLACEHOLDER", summary)

    return header + output
