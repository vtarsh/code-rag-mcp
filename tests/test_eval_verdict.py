"""Tests for scripts/eval_verdict.py — the single source of truth for verdict decisions.

Covers: metric helpers, counting logic, gate decision boundaries, and a full
integration check against the real historical snapshots (v4, v6.2, v7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Scripts/ isn't a package; import via explicit path insertion.
import sys
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.eval_verdict import (  # noqa: E402
    DELTA_HIT5_THRESHOLD,
    DELTA_R10_THRESHOLD,
    MIN_NET_IMPROVED,
    compute_hit5_mean,
    compute_mrr_at_10,
    compute_r10_mean,
    count_improvements_regressions,
    decide_verdict,
    verdict_from_snapshot,
)


# ---- Metric helper sanity ----

class TestComputeR10Mean:
    def test_empty(self):
        assert compute_r10_mean({}) == 0.0

    def test_all_present(self):
        per_task = {
            "T-1": {"recall_at_10": 1.0},
            "T-2": {"recall_at_10": 0.0},
            "T-3": {"recall_at_10": 0.5},
        }
        assert compute_r10_mean(per_task) == pytest.approx(0.5)

    def test_subset_via_tickets_arg(self):
        per_task = {
            "A": {"recall_at_10": 1.0},
            "B": {"recall_at_10": 0.0},
            "C": {"recall_at_10": 1.0},
        }
        assert compute_r10_mean(per_task, ["A", "C"]) == pytest.approx(1.0)


class TestComputeHit5Mean:
    def test_none_counts_as_miss(self):
        per_task = {
            "T-1": {"rank_of_first_gt": 1},
            "T-2": {"rank_of_first_gt": None},
            "T-3": {"rank_of_first_gt": 6},  # just above threshold
        }
        assert compute_hit5_mean(per_task) == pytest.approx(1 / 3)

    def test_boundary_rank_5_counts(self):
        per_task = {"T-1": {"rank_of_first_gt": 5}}
        assert compute_hit5_mean(per_task) == 1.0

    def test_empty(self):
        assert compute_hit5_mean({}) == 0.0


class TestBuildDeltaNoneHandling:
    """Regression guard for the bug fixed 2026-04-20 in merge_eval_shards:
    `(rank or 999) - (rank or 999)` collapsed (None, None) to delta=0,
    hiding tickets where FT never found GT.
    """

    def _run_both(self, base, ft):
        """Both scripts' build_delta must now produce identical output."""
        from scripts.eval_finetune import build_delta as ef_bd
        from scripts.merge_eval_shards import build_delta as me_bd
        return ef_bd(base, ft), me_bd(base, ft)

    def test_both_none_emits_none_not_zero(self):
        base = {"T": {"recall_at_10": 0.0, "recall_at_25": 0.0, "rank_of_first_gt": None}}
        ft = {"T": {"recall_at_10": 0.0, "recall_at_25": 0.0, "rank_of_first_gt": None}}
        d1, d2 = self._run_both(base, ft)
        assert d1["T"]["rank_of_first_gt_delta"] is None
        assert d2["T"]["rank_of_first_gt_delta"] is None

    def test_baseline_found_ft_missed_is_none_not_huge_positive(self):
        """Old merge code would report delta=+996 (999-3), new code emits None."""
        base = {"T": {"recall_at_10": 1.0, "recall_at_25": 1.0, "rank_of_first_gt": 3}}
        ft = {"T": {"recall_at_10": 0.0, "recall_at_25": 0.0, "rank_of_first_gt": None}}
        d1, d2 = self._run_both(base, ft)
        assert d1["T"]["rank_of_first_gt_delta"] is None
        assert d2["T"]["rank_of_first_gt_delta"] is None

    def test_both_found_emits_signed_int(self):
        base = {"T": {"recall_at_10": 1.0, "recall_at_25": 1.0, "rank_of_first_gt": 5}}
        ft = {"T": {"recall_at_10": 1.0, "recall_at_25": 1.0, "rank_of_first_gt": 2}}
        d1, d2 = self._run_both(base, ft)
        # Negative = ft moved GT up.
        assert d1["T"]["rank_of_first_gt_delta"] == -3
        assert d2["T"]["rank_of_first_gt_delta"] == -3

    def test_field_names_aligned_across_scripts(self):
        """After 2026-04-20 fix, key name is `rank_of_first_gt_delta` in both."""
        base = {"T": {"recall_at_10": 1.0, "recall_at_25": 1.0, "rank_of_first_gt": 1}}
        ft = {"T": {"recall_at_10": 1.0, "recall_at_25": 1.0, "rank_of_first_gt": 1}}
        d1, d2 = self._run_both(base, ft)
        assert set(d1["T"].keys()) == set(d2["T"].keys())
        assert "rank_of_first_gt_delta" in d1["T"]


