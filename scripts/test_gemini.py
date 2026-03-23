#!/usr/bin/env python3
"""Test Gemini 3.1 Pro as reasoning layer for analyze_task."""

import json
import re
import sqlite3
import sys
from pathlib import Path

from google import genai

API_KEY = "REDACTED_ROTATE_KEY"
MODEL = "gemini-3.1-pro-preview"
DB_PATH = Path(__file__).parent.parent / "db" / "knowledge.db"

SYSTEM_PROMPT = """You are analyzing a software development task to predict which repositories need code changes.

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

## Integration Complexity Tiers
- Adding method to existing provider: 2-3 repos
- New redirect APM: 8 repos (provider + credentials + features + api + webhooks)
- Full new provider: 10-12 repos
- Provider + own 3DS: 12-14 repos

## Sale Completion Patterns
- Status Polling: provider has GET /status → webhook triggers sale → sale calls status
- Webhook Data: webhook saves to loggers → sale polls loggers
- Direct API: sale calls provider API directly

## Instructions
Predict repos that need ACTUAL code changes (not just package.json bumps).
Be precise — only include repos you are confident about.
Return ONLY valid JSON, no markdown wrappers:
{"predicted_repos": [{"repo": "name", "confidence": "high|medium|low", "reason": "brief"}]}"""


def test_task(task_id: str) -> dict:
    """Test Gemini prediction on a single task."""
    client = genai.Client(api_key=API_KEY)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    r = db.execute(
        "SELECT summary, description, repos_changed FROM task_history WHERE ticket_id = ?",
        (task_id,),
    ).fetchone()
    if not r:
        print(f"Task {task_id} not found")
        return {}

    expected = set(json.loads(r["repos_changed"]) if r["repos_changed"] else [])
    desc = (r["description"] or "N/A")[:800]

    user_prompt = f"Ticket: {task_id}\nSummary: {r['summary']}\nDescription: {desc}"

    response = client.models.generate_content(
        model=MODEL,
        contents=[{"role": "user", "parts": [{"text": SYSTEM_PROMPT + "\n\n## Task\n" + user_prompt}]}],
    )

    # Parse response — strip markdown wrappers
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON from text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            print(f"Failed to parse: {text[:200]}")
            return {}

    predicted = {r["repo"] for r in result["predicted_repos"]}
    hits = expected & predicted
    missed = expected - predicted

    recall = len(hits) / len(expected) * 100 if expected else 0
    precision = len(hits) / len(predicted) * 100 if predicted else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    print(f"\n=== {task_id}: {r['summary']} ===")
    for p in result["predicted_repos"]:
        marker = "✅" if p["repo"] in expected else "⚠️ "
        print(f"  {marker} {p['repo']:40} ({p['confidence']}) {p['reason']}")
    if missed:
        print(f"  MISSED: {sorted(missed)}")
    print(f"  Recall: {recall:.0f}% ({len(hits)}/{len(expected)})")
    print(f"  Precision: {precision:.0f}% ({len(hits)}/{len(predicted)})")
    print(f"  F1: {f1:.0f}%")

    return {"recall": recall, "precision": precision, "f1": f1, "predicted": len(predicted), "expected": len(expected)}


if __name__ == "__main__":
    tasks = sys.argv[1:] if len(sys.argv) > 1 else ["PI-54"]
    for t in tasks:
        test_task(t)
