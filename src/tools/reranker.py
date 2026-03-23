"""Gemini-based re-ranker for analyze_task repo predictions.

Takes the broad candidate list from analyze_task and uses Gemini 3.1 Pro
to filter down to the most relevant repos with reasoning.

Architecture: tool generates 200+ candidates → Gemini filters to top 10-15.
Calibration showed: rich context (65% recall, 98% precision, F1=76%).
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

## Integration Complexity
- Adding method to existing provider: 2-3 repos
- New redirect APM: 8 repos
- Full new provider: 10-12 repos
- Cross-cutting schema change: 5-15 repos (mostly package bumps)

## Instructions
From the candidate list below, select ONLY repos that likely need ACTUAL code changes.
Be precise — fewer correct predictions are better than many guesses.
Return ONLY valid JSON (no markdown wrappers):
{"repos": [{"repo": "name", "confidence": "high|medium|low", "reason": "brief"}]}"""


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

    user_prompt = f"""## Task
{description}

## Candidate repos ({len(candidate_repos)} from automated analysis)
{json.dumps(candidate_repos[:50])}

{f"## PI Integration Template\n{template[:1500]}" if template else ""}
{graph_ctx}
{similar}

Select the repos most likely to need ACTUAL code changes. Be precise."""

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
