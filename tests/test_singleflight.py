"""Tests for singleflight cache_or_compute — prevents concurrent duplicate work."""

import threading
import time

import pytest

from src.cache import _inflight_events, _query_cache, cache_or_compute


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset shared state between tests."""
    _query_cache.clear()
    _inflight_events.clear()
    yield
    _query_cache.clear()
    _inflight_events.clear()


def test_single_thread_cache_miss_then_hit():
    calls = [0]

    def compute():
        calls[0] += 1
        return "result-v1"

    r1 = cache_or_compute("k1", compute)
    r2 = cache_or_compute("k1", compute)
    assert r1 == "result-v1"
    assert r2 == "result-v1"
    assert calls[0] == 1  # Second call hit cache.


def test_concurrent_identical_queries_dedup():
    """N threads call cache_or_compute with the same key simultaneously.
    Only one of them should run the compute_fn; the others wait and reuse.
    """
    N = 5
    calls = [0]
    barrier = threading.Barrier(N)

    def compute():
        calls[0] += 1
        # Simulate a slow computation so followers have time to queue up.
        time.sleep(0.2)
        return f"result-{calls[0]}"

    results: list[str] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()  # Release all threads at once.
        r = cache_or_compute("concurrent-key", compute)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls[0] == 1, f"compute_fn ran {calls[0]} times, expected 1"
    assert len(results) == N
    # All followers got the same result as the leader.
    assert all(r == "result-1" for r in results)


def test_different_keys_run_independently():
    """Distinct keys should NOT block each other."""
    calls = {"a": 0, "b": 0}

    def make_compute(key):
        def compute():
            calls[key] += 1
            time.sleep(0.1)
            return f"r-{key}"

        return compute

    def worker(key):
        return cache_or_compute(key, make_compute(key))

    t0 = time.time()
    threads = [
        threading.Thread(target=lambda: worker("a")),
        threading.Thread(target=lambda: worker("b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    assert calls["a"] == 1
    assert calls["b"] == 1
    # Both ran in parallel — should be ~0.1s, not ~0.2s.
    assert elapsed < 0.18


def test_leader_exception_allows_follower_to_retry():
    """If leader's compute raises, followers should fall through and compute."""
    call_log: list[str] = []
    leader_started = threading.Event()
    follower_can_start = threading.Event()

    def leader_compute():
        call_log.append("leader")
        leader_started.set()
        follower_can_start.wait(timeout=2.0)
        raise RuntimeError("leader failure")

    def follower_compute():
        call_log.append("follower")
        return "follower-result"

    leader_result = [None]
    leader_error = [None]

    def run_leader():
        try:
            leader_result[0] = cache_or_compute("err-key", leader_compute)
        except RuntimeError as e:
            leader_error[0] = e

    def run_follower():
        leader_started.wait(timeout=2.0)
        # Brief sleep to ensure leader owns the inflight slot.
        time.sleep(0.05)
        follower_can_start.set()
        # Follower waits for leader's event, then falls through to compute_fn.
        return cache_or_compute("err-key", follower_compute)

    leader_thread = threading.Thread(target=run_leader)
    follower_result = [None]
    follower_thread = threading.Thread(target=lambda: follower_result.__setitem__(0, run_follower()))

    leader_thread.start()
    follower_thread.start()
    leader_thread.join()
    follower_thread.join()

    assert leader_error[0] is not None
    assert leader_result[0] is None
    assert follower_result[0] == "follower-result"
    assert "leader" in call_log
    assert "follower" in call_log


def test_cached_result_skips_singleflight():
    """Pre-populated cache → immediate hit, no compute, no inflight entry."""
    _query_cache["pre-key"] = (time.time(), "cached-value")
    calls = [0]

    def compute():
        calls[0] += 1
        return "fresh"

    result = cache_or_compute("pre-key", compute)
    assert result == "cached-value"
    assert calls[0] == 0
    assert "pre-key" not in _inflight_events
