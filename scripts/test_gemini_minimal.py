#!/usr/bin/env python3
"""Test Gemini 3.1 Pro with MINIMAL prompt — just task + repo list, no architecture context."""

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from google import genai

API_KEY = "AIzaSyDyAx-LkB60xSQb7MGNLuOe6dhJlOPkwlg"
MODEL = "gemini-3.1-pro-preview"
DB_PATH = Path(__file__).parent.parent / "db" / "knowledge.db"


# Get all repo names from DB
def get_all_repos():
    db = sqlite3.connect(str(DB_PATH))
    return [r[0] for r in db.execute("SELECT name FROM repos ORDER BY name").fetchall()]


ALL_REPOS = get_all_repos()

MINIMAL_SYSTEM_PROMPT = f"""Predict which repos need code changes for the given task. Return JSON only.

Available repos ({len(ALL_REPOS)} total):
{chr(10).join(ALL_REPOS)}

Return ONLY valid JSON:
{{"predicted_repos": [{{"repo": "name", "confidence": "high|medium|low", "reason": "brief"}}]}}"""

MINIMAL_SYSTEM_PROMPT_THINKING = MINIMAL_SYSTEM_PROMPT  # same prompt for thinking mode

TASKS = ["PI-54", "PI-40", "PI-21", "PI-5", "CORE-2451"]


def test_task(task_id: str, use_thinking: bool = False) -> dict:
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

    prompt = MINIMAL_SYSTEM_PROMPT + "\n\n## Task\n" + user_prompt

    config = {}
    if use_thinking:
        config = {"thinking_config": {"thinking_budget": 8192}}

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config=config if config else None,
        )
    except Exception as e:
        print(f"  API error: {e}")
        return {}

    # Parse response — strip markdown wrappers
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                print(f"  Failed to parse: {text[:300]}")
                return {}
        else:
            print(f"  Failed to parse: {text[:300]}")
            return {}

    predicted = {r["repo"] for r in result.get("predicted_repos", [])}
    hits = expected & predicted
    missed = expected - predicted

    recall = len(hits) / len(expected) * 100 if expected else 0
    precision = len(hits) / len(predicted) * 100 if predicted else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    mode = "THINKING" if use_thinking else "STANDARD"
    print(f"\n=== {task_id} [{mode}]: {r['summary'][:60]} ===")
    for p in result.get("predicted_repos", []):
        marker = "HIT" if p["repo"] in expected else "FP "
        print(f"  {marker} {p['repo']:45} ({p['confidence']}) {p['reason']}")
    if missed:
        print(f"  MISSED: {sorted(missed)}")
    print(f"  Recall: {recall:.0f}% ({len(hits)}/{len(expected)})")
    print(f"  Precision: {precision:.0f}% ({len(hits)}/{len(predicted)})")
    print(f"  F1: {f1:.0f}%")

    return {
        "task": task_id,
        "mode": mode,
        "recall": recall,
        "precision": precision,
        "f1": f1,
        "predicted": len(predicted),
        "expected": len(expected),
        "hits": len(hits),
        "missed": sorted(missed),
    }


def main():
    tasks = sys.argv[1:] if len(sys.argv) > 1 else TASKS

    print("=" * 80)
    print("GEMINI 3.1 PRO — MINIMAL PROMPT (no architecture context)")
    print(f"Prompt: task description + {len(ALL_REPOS)} repo names, nothing else")
    print("=" * 80)

    # Standard mode
    results_standard = []
    for t in tasks:
        res = test_task(t, use_thinking=False)
        if res:
            results_standard.append(res)
        time.sleep(1)

    # Thinking mode
    print("\n" + "=" * 80)
    print("NOW TESTING WITH THINKING/REASONING ENABLED (budget=8192)")
    print("=" * 80)

    results_thinking = []
    for t in tasks:
        res = test_task(t, use_thinking=True)
        if res:
            results_thinking.append(res)
        time.sleep(1)

    # Summary table
    print("\n" + "=" * 80)
    print("RESULTS SUMMARY")
    print("=" * 80)
    print(f"{'Task':<12} {'Mode':<10} {'Recall':>8} {'Precision':>10} {'F1':>6} {'Hits':>6} {'Pred':>6} {'Expect':>8}")
    print("-" * 70)

    for r in results_standard + results_thinking:
        print(
            f"{r['task']:<12} {r['mode']:<10} {r['recall']:>7.0f}% {r['precision']:>9.0f}% {r['f1']:>5.0f}% "
            f"{r['hits']:>5}/{r['predicted']:<4} {r['expected']:>5}"
        )

    # Averages
    for label, results in [("STANDARD", results_standard), ("THINKING", results_thinking)]:
        if results:
            avg_r = sum(r["recall"] for r in results) / len(results)
            avg_p = sum(r["precision"] for r in results) / len(results)
            avg_f = sum(r["f1"] for r in results) / len(results)
            print(f"\n  {label} AVG: Recall={avg_r:.1f}% Precision={avg_p:.1f}% F1={avg_f:.1f}%")


if __name__ == "__main__":
    main()
