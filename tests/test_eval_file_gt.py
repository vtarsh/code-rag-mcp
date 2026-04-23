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

# scripts/ is not a package — mirror the import pattern used in test_eval_verdict.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.eval_finetune import (  # noqa: E402
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
        ("repoA", "src/a.py"),        # hit
        ("repoA", "src/noise1.py"),
        ("repoB", "src/b.py"),        # hit
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
    assert compute_file_recall(ranked, expected, k=10) == 0.4


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
        ("repoA", "src/a.py"),   # top-1
        ("repoA", "src/b.py"),   # top-2 — excluded when k=1
    ]
    expected = ["src/a.py", "src/b.py"]
    assert compute_file_recall(ranked, expected, k=1) == 0.5
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
    ranked = [
        {"repo_name": f"repo{i}", "file_path": f"src/{i}.py"} for i in range(20)
    ]
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
