"""Unit tests for file-level GT helpers added per
`docs/eval_file_level_gt_proposal.md` §2 and §6.

Covers the two new primitives in `scripts/eval_finetune.py`:
  - `compute_file_recall(ranked_files, expected_files, k)`
  - `top_k_files(ranked, k)`  — (repo, file_path) dedup

Tests deliberately avoid touching the full eval pipeline; they exercise the
helpers in isolation so regressions here are cheap to localize.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not a package — mirror the import pattern used in test_eval_verdict.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.eval.eval_finetune import (  # noqa: E402
    DELTA_FILE_R10_THRESHOLD,
    GATE_VERSION_V1,
    GATE_VERSION_V2,
    compute_file_recall,
    top_k_files,
)

# ---- compute_file_recall ----


def test_compute_file_recall_basic_positive():
    """Every expected file is present in top-k → recall = 1.0."""
    ranked = [
        ("repoA", "src/a.py"),
        ("repoA", "src/b.py"),
        ("repoB", "src/c.py"),
    ]
    expected = ["src/a.py", "src/b.py", "src/c.py"]
    assert compute_file_recall(ranked, expected, k=10) == 1.0


def test_compute_file_recall_no_match():
    """Zero overlap between ranked and expected → recall = 0.0."""
    ranked = [
        ("repoA", "src/a.py"),
        ("repoA", "src/b.py"),
    ]
    expected = ["different/x.py", "different/y.py"]
    assert compute_file_recall(ranked, expected, k=10) == 0.0


def test_compute_file_recall_empty_expected():
    """Empty `expected_files` must return 0.0 without ZeroDivisionError.

    Matches the repo-level `compute_recall` convention — tickets with no
    recorded files_changed contribute 0 to the mean (they are effectively
    ignored for the file-recall signal).
    """
    ranked = [("repoA", "src/a.py")]
    result = compute_file_recall(ranked, expected_files=[], k=10)
    assert result == 0.0


def test_compute_file_recall_partial():
    """2 of 5 expected files appear in the top-10 → recall = 0.4."""
    ranked = [
        ("repoA", "src/a.py"),  # hit
        ("repoA", "src/noise1.py"),
        ("repoB", "src/b.py"),  # hit
        ("repoB", "src/noise2.py"),
        ("repoC", "src/noise3.py"),
        ("repoC", "src/noise4.py"),
        ("repoD", "src/noise5.py"),
        ("repoD", "src/noise6.py"),
        ("repoE", "src/noise7.py"),
        ("repoF", "src/noise8.py"),
    ]
    expected = [
        "src/a.py",
        "src/b.py",
        "src/missing1.py",
        "src/missing2.py",
        "src/missing3.py",
    ]
    assert compute_file_recall(ranked, expected, k=10) == pytest.approx(0.4)


def test_compute_file_recall_dedup():
    """Duplicate paths in `ranked` must not double-count the intersection."""
    ranked = [
        ("repoA", "src/a.py"),  # same file appears twice in ranked
        ("repoA", "src/a.py"),
        ("repoB", "src/b.py"),
    ]
    expected = ["src/a.py", "src/b.py"]
    # Numerator = |{a,b}| = 2, denominator = 2 → 1.0, NOT 3/2.
    assert compute_file_recall(ranked, expected, k=10) == 1.0


def test_compute_file_recall_respects_k():
    """k truncates ranked list before intersection."""
    ranked = [
        ("repoA", "src/a.py"),  # top-1
        ("repoA", "src/b.py"),  # top-2 — excluded when k=1
    ]
    expected = ["src/a.py", "src/b.py"]
    assert compute_file_recall(ranked, expected, k=1) == pytest.approx(0.5)
    assert compute_file_recall(ranked, expected, k=10) == 1.0


# ---- top_k_files ----


def test_top_k_files_dedup():
    """Same (repo_name, file_path) twice yields one entry."""
    ranked = [
        {"repo_name": "repoA", "file_path": "src/a.py", "content": "x"},
        {"repo_name": "repoA", "file_path": "src/a.py", "content": "y"},  # dup
        {"repo_name": "repoB", "file_path": "src/a.py", "content": "z"},  # same path, different repo — keep
    ]
    out = top_k_files(ranked, k=10)
    assert out == [("repoA", "src/a.py"), ("repoB", "src/a.py")]


def test_top_k_files_skips_missing_fields():
    """Chunks missing repo_name or file_path are dropped (parity with top_k_repos)."""
    ranked = [
        {"repo_name": "", "file_path": "src/a.py"},
        {"repo_name": "repoA", "file_path": ""},
        {"repo_name": "repoA", "file_path": "src/ok.py"},
    ]
    assert top_k_files(ranked, k=10) == [("repoA", "src/ok.py")]


def test_top_k_files_respects_k():
    """Stops at k unique entries even if more are available."""
    ranked = [{"repo_name": f"repo{i}", "file_path": f"src/{i}.py"} for i in range(20)]
    out = top_k_files(ranked, k=5)
    assert len(out) == 5
    assert out[0] == ("repo0", "src/0.py")
    assert out[4] == ("repo4", "src/4.py")


# ---- gate-version constants ----


def test_gate_version_constants():
    """Constants must be plain strings used verbatim in snapshot JSON."""
    assert GATE_VERSION_V1 == "v1"
    assert GATE_VERSION_V2 == "v2"
    assert GATE_VERSION_V1 != GATE_VERSION_V2


def test_delta_file_r10_threshold_value():
    """Threshold (0.01) is deliberately lower than repo r@10 gate (0.02)."""
    assert DELTA_FILE_R10_THRESHOLD == 0.01


# ---- decide_verdict_v2 (proposal §3, §4) ----
#
# All v2 tests live here rather than in test_eval_verdict.py because they
# verify the file-level-GT gate, which is thematically owned by this module.
# The existing test_eval_verdict.py tests continue to pin v1 behavior.

from scripts.eval.eval_verdict import (  # noqa: E402
    DELTA_FILE_R10_THRESHOLD_V2,
    DELTA_HIT5_THRESHOLD,
    DELTA_R10_THRESHOLD,
    MIN_NET_STRATUM_V2,
    decide_verdict_v2,
    verdict_from_snapshot_dual,
)


def _mk_deltas(n1_imp=0, n1_reg=0, n2p_imp=0, n2p_reg=0):
    """Craft a per_task_deltas dict with the requested stratum split.

    n1 tickets use Δr@10 = ±1.0 (clear binary flip), n2+ tickets use ±0.10
    (well above the 0.05 continuous threshold). n_gt_repos = 1 for n1,
    = 3 for n2+.
    """
    out: dict[str, dict] = {}
    tid = 0
    for _ in range(n1_imp):
        out[f"T-{tid}"] = {"recall_at_10": 1.0, "n_gt_repos": 1}
        tid += 1
    for _ in range(n1_reg):
        out[f"T-{tid}"] = {"recall_at_10": -1.0, "n_gt_repos": 1}
        tid += 1
    for _ in range(n2p_imp):
        out[f"T-{tid}"] = {"recall_at_10": 0.10, "n_gt_repos": 3}
        tid += 1
    for _ in range(n2p_reg):
        out[f"T-{tid}"] = {"recall_at_10": -0.10, "n_gt_repos": 3}
        tid += 1
    return out


def test_v2_primary_pass_all_thresholds_met():
    """Primary triple + both strata net=+16 → PROMOTE."""
    deltas = _mk_deltas(n1_imp=16, n2p_imp=16)
    result = decide_verdict_v2(
        {
            "delta_r10_all": DELTA_R10_THRESHOLD,
            "delta_hit5_all": DELTA_HIT5_THRESHOLD,
            "delta_file_r10_all": DELTA_FILE_R10_THRESHOLD_V2,
        },
        deltas,
    )
    assert result["verdict"] == "PROMOTE"
    assert result["gate_version"] == "v2"
    assert result["stratified"]["net_n1"] == 16
    assert result["stratified"]["net_n2plus"] == 16


def test_v2_primary_fail_delta_r10_zero():
    """Δr@10 = 0 → HOLD (sub-threshold, not a regression)."""
    deltas = _mk_deltas(n1_imp=30, n2p_imp=30)
    result = decide_verdict_v2(
        {
            "delta_r10_all": 0.0,
            "delta_hit5_all": DELTA_HIT5_THRESHOLD,
            "delta_file_r10_all": DELTA_FILE_R10_THRESHOLD_V2,
        },
        deltas,
    )
    assert result["verdict"] == "HOLD"
    assert "Δr@10" in result["reason"]


def test_v2_primary_fail_delta_hit5_zero():
    """ΔHit@5 = 0 → HOLD."""
    deltas = _mk_deltas(n1_imp=30, n2p_imp=30)
    result = decide_verdict_v2(
        {
            "delta_r10_all": DELTA_R10_THRESHOLD,
            "delta_hit5_all": 0.0,
            "delta_file_r10_all": DELTA_FILE_R10_THRESHOLD_V2,
        },
        deltas,
    )
    assert result["verdict"] == "HOLD"
    assert "ΔHit@5" in result["reason"]


def test_v2_primary_fail_delta_file_r10_zero():
    """Δfile_r@10 = 0 → HOLD (below 0.01 threshold, not a regression)."""
    deltas = _mk_deltas(n1_imp=30, n2p_imp=30)
    result = decide_verdict_v2(
        {
            "delta_r10_all": DELTA_R10_THRESHOLD,
            "delta_hit5_all": DELTA_HIT5_THRESHOLD,
            "delta_file_r10_all": 0.0,
        },
        deltas,
    )
    assert result["verdict"] == "HOLD"
    assert "Δfile_r@10" in result["reason"]


def test_v2_stratified_net_n1_below_threshold():
    """net_n1 = 14 (just below 15) → HOLD, even though n2plus passes comfortably."""
    deltas = _mk_deltas(n1_imp=14, n2p_imp=20)
    result = decide_verdict_v2(
        {
            "delta_r10_all": DELTA_R10_THRESHOLD,
            "delta_hit5_all": DELTA_HIT5_THRESHOLD,
            "delta_file_r10_all": DELTA_FILE_R10_THRESHOLD_V2,
        },
        deltas,
    )
    assert result["verdict"] == "HOLD"
    assert "net_n1" in result["reason"]
    assert result["stratified"]["net_n1"] == 14


def test_v2_stratified_net_n2plus_below_threshold():
    """net_n2plus = 14 → HOLD. Covers the asymmetric case where only
    the multi-repo stratum is narrow."""
    deltas = _mk_deltas(n1_imp=20, n2p_imp=14)
    result = decide_verdict_v2(
        {
            "delta_r10_all": DELTA_R10_THRESHOLD,
            "delta_hit5_all": DELTA_HIT5_THRESHOLD,
            "delta_file_r10_all": DELTA_FILE_R10_THRESHOLD_V2,
        },
        deltas,
    )
    assert result["verdict"] == "HOLD"
    assert "net_n2plus" in result["reason"]
    assert result["stratified"]["net_n2plus"] == 14


def test_v2_stratified_both_at_exact_threshold_promotes():
    """Boundary: both strata exactly at MIN_NET_STRATUM_V2 (15) → PROMOTE."""
    deltas = _mk_deltas(n1_imp=MIN_NET_STRATUM_V2, n2p_imp=MIN_NET_STRATUM_V2)
    result = decide_verdict_v2(
        {
            "delta_r10_all": DELTA_R10_THRESHOLD,
            "delta_hit5_all": DELTA_HIT5_THRESHOLD,
            "delta_file_r10_all": DELTA_FILE_R10_THRESHOLD_V2,
        },
        deltas,
    )
    assert result["verdict"] == "PROMOTE"
    assert result["stratified"]["net_n1"] == MIN_NET_STRATUM_V2
    assert result["stratified"]["net_n2plus"] == MIN_NET_STRATUM_V2


def test_v2_reject_delta_r10_negative():
    """Any primary Δ<0 → REJECT, regardless of stratum health."""
    deltas = _mk_deltas(n1_imp=30, n2p_imp=30)
    result = decide_verdict_v2(
        {
            "delta_r10_all": -0.01,
            "delta_hit5_all": DELTA_HIT5_THRESHOLD,
            "delta_file_r10_all": DELTA_FILE_R10_THRESHOLD_V2,
        },
        deltas,
    )
    assert result["verdict"] == "REJECT"
    assert "primary regressed" in result["reason"]


def test_v2_reject_net_n1_negative():
    """More 1-repo losers than winners → REJECT, even with positive primaries."""
    deltas = _mk_deltas(n1_imp=5, n1_reg=10, n2p_imp=30)
    result = decide_verdict_v2(
        {
            "delta_r10_all": DELTA_R10_THRESHOLD,
            "delta_hit5_all": DELTA_HIT5_THRESHOLD,
            "delta_file_r10_all": DELTA_FILE_R10_THRESHOLD_V2,
        },
        deltas,
    )
    assert result["verdict"] == "REJECT"
    assert "1-repo stratum" in result["reason"]
    assert result["stratified"]["net_n1"] == -5


def test_dispatcher_emits_both_v1_and_v2():
    """verdict_from_snapshot_dual must emit verdict_v1 AND verdict_v2 keys
    when both snapshots carry file_recall_at_10 on every entry."""
    # Two tickets with full schema (n_gt_repos + file_recall_at_10).
    # We craft a minimal PROMOTE-y scenario that's unambiguous enough to
    # verify the dispatcher plumbing, not the exact v1/v2 decision.
    base = {
        "T-1": {
            "recall_at_10": 0.0,
            "recall_at_25": 0.0,
            "rank_of_first_gt": None,
            "file_recall_at_10": 0.0,
            "n_gt_repos": 1,
        },
        "T-2": {
            "recall_at_10": 0.0,
            "recall_at_25": 0.0,
            "rank_of_first_gt": None,
            "file_recall_at_10": 0.0,
            "n_gt_repos": 3,
        },
    }
    cand = {
        "T-1": {
            "recall_at_10": 1.0,
            "recall_at_25": 1.0,
            "rank_of_first_gt": 1,
            "file_recall_at_10": 1.0,
            "n_gt_repos": 1,
        },
        "T-2": {
            "recall_at_10": 1.0,
            "recall_at_25": 1.0,
            "rank_of_first_gt": 1,
            "file_recall_at_10": 1.0,
            "n_gt_repos": 3,
        },
    }
    result = verdict_from_snapshot_dual(base, cand)
    assert "verdict_v1" in result
    assert "verdict_v2" in result
    assert result["verdict_v1"]["gate_version"] == "v1"
    assert result["verdict_v2"]["gate_version"] == "v2"
    # Both should reach a verdict string — v2 won't be "unavailable" because
    # file_recall_at_10 is present on every row.
    assert "unavailable" not in result["verdict_v2"]["reason"]


def test_dispatcher_v2_unavailable_when_legacy_snapshot():
    """Legacy snapshot (no file_recall_at_10) → v1 normal, v2 HOLD/unavailable."""
    base = {
        "T-1": {
            "recall_at_10": 0.5,
            "recall_at_25": 0.5,
            "rank_of_first_gt": 3,
            # NO file_recall_at_10 — simulates a pre-v2 snapshot.
            "n_gt_repos": 1,
        },
    }
    cand = {
        "T-1": {
            "recall_at_10": 1.0,
            "recall_at_25": 1.0,
            "rank_of_first_gt": 1,
            # NO file_recall_at_10.
            "n_gt_repos": 1,
        },
    }
    result = verdict_from_snapshot_dual(base, cand)
    # v1 must still compute (legacy snapshots must still score).
    assert result["verdict_v1"]["gate_version"] == "v1"
    assert result["verdict_v1"]["verdict"] in {"PROMOTE", "HOLD", "REJECT"}
    # v2 must be HOLD/unavailable — NOT REJECT.
    assert result["verdict_v2"]["verdict"] == "HOLD"
    assert result["verdict_v2"]["gate_version"] == "v2"
    assert "unavailable" in result["verdict_v2"]["reason"]


# =====================================================================
# §5 — pick_test_tasks_stratified (proposal §4)
# =====================================================================


from scripts.data.prepare_finetune_data import pick_test_tasks_stratified  # noqa: E402


def _mk_task(tid: str, files: list[str]) -> dict:
    """Minimal task dict the sampler needs: ticket_id + files_changed."""
    return {
        "ticket_id": tid,
        "files_changed": list(files),
        "summary": "",
        "description": "",
        "repos_changed": [],
        "patches": [],
        "resolution": None,
    }


def test_pick_test_tasks_stratified_per_bucket_min():
    """BO + CORE both have ≥20 eligible → both contribute ≥20 tickets to test."""
    tasks = []
    # Each bucket: 30 tickets, each with disjoint single-file changes so the
    # anti-leakage check passes trivially.
    fid = 0
    for prefix in ("BO", "CORE"):
        for i in range(30):
            tasks.append(_mk_task(f"{prefix}-{i}", [f"repoA/file_{fid}.py"]))
            fid += 1
    # 5 PI tickets — below the 20-eligible threshold; PI should NOT be
    # forced to contribute the per-bucket guarantee.
    for i in range(5):
        tasks.append(_mk_task(f"PI-{i}", [f"repoA/file_{fid}.py"]))
        fid += 1
    # test_ratio=0.5 (high enough so target_test_n ≥ 40 = 20+20).
    _test_set, manifest = pick_test_tasks_stratified(tasks, test_ratio=0.45, seed=42)
    counts = manifest["per_project_counts"]
    assert counts.get("BO", 0) >= 20, f"BO underfilled: {counts}"
    assert counts.get("CORE", 0) >= 20, f"CORE underfilled: {counts}"
    assert manifest["seed_used"] == 42
    assert manifest["retry_count"] == 0


def test_pick_test_tasks_stratified_cap_share():
    """A single ticket cannot own >10% of test-positive GT.

    Construct a pool where one ticket has 50 files and the others have 1 each;
    total positive GT ≈ 50 + 24 = 74. The 50-file ticket alone would be 67% of
    GT (well over the 10% cap) and must be skipped from test selection.
    """
    tasks = [_mk_task("BO-0", [f"repoA/big_{i}.py" for i in range(50)])]
    for i in range(1, 25):
        tasks.append(_mk_task(f"BO-{i}", [f"repoA/small_{i}.py"]))
    # Ratio 0.4 → target_test_n=10. The big BO-0 must NOT appear.
    test_set, manifest = pick_test_tasks_stratified(tasks, test_ratio=0.40, seed=7)
    test_ids = {t["ticket_id"] for t in test_set}
    assert "BO-0" not in test_ids, f"big-share ticket leaked into test: {test_ids}"
    assert manifest["max_ticket_positive_share"] <= 0.10 + 1e-9


def test_pick_test_tasks_stratified_anti_leakage_retries():
    """Sampler retries on >5% file overlap and eventually finds a clean split.

    Strategy: scan seeds 0..200 to find the smallest seed where the sampler
    raises (initial sample fails) AND a different seed in the same range
    succeeds with retry_count > 0. This proves the retry plumbing without
    coupling to a specific RNG state.

    Setup: a low-overlap pool with one "shared" file mixed in. Most seeds
    succeed on retry 0, but a small fraction of seeds need ≥1 retry — we
    assert *some* seed produces retry_count ≥ 1 AND the resulting overlap is
    within bounds.
    """
    # Pool: BO tickets where most paths are unique, but a cluster of tickets
    # share several "leaky" files. With 22 BO tickets and per-bucket
    # guarantee=20, exactly 2 tickets land in train. If those 2 train tickets
    # are from the leaky cluster, every leaky file in test also appears in
    # train (overlap > 5%, retry triggered). If they're from the unique
    # cluster, overlap ≈ 0 (clean first sample).
    tasks = []
    for i in range(11):
        # 11 BO tickets with unique single-file paths.
        tasks.append(_mk_task(f"BO-{i}", [f"unique/path_{i}.py"]))
    # 11 BO tickets, EACH touching 2 distinct files from a shared 6-file leaky
    # pool. Any 2 of these in test guarantee the leaky files are also covered
    # in train (since 9 of them stay in train). Overlap pct ranges 0..0.4 by
    # how the train/test split lands the leaky vs unique tickets.
    leaky_files = [f"leaky/file_{j}.py" for j in range(6)]
    for i in range(11):
        a = leaky_files[i % 6]
        b = leaky_files[(i + 3) % 6]
        tasks.append(_mk_task(f"BO-{100 + i}", [a, b]))
    # Confirm at least one seed exercises the retry path (retry_count ≥ 1).
    saw_retry = False
    last_manifest: dict | None = None
    for s in range(0, 60):
        try:
            _, mf = pick_test_tasks_stratified(tasks, test_ratio=0.10, seed=s)
        except ValueError:
            continue
        last_manifest = mf
        # Verify the contract on every successful sample.
        assert mf["file_overlap_pct"] <= 0.05 + 1e-9, f"seed={s} returned manifest with overlap above threshold: {mf}"
        if mf["retry_count"] >= 1:
            saw_retry = True
            assert mf["seed_used"] == s + mf["retry_count"]
            break
    assert saw_retry, f"no seed in 0..59 exercised retry path on this pool; last_manifest={last_manifest}"


def test_pick_test_tasks_stratified_anti_leakage_gives_up():
    """Every ticket changes the SAME file → no sample can ever pass; raises."""
    tasks = [_mk_task(f"BO-{i}", ["shared/one_and_only.py"]) for i in range(25)]
    with pytest.raises(ValueError, match="anti-leakage"):
        pick_test_tasks_stratified(tasks, test_ratio=0.10, seed=42)
