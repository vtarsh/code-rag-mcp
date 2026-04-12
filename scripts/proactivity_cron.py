#!/usr/bin/env python3
"""Standalone cron job for proactivity monitoring + adversarial
paraphrase discovery.

Run once per invocation (no internal loop — launchd/cron handles scheduling).

Each run:
  1. Generate a NEW PI-60-like task paraphrase via Gemini (NO literal
     references to paysafe/s2s/reusable-payout/etc.).
  2. Call daemon's analyze_task on that paraphrase.
  3. Score the output against the 5 vboychyk reviewer bugs via the
     existing regex rubric from scripts/proactivity_eval.py.
  4. Append a structured record to logs/proactivity_cron.jsonl.
  5. If score < 5, append the paraphrase to
     logs/proactivity_failing_prompts.jsonl as a discovered adversarial
     test case for later tuning.

Designed to run every 2 hours via launchd. Over 24h that's 12 runs, so
12 new paraphrases + 12 score measurements. Failing paraphrases
accumulate into a test suite that drives the next round of fixes.

Writes:
  - logs/proactivity_cron.jsonl — every run
  - logs/proactivity_failing_prompts.jsonl — only when score < 5
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(os.environ.get("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
LOG_DIR = BASE_DIR / "logs"
CRON_LOG = LOG_DIR / "proactivity_cron.jsonl"
FAILURES_LOG = LOG_DIR / "proactivity_failing_prompts.jsonl"
DAEMON_URL = "http://localhost:8742/tool/analyze_task"

# Add project root to sys.path so we can import proactivity_eval and shared_sections.
sys.path.insert(0, str(BASE_DIR))

from scripts.proactivity_eval import RUBRICS, score_rubric  # noqa: E402


# Seed prompt used to ask Gemini for a fresh paraphrase. Keep it
# vocabulary-neutral so the paraphrase is genuinely new each run.
PARAPHRASE_PROMPT = """\
Write ONE task description (3-5 sentences) for a software integration
that a payment gateway team might receive. The task MUST have all of
these underlying characteristics, but DO NOT use any of the banned
words literally:

Characteristics:
  - A new alternative/non-card payment provider is being added.
  - The consumer provides some contact identifier (phone/email/id) at
    a verification step.
  - The provider supports (a) direct charge, (b) sending value back to
    the consumer, and (c) asynchronous server-to-server notifications
    about status.
  - Returning consumers can re-use a previously issued credential/token.

Banned words (do not use any form of these in the output):
  paysafe, alternative payment method, APM, webhook, reusable payout,
  s2s, initialize, scope, cross-provider.

Output ONLY the task description, no preamble or labels.
"""


def call_gemini_for_paraphrase() -> str | None:
    try:
        from src.config import GEMINI_API_KEYS
    except Exception:
        return None
    if not GEMINI_API_KEYS:
        return None
    try:
        from google import genai
    except ImportError:
        return None
    for key in GEMINI_API_KEYS:
        try:
            client = genai.Client(api_key=key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[{"role": "user", "parts": [{"text": PARAPHRASE_PROMPT}]}],
                config={"temperature": 0.9},  # want variety per run
            )
            text = (resp.text or "").strip()
            # Strip surrounding quotes/markdown if any.
            text = text.strip('"').strip("'").strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(lines[1:-1] if len(lines) > 2 else lines)
            return text or None
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "quota" in err or "resource_exhausted" in err:
                continue
            print(f"[cron] paraphrase gemini failed: {e}", file=sys.stderr)
            return None
    return None


def call_analyze_task(description: str) -> str | None:
    try:
        req = urllib.request.Request(
            DAEMON_URL,
            data=json.dumps({"description": description}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read()).get("result", "")
    except Exception as e:
        print(f"[cron] daemon call failed: {e}", file=sys.stderr)
        return None


def score(output: str) -> tuple[int, list[dict]]:
    details = []
    hits = 0
    for r in RUBRICS:
        hit, matched = score_rubric(r, output)
        if hit:
            hits += 1
        details.append({"id": r.id, "hit": hit, "matched": matched[:3]})
    return hits, details


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    paraphrase = call_gemini_for_paraphrase()
    if not paraphrase:
        append_jsonl(CRON_LOG, {
            "ts": timestamp,
            "event": "paraphrase_failed",
            "reason": "gemini unavailable or quota exhausted",
        })
        return 1

    output = call_analyze_task(paraphrase)
    if output is None:
        append_jsonl(CRON_LOG, {
            "ts": timestamp,
            "event": "daemon_failed",
            "paraphrase": paraphrase[:200],
        })
        return 2

    hits, details = score(output)
    record = {
        "ts": timestamp,
        "event": "measured",
        "paraphrase": paraphrase,
        "score": hits,
        "max": len(RUBRICS),
        "rubric_details": details,
        "output_len": len(output),
    }
    append_jsonl(CRON_LOG, record)

    if hits < len(RUBRICS):
        append_jsonl(FAILURES_LOG, {
            "ts": timestamp,
            "score": hits,
            "paraphrase": paraphrase,
            "missed_rubrics": [d["id"] for d in details if not d["hit"]],
        })

    print(f"[cron] {timestamp} score={hits}/{len(RUBRICS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
