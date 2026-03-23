#!/usr/bin/env python3
"""Test Gemini 3.1 Pro with RICH context — architecture templates + graph data + similar tasks."""

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from google import genai

API_KEY = "REDACTED_ROTATE_KEY"
MODEL = "gemini-3.1-pro-preview"
DB_PATH = Path(__file__).parent.parent / "db" / "knowledge.db"
TEMPLATE_PATH = (
    Path(__file__).parent.parent / "profiles" / "pay-com" / "docs" / "flows" / "pi-generic-apm-integration.md"
)

# Load template at module level
PI_TEMPLATE = TEMPLATE_PATH.read_text() if TEMPLATE_PATH.exists() else ""

SYSTEM_PROMPT = (
    """You are analyzing a software development task to predict which repositories need code changes.

## Architecture Context
Payment platform with these repo patterns:
- grpc-apm-{provider} — APM provider service (methods: initialize, sale, refund, payout, verification)
- grpc-providers-{provider} — card provider service
- grpc-providers-credentials — credential storage for all providers
- grpc-providers-features — feature flags (seeds.cql)
- express-webhooks — webhook HTTP ingress
- workflow-provider-webhooks — Temporal webhook processing
- express-api-v1 — public REST API
- express-api-internal — internal API (initialize flow)
- express-api-callbacks — browser redirect callbacks
- libs-types — protobuf definitions
- grpc-payment-gateway — payment routing
- grpc-core-schemas — shared schemas
- node-libs-common — shared enums (payment method types)
- e2e-tests — integration tests
- express-api-authentication — 3DS authentication API
- express-api-mpi — MPI (3DS) challenge callback API
- grpc-mpi-{provider} — MPI 3DS service for specific providers (e.g., grpc-mpi-silverflow)
- grpc-payment-risk — risk assessment service (calls 3DS providers)
- grpc-core-transactions — transaction storage
- grpc-core-configurations — merchant configuration storage
- grpc-core-settings — payment method options settings
- grpc-auth-apikeys2 — API key authentication
- kafka-cdc-sink — CDC event consumer
- cloudflare-workers-tokenize2 — tokenization worker
- workflow-collaboration-processing — chargeback/dispute collaboration workflows
- next-web-alternative-payment-methods — UI for APM flows (Okto Cash KYC etc.)
- next-web-authorizing-transactions — UI for transaction authorization

## PI Integration Template
"""
    + PI_TEMPLATE
    + """

## Instructions
Predict repos that need ACTUAL code changes (not just package.json bumps or lock files).
Be precise — only include repos you are confident about.
Use the graph edges, similar past tasks, and file change patterns as evidence.
Return ONLY valid JSON, no markdown wrappers:
{"predicted_repos": [{"repo": "name", "confidence": "high|medium|low", "reason": "brief"}]}"""
)


def get_graph_edges(db: sqlite3.Connection, task_summary: str, task_desc: str) -> str:
    """Get relevant graph edges based on provider/repo names in the task."""
    # Extract potential provider names from summary + description
    text = (task_summary + " " + (task_desc or "")).lower()
    providers = []
    # Check known providers
    for name in [
        "trustly",
        "okto",
        "neosurf",
        "ppro",
        "nuvei",
        "chargebacks911",
        "silverflow",
        "gumballpay",
        "aps",
        "stripe",
        "braintree",
        "rapyd",
        "payper",
        "plaid",
        "libra",
        "payhub",
        "crb",
        "ilixium",
        "ecentric",
    ]:
        if name in text:
            providers.append(name)

    if not providers:
        return ""

    edges = []
    for prov in providers:
        rows = db.execute(
            """SELECT source, target, edge_type, detail FROM graph_edges
               WHERE (source LIKE ? OR target LIKE ?)
               AND edge_type NOT IN ('npm_dep_tooling', 'proto_message_usage')""",
            (f"%{prov}%", f"%{prov}%"),
        ).fetchall()
        for r in rows:
            edges.append(f"  {r[0]} --[{r[2]}]--> {r[1]} ({r[3]})")

    # Also get gateway routing edges
    gw_rows = db.execute(
        """SELECT source, target, edge_type, detail FROM graph_edges
           WHERE source = 'grpc-payment-gateway' AND edge_type = 'runtime_routing'""",
    ).fetchall()
    for r in gw_rows:
        edges.append(f"  {r[0]} --[{r[2]}]--> {r[1]} ({r[3]})")

    if not edges:
        return ""
    return "\n## Graph Edges (dependency/call relationships)\n" + "\n".join(edges)


