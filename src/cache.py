"""Query cache + runtime stats + tool tracking decorator.

LRU cache with TTL for search results.
Runtime counters for monitoring via health_check.
"""

from __future__ import annotations

import functools
import hashlib
import json
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from src.types import RuntimeStats, ToolCallStat

# --- Cache storage ---
_query_cache: dict[str, tuple[float, str]] = {}  # key → (timestamp, result)
_CACHE_TTL = 300  # 5 minutes
_CACHE_MAX = 64

# --- Runtime stats ---
_stats = {
    "start_time": time.time(),
    "cache_hits": 0,
    "cache_misses": 0,
    "tool_calls": {},  # tool_name → count
    "tool_times": {},  # tool_name → list of durations (seconds)
}


def cache_key(func_name: str, **kwargs: object) -> str:
    """Build a deterministic cache key from function name + args."""
    raw = f"{func_name}:" + json.dumps(kwargs, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


def cache_get(key: str) -> str | None:
    """Return cached result if exists and not expired. Updates timestamp (LRU)."""
    entry = _query_cache.get(key)
    if entry is None:
        _stats["cache_misses"] += 1
        return None
    ts, result = entry
    if time.time() - ts > _CACHE_TTL:
        del _query_cache[key]
        _stats["cache_misses"] += 1
        return None
    # Touch: update timestamp so recently-used entries survive eviction
    _query_cache[key] = (time.time(), result)
    _stats["cache_hits"] += 1
    return result


def cache_set(key: str, result: str) -> None:
    """Store result in cache, evicting oldest if full."""
    if len(_query_cache) >= _CACHE_MAX:
        oldest_key = min(_query_cache, key=lambda k: _query_cache[k][0])
        del _query_cache[oldest_key]
    _query_cache[key] = (time.time(), result)


def _track_tool(func_name: str, duration: float) -> None:
    """Record a tool call for runtime stats."""
    _stats["tool_calls"][func_name] = _stats["tool_calls"].get(func_name, 0) + 1
    times = _stats["tool_times"].setdefault(func_name, [])
    times.append(duration)
    if len(times) > 100:
        _stats["tool_times"][func_name] = times[-100:]


P = ParamSpec("P")
T = TypeVar("T")


def tracked(fn: Callable[P, T]) -> Callable[P, T]:
    """Decorator to track tool call count and duration."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        t0 = time.time()
        result = fn(*args, **kwargs)
        _track_tool(fn.__name__, time.time() - t0)
        return result

    return wrapper  # type: ignore[return-value]


def get_runtime_stats() -> RuntimeStats:
    """Build typed runtime stats snapshot for health_check."""
    uptime = time.time() - _stats["start_time"]

    tool_stats: list[ToolCallStat] = []
    for name, count in sorted(_stats["tool_calls"].items(), key=lambda x: -x[1]):
        times = _stats["tool_times"].get(name, [])
        avg_ms = (sum(times) / len(times) * 1000) if times else 0
        total_ms = sum(times) * 1000
        tool_stats.append(ToolCallStat(name=name, call_count=count, avg_ms=avg_ms, total_ms=total_ms))

    hits = _stats["cache_hits"]
    misses = _stats["cache_misses"]
    total_cache = hits + misses
    hit_rate = (hits / total_cache * 100) if total_cache else None

    return RuntimeStats(
        uptime_min=uptime / 60,
        tool_stats=tool_stats,
        total_calls=sum(_stats["tool_calls"].values()),
        cache_hits=hits,
        cache_misses=misses,
        cache_hit_rate=hit_rate,
    )
