"""Unit tests for scripts/compare_query_modes.py analysis helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.compare_query_modes import per_project_delta


def _snap(
    baseline_by_ticket: dict[str, tuple[float, int | None]],
    ft_by_ticket: dict[str, tuple[float, int | None]],
) -> dict:
    """Build a minimal eval snapshot from (r@10, rank_of_first_gt) per ticket."""

    def _pt(rows):
        return {
            tid: {
                "recall_at_10": r10,
                "recall_at_25": r10,
                "rank_of_first_gt": rank,
            }
            for tid, (r10, rank) in rows.items()
        }

    return {
        "per_task_baseline": _pt(baseline_by_ticket),
        "per_task_ft_v1": _pt(ft_by_ticket),
    }


def test_per_project_delta_splits_by_prefix() -> None:
    summary = _snap(
        {"PI-1": (0.5, 2), "PI-2": (0.2, None), "BO-1": (0.9, 1)},
        {"PI-1": (0.7, 1), "PI-2": (0.4, 3), "BO-1": (0.9, 1)},
    )
    enriched = _snap(
        {"PI-1": (0.6, 2), "PI-2": (0.0, None), "BO-1": (0.95, 1)},
        {"PI-1": (0.9, 1), "PI-2": (0.5, 2), "BO-1": (0.95, 1)},
    )
    rows = per_project_delta(summary, enriched, "per_task_baseline")
    by_proj = {r["project"]: r for r in rows}
    assert set(by_proj) == {"PI", "BO", "ALL"}
    # PI baseline: summary mean=(0.5+0.2)/2=0.35; enriched mean=(0.6+0.0)/2=0.30
    assert abs(by_proj["PI"]["r10_summary"] - 0.35) < 1e-9
    assert abs(by_proj["PI"]["r10_enriched"] - 0.30) < 1e-9
    assert abs(by_proj["PI"]["delta_r10"] - (-0.05)) < 1e-9
    # ALL delta averages over all 3 tickets
    expected_all_delta = ((0.6 + 0.0 + 0.95) - (0.5 + 0.2 + 0.9)) / 3
    assert abs(by_proj["ALL"]["delta_r10"] - expected_all_delta) < 1e-9


def test_delta_counts_improved_regressed_tied() -> None:
    summary = _snap({"PI-1": (0.5, 1), "PI-2": (0.5, 1), "PI-3": (0.5, 1)}, {})
    enriched = _snap({"PI-1": (0.8, 1), "PI-2": (0.3, 2), "PI-3": (0.5, 1)}, {})
    rows = per_project_delta(summary, enriched, "per_task_baseline")
    pi = next(r for r in rows if r["project"] == "PI")
    assert pi["improved_r10"] == 1
    assert pi["regressed_r10"] == 1
    assert pi["net_r10"] == 0


def test_common_tickets_only() -> None:
    """If one snapshot has extra tickets, only intersection is scored."""
    summary = _snap(
        {"PI-1": (0.5, 1), "PI-2": (0.5, 1), "BO-1": (0.5, 1)},
        {"PI-1": (0.5, 1), "PI-2": (0.5, 1), "BO-1": (0.5, 1)},
    )
    enriched = _snap(
        {"PI-1": (0.9, 1)},  # PI-2 and BO-1 missing
        {"PI-1": (0.9, 1)},
    )
    rows = per_project_delta(summary, enriched, "per_task_baseline")
    by_proj = {r["project"]: r for r in rows}
    assert by_proj["ALL"]["n"] == 1
    assert "PI" in by_proj
    assert "BO" not in by_proj  # no common BO ticket
