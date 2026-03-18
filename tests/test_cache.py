"""Tests for cache.py — LRU cache, tracking decorator, runtime stats."""

import time

from src.cache import (
    _query_cache,
    _stats,
    cache_get,
    cache_key,
    cache_set,
    get_runtime_stats,
    tracked,
)


def _reset_cache():
    """Clear cache and stats between tests."""
    _query_cache.clear()
    _stats["cache_hits"] = 0
    _stats["cache_misses"] = 0
    _stats["tool_calls"].clear()
    _stats["tool_times"].clear()


class TestCacheKey:
    def test_deterministic(self):
        k1 = cache_key("search", query="test", limit=10)
        k2 = cache_key("search", query="test", limit=10)
        assert k1 == k2

    def test_different_args_different_keys(self):
        k1 = cache_key("search", query="foo")
        k2 = cache_key("search", query="bar")
        assert k1 != k2

    def test_different_funcs_different_keys(self):
        k1 = cache_key("search", query="test")
        k2 = cache_key("semantic_search", query="test")
        assert k1 != k2


class TestCacheGetSet:
    def setup_method(self):
        _reset_cache()

    def test_miss_returns_none(self):
        assert cache_get("nonexistent") is None

    def test_set_then_get(self):
        cache_set("key1", "result1")
        assert cache_get("key1") == "result1"

    def test_cache_hit_increments_stat(self):
        cache_set("key1", "result1")
        cache_get("key1")
        assert _stats["cache_hits"] == 1

    def test_cache_miss_increments_stat(self):
        cache_get("missing")
        assert _stats["cache_misses"] == 1

    def test_expired_entry_returns_none(self):
        cache_set("key1", "result1")
        # Manually expire
        _query_cache["key1"] = (time.time() - 400, "result1")
        assert cache_get("key1") is None

    def test_eviction_when_full(self):
        # Fill cache to max (64)
        for i in range(64):
            cache_set(f"key{i}", f"val{i}")
        # Adding one more should evict oldest
        cache_set("new_key", "new_val")
        assert len(_query_cache) == 64
        assert cache_get("new_key") == "new_val"

    def test_lru_touch_updates_timestamp(self):
        cache_set("key1", "result1")
        old_ts = _query_cache["key1"][0]
        time.sleep(0.01)
        cache_get("key1")
        new_ts = _query_cache["key1"][0]
        assert new_ts > old_ts


class TestTrackedDecorator:
    def setup_method(self):
        _reset_cache()

    def test_tracks_call_count(self):
        @tracked
        def my_tool():
            return "ok"

        my_tool()
        my_tool()
        assert _stats["tool_calls"]["my_tool"] == 2

    def test_tracks_duration(self):
        @tracked
        def slow_tool():
            time.sleep(0.01)
            return "done"

        slow_tool()
        times = _stats["tool_times"]["slow_tool"]
        assert len(times) == 1
        assert times[0] >= 0.01

    def test_preserves_return_value(self):
        @tracked
        def returns_data():
            return {"key": "value"}

        assert returns_data() == {"key": "value"}

    def test_preserves_function_name(self):
        @tracked
        def named_function():
            pass

        assert named_function.__name__ == "named_function"


class TestRuntimeStats:
    def setup_method(self):
        _reset_cache()

    def test_empty_stats(self):
        stats = get_runtime_stats()
        assert stats.total_calls == 0
        assert stats.tool_stats == []
        assert stats.cache_hit_rate is None

    def test_with_data(self):
        cache_set("k", "v")
        cache_get("k")  # hit
        cache_get("miss")  # miss

        @tracked
        def test_tool():
            return "ok"

        test_tool()

        stats = get_runtime_stats()
        assert stats.total_calls == 1
        assert stats.cache_hits == 1
        assert stats.cache_misses == 1
        assert stats.cache_hit_rate == 50.0
        assert len(stats.tool_stats) == 1
        assert stats.tool_stats[0].name == "test_tool"
