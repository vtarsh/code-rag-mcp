#!/usr/bin/env python3
"""Build flow context for injection into audit agent prompts.

Usage:
    python scripts/build_audit_context.py --task PI-60 --provider payper

Reads from knowledge.db (task_history, graph_edges), flow YAMLs, raw repos,
and reference docs to produce a structured markdown context block for audit agents.

Standalone — does not import src/ modules. Uses CODE_RAG_HOME env or defaults
to parent of script directory.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml

# --- Path resolution (standalone, no src/ imports) ---
BASE_DIR = Path(__file__).resolve().parent.parent
if env_home := __import__("os").getenv("CODE_RAG_HOME"):
    BASE_DIR = Path(env_home)

DB_PATH = BASE_DIR / "db" / "knowledge.db"
RAW_DIR = BASE_DIR / "raw"

# Profile resolution (simplified: env -> .active_profile -> "example")
_profile_name = __import__("os").getenv("ACTIVE_PROFILE", "")
if not _profile_name:
    _marker = BASE_DIR / ".active_profile"
    if _marker.exists():
        _profile_name = _marker.read_text().strip()
if not _profile_name:
    _profile_name = "example"

PROFILE_DIR = BASE_DIR / "profiles" / _profile_name
FLOWS_DIR = PROFILE_DIR / "docs" / "flows"
REFS_DIR = PROFILE_DIR / "docs" / "references"

# Edge types relevant for audit context
AUDIT_EDGE_TYPES = (
    "grpc_call",
    "npm_dep",
    "cascade_upstream",
    "runtime_routing",
    "webhook_handler",
    "grpc_client_usage",
    "child_workflow",
    "webhook_dispatch",
    "callback_handler",
    "temporal_signal",
)


def get_db() -> sqlite3.Connection:
    """Open a read-only connection to knowledge.db."""
    if not DB_PATH.exists():
        print(f"Error: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# --- Task loading ---


def load_task(conn: sqlite3.Connection, task_key: str) -> dict | None:
    """Load task from task_history by partial ticket_id match."""
    row = conn.execute(
        "SELECT ticket_id, summary, repos_changed, files_changed FROM task_history WHERE ticket_id LIKE ?",
        (f"%{task_key}%",),
    ).fetchone()
    if not row:
        return None
    return {
        "ticket_id": row["ticket_id"],
        "summary": row["summary"] or "",
        "repos_changed": _parse_json_list(row["repos_changed"]),
        "files_changed": _parse_json_list(row["files_changed"]),
    }


def _parse_json_list(val: str | None) -> list[str]:
    if not val:
        return []
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# --- Graph edges ---


def load_neighbors(conn: sqlite3.Connection, repos: list[str]) -> dict[str, dict]:
    """For each repo, find upstream/downstream neighbors from graph_edges."""
    placeholders = ",".join("?" for _ in AUDIT_EDGE_TYPES)
    result: dict[str, dict] = {}
    for repo in repos:
        upstream: list[str] = []
        downstream: list[str] = []
        rows = conn.execute(
            f"SELECT source, target, edge_type FROM graph_edges "
            f"WHERE (source = ? OR target = ?) AND edge_type IN ({placeholders})",
            (repo, repo, *AUDIT_EDGE_TYPES),
        ).fetchall()
        for r in rows:
            if r["source"] == repo and r["target"] not in downstream:
                downstream.append(r["target"])
            elif r["target"] == repo and r["source"] not in upstream:
                upstream.append(r["source"])
        result[repo] = {"upstream": upstream, "downstream": downstream}
    return result


# --- Flow YAML loading ---


def load_flows(provider: str | None, summary: str) -> list[dict]:
    """Load relevant flow YAML files based on provider and task description."""
    if not FLOWS_DIR.is_dir():
        return []

    flow_files: list[Path] = []
    summary_lower = summary.lower()

    # Always load for PI tasks
    _add_if_exists(flow_files, FLOWS_DIR / "new-provider-setup-flow.yaml")

    if provider:
        _add_if_exists(flow_files, FLOWS_DIR / "apm-redirect-flow.yaml")
        _add_if_exists(flow_files, FLOWS_DIR / "async-refund-flow.yaml")

    if "webhook" in summary_lower:
        _add_if_exists(flow_files, FLOWS_DIR / "workflow-provider-webhooks.yaml")

    flows: list[dict] = []
    for fp in flow_files:
        try:
            data = yaml.safe_load(fp.read_text())
            if isinstance(data, dict) and "flows" in data:
                for flow in data["flows"]:
                    flow["_source_file"] = fp.name
                    flows.append(flow)
        except Exception:
            pass
    return flows


def _add_if_exists(target: list[Path], path: Path) -> None:
    if path.exists() and path not in target:
        target.append(path)


# --- Raw repo methods ---


def find_existing_methods(repos: list[str]) -> dict[str, list[str]]:
    """For each repo, find existing method files in RAW_DIR/{repo}/methods/."""
    result: dict[str, list[str]] = {}
    for repo in repos:
        methods_dir = RAW_DIR / repo / "methods"
        if methods_dir.is_dir():
            methods = sorted(f.stem for f in methods_dir.glob("*.js") if f.stem != "index")
            if methods:
                result[repo] = methods
    return result


# --- Reference matrix ---


def load_reference_matrix() -> str | None:
    """Load provider-reference-matrix.md as raw text."""
    path = REFS_DIR / "provider-reference-matrix.md"
    if path.exists():
        return path.read_text()
    return None


# --- Scope detection ---


def detect_scope(files_changed: list[str]) -> list[str]:
    """Extract method names from files_changed paths."""
    methods: list[str] = []
    for f in files_changed:
        parts = Path(f).parts
        # Pattern: repo/methods/method_name.js
        if "methods" in parts:
            idx = list(parts).index("methods")
            if idx + 1 < len(parts):
                name = Path(parts[idx + 1]).stem
                if name != "index" and name not in methods:
                    methods.append(name)
        # Pattern: repo/libs/payload-builders/get-X-payload.js
        if "payload-builders" in parts:
            fname = Path(parts[-1]).stem
            if fname.startswith("get-") and fname.endswith("-payload"):
                method = fname[4:-8]  # strip get- and -payload
                if method not in methods:
                    methods.append(method)
    return methods


# --- Files changed per repo ---


def files_per_repo(files_changed: list[str], repos: list[str]) -> dict[str, list[str]]:
    """Group files_changed by repo name (first path component)."""
    result: dict[str, list[str]] = {r: [] for r in repos}
    for f in files_changed:
        parts = f.split("/", 1)
        if len(parts) == 2 and parts[0] in result:
            result[parts[0]].append(parts[1])
    return result


# --- Flow step description lookup ---


def find_repo_role(flows: list[dict], repo: str) -> str:
    """Find repo's role description from flow steps."""
    for flow in flows:
        for step in flow.get("steps", []):
            target = step.get("target", "")
            if target == repo or ("{provider}" in target and repo.startswith("grpc-apm-")):
                return step.get("description", "")
        for prereq in flow.get("prerequisites", []):
            if prereq.get("target", "") == repo:
                return prereq.get("what", "")
    return ""


