"""Gemini-based re-ranker for analyze_task repo predictions.

Takes the broad candidate list from analyze_task and uses Gemini 3.1 Pro
to filter down to the most relevant repos with reasoning.

Architecture: tool generates 200+ candidates → Gemini filters to top 10-15.
Calibration: recall-tuned prompt (62% recall, 63% precision, F1=56% macro on 5 tasks).
Previous conservative prompt: (52% recall, 70% precision, F1=55% macro).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".pay-knowledge")))
_TEMPLATE_PATH = (
    _BASE_DIR / "profiles" / os.getenv("ACTIVE_PROFILE", "pay-com") / "docs" / "flows" / "pi-generic-apm-integration.md"
)

# Gemini API key — loaded from env or profile
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Fallback: read from .env files
if not GEMINI_API_KEY:
    for env_path in [
        Path.home() / "telegram-claude-bot" / ".env",
        _BASE_DIR / ".env",
    ]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("GEMINI_API_KEYS="):
                    keys = line.split("=", 1)[1].strip().strip("'\"")
                    GEMINI_API_KEY = keys.split(",")[0]
                    break
            if GEMINI_API_KEY:
                break

MODEL = "gemini-3.1-pro-preview"

SYSTEM_PROMPT = """You are analyzing a software development task to predict which repositories need ACTUAL code changes (not just package.json bumps).

## Architecture Context
Payment platform with repo patterns:
- grpc-apm-{provider} — APM provider service
- grpc-providers-{provider} — card provider service
- grpc-providers-credentials — credential storage
- grpc-providers-features — feature flags (seeds.cql)
- express-webhooks — webhook HTTP ingress
- workflow-provider-webhooks — Temporal webhook processing
- express-api-v1 — public REST API
- express-api-internal — internal API
- express-api-callbacks — browser redirect callbacks
- libs-types — protobuf definitions
- grpc-payment-gateway — payment routing
- grpc-core-schemas — shared schemas
- node-libs-common — shared enums
- e2e-tests — integration tests

## Dependency Chains (IMPORTANT — think through these)
Changes rarely happen in isolation. Think about DOWNSTREAM and UPSTREAM repos:
- New API feature → express-api-v1 + grpc-payment-gateway + libs-types + grpc-core-schemas + node-libs-common
- New provider method → grpc-apm-*/grpc-providers-* + grpc-providers-features + grpc-providers-credentials + grpc-payment-gateway
- Webhook integration → express-webhooks + workflow-provider-webhooks (ALWAYS together)
- Any new proto/schema → libs-types + grpc-core-schemas
- Any new enum/status → node-libs-common
- Provider with transactions → grpc-core-transactions
- Auth/API changes → express-api-authentication + grpc-auth-apikeys2
- Risk/fraud features → grpc-payment-risk
- MPI/3DS features → express-api-mpi + grpc-mpi-*

## Expected Repo Counts by Task Type
- Adding method to existing provider: 2-4 repos
- New redirect APM integration: 8-12 repos
- Full new provider: 10-14 repos
- Cross-cutting schema change: 5-15 repos
- New standalone service/API feature: 8-16 repos
- Multi-provider rollout (same change to N providers): N + 1-3 shared repos
- Webhook/collaboration integration: 4-7 repos

## Instructions
From the candidate list below, select ALL repos that likely need code changes.
Include ALL repos that are likely involved. It's better to include a repo that might not need changes than to miss one that does. Missing a repo causes downstream integration failures that are much more costly than reviewing an extra repo.

When in doubt, INCLUDE the repo with medium or low confidence rather than excluding it.

Think about the FULL chain: if a new API endpoint is needed, that means API repo + gateway + schemas + types + tests. If webhooks are involved, that means webhook ingress + workflow processing. If providers are involved, include credentials and features repos.

