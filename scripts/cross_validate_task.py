#!/usr/bin/env python3
"""
Phase 8, Step 7: Cross-validate task_history entries against MCP indices.

For each collected task, uses search/trace_chain/context_builder to find repos
that SHOULD have been involved but weren't linked in Jira/GitHub.

Usage:
    python scripts/cross_validate_task.py PI-54              # single task
    python scripts/cross_validate_task.py --all              # all tasks
    python scripts/cross_validate_task.py PI-54 --dry-run    # print without saving
"""

import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"
DAEMON_URL = "http://localhost:8742"


# ---------------------------------------------------------------------------
# MCP daemon helpers
# ---------------------------------------------------------------------------


def _mcp_call(tool: str, args: dict) -> str:
    """Call MCP tool via daemon HTTP API."""
    data = json.dumps(args).encode()
    req = urllib.request.Request(
        f"{DAEMON_URL}/tool/{tool}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            return result.get("result", "")
    except Exception as e:
        return f"Error calling {tool}: {e}"


def mcp_search(query: str, limit: int = 15) -> str:
    return _mcp_call("search", {"query": query, "limit": limit})


def mcp_trace_chain(start: str, direction: str = "both", max_depth: int = 3) -> str:
    return _mcp_call("trace_chain", {"start": start, "direction": direction, "max_depth": max_depth})


def mcp_context_builder(query: str, repo: str = "", search_limit: int = 10) -> str:
    return _mcp_call("context_builder", {"query": query, "repo": repo, "search_limit": search_limit})


# ---------------------------------------------------------------------------
# Repo extraction from MCP results
# ---------------------------------------------------------------------------


def extract_repos_from_text(text: str) -> set[str]:
    """Extract repo names mentioned in MCP tool output."""
    import re

    # Match patterns like **repo-name** | or repo: repo-name or Repos: repo1, repo2
    repos: set[str] = set()

    # Pattern: **repo-name** (bold in search results)
    for m in re.finditer(r"\*\*([a-z][a-z0-9-]+(?:-[a-z0-9]+)*)\*\*", text):
        candidate = m.group(1)
        if len(candidate) > 3 and not candidate.startswith(("the-", "and-", "for-", "not-")):
            repos.add(candidate)

    # Pattern: repo-name/ (path prefix)
    for m in re.finditer(r"(?:^|\s)([a-z][a-z0-9-]+-[a-z0-9-]+)/", text, re.MULTILINE):
        repos.add(m.group(1))

    # Pattern: → repo-name (graph edges)
    for m in re.finditer(r"[→←]\s*([a-z][a-z0-9-]+-[a-z0-9-]+)", text):
        repos.add(m.group(1))

    # Pattern: pay-com/repo-name
    for m in re.finditer(r"pay-com/([a-z][a-z0-9-]+)", text):
        repos.add(m.group(1))

    # Pattern: "  - repo-name" (list items in find_dependencies output)
    for m in re.finditer(r"^\s+-\s+([a-z][a-z0-9-]+-[a-z0-9-]+)", text, re.MULTILINE):
        repos.add(m.group(1))

    # Pattern: repo-name (standalone on a line, e.g. in trace_chain)
    for m in re.finditer(r"(?:^|\n)\s*([a-z](?:[a-z0-9]+-)+[a-z0-9]+)\s*(?:\n|$)", text):
        candidate = m.group(1)
        if len(candidate) > 5:
            repos.add(candidate)

    return repos


# ---------------------------------------------------------------------------
# Cross-validation logic
# ---------------------------------------------------------------------------


def cross_validate(ticket_id: str, *, dry_run: bool = False) -> list[dict]:
    """Cross-validate a task against MCP indices. Returns list of gaps."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM task_history WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if not row:
            print(f"  [error] {ticket_id} not found in task_history")
            return []

        summary = row["summary"] or ""
        description = row["description"] or ""
        repos_changed = set(json.loads(row["repos_changed"] or "[]"))

        print(f"\n  Task: {ticket_id} — {summary}")
        print(f"  Known repos ({len(repos_changed)}): {', '.join(sorted(repos_changed))}")

        # Strategy 1: Search by summary keywords
        print("  [search] Querying by summary...")
        search_result = mcp_search(summary, limit=15)
        search_repos = extract_repos_from_text(search_result)

        # Strategy 2: Find INCOMING callers (who calls/depends on our repos)
        # Track edge types for confidence scoring
        MAIN_FLOW_EDGES = {"runtime_routing", "grpc_method_call", "grpc_client_usage", "flow_step", "webhook_handler"}
        print("  [graph] Finding direct callers of known repos...")
        trace_repos: set[str] = set()
        main_flow_repos: set[str] = set()  # repos connected via main flow edges
        db = sqlite3.connect(str(DB_PATH))
        try:
            for repo in repos_changed:
                # Who calls/depends on this repo? (incoming edges)
                callers = db.execute(
                    "SELECT DISTINCT source, edge_type FROM graph_edges WHERE target = ?", (repo,)
                ).fetchall()
                for caller, etype in callers:
                    if not caller.startswith("pkg:"):
                        trace_repos.add(caller)
                        if etype in MAIN_FLOW_EDGES:
                            main_flow_repos.add(caller)
                # Also: who does this repo call via runtime_routing only
                targets = db.execute(
                    """SELECT DISTINCT target FROM graph_edges
                       WHERE source = ? AND edge_type = 'runtime_routing'""",
                    (repo,),
                ).fetchall()
                for (target,) in targets:
                    if not target.startswith("pkg:"):
                        trace_repos.add(target)
                        main_flow_repos.add(target)
        finally:
            db.close()

        # Strategy 3: Context builder with description keywords
        print("  [context_builder] Building context from description...")
        # Extract key terms from description (first 200 chars)
        desc_query = summary
        if description:
            # Add first meaningful sentence from description
            first_line = description.split("\n")[0].strip()[:150]
            if first_line:
                desc_query = f"{summary} {first_line}"
        context_result = mcp_context_builder(desc_query, search_limit=10)
        context_repos = extract_repos_from_text(context_result)

        # Strategy 4: Targeted search for key terms from description
        print("  [search] Targeted keyword searches...")
        targeted_repos: set[str] = set()
        # Extract unique significant words from summary
        keywords = [w.lower() for w in summary.split() if len(w) > 4 and w.isalpha()]
        # Search for pairs of keywords that might find relevant code
        if len(keywords) >= 2:
            for i in range(min(3, len(keywords))):
                pair_query = f"{keywords[i]} {keywords[(i + 1) % len(keywords)]}"
                targeted_result = mcp_search(pair_query, limit=10)
                targeted_repos.update(extract_repos_from_text(targeted_result))

        # Combine all found repos
        all_found = search_repos | trace_repos | context_repos | targeted_repos
        # Remove known repos to find gaps
        potential_gaps = all_found - repos_changed

        # Filter: only keep repos that actually exist in our DB
        existing_repos = {r[0] for r in conn.execute("SELECT name FROM repos").fetchall()}
        potential_gaps = potential_gaps & existing_repos

        # ---------------------------------------------------------------
        # ---------------------------------------------------------------
        # 5-Tier Noise Filter (Phase 10 — precision-focused)
        # ---------------------------------------------------------------
        content_found = search_repos | context_repos | targeted_repos
        summary_lower = summary.lower()

        # Tier 1: Auto-exclude — always noise, regardless of confidence
        TIER1_ALWAYS_EXCLUDE = {
            # Frontends / plugins
            "receipts-web",
            "woocommerce-gateway-paycom",
            "node-libs-envoy-proto",
            # Reconciliation workflows
            "workflow-reconciliation-authorization",
            "workflow-reconciliation-avs",
            "workflow-reconciliation-bank-accounts",
            "workflow-reconciliation-file-processing",
            "workflow-reverse-reconciliation",
            "workflow-worldpay-reconciliation-exchange-rates",
            "workflow-silverflow-reconciliation-settlement",
            "workflow-reconciliation-chargeback",
            "workflow-reconciliation-processing",
            "workflow-reconciliation-ach-output",
            "workflow-reconciliation-fraud",
            "workflow-reconciliation-bank-transfer",
            # Hub webhook repos (connect to everything)
            "express-webhooks-zimpler",
            "express-webhooks-moneybite",
            # Dormant repos (verified <5 commits/year, thin proxy or static)
            "express-api-webhooks",  # 22 commits lifetime, CRUD proxy over grpc-core-webhooks
            "workflow-sub-payments-master",  # 20 commits lifetime, batch processor
            "workflow-subscriptions-manager",  # static, last code change Sep 2025
        }

        # Tier 2: Hub libs — exclude below confidence threshold
        TIER2_HUB_LIBS = {
            "libs-types",
            "node-libs-common",
            "grpc-core-schemas",
            "providers-proto",
            "space-web",
            "grpc-loggers-rest",
            "express-vault",
        }
        TIER2_CONFIDENCE_FLOOR = 0.45

        # Tier 3: Reconciliation/settlement repos — exclude unless task is about recon
        TIER3_RECON_REPOS = {
            "workflow-nets-reconciliation-settlement",
            "workflow-crb-account-activity-reconciliation",
            "workflow-worldpay-reconciliation-settlement",
            "workflow-credorax-reconciliation-settlement",
            "workflow-tabapay-reconciliation-settlement",
            "workflow-transaction-reconciliation",
            "workflow-reconciliation-processing",
            "grpc-reconciliation-config",
            "grpc-core-reconciliation",
        }
        RECON_KEYWORDS = {"reconciliation", "settlement", "recon"}
        task_is_about_recon = any(kw in summary_lower for kw in RECON_KEYWORDS)

        # Tier 4: Orchestrator exemption — hub orchestrators only flagged for orchestration tasks
        ORCHESTRATOR_REPOS = {
            "grpc-payment-gateway",  # 193 edges, provider-agnostic dispatcher
            "express-api-v1",  # main API, connects to everything
        }
        ORCHESTRATION_KEYWORDS = {
            "routing",
            "gateway",
            "orchestrat",
            "dispatch",
            "middleware",
            "pricing",
            "risk check",
            "3ds",
            "sca",
            "payment method type",
            "new payment method",
            "api endpoint",
            "rate limit",
        }
        task_is_orchestration = any(kw in summary_lower for kw in ORCHESTRATION_KEYWORDS)

        # Infra prefixes — still filter unless found by content search
        infra_prefixes = ("node-libs-", "libs-", "mali", "pkg:")

        # Tier 5: Hub detection — repos with >15 incoming edges, their graph neighbors are noisy
        hub_callers: set[str] = set()
        db2 = sqlite3.connect(str(DB_PATH))
        try:
            for repo in repos_changed:
                incoming = db2.execute(
                    "SELECT COUNT(DISTINCT source) FROM graph_edges WHERE target = ?", (repo,)
                ).fetchone()[0]
                # Exclude runtime_routing edges from main_flow detection (dynamic dispatch, not code coupling)
                outgoing_structural = db2.execute(
                    """SELECT COUNT(DISTINCT target) FROM graph_edges
                       WHERE source = ? AND edge_type NOT IN ('runtime_routing', 'npm_dep', 'npm_dep_tooling')""",
                    (repo,),
                ).fetchone()[0]

                if incoming > 15:
                    callers = db2.execute(
                        "SELECT DISTINCT source FROM graph_edges WHERE target = ?", (repo,)
                    ).fetchall()
                    for (c,) in callers:
                        if c not in content_found:
                            hub_callers.add(c)

                if outgoing_structural > 10:
                    targets = db2.execute(
                        """SELECT DISTINCT target FROM graph_edges
                           WHERE source = ? AND edge_type NOT IN ('runtime_routing', 'npm_dep', 'npm_dep_tooling')""",
                        (repo,),
                    ).fetchall()
                    for (t,) in targets:
                        if t not in content_found and t not in repos_changed:
                            hub_callers.add(t)
        finally:
            db2.close()

        filtered_gaps = set()
        for repo in potential_gaps:
            # Tier 1: always exclude
            if repo in TIER1_ALWAYS_EXCLUDE:
                continue
            # Tier 3: recon repos only if task is about recon
            if repo in TIER3_RECON_REPOS and not task_is_about_recon:
                continue
            # Tier 4: orchestrators only if task is about orchestration
            if repo in ORCHESTRATOR_REPOS and not task_is_orchestration and repo not in content_found:
                continue
            # Infra prefix filter
            is_infra = any(repo.startswith(p) for p in infra_prefixes)
            if is_infra and repo not in content_found:
                continue
            # Tier 5: Hub caller filter
            if repo in hub_callers:
                continue
            filtered_gaps.add(repo)
        potential_gaps = filtered_gaps

        # Score confidence with tiers: CRITICAL (main flow), HIGH, MED, LOW
        gaps: list[dict] = []
        for repo in sorted(potential_gaps):
            sources: list[str] = []
            if repo in search_repos:
                sources.append("search")
            if repo in trace_repos:
                sources.append("graph_edges")
            if repo in context_repos:
                sources.append("context_builder")
            if repo in targeted_repos:
                sources.append("targeted_search")
            is_main_flow = repo in main_flow_repos

            # Recalibrated confidence scoring (Phase 10)
            # Best combo: graph_edges + targeted_search (88.5% precision)
            # Worst combo: all strategies together (33% precision — specificity penalty)

            # Content-based strategies are more precise than graph-only
            content_sources = sum(1 for s in sources if s in ("search", "targeted_search", "context_builder"))
            graph_sources = sum(1 for s in sources if s == "graph_edges")

            # Base: content strategies weighted higher
            confidence = min(1.0, (content_sources * 0.35) + (graph_sources * 0.2))

            # Best combo boost: graph + targeted_search (88.5% precision)
            if "graph_edges" in sources and "targeted_search" in sources:
                confidence = min(1.0, confidence + 0.25)

            # Main flow gets modest boost (not major — 77% precision, lower than targeted)
            is_main_flow = repo in main_flow_repos
            if is_main_flow:
                confidence = min(1.0, confidence + 0.15)
                sources.append("main_flow")

            # Specificity penalty: 4+ strategies = likely hub repo noise
            if len(sources) >= 4:
                confidence = confidence * 0.7

            # Graph + content combo (still good signal)
            if repo in trace_repos and (repo in search_repos or repo in context_repos or repo in targeted_repos):
                confidence = min(1.0, confidence + 0.1)

            # Tier 2: Hub libs dampening — reduce confidence, skip if below floor
            if repo in TIER2_HUB_LIBS:
                confidence = confidence * 0.6  # dampen hub lib confidence
                if confidence < TIER2_CONFIDENCE_FLOOR:
                    continue

            # Method-level boost (93.3% precision — use as positive signal only)
            task_methods = set()
            for f in json.loads(row["files_changed"] or "[]"):
                if "/methods/" in f and ".spec." not in f:
                    mname = f.split("/methods/")[-1].replace(".js", "").replace(".ts", "")
                    if mname:
                        task_methods.add(mname)
            if task_methods:
                repo_methods = (
                    {
                        r[0]
                        for r in conn.execute(
                            "SELECT method_name FROM method_matrix WHERE repo_name = ?", (repo,)
                        ).fetchall()
                    }
                    if conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='method_matrix'"
                    ).fetchone()[0]
                    else set()
                )
                if repo_methods & task_methods:
                    # method match found
                    confidence = min(1.0, confidence + 0.2)
                    sources.append("method_match")

            # Global confidence floor
            if confidence < 0.2:
                continue

            # Determine gap type and root cause
            gap_type = "missing_repo"
            if is_main_flow:
                gap_type = "main_flow_repo"
            root_cause = "unknown"
            if is_main_flow:
                root_cause = "main request flow (gRPC/routing/webhook)"
            elif repo in trace_repos and repo not in search_repos and repo not in targeted_repos:
                root_cause = "direct caller/callee (not in task content)"
            elif repo in search_repos and repo not in trace_repos:
                root_cause = "related by content (not a direct dependency)"
            elif len(sources) >= 2:
                root_cause = "strong signal from multiple sources"

            gap = {
                "ticket_id": ticket_id,
                "gap_type": gap_type,
                "found_by": ",".join(sources),
                "expected": repo,
                "actual": json.dumps(sorted(repos_changed)),
                "confidence": round(confidence, 2),
                "root_cause": root_cause,
                "notes": f"Found by {len(sources)} strategy(ies): {', '.join(sources)}",
            }
            gaps.append(gap)

        # Sort by confidence descending for report
        gaps.sort(key=lambda g: -g["confidence"])

        # Report
        print(f"\n  MCP found {len(all_found)} total repos, {len(potential_gaps)} potential gaps:")
        for g in gaps:
            conf = (
                "CRITICAL"
                if g["gap_type"] == "main_flow_repo"
                else "HIGH"
                if g["confidence"] >= 0.67
                else "MED"
                if g["confidence"] >= 0.34
                else "LOW"
            )
            print(f"    [{conf}] {g['expected']} — {g['found_by']} — {g['root_cause']}")

        if not gaps:
            print("    No gaps found — task_history appears complete")

        # Save to DB
        if not dry_run and gaps:
            for g in gaps:
                conn.execute(
                    """INSERT INTO task_gaps
                       (ticket_id, gap_type, found_by, expected, actual, confidence, root_cause, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        g["ticket_id"],
                        g["gap_type"],
                        g["found_by"],
                        g["expected"],
                        g["actual"],
                        g["confidence"],
                        g["root_cause"],
                        g["notes"],
                    ),
                )
            conn.commit()
            print(f"\n  Saved {len(gaps)} gaps to task_gaps")

        return gaps
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) < 2:
        print("Usage: python cross_validate_task.py <TICKET-ID|--all> [--dry-run]")
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv

    if sys.argv[1] == "--all":
        conn = sqlite3.connect(str(DB_PATH))
        tickets = [r[0] for r in conn.execute("SELECT ticket_id FROM task_history").fetchall()]
        conn.close()
        print(f"Cross-validating {len(tickets)} tasks...")
        all_gaps: list[dict] = []
        for ticket_id in tickets:
            gaps = cross_validate(ticket_id, dry_run=dry_run)
            all_gaps.extend(gaps)
        print(f"\n{'=' * 60}")
        print(f"TOTAL: {len(all_gaps)} gaps across {len(tickets)} tasks")
        high = sum(1 for g in all_gaps if g["confidence"] >= 0.67)
        med = sum(1 for g in all_gaps if 0.34 <= g["confidence"] < 0.67)
        low = sum(1 for g in all_gaps if g["confidence"] < 0.34)
        print(f"  HIGH: {high}, MED: {med}, LOW: {low}")
    else:
        ticket_id = sys.argv[1].upper().strip()
        cross_validate(ticket_id, dry_run=dry_run)


if __name__ == "__main__":
    main()