# --- Common mistakes extraction ---


def collect_common_mistakes(flows: list[dict]) -> list[dict]:
    """Collect all common_mistakes from loaded flows."""
    mistakes: list[dict] = []
    for flow in flows:
        for m in flow.get("common_mistakes", []):
            mistakes.append(m)
    return mistakes


# --- Output formatting ---


def format_output(
    task: dict,
    provider: str | None,
    neighbors: dict[str, dict],
    flows: list[dict],
    methods_map: dict[str, list[str]],
    scope: list[str],
    repo_files: dict[str, list[str]],
    ref_matrix: str | None,
) -> str:
    lines: list[str] = []
    provider_label = provider or "unknown"

    # Header
    lines.append(f"# Audit Context: {task['ticket_id']} ({provider_label})")
    lines.append("")

    # Task section
    lines.append("## Task")
    lines.append(f"**Summary**: {task['summary']}")
    lines.append(f"**Scope**: {', '.join(scope) if scope else 'N/A'}")
    lines.append(f"**Repos changed**: {', '.join(task['repos_changed'])}")
    lines.append("")

    # Execution chain from flow steps
    lines.append("## Execution Chain")
    for flow in flows:
        lines.append(f"### {flow.get('name', 'Unnamed flow')}")
        lines.append(f"*Source: {flow.get('_source_file', 'unknown')}*")
        lines.append("")
        for i, step in enumerate(flow.get("steps", []), 1):
            target = step.get("target", "?")
            if provider and "{provider}" in target:
                target = target.replace("{provider}", provider)
            action = step.get("action", "?")
            method = step.get("method", "")
            desc = step.get("description", "")
            method_str = f" `{method}`" if method else ""
            lines.append(f"{i}. **{action}** → {target}{method_str} — {desc}")
        lines.append("")

    # Per-repo context
    lines.append("## Per-Repo Context")
    for repo in task["repos_changed"]:
        lines.append(f"### {repo}")
        role = find_repo_role(flows, repo)
        nb = neighbors.get(repo, {"upstream": [], "downstream": []})
        methods = methods_map.get(repo, [])
        files = repo_files.get(repo, [])

        lines.append(f"- **Role**: {role or 'N/A'}")
        lines.append(f"- **Upstream**: {', '.join(nb['upstream']) if nb['upstream'] else 'none'}")
        lines.append(f"- **Downstream**: {', '.join(nb['downstream']) if nb['downstream'] else 'none'}")
        lines.append(f"- **Existing methods**: {', '.join(methods) if methods else 'N/A'}")
        lines.append(f"- **Files changed**: {len(files)} files")
        if files:
            for f in files[:15]:
                lines.append(f"  - {f}")
            if len(files) > 15:
                lines.append(f"  - ... and {len(files) - 15} more")
        lines.append("")

    # Reference matrix
    if ref_matrix:
        lines.append("## Reference Provider Comparison")
        # Include just the quick reference table and audit checklist, not full doc
        in_table = False
        in_checklist = False
        for line in ref_matrix.splitlines():
            if line.startswith("## Quick Reference Table"):
                in_table = True
            elif line.startswith("## Audit Checklist"):
                in_checklist = True
            elif line.startswith("## ") and in_table:
                in_table = False
            elif line.startswith("## ") and in_checklist:
                in_checklist = False

            if in_table or in_checklist:
                lines.append(line)
        lines.append("")

    # Flow steps with contracts
    lines.append("## Flow Steps for Audit")
    step_num = 0
    for flow in flows:
        lines.append(f"### {flow.get('name', 'Unnamed')}")
        for step in flow.get("steps", []):
            step_num += 1
            target = step.get("target", "?")
            if provider and "{provider}" in target:
                target = target.replace("{provider}", provider)

            lines.append(f"{step_num}. **{step.get('action', '?')}** → {target}")

            if ic := step.get("input_contract"):
                lines.append(f"   - Input: {', '.join(c.get('field', '?') for c in ic)}")
            if oc := step.get("output_contract"):
                lines.append(f"   - Output: {', '.join(c.get('field', '?') for c in oc)}")
            if fm := step.get("failure_modes"):
                for mode in fm:
                    lines.append(f"   - FAILURE: {mode}")
            if verify := step.get("verify"):
                for v in verify:
                    if provider:
                        v = v.replace("{provider}", provider)
                    lines.append(f"   - Verify: `{v}`")
        lines.append("")

    # Common mistakes
    mistakes = collect_common_mistakes(flows)
    if mistakes:
        lines.append("## Common Mistakes")
        for m in mistakes:
            sev = m.get("severity", "?")
            lines.append(f"- **[{sev}]** {m.get('mistake', '?')}")
            if symptom := m.get("symptom"):
                lines.append(f"  - Symptom: {symptom}")
            if fix := m.get("fix"):
                lines.append(f"  - Fix: {fix}")
            if source := m.get("source"):
                lines.append(f"  - Source: {source}")
        lines.append("")

    # Prerequisites from flows
    prereqs: list[dict] = []
    for flow in flows:
        prereqs.extend(flow.get("prerequisites", []))
    if prereqs:
        lines.append("## Prerequisites")
        for p in prereqs:
            target = p.get("target", "?")
            lines.append(f"- **{target}**: {p.get('what', '?')}")
            if without := p.get("without_it"):
                lines.append(f"  - Without it: {without}")
        lines.append("")

    return "\n".join(lines)


# --- Main ---


def main() -> None:
    parser = argparse.ArgumentParser(description="Build audit context for a task")
    parser.add_argument("--task", required=True, help="Task ticket ID (e.g. PI-60)")
    parser.add_argument("--provider", default=None, help="Provider name (e.g. payper)")
    args = parser.parse_args()

    conn = get_db()
    try:
        task = load_task(conn, args.task)
        if not task:
            print(f"Error: task matching '{args.task}' not found in task_history", file=sys.stderr)
            sys.exit(1)

        provider = args.provider
        repos = task["repos_changed"]

        neighbors = load_neighbors(conn, repos)
        flows = load_flows(provider, task["summary"])
        methods_map = find_existing_methods(repos)
        scope = detect_scope(task["files_changed"])
        repo_files = files_per_repo(task["files_changed"], repos)
        ref_matrix = load_reference_matrix()

        output = format_output(
            task=task,
            provider=provider,
            neighbors=neighbors,
            flows=flows,
            methods_map=methods_map,
            scope=scope,
            repo_files=repo_files,
            ref_matrix=ref_matrix,
        )
        print(output)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
