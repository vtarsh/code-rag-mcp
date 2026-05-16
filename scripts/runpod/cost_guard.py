#!/usr/bin/env python3
"""Cost guard for RunPod operations.

Reads RUNPOD_API_KEY from env (typically `source ~/.runpod/credentials` first).
Fetches today's spending via REST GET /v1/billing/pods.
Raises CostGuardError if (today_spend + estimated_run) would breach the cap.

Caps (override via env):
- RUNPOD_MAX_DAILY_SPEND_USD (default: 5)  — first-week conservative
- RUNPOD_MAX_SINGLE_RUN_USD  (default: 5)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

API_BASE: Final = os.getenv("RUNPOD_API_BASE", "https://rest.runpod.io/v1")
DEFAULT_DAILY_CAP_USD: Final = float(os.getenv("RUNPOD_MAX_DAILY_SPEND_USD", "5"))
DEFAULT_SINGLE_RUN_CAP_USD: Final = float(os.getenv("RUNPOD_MAX_SINGLE_RUN_USD", "5"))
HTTP_TIMEOUT_SEC: Final = 15.0


class CostGuardError(RuntimeError):
    """Raised when a planned run would breach a configured cost cap or auth fails."""


def _read_api_key() -> str:
    key = os.getenv("RUNPOD_API_KEY")
    if not key:
        raise CostGuardError("RUNPOD_API_KEY missing. Run `source ~/.runpod/credentials` first.")
    if not key.startswith("rpa_"):
        raise CostGuardError("RUNPOD_API_KEY format unexpected (no rpa_ prefix).")
    return key


def _get(path: str):
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {_read_api_key()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_today_spend_usd() -> float:
    """Sum today's pod spend in USD via REST /v1/billing/pods.

    Returns 0.0 if no pods (common on cold accounts) or no rows for today.
    The endpoint returns a top-level JSON array; older shapes wrap it in
    {pods: [...]} or {data: [...]}.
    """
    try:
        data = _get("/billing/pods")
    except urllib.error.HTTPError as e:
        # 404 is expected when there are no billable pods yet — treat as $0.
        if e.code == 404:
            return 0.0
        raise CostGuardError(f"billing/pods HTTP {e.code}: {e.reason}") from e
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("pods") or data.get("data") or data.get("items") or []
    else:
        rows = []
    today = datetime.now(UTC).date().isoformat()
    total = 0.0
    for row in rows:
        ts = str(row.get("timestamp") or row.get("date") or row.get("createdAt") or "")
        if ts.startswith(today):
            total += float(row.get("cost", 0.0) or 0.0)
    return total


def assert_can_spend(
    estimated_run_usd: float,
    daily_cap_usd: float = DEFAULT_DAILY_CAP_USD,
    single_run_cap_usd: float = DEFAULT_SINGLE_RUN_CAP_USD,
    today_spend_fn: Callable[[], float] = fetch_today_spend_usd,
) -> None:
    """Raise CostGuardError if estimated alone exceeds the single-run cap, OR if
    (today_spend + estimated) would exceed the daily cap. Otherwise return None.
    """
    if estimated_run_usd > single_run_cap_usd:
        raise CostGuardError(
            f"Estimated run ${estimated_run_usd:.2f} exceeds single-run cap ${single_run_cap_usd:.2f}."
        )
    today = today_spend_fn()
    if today + estimated_run_usd > daily_cap_usd:
        raise CostGuardError(
            f"Today's spend ${today:.2f} + estimated ${estimated_run_usd:.2f} "
            f"= ${today + estimated_run_usd:.2f} would exceed daily cap "
            f"${daily_cap_usd:.2f}. Aborting."
        )


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="RunPod cost guard")
    p.add_argument(
        "--check",
        type=float,
        default=0.0,
        help="Test: would spending $X today be allowed? (USD)",
    )
    args = p.parse_args()
    try:
        assert_can_spend(args.check)
        print(
            f"OK: ${args.check:.2f} run within caps "
            f"(daily=${DEFAULT_DAILY_CAP_USD}, single=${DEFAULT_SINGLE_RUN_CAP_USD})"
        )
    except CostGuardError as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        sys.exit(2)