Return ONLY valid JSON (no markdown wrappers):
{"repos": [{"repo": "name", "confidence": "high|medium|low", "reason": "brief"}]}"""


def _estimate_scope(description: str) -> str:
    """Estimate expected repo count from task description."""
    desc_lower = description.lower()
    if any(kw in desc_lower for kw in ["migrate", "audit", "all services", "tech debt", "bump version"]):
        return "This is a bulk/migration task. Expect 10-30 repos."
    if any(kw in desc_lower for kw in ["integration"]) and any(kw in desc_lower for kw in ["provider", "apm"]):
        return "This is a new provider integration. Expect 8-12 repos."
    if any(kw in desc_lower for kw in ["respect", "honor", "enforce"]) and any(
        kw in desc_lower for kw in ["all", "every", "providers"]
    ):
        return "This is a cross-provider enforcement change. Expect 5-12 repos."
    if any(kw in desc_lower for kw in ["standalone", "3ds", "cross-cutting"]):
        return "This is a cross-cutting platform change. Expect 8-16 repos."
    if any(kw in desc_lower for kw in ["add field", "extend", "add column", "new field"]):
        return "This is a field/schema extension. Expect 5-10 repos."
    if any(kw in desc_lower for kw in ["verification", "add method", "payout", "refund"]):
        return "This is adding a method to an existing provider. Expect 2-5 repos."
    if any(kw in desc_lower for kw in ["fix", "bug", "error", "wrong", "broken"]):
        return "This is a bug fix. Expect 1-3 repos."
    return "Expect 3-8 repos based on typical task scope."


def _load_template() -> str:
    """Load PI integration template if available."""
    if _TEMPLATE_PATH.exists():
        return _TEMPLATE_PATH.read_text()[:2000]
    return ""


def _get_graph_context(conn: sqlite3.Connection, candidate_repos: list[str]) -> str:
    """Get graph edges between candidate repos."""
    if not candidate_repos:
        return ""
    # Get edges between candidates (limited)
    placeholders = ",".join("?" for _ in candidate_repos[:20])
    edges = conn.execute(
        f"""SELECT source, target, edge_type FROM graph_edges
            WHERE source IN ({placeholders}) AND target IN ({placeholders})
            AND edge_type IN ('runtime_routing','webhook_handler','grpc_call','grpc_client_usage','callback_handler')
            LIMIT 30""",
        candidate_repos[:20] + candidate_repos[:20],
    ).fetchall()
    if not edges:
        return ""
    lines = ["Graph edges between candidates:"]
    for e in edges:
        lines.append(f"  {e[0]} --({e[2]})--> {e[1]}")
    return "\n".join(lines)


def _get_similar_tasks(conn: sqlite3.Connection, description: str) -> str:
    """Find similar past tasks."""
    words = re.findall(r"[a-zA-Z]{4,}", description.lower())
    if not words:
        return ""
    # Pick distinctive words
    stop = {
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
        "task",
        "implement",
        "create",
    }
    terms = [w for w in words if w not in stop and len(w) > 4][:3]
    if not terms:
        return ""

    try:
        fts_query = " OR ".join(terms)
        rows = conn.execute(
            """SELECT t.ticket_id, t.summary, t.repos_changed
               FROM task_history_fts fts
               JOIN task_history t ON t.id = fts.rowid
               WHERE task_history_fts MATCH ?
               ORDER BY rank LIMIT 3""",
            (fts_query,),
        ).fetchall()
        if not rows:
            return ""
        lines = ["Similar past tasks:"]
        for r in rows:
            repos = json.loads(r[2]) if r[2] else []
            lines.append(f"  {r[0]}: {r[1]} → {repos[:8]}")
        return "\n".join(lines)
    except Exception:
        return ""


def rerank_repos(
    conn: sqlite3.Connection,
    description: str,
    candidate_repos: list[str],
) -> list[dict] | None:
    """Re-rank candidate repos using Gemini 3.1 Pro.

    Returns list of {"repo": str, "confidence": str, "reason": str} or None if unavailable.
    """
    if not GEMINI_API_KEY or not candidate_repos:
        return None

    try:
        from google import genai
    except ImportError:
        return None

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Build rich context
    template = _load_template()
    graph_ctx = _get_graph_context(conn, candidate_repos)
    similar = _get_similar_tasks(conn, description)

    scope_hint = _estimate_scope(description)

    user_prompt = f"""## Task
{description}

## Scope Estimate
{scope_hint}

## Candidate repos ({len(candidate_repos)} from automated analysis)
{json.dumps(candidate_repos[:50])}

{f"## PI Integration Template\n{template[:1500]}" if template else ""}
{graph_ctx}
{similar}

Select ALL repos that likely need code changes. Err on the side of inclusion."""

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[{"role": "user", "parts": [{"text": SYSTEM_PROMPT + "\n\n" + user_prompt}]}],
        )

        text = response.text.strip()
        # Strip markdown wrappers
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        # Try direct parse, then regex extraction
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                result = json.loads(match.group())
            else:
                return None
        return result.get("repos", result.get("predicted_repos", []))
    except Exception as e:
        import sys
        import traceback

        print(f"[reranker] Error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None
