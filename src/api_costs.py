"""API cost tracking — log every external API call with estimated cost.

Writes to logs/api_costs.jsonl. Lightweight, never raises.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

_LOG_DIR = Path(__file__).parent.parent / "logs"
_COSTS_LOG = _LOG_DIR / "api_costs.jsonl"

# Pricing per 1M tokens (USD) — update when pricing changes
PRICING = {
    "gemini-embedding-001": {"input": 0.15},
    "gemini-embedding-001-batch": {"input": 0.075},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
}


@dataclass
class ApiCostRecord:
    provider: str  # "gemini-embedding", "gemini-rerank"
    model: str  # "gemini-embedding-001", "gemini-2.5-flash"
    operation: str  # "embed_query", "embed_batch", "rerank"
    input_tokens: int
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_ms: float = 0.0


def estimate_cost(model: str, input_tokens: int, output_tokens: int = 0) -> float:
    """Estimate cost in USD for a given model and token counts."""
    pricing = PRICING.get(model, {})
    input_cost = input_tokens * pricing.get("input", 0) / 1_000_000
    output_cost = output_tokens * pricing.get("output", 0) / 1_000_000
    return input_cost + output_cost


def log_api_cost(record: ApiCostRecord) -> None:
    """Append cost record to JSONL log. Never raises."""
    try:
        _LOG_DIR.mkdir(exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **asdict(record),
        }
        with open(_COSTS_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_daily_cost() -> float:
    """Sum today's estimated costs."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _sum_costs(lambda ts: ts.startswith(today))


def get_total_cost() -> float:
    """Sum all recorded costs."""
    return _sum_costs(lambda _: True)


def _sum_costs(predicate) -> float:
    if not _COSTS_LOG.exists():
        return 0.0
    total = 0.0
    for line in _COSTS_LOG.read_text().splitlines():
        try:
            rec = json.loads(line)
            if predicate(rec.get("ts", "")):
                total += rec.get("estimated_cost_usd", 0)
        except (json.JSONDecodeError, KeyError):
            continue
    return total
