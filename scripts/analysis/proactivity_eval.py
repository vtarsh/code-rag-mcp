#!/usr/bin/env python3
"""Proactivity eval — does analyze_task flag the 5 PI-60 reviewer bugs
on a CLEAN prompt?

Ground truth: `profiles/pay-com/real_comments_2026-04-10.md` — 5 line-level
review comments from reviewer `vboychyk` on PI-60 payper PRs. Each is a
pattern that existed elsewhere in the codebase but our sessions did not
surface when touching payper.

Clean prompt: a generic APM integration description paraphrased from the
PI-60 Jira ticket with NO reference to paysafe, s2s, shared routes,
reusablePayouts logic, method threading, or webhook tx enumeration.

Metric: for each of the 5 bugs, score 1 if analyze_task output contains
ANY of the predefined rubric signals (regex + case-insensitive); 0 otherwise.
Return the total 0..5.

Usage:
    python3 scripts/proactivity_eval.py
    python3 scripts/proactivity_eval.py --verbose
    python3 scripts/proactivity_eval.py --raw-output /tmp/analyze.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DAEMON_URL = "http://localhost:8742/tool/analyze_task"
EXCLUDE_TASK_ID = "PI-60"

# Clean prompt — paraphrase of PI-60's surface requirements with NO leak
# of the 5 reviewer-flagged mistakes. Intentionally generic: any
# phone-based APM with sale/payout/webhook flow.
CLEAN_PROMPT = (
    "Integrate a new alternative payment method provider into the "
    "payment gateway. The provider collects a phone number during "
    "verification and supports: (1) sale — direct charge from the "
    "consumer, (2) payout — send money back to the consumer via phone "
    "number, (3) webhook status updates for payment state transitions. "
    "The provider also supports reusable payout tokens so returning "
    "customers can receive money without re-entering details."
)


@dataclass
class Rubric:
    id: str
    title: str
    signals: list[str]  # regex patterns (case-insensitive)
    bug_ref: str  # short description of the PR bug


# Each rubric scores 1 if ANY signal matches the analyze_task output.
# Signals are chosen so that a thoughtful analyze_task section would
# emit them naturally — NOT by keyword-stuffing the clean prompt.
RUBRICS: list[Rubric] = [
    Rubric(
        id="T1-cross-provider",
        title="Cross-provider impact on shared payout route",
        signals=[
            r"paysafe",
            r"cross[- ]?provider",
            r"shared[- ]?file[s]?",
            r"breaking[- ]?change",
            r"other\s+provider[s]?",
            r"shared\s+route[s]?",
            r"impact\s+on\s+(existing|other)",
        ],
        bug_ref="Added phone as required on Interac payout route; broke paysafe (existing provider). MCP should flag shared route modification.",
    ),
    Rubric(
        id="T2-s2s-scope",
        title="Scope check: s2s flow not in scope",
        signals=[
            r"s2s\s+flow",
            r"s2s\s+scope",
            r"out\s+of\s+scope",
            r"not\s+in\s+scope",
            r"scope\s+(check|boundary)",
            r"initialize.*path.*s2s",
        ],
        bug_ref="Added initialize-path for s2s flow though s2s was not in PI-60 scope. MCP should surface trace_chain of call sites to bound scope.",
    ),
    Rubric(
        id="T3-fallback-chain",
        title="Fallback chain logic for reusable payout flag",
        signals=[
            r"reusable\s*payout",
            r"fallback\s+chain",
            r"map[- ]response",
            r"email\s+fallback",
            r"payout\s+token\s+(flag|logic)",
            r"truth\s+table",
        ],
        bug_ref="reusablePayouts flag logic wrong: phone used as mandatory instead of email-required, phone-optional. MCP should flag field fallback chain.",
    ),
    Rubric(
        id="T4-method-threading",
        title="Payment method threading from request",
        signals=[
            r"method\s+threading",
            r"pass(ing)?\s+payment\s+method\s+from\s+request",
            r"thread(ing)?\s+(payment\s+)?method",
            r"methods?/(payout|sale|refund)\.js",
            r"compose\s+final\s+method",
            r"convention\s+violation",
        ],
        bug_ref="Hardcoded payment method in methods/payout.js instead of threading from request. Violates generic convention used by other providers.",
    ),
    Rubric(
        id="T5-webhook-enum",
        title="Webhook transaction-type enumeration completeness",
        signals=[
            r"webhook\s+tx",
            r"webhook\s+(router|handler)",
            r"handle[- ]activities",
            r"enum\s+complete",
            r"all\s+tx(n)?\s+types",
            r"transaction[- ]type\s+(routing|switch)",
            r"payout.*webhook\s+route",
        ],
        bug_ref="Webhook handler enumerated sale/refund but forgot payout. Webhooks for payper payouts would silently drop.",
    ),
]


def call_analyze_task(prompt: str) -> str:
    payload = json.dumps(
        {
            "description": prompt,
            "exclude_task_id": EXCLUDE_TASK_ID,
        }
    ).encode()
    req = urllib.request.Request(
        DAEMON_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read()).get("result", "")


def score_rubric(r: Rubric, text: str) -> tuple[bool, list[str]]:
    """Return (hit, matched_signals) for a single rubric."""
    matched = []
    for pat in r.signals:
        if re.search(pat, text, re.I):
            matched.append(pat)
    return bool(matched), matched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--raw-output", help="Save full analyze_task output to this path")
    parser.add_argument("--prompt", help="Override clean prompt")
    args = parser.parse_args()

    prompt = args.prompt or CLEAN_PROMPT
    t0 = time.time()
    try:
        output = call_analyze_task(prompt)
    except Exception as e:
        print(f"ERROR: analyze_task call failed: {e}", file=sys.stderr)
        return 2
    dur = time.time() - t0

    if args.raw_output:
        Path(args.raw_output).write_text(output)

    hits = 0
    details = []
    for r in RUBRICS:
        hit, matched = score_rubric(r, output)
        if hit:
            hits += 1
        details.append({"id": r.id, "hit": hit, "matched": matched, "title": r.title})
        status = "✓" if hit else "✗"
        match_str = f" [{', '.join(matched[:3])}]" if matched else ""
        if args.verbose or not hit:
            print(f"  {status} {r.id}: {r.title}{match_str}")

    score = hits / len(RUBRICS)
    print()
    print(f"Proactivity score: {hits}/{len(RUBRICS)} = {score:.4f}")
    print(f"Output len: {len(output)} chars")
    print(f"Eval time: {dur:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
