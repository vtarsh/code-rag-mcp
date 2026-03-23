#!/usr/bin/env python3
"""Test Gemini 3.1 Pro with REASONING mode (thinking budget) on analyze_task."""

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

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
    """Test Gemini prediction with reasoning on a single task."""
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

    config = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=5000))

    t0 = time.time()
    response = client.models.generate_content(
        model=MODEL,
        contents=[{"role": "user", "parts": [{"text": SYSTEM_PROMPT + "\n\n## Task\n" + user_prompt}]}],
        config=config,
    )
    elapsed = time.time() - t0

    # Analyze parts: separate thinking from output
    thinking_text = ""
    output_text = ""

    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if hasattr(part, "thought") and part.thought:
                thinking_text = part.text or ""
            else:
                output_text += part.text or ""

    # Usage metadata
    usage = response.usage_metadata
    prompt_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
    candidates_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
    thoughts_tokens = getattr(usage, "thoughts_token_count", 0) if usage else 0
    total_tokens = getattr(usage, "total_token_count", 0) if usage else 0

    # Parse response — strip markdown wrappers
    text = output_text.strip()
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
            print(f"Thinking: {thinking_text[:200]}")
            return {}

    predicted = {r["repo"] for r in result["predicted_repos"]}
    hits = expected & predicted
    false_pos = predicted - expected
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
    print(f"  Time: {elapsed:.1f}s")
    print(
        f"  Tokens — prompt: {prompt_tokens}, output: {candidates_tokens}, thinking: {thoughts_tokens}, total: {total_tokens}"
    )
    if thinking_text:
        print(f"  Thinking preview: {thinking_text[:200]}...")

    return {
        "task": task_id,
        "recall": recall,
        "precision": precision,
        "f1": f1,
        "predicted": len(predicted),
        "expected": len(expected),
        "hits": len(hits),
        "missed": len(missed),
        "false_pos": len(false_pos),
        "time_s": round(elapsed, 1),
        "thinking_tokens": thoughts_tokens,
        "total_tokens": total_tokens,
    }


if __name__ == "__main__":
    tasks = sys.argv[1:] if len(sys.argv) > 1 else ["PI-54", "PI-40", "PI-21", "PI-5", "CORE-2451"]

    results = []
    for t in tasks:
        res = test_task(t)
        if res:
            results.append(res)

    if results:
        print("\n" + "=" * 90)
        print(
            f"{'Task':<12} {'Recall':>7} {'Prec':>7} {'F1':>7} {'Hit/Exp':>8} {'FP':>4} {'Time':>6} {'Think':>7} {'Total':>7}"
        )
        print("-" * 90)
        for r in results:
            print(
                f"{r['task']:<12} {r['recall']:>6.0f}% {r['precision']:>6.0f}% {r['f1']:>6.0f}% {r['hits']:>3}/{r['expected']:<4} {r['false_pos']:>4} {r['time_s']:>5.1f}s {r['thinking_tokens']:>7} {r['total_tokens']:>7}"
            )

        avg_recall = sum(r["recall"] for r in results) / len(results)
        avg_prec = sum(r["precision"] for r in results) / len(results)
        avg_f1 = sum(r["f1"] for r in results) / len(results)
        avg_time = sum(r["time_s"] for r in results) / len(results)
        total_think = sum(r["thinking_tokens"] for r in results)
        total_tok = sum(r["total_tokens"] for r in results)
        print("-" * 90)
        print(
            f"{'AVERAGE':<12} {avg_recall:>6.0f}% {avg_prec:>6.0f}% {avg_f1:>6.0f}% {'':>8} {'':>4} {avg_time:>5.1f}s {total_think:>7} {total_tok:>7}"
        )
        print("=" * 90)