class TestComputeMrr:
    def test_reciprocal_averaging(self):
        per_task = {
            "T-1": {"rank_of_first_gt": 1},   # 1/1 = 1
            "T-2": {"rank_of_first_gt": 2},   # 1/2
            "T-3": {"rank_of_first_gt": 11},  # beyond top-10 -> 0
            "T-4": {"rank_of_first_gt": None},
        }
        assert compute_mrr_at_10(per_task) == pytest.approx((1.0 + 0.5) / 4)


# ---- Counting improvements/regressions ----

class TestCountingGate:
    def test_default_threshold(self):
        deltas = {
            "A": {"recall_at_10": +0.06},   # improved (≥ 0.05)
            "B": {"recall_at_10": +0.05},   # improved (equal)
            "C": {"recall_at_10": +0.04},   # neutral
            "D": {"recall_at_10": -0.04},   # neutral
            "E": {"recall_at_10": -0.05},   # regressed (equal)
            "F": {"recall_at_10": -0.10},   # regressed
        }
        n_imp, n_reg = count_improvements_regressions(deltas)
        assert (n_imp, n_reg) == (2, 2)

    def test_custom_threshold(self):
        deltas = {
            "A": {"recall_at_10": +0.02},
            "B": {"recall_at_10": -0.02},
        }
        n_imp, n_reg = count_improvements_regressions(deltas, threshold_pp=0.01)
        assert (n_imp, n_reg) == (1, 1)

    def test_missing_key_treated_as_zero(self):
        deltas = {"A": {}, "B": {"recall_at_10": 0.1}}
        n_imp, n_reg = count_improvements_regressions(deltas)
        assert (n_imp, n_reg) == (1, 0)


# ---- Gate decision logic ----

class TestDecideVerdict:
    def _args(self, **over):
        base: dict = {
            "delta_r10_all": 0.04,
            "delta_hit5_all": 0.05,
            "n_improved": 100,
            "n_regressed": 40,
        }
        base.update(over)
        return base

    def test_promote_v62_like(self):
        """v6.2 historical: Δr@10=+0.043, ΔHit@5=+0.057, net=+89."""
        res = decide_verdict(**self._args(
            delta_r10_all=0.043, delta_hit5_all=0.057,
            n_improved=129, n_regressed=40,
        ))
        assert res.verdict == "PROMOTE"
        assert "Δr@10=+0.043" in res.reason

    def test_promote_boundary(self):
        res = decide_verdict(**self._args(
            delta_r10_all=DELTA_R10_THRESHOLD,
            delta_hit5_all=DELTA_HIT5_THRESHOLD,
            n_improved=20 + MIN_NET_IMPROVED,
            n_regressed=20,
        ))
        assert res.verdict == "PROMOTE"

    def test_reject_r10_negative(self):
        res = decide_verdict(**self._args(delta_r10_all=-0.01))
        assert res.verdict == "REJECT"
        assert "primary regressed" in res.reason

    def test_reject_hit5_negative_even_if_r10_positive(self):
        """Covers the dangerous case: r@10 up via rank-9→10 reshuffles but
        Hit@5 dropped (users see worse results)."""
        res = decide_verdict(**self._args(
            delta_r10_all=0.05, delta_hit5_all=-0.01,
        ))
        assert res.verdict == "REJECT"
        assert "top-5 quality regressed" in res.reason

    def test_reject_net_negative(self):
        res = decide_verdict(**self._args(
            delta_r10_all=0.02, delta_hit5_all=0.02,
            n_improved=30, n_regressed=50,
        ))
        assert res.verdict == "REJECT"
        assert "more losers than winners" in res.reason

    def test_hold_subthreshold_r10(self):
        """Positive but tiny gain — not a clear win."""
        res = decide_verdict(**self._args(
            delta_r10_all=0.005, delta_hit5_all=0.05,
        ))
        assert res.verdict == "HOLD"
        assert "Δr@10" in res.reason

    def test_hold_net_below_min(self):
        """Big deltas but only a few ticket wins — too narrow."""
        res = decide_verdict(**self._args(
            delta_r10_all=0.1, delta_hit5_all=0.1,
            n_improved=22, n_regressed=10,   # net=+12 < MIN_NET_IMPROVED
        ))
        assert res.verdict == "HOLD"
        assert "net" in res.reason

    def test_zero_change_hold(self):
        res = decide_verdict(
            delta_r10_all=0.0, delta_hit5_all=0.0,
            n_improved=0, n_regressed=0,
        )
        assert res.verdict == "HOLD"

    def test_diagnostic_metrics_propagate(self):
        res = decide_verdict(
            **self._args(),
            diagnostic_metrics={"mrr_baseline_diag": 0.6, "mrr_ft_diag": 0.7},
        )
        assert res.metrics["mrr_baseline_diag"] == 0.6
        assert res.metrics["mrr_ft_diag"] == 0.7
        assert res.metrics["net_improved_r10"] == 60


