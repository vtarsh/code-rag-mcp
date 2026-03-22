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

# subprocess re-export for test mocking
import subprocess

from src.container import get_db, require_db

from .base import AnalysisContext

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
    "_find_task_branches",
    "_find_task_prs",
    "_gh_api",
    "_task_id_matches",
    "_validate_repo_name",
    "analyze_task_tool",
    "get_db",
    "subprocess",
]
from .pi_analyzer import (
    detect_provider,
    section_change_impact,
    section_impact,
    section_provider,
    section_provider_checklist,
    section_webhooks,
)
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
    section_task_patterns,
)


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


def _analyze_task_impl(conn: sqlite3.Connection, description: str, provider: str) -> str:
    """Orchestrate task analysis. Dispatches to shared + domain-specific sections."""
    words = set(re.findall(r"[a-zA-Z]{3,}", description.lower()))

    # Auto-detect provider from task description
    if not provider:
        provider = detect_provider(conn, words)

    ctx = AnalysisContext(
        conn=conn,
        description=description,
        words=words,
        provider=provider,
    )

    output = f"# Task Analysis\n\n**Task**: {description}\n\n"

    # Shared sections (all task types)
    output += section_gotchas(ctx)
    output += section_existing_tasks(ctx)
    output += section_task_patterns(ctx)
    output += section_file_patterns(ctx)

    # PI-specific sections (only when provider detected)
    output += section_provider(ctx)
    output += section_proto(ctx)
    output += section_webhooks(ctx)
    output += section_gateway(ctx)
    output += section_impact(ctx)

    # Shared analysis sections
    method_output, task_methods, method_status = section_methods(ctx)
    output += method_output

    github_output, pr_data, branch_data = section_github(ctx)
    output += github_output

    output += section_completeness(ctx, task_methods, method_status, pr_data, branch_data)

    # PI-specific post-analysis
    output += section_change_impact(ctx)
    output += section_provider_checklist(ctx)

    # CI risk (all task types)
    output += section_ci_risk(ctx)

    return output