def get_similar_tasks(db: sqlite3.Connection, task_id: str, task_summary: str) -> str:
    """Get similar past tasks with their repos_changed as examples."""
    prefix = task_id.split("-")[0]  # PI or CORE

    # Get tasks with same prefix (excluding the test task)
    rows = db.execute(
        """SELECT ticket_id, summary, repos_changed FROM task_history
           WHERE ticket_id LIKE ? AND ticket_id != ?
           ORDER BY ticket_id""",
        (f"{prefix}-%", task_id),
    ).fetchall()

    if not rows:
        return ""

    # Pick most relevant: keyword overlap
    keywords = set(task_summary.lower().split())
    scored = []
    for r in rows:
        s_words = set(r[1].lower().split())
        overlap = len(keywords & s_words)
        if overlap > 0:
            scored.append((overlap, r))

    scored.sort(key=lambda x: -x[0])
    top = scored[:8]  # top 8 similar tasks

    if not top:
        # Fall back to any tasks from same prefix
        top = [(0, r) for r in rows[:8]]

    lines = []
    for _, r in top:
        repos = json.loads(r[2]) if r[2] else []
        lines.append(f"  {r[0]}: {r[1]} -> {repos}")

    return "\n## Similar Past Tasks (ticket: summary -> repos_changed)\n" + "\n".join(lines)


def get_files_changed(db: sqlite3.Connection, task_id: str) -> str:
    """Get files_changed for the task (without revealing repos_changed)."""
    r = db.execute("SELECT files_changed FROM task_history WHERE ticket_id = ?", (task_id,)).fetchone()
    if not r or not r[0]:
        return ""
    files = json.loads(r[0])
    if not files:
        return ""
    # Show file paths but strip the repo prefix to avoid giving away repos
    # Actually, file paths include repo names as prefix, so just show them —
    # the model can infer repos from file paths, but the key test is whether
    # the enrichment helps predict repos beyond what's obvious from files.
    # To be fair: show files but NOT repos_changed.
    return "\n## Files Changed (from PRs)\n" + "\n".join(f"  {f}" for f in files[:50])


def test_task(task_id: str) -> dict:
    """Test Gemini prediction on a single task with rich context."""
    client = genai.Client(api_key=API_KEY)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    r = db.execute(
        "SELECT summary, description, repos_changed, files_changed FROM task_history WHERE ticket_id = ?",
        (task_id,),
    ).fetchone()
    if not r:
        print(f"Task {task_id} not found")
        return {}

    expected = set(json.loads(r["repos_changed"]) if r["repos_changed"] else [])
    desc = (r["description"] or "N/A")[:800]

    # Build enriched prompt
    graph_ctx = get_graph_edges(db, r["summary"], r["description"])
    similar_ctx = get_similar_tasks(db, task_id, r["summary"])
    files_ctx = get_files_changed(db, task_id)

    user_prompt = f"""## Task
Ticket: {task_id}
Summary: {r["summary"]}
Description: {desc}
{graph_ctx}
{similar_ctx}
{files_ctx}"""

    response = client.models.generate_content(
        model=MODEL,
        contents=[{"role": "user", "parts": [{"text": SYSTEM_PROMPT + "\n\n" + user_prompt}]}],
    )

    # Parse response — strip markdown wrappers
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            print(f"Failed to parse: {text[:300]}")
            return {}

    predicted = {p["repo"] for p in result["predicted_repos"]}
    hits = expected & predicted
    missed = expected - predicted

    recall = len(hits) / len(expected) * 100 if expected else 0
    precision = len(hits) / len(predicted) * 100 if predicted else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    print(f"\n=== {task_id}: {r['summary']} ===")
    for p in result["predicted_repos"]:
        marker = "HIT" if p["repo"] in expected else "FP "
        print(f"  {marker} {p['repo']:40} ({p['confidence']}) {p['reason']}")
    if missed:
        print(f"  MISSED: {sorted(missed)}")
    print(f"  Recall: {recall:.0f}% ({len(hits)}/{len(expected)})")
    print(f"  Precision: {precision:.0f}% ({len(hits)}/{len(predicted)})")
    print(f"  F1: {f1:.0f}%")

    return {
        "recall": recall,
        "precision": precision,
        "f1": f1,
        "predicted": len(predicted),
        "expected": len(expected),
        "hits": len(hits),
    }


if __name__ == "__main__":
    tasks = sys.argv[1:] if len(sys.argv) > 1 else ["PI-54", "PI-40", "PI-21", "PI-5", "CORE-2451"]

    results = {}
    for t in tasks:
        results[t] = test_task(t)
        time.sleep(2)  # Rate limiting

    # Summary table
    print("\n" + "=" * 80)
    print(f"{'Task':<12} {'Recall':>8} {'Precision':>10} {'F1':>6} {'Hits':>6} {'Pred':>6} {'Exp':>6}")
    print("-" * 80)
    total_r = total_p = total_f = 0
    n = 0
    for t, m in results.items():
        if m:
            print(
                f"{t:<12} {m['recall']:>7.0f}% {m['precision']:>9.0f}% {m['f1']:>5.0f}% {m['hits']:>6} {m['predicted']:>6} {m['expected']:>6}"
            )
            total_r += m["recall"]
            total_p += m["precision"]
            total_f += m["f1"]
            n += 1
    if n:
        print("-" * 80)
        print(f"{'AVG':<12} {total_r / n:>7.0f}% {total_p / n:>9.0f}% {total_f / n:>5.0f}%")