# ---- Integration against the real historical snapshots ----

_HISTORY = _REPO_ROOT / "profiles" / "pay-com" / "finetune_history"


@pytest.fixture(scope="module")
def snapshot_v4():
    return json.loads((_HISTORY / "gte_v4.json").read_text())


@pytest.fixture(scope="module")
def snapshot_v6_2():
    return json.loads((_HISTORY / "gte_v6_2.json").read_text())


@pytest.fixture(scope="module")
def snapshot_v7():
    return json.loads((_HISTORY / "gte_v7.json").read_text())


class TestHistoricalSnapshots:
    """Integration sanity: our historical snapshots should re-score consistently.

    Reference numbers (independently computed by audit 2026-04-20 with
    per_task data from the same files):

        v4:   Δr@10=+0.0406, Δhit5=+0.0495, improved=115, regressed=41
        v6.2: Δr@10=+0.0430, Δhit5=+0.0572, improved=129, regressed=40
        v7:   Δr@10=+0.0339, Δhit5=+0.0561, improved=125, regressed=52

    All three pass current gate (net ≥ 20, deltas ≥ 2pp on both primaries).
    If these numbers drift, either eval changed OR this module drifted.
    """

    @pytest.mark.skipif(not (_HISTORY / "gte_v4.json").is_file(), reason="v4 snapshot absent")
    def test_v4_promotes_and_matches_audit_numbers(self, snapshot_v4):
        res = verdict_from_snapshot(
            snapshot_v4["per_task_baseline"],
            snapshot_v4["per_task_ft_v1"],
            snapshot_v4["per_task_delta"],
        )
        assert res.verdict == "PROMOTE"
        assert res.metrics["delta_r10_all"] == pytest.approx(0.041, abs=0.002)
        assert res.metrics["delta_hit5_all"] == pytest.approx(0.050, abs=0.002)
        assert res.metrics["n_improved_r10"] == 115
        assert res.metrics["n_regressed_r10"] == 41

    @pytest.mark.skipif(not (_HISTORY / "gte_v6_2.json").is_file(), reason="v6.2 snapshot absent")
    def test_v62_promotes_and_matches_audit_numbers(self, snapshot_v6_2):
        res = verdict_from_snapshot(
            snapshot_v6_2["per_task_baseline"],
            snapshot_v6_2["per_task_ft_v1"],
            snapshot_v6_2["per_task_delta"],
        )
        assert res.verdict == "PROMOTE"
        assert res.metrics["delta_r10_all"] == pytest.approx(0.043, abs=0.002)
        assert res.metrics["delta_hit5_all"] == pytest.approx(0.057, abs=0.002)
        assert res.metrics["n_improved_r10"] == 129
        assert res.metrics["n_regressed_r10"] == 40

    @pytest.mark.skipif(not (_HISTORY / "gte_v7.json").is_file(), reason="v7 snapshot absent")
    def test_v7_promotes_on_jira_eval_but_is_known_bad_on_runtime(self, snapshot_v7):
        """v7 passes Jira gate — which is WHY runtime benchmarks are a separate
        required guard (this gate doesn't know about latency or runtime
        queries). Gate is necessary but not sufficient for prod decisions.
        """
        res = verdict_from_snapshot(
            snapshot_v7["per_task_baseline"],
            snapshot_v7["per_task_ft_v1"],
            snapshot_v7["per_task_delta"],
        )
        assert res.verdict == "PROMOTE"
        assert res.metrics["n_regressed_r10"] == 52

    @pytest.mark.skipif(not (_HISTORY / "gte_v6_2.json").is_file(), reason="v6.2 snapshot absent")
    def test_v62_mrr_diagnostic_is_present(self, snapshot_v6_2):
        """MRR is diagnostic only — it MUST appear in metrics but MUST NOT
        affect the verdict. (v6.2 MRR delta ≈ +0.084, just for reference.)
        """
        res = verdict_from_snapshot(
            snapshot_v6_2["per_task_baseline"],
            snapshot_v6_2["per_task_ft_v1"],
            snapshot_v6_2["per_task_delta"],
        )
        assert "mrr_ft_diag" in res.metrics
        assert "mrr_baseline_diag" in res.metrics
        assert "delta_mrr_diag" in res.metrics
