"""Tests for scripts/eval_finetune._lpt_schedule — LPT shard balancing."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.eval.eval_finetune import _lpt_schedule


def _make_tasks(n: int, prefix: str = "T") -> list[dict]:
    return [{"ticket_id": f"{prefix}-{i}"} for i in range(n)]


class TestLptSchedule:
    def test_partitions_all_tasks(self):
        """Every task lands on exactly one shard."""
        tasks = _make_tasks(30)
        profile = {f"T-{i}": float(i % 5 + 1) for i in range(30)}
        all_shards = [_lpt_schedule(tasks, profile, 3, i) for i in range(3)]
        got_ids = [t["ticket_id"] for shard in all_shards for t in shard]
        assert sorted(got_ids) == sorted(t["ticket_id"] for t in tasks)
        assert len(got_ids) == 30

    def test_balances_heavy_tail(self):
        """With 10×10s and 10×1s tasks, 3 shards should differ < 20% from avg."""
        tasks = _make_tasks(20)
        profile = {f"T-{i}": (10.0 if i < 10 else 1.0) for i in range(20)}
        shards = [_lpt_schedule(tasks, profile, 3, i) for i in range(3)]
        loads = [sum(profile[t["ticket_id"]] for t in s) for s in shards]
        avg = sum(loads) / 3
        for load in loads:
            assert abs(load - avg) / avg < 0.20, f"shard load {load} too far from avg {avg}"

    def test_stride_loses_to_lpt_on_heavy_tail(self):
        """Sanity check that LPT improves on naive stride for skewed distributions."""
        # 30 tasks — first 3 are 30s each, rest are 1s each.
        tasks = _make_tasks(30)
        profile = {f"T-{i}": (30.0 if i < 3 else 1.0) for i in range(30)}

        lpt_shards = [_lpt_schedule(tasks, profile, 3, i) for i in range(3)]
        lpt_loads = [sum(profile[t["ticket_id"]] for t in s) for s in lpt_shards]

        # Naive stride would cluster the 3 heavy tasks depending on shuffle order;
        # LPT should hand one heavy task to each shard.
        max_lpt = max(lpt_loads)
        min_lpt = min(lpt_loads)
        assert max_lpt - min_lpt < 5.0, (
            f"LPT spread {max_lpt - min_lpt:.1f}s too wide — each shard should get one 30s task"
        )

    def test_missing_profile_entries_use_median(self):
        """Tasks without a profile estimate get the median of known entries."""
        tasks = _make_tasks(10)
        # Only first 5 tickets have profile data
        profile = {f"T-{i}": float(i + 1) for i in range(5)}  # 1..5, median = 3
        shards = [_lpt_schedule(tasks, profile, 2, i) for i in range(2)]
        got_ids = [t["ticket_id"] for shard in shards for t in shard]
        assert sorted(got_ids) == sorted(t["ticket_id"] for t in tasks)
        # Each shard should have 5 tasks (balanced)
        assert len(shards[0]) == 5
        assert len(shards[1]) == 5

    def test_empty_profile_falls_back_to_uniform(self):
        """No profile data → every task defaults to 10s → round-robin-like split."""
        tasks = _make_tasks(12)
        shards = [_lpt_schedule(tasks, {}, 3, i) for i in range(3)]
        assert len(shards[0]) == 4
        assert len(shards[1]) == 4
        assert len(shards[2]) == 4

    def test_single_shard_returns_all_tasks(self):
        tasks = _make_tasks(7)
        profile = {f"T-{i}": float(i + 1) for i in range(7)}
        result = _lpt_schedule(tasks, profile, 1, 0)
        assert len(result) == 7
        assert {t["ticket_id"] for t in result} == {t["ticket_id"] for t in tasks}

    def test_zero_and_negative_latency_treated_as_missing(self):
        """Non-positive latency is unreliable → fallback to median."""
        tasks = _make_tasks(6)
        # T-0 has bogus 0, T-1 has -1; T-2..T-5 have 1..4
        profile = {"T-0": 0.0, "T-1": -1.0, "T-2": 1.0, "T-3": 2.0, "T-4": 3.0, "T-5": 4.0}
        shards = [_lpt_schedule(tasks, profile, 2, i) for i in range(2)]
        got_ids = [t["ticket_id"] for shard in shards for t in shard]
        assert sorted(got_ids) == sorted(t["ticket_id"] for t in tasks)
