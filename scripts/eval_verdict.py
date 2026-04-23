"""Shared verdict logic for reranker eval.

Single source of truth for `decide_verdict()` — previously duplicated in
`eval_finetune.py` and `merge_eval_shards.py` with divergent thresholds
(a PROMOTE via one pipeline could be a REJECT via the other).

Rationale for the gate design (post-audit 2026-04-20):

Old gate was `max_regressions=3` on 909 tickets — mathematically unworkable
because 46% of tickets have n_gt_repos=1 (r@10 is binary; a single rank
flip = ±100pp delta). The 3-regression ceiling was incompatible with the
noise floor (~40 flips are expected even on a neutral FT).

New gate tracks three signals on the FULL evaluated set (not the 5-ticket
test split — that was statistically fragile, one flip = 20pp Δ):
  1. Δr@10    (primary)     — mean recall at 10 over all tickets
  2. ΔHit@5   (co-primary)  — fraction of tickets with GT at rank ≤ 5
  3. net      (counts)      — n_improved - n_regressed on r@10 (±5pp)

MRR was evaluated as a candidate primary and REJECTED: on our historical
snapshots MRR ranks v7 (our rejected model) as #1 above v6.2. 44% of
n_gt=1 MRR-deltas are pure rank-2→1 noise that doesn't affect top-10
recall a user sees. We keep MRR as a diagnostic field, never in the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# ---- Gate thresholds (tunable in one place) --------------------------------

# Minimum Δr@10 on train set to be considered a candidate for PROMOTE.
DELTA_R10_THRESHOLD = 0.02  # +2pp

# Minimum ΔHit@5 on train set — co-primary signal, catches reshuffles
# that push GT below the fold.
DELTA_HIT5_THRESHOLD = 0.02  # +2pp

# Minimum net (n_improved - n_regressed) on r@10 ±5pp over the FULL eval.
# Historical baseline for context: v4 net=+74, v6.2 net=+89, v7 net=+73.
MIN_NET_IMPROVED = 20

# Regression delta threshold (a ticket "regressed" if Δr@10 ≤ -DELTA_PP).
REGRESSION_DELTA_PP = 0.05  # 5pp drop


# ---- Data classes ----------------------------------------------------------

@dataclass(frozen=True)
class VerdictResult:
    verdict: str       # PROMOTE | HOLD | REJECT
    reason: str        # human-readable
    metrics: dict      # diagnostic numbers (all_delta_r10, all_delta_hit5, mrr, etc.)


# ---- Metric helpers (computed from per_task_* dicts) -----------------------

def mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def compute_r10_mean(per_task: dict[str, dict], tickets: Iterable[str] | None = None) -> float:
    """Mean recall_at_10 over given tickets (or all if None).

    Missing tickets contribute 0 (same convention as the aggregate() in
    both scripts). Tickets with `error` still have recall_at_10=0.0 set
    at eval time — no special casing here.
    """
    if tickets is None:
        vals = [v["recall_at_10"] for v in per_task.values() if "recall_at_10" in v]
    else:
        vals = [per_task[t]["recall_at_10"] for t in tickets if t in per_task]
    return mean(vals)


def compute_hit5_mean(per_task: dict[str, dict], tickets: Iterable[str] | None = None) -> float:
    """Hit@5: fraction of tickets with rank_of_first_gt ≤ 5.

    None rank (no GT in top-N) counts as miss (0). Co-primary metric.
    """
    if tickets is None:
        ranks = [v.get("rank_of_first_gt") for v in per_task.values()]
    else:
        ranks = [per_task[t].get("rank_of_first_gt") for t in tickets if t in per_task]
    if not ranks:
        return 0.0
    hits = sum(1 for r in ranks if r is not None and r <= 5)
    return hits / len(ranks)


def compute_mrr_at_10(per_task: dict[str, dict], tickets: Iterable[str] | None = None) -> float:
    """Diagnostic only — NOT in the verdict gate. See module docstring.

    MRR = mean of (1/rank if rank ≤ 10 else 0).
    """
    if tickets is None:
        ranks = [v.get("rank_of_first_gt") for v in per_task.values()]
    else:
        ranks = [per_task[t].get("rank_of_first_gt") for t in tickets if t in per_task]
    if not ranks:
        return 0.0
    rr = [(1.0 / r) if (r is not None and r <= 10) else 0.0 for r in ranks]
    return mean(rr)


def count_improvements_regressions(
    deltas: dict[str, dict],
    *,
    threshold_pp: float = REGRESSION_DELTA_PP,
) -> tuple[int, int]:
    """Return (n_improved, n_regressed) on r@10 with ±threshold_pp gate.

    A ticket is "improved" if its recall_at_10 delta ≥ +threshold_pp,
    "regressed" if ≤ -threshold_pp. Near-zero deltas are neither.
    """
    n_imp = sum(1 for d in deltas.values() if d.get("recall_at_10", 0.0) >= threshold_pp)
    n_reg = sum(1 for d in deltas.values() if d.get("recall_at_10", 0.0) <= -threshold_pp)
    return n_imp, n_reg


# ---- The gate itself -------------------------------------------------------

def decide_verdict(
    *,
    delta_r10_all: float,
    delta_hit5_all: float,
    n_improved: int,
    n_regressed: int,
    delta_r10_threshold: float = DELTA_R10_THRESHOLD,
    delta_hit5_threshold: float = DELTA_HIT5_THRESHOLD,
    min_net_improved: int = MIN_NET_IMPROVED,
    diagnostic_metrics: dict | None = None,
) -> VerdictResult:
    """[v1 / legacy] Return PROMOTE / HOLD / REJECT on the full eval set.

    Kept for snapshots that pre-date the file-level GT schema (no
    ``file_recall_at_10``). New runs should consume both v1 and v2 via
    ``verdict_from_snapshot_dual`` so the dispatcher can fall back here
    when v2 is unavailable. See ``decide_verdict_v2`` for the current gate.

    Contract:
      - delta_r10_all, delta_hit5_all: aggregate deltas on the FULL eval
        (train+test, not the 5-ticket test split). Positive = better.
      - n_improved, n_regressed: counts from count_improvements_regressions().

    Decision rules:
      REJECT — any primary metric regressed on aggregate (Δ<0 on r@10 OR Hit@5).
      REJECT — net (improved-regressed) < 0 (more losers than winners).
      PROMOTE — all three signals pass thresholds.
      HOLD — primary deltas positive but sub-threshold, or net below
             min_net_improved. Indicates "not worse, but not clearly better."

    Ties/edge cases:
      - delta_r10_all exactly == 0: treated as non-regression but falls to HOLD.
      - delta_r10_all < 0 takes precedence (REJECT) regardless of Hit@5.
    """
    net = n_improved - n_regressed
    metrics = {
        "delta_r10_all": round(delta_r10_all, 4),
        "delta_hit5_all": round(delta_hit5_all, 4),
        "n_improved_r10": n_improved,
        "n_regressed_r10": n_regressed,
        "net_improved_r10": net,
    }
    if diagnostic_metrics:
        metrics.update(diagnostic_metrics)

    if delta_r10_all < 0:
        return VerdictResult(
            "REJECT",
            f"Δr@10={delta_r10_all:+.3f} on full eval — primary regressed",
            metrics,
        )
    if delta_hit5_all < 0:
        return VerdictResult(
            "REJECT",
            f"ΔHit@5={delta_hit5_all:+.3f} — top-5 quality regressed",
            metrics,
        )
    if net < 0:
        return VerdictResult(
            "REJECT",
            f"net={net:+d} ({n_improved} improved, {n_regressed} regressed) — more losers than winners",
            metrics,
        )

    promote_ok = (
        delta_r10_all >= delta_r10_threshold
        and delta_hit5_all >= delta_hit5_threshold
        and net >= min_net_improved
    )
    if promote_ok:
        return VerdictResult(
            "PROMOTE",
            (
                f"Δr@10={delta_r10_all:+.3f}, ΔHit@5={delta_hit5_all:+.3f}, "
                f"net=+{net} (impr={n_improved}, regr={n_regressed})"
            ),
            metrics,
        )

    shortfall = []
    if delta_r10_all < delta_r10_threshold:
        shortfall.append(f"Δr@10={delta_r10_all:+.3f}<{delta_r10_threshold:+.3f}")
    if delta_hit5_all < delta_hit5_threshold:
        shortfall.append(f"ΔHit@5={delta_hit5_all:+.3f}<{delta_hit5_threshold:+.3f}")
    if net < min_net_improved:
        shortfall.append(f"net={net:+d}<{min_net_improved:+d}")
    return VerdictResult(
        "HOLD",
        f"positive but sub-threshold: {'; '.join(shortfall)}",
        metrics,
    )


# ---- Convenience: compute everything from snapshot dicts ------------------

def verdict_from_snapshot(
    per_task_baseline: dict[str, dict],
    per_task_ft: dict[str, dict],
    per_task_delta: dict[str, dict],
) -> VerdictResult:
    """One-call wrapper: derive all gate inputs + diagnostics from snapshot dicts."""
    r10_base = compute_r10_mean(per_task_baseline)
    r10_ft = compute_r10_mean(per_task_ft)
    hit5_base = compute_hit5_mean(per_task_baseline)
    hit5_ft = compute_hit5_mean(per_task_ft)
    mrr_base = compute_mrr_at_10(per_task_baseline)
    mrr_ft = compute_mrr_at_10(per_task_ft)
    n_imp, n_reg = count_improvements_regressions(per_task_delta)

    return decide_verdict(
        delta_r10_all=r10_ft - r10_base,
        delta_hit5_all=hit5_ft - hit5_base,
        n_improved=n_imp,
        n_regressed=n_reg,
        diagnostic_metrics={
            "r10_baseline": round(r10_base, 4),
            "r10_ft": round(r10_ft, 4),
            "hit5_baseline": round(hit5_base, 4),
            "hit5_ft": round(hit5_ft, 4),
            "mrr_baseline_diag": round(mrr_base, 4),
            "mrr_ft_diag": round(mrr_ft, 4),
            "delta_mrr_diag": round(mrr_ft - mrr_base, 4),
        },
    )


# =====================================================================
# v2 gate — file-level co-primary + stratified net (proposal §3)
# =====================================================================
#
# Additive to v1: kept alongside `decide_verdict` because legacy snapshots
# (gte_v4.json, gte_v6_2.json, gte_v7.json, …) do not carry
# `file_recall_at_10` in per_task and will fall back to v1 via the
# `verdict_from_snapshot_dual` dispatcher.
#
# Thresholds below are first-principles guesses from §3; calibration on
# v6.2 vs v8 is deferred to §7 of the proposal (next run).

DELTA_FILE_R10_THRESHOLD_V2 = 0.01   # +1pp — file-level is strictly harder
MIN_NET_STRATUM_V2 = 15               # required net in BOTH strata

# Stratum thresholds:
#   n_gt_repos == 1:    r@10 is binary (0 or 1) — only full flips matter.
#   n_gt_repos >= 2:    continuous — 5pp threshold matches v1 REGRESSION_DELTA_PP.
STRATUM_N1_FLIP_THRESHOLD = 0.5
STRATUM_N2PLUS_DELTA = 0.05


def _stratified_counts(
    per_task_deltas: dict[str, dict],
) -> dict[str, int]:
    """Return stratified improved/regressed counts by ``n_gt_repos``.

    `per_task_deltas[tid]` must carry both `recall_at_10` (delta) AND
    `n_gt_repos` (ticket metadata). Tickets missing `n_gt_repos` are
    silently skipped — caller must inject the field from baseline
    per_task before invoking the gate.

    Returns six counts: n1_improved, n1_regressed, n2plus_improved,
    n2plus_regressed, net_n1, net_n2plus.
    """
    n1_imp = n1_reg = 0
    n2p_imp = n2p_reg = 0
    for d in per_task_deltas.values():
        n_gt = d.get("n_gt_repos")
        if n_gt is None:
            continue
        delta = d.get("recall_at_10", 0.0)
        if delta is None:
            continue
        if n_gt == 1:
            # Binary flip — |Δ| ≥ 0.5 ≈ 0→1 or 1→0.
            if delta >= STRATUM_N1_FLIP_THRESHOLD:
                n1_imp += 1
            elif delta <= -STRATUM_N1_FLIP_THRESHOLD:
                n1_reg += 1
        elif n_gt >= 2:
            if delta >= STRATUM_N2PLUS_DELTA:
                n2p_imp += 1
            elif delta <= -STRATUM_N2PLUS_DELTA:
                n2p_reg += 1
    return {
        "n1_improved": n1_imp,
        "n1_regressed": n1_reg,
        "n2plus_improved": n2p_imp,
        "n2plus_regressed": n2p_reg,
        "net_n1": n1_imp - n1_reg,
        "net_n2plus": n2p_imp - n2p_reg,
    }


def decide_verdict_v2(
    delta_metrics: dict,
    per_task_deltas: dict[str, dict],
    *,
    delta_r10_threshold: float = DELTA_R10_THRESHOLD,
    delta_hit5_threshold: float = DELTA_HIT5_THRESHOLD,
    delta_file_r10_threshold: float = DELTA_FILE_R10_THRESHOLD_V2,
    min_net_stratum: int = MIN_NET_STRATUM_V2,
) -> dict:
    """v2 gate (current): primary triple + stratified net on ``n_gt_repos``.

    Proposal §3. PROMOTE iff ALL primaries pass (Δr@10 ≥ +0.02,
    ΔHit@5 ≥ +0.02, Δfile_r@10 ≥ +0.01) AND
    ``min(net_n1, net_n2plus) ≥ 15``. REJECT iff any primary Δ<0 or either
    stratum net<0. Otherwise HOLD (positive but sub-threshold).

    Args:
      delta_metrics: dict with keys `delta_r10_all`, `delta_hit5_all`,
        `delta_file_r10_all`. Missing keys default to 0.0 (so a caller
        without file-level data falls to HOLD, not REJECT).
      per_task_deltas: per-ticket deltas with `recall_at_10` AND
        `n_gt_repos` populated. Tickets lacking `n_gt_repos` are skipped.

    Returns:
      dict with `verdict`, `reason`, `primary`, `stratified`, `gate_version`.
      `verdict` is one of "PROMOTE" | "HOLD" | "REJECT".
    """
    d_r10 = float(delta_metrics.get("delta_r10_all", 0.0))
    d_hit5 = float(delta_metrics.get("delta_hit5_all", 0.0))
    d_file_r10 = float(delta_metrics.get("delta_file_r10_all", 0.0))

    strat = _stratified_counts(per_task_deltas)

    primary = {
        "delta_r10_all": round(d_r10, 4),
        "delta_hit5_all": round(d_hit5, 4),
        "delta_file_r10_all": round(d_file_r10, 4),
        "delta_r10_threshold": delta_r10_threshold,
        "delta_hit5_threshold": delta_hit5_threshold,
        "delta_file_r10_threshold": delta_file_r10_threshold,
    }

    # REJECT: any primary Δ<0 or either stratum net<0.
    if d_r10 < 0:
        return {
            "verdict": "REJECT",
            "reason": f"Δr@10={d_r10:+.3f} — primary regressed",
            "primary": primary,
            "stratified": strat,
            "gate_version": "v2",
        }
    if d_hit5 < 0:
        return {
            "verdict": "REJECT",
            "reason": f"ΔHit@5={d_hit5:+.3f} — top-5 quality regressed",
            "primary": primary,
            "stratified": strat,
            "gate_version": "v2",
        }
    if d_file_r10 < 0:
        return {
            "verdict": "REJECT",
            "reason": f"Δfile_r@10={d_file_r10:+.3f} — file-level co-primary regressed",
            "primary": primary,
            "stratified": strat,
            "gate_version": "v2",
        }
    if strat["net_n1"] < 0:
        return {
            "verdict": "REJECT",
            "reason": (
                f"net_n1={strat['net_n1']:+d} "
                f"({strat['n1_improved']} improved, {strat['n1_regressed']} regressed) "
                f"— 1-repo stratum more losers than winners"
            ),
            "primary": primary,
            "stratified": strat,
            "gate_version": "v2",
        }
    if strat["net_n2plus"] < 0:
        return {
            "verdict": "REJECT",
            "reason": (
                f"net_n2plus={strat['net_n2plus']:+d} "
                f"({strat['n2plus_improved']} improved, {strat['n2plus_regressed']} regressed) "
                f"— multi-repo stratum more losers than winners"
            ),
            "primary": primary,
            "stratified": strat,
            "gate_version": "v2",
        }

    # PROMOTE: all primaries meet threshold AND min stratum net ≥ min_net_stratum.
    primary_ok = (
        d_r10 >= delta_r10_threshold
        and d_hit5 >= delta_hit5_threshold
        and d_file_r10 >= delta_file_r10_threshold
    )
    stratum_ok = min(strat["net_n1"], strat["net_n2plus"]) >= min_net_stratum
    if primary_ok and stratum_ok:
        return {
            "verdict": "PROMOTE",
            "reason": (
                f"Δr@10={d_r10:+.3f}, ΔHit@5={d_hit5:+.3f}, "
                f"Δfile_r@10={d_file_r10:+.3f}, "
                f"net_n1=+{strat['net_n1']}, net_n2plus=+{strat['net_n2plus']}"
            ),
            "primary": primary,
            "stratified": strat,
            "gate_version": "v2",
        }

    # HOLD: positive but sub-threshold on ≥1 axis.
    shortfall: list[str] = []
    if d_r10 < delta_r10_threshold:
        shortfall.append(f"Δr@10={d_r10:+.3f}<{delta_r10_threshold:+.3f}")
    if d_hit5 < delta_hit5_threshold:
        shortfall.append(f"ΔHit@5={d_hit5:+.3f}<{delta_hit5_threshold:+.3f}")
    if d_file_r10 < delta_file_r10_threshold:
        shortfall.append(f"Δfile_r@10={d_file_r10:+.3f}<{delta_file_r10_threshold:+.3f}")
    if strat["net_n1"] < min_net_stratum:
        shortfall.append(f"net_n1={strat['net_n1']:+d}<{min_net_stratum}")
    if strat["net_n2plus"] < min_net_stratum:
        shortfall.append(f"net_n2plus={strat['net_n2plus']:+d}<{min_net_stratum}")
    return {
        "verdict": "HOLD",
        "reason": "positive but sub-threshold: " + "; ".join(shortfall),
        "primary": primary,
        "stratified": strat,
        "gate_version": "v2",
    }


def _compute_file_r10_mean(per_task: dict[str, dict]) -> float | None:
    """Mean of file_recall_at_10 over per_task entries; None if any missing.

    v2 requires ALL evaluated entries to carry file_recall_at_10. A single
    None (legacy row) disqualifies v2 and collapses to v1-only.
    """
    vals: list[float] = []
    for v in per_task.values():
        x = v.get("file_recall_at_10")
        if x is None:
            return None
        vals.append(float(x))
    if not vals:
        return None
    return sum(vals) / len(vals)


def _inject_n_gt_repos(
    per_task_deltas: dict[str, dict],
    per_task_baseline: dict[str, dict],
) -> dict[str, dict]:
    """Shallow-copy deltas with `n_gt_repos` added from baseline per-task.

    The stratifier requires `n_gt_repos` on each delta; eval_finetune.py
    writes it on per_task entries but build_delta strips non-metric keys.
    """
    out: dict[str, dict] = {}
    for tid, d in per_task_deltas.items():
        entry = dict(d)
        base = per_task_baseline.get(tid, {})
        if "n_gt_repos" not in entry and "n_gt_repos" in base:
            entry["n_gt_repos"] = base["n_gt_repos"]
        out[tid] = entry
    return out


def verdict_from_snapshot_dual(
    snapshot_baseline: dict[str, dict],
    snapshot_candidate: dict[str, dict],
    *,
    per_task_delta: dict[str, dict] | None = None,
) -> dict:
    """Emit both v1 and v2 verdicts from snapshot dicts in one call.

    v1 is always computed (legacy snapshots must still score). v2 is
    computed iff every entry in both baseline AND candidate per_task has
    `file_recall_at_10` — otherwise v2 is reported as HOLD/unavailable
    rather than a spurious REJECT.

    Args:
      snapshot_baseline, snapshot_candidate: per_task dicts (mapping
        ticket_id → metric entry).
      per_task_delta: optional precomputed delta dict. If None, derived
        locally via `build_delta`-equivalent logic inline (recall_at_10,
        recall_at_25, rank_of_first_gt_delta, file_recall_at_10).

    Returns:
      `{"verdict_v1": {...}, "verdict_v2": {...}}` — each value is either
      the gate payload or `{"verdict": "HOLD", "reason": "unavailable: ...",
      "gate_version": "v2"}` for v2 when file-level data is missing.
    """
    # --- Derive v1 inputs from raw snapshots -----------------------------
    # build_delta lives in merge_eval_shards; replicate the minimum here to
    # avoid a cross-script import cycle (merge_eval_shards already imports
    # verdict_from_snapshot from this module).
    if per_task_delta is None:
        pd: dict[str, dict] = {}
        for tid in set(snapshot_baseline) & set(snapshot_candidate):
            b = snapshot_baseline[tid]
            f = snapshot_candidate[tid]
            b_rank = b.get("rank_of_first_gt")
            f_rank = f.get("rank_of_first_gt")
            rank_delta: int | None
            if b_rank is None or f_rank is None:
                rank_delta = None
            else:
                rank_delta = f_rank - b_rank
            b_file = b.get("file_recall_at_10")
            f_file = f.get("file_recall_at_10")
            if b_file is None or f_file is None:
                file_delta: float | None = None
            else:
                file_delta = round(float(f_file) - float(b_file), 4)
            pd[tid] = {
                "recall_at_10": round(
                    f.get("recall_at_10", 0.0) - b.get("recall_at_10", 0.0), 4
                ),
                "recall_at_25": round(
                    f.get("recall_at_25", 0.0) - b.get("recall_at_25", 0.0), 4
                ),
                "rank_of_first_gt_delta": rank_delta,
                "file_recall_at_10": file_delta,
            }
        per_task_delta = pd

    v1_result = verdict_from_snapshot(
        snapshot_baseline, snapshot_candidate, per_task_delta
    )
    v1_payload = {
        "verdict": v1_result.verdict,
        "reason": v1_result.reason,
        "metrics": v1_result.metrics,
        "gate_version": "v1",
    }

    # --- v2 — only if BOTH snapshots carry file_recall_at_10 on every row
    base_file_mean = _compute_file_r10_mean(snapshot_baseline)
    cand_file_mean = _compute_file_r10_mean(snapshot_candidate)
    if base_file_mean is None or cand_file_mean is None:
        v2_payload = {
            "verdict": "HOLD",
            "reason": "unavailable: file_recall_at_10 missing on legacy snapshot",
            "gate_version": "v2",
        }
        return {"verdict_v1": v1_payload, "verdict_v2": v2_payload}

    r10_base = compute_r10_mean(snapshot_baseline)
    r10_cand = compute_r10_mean(snapshot_candidate)
    hit5_base = compute_hit5_mean(snapshot_baseline)
    hit5_cand = compute_hit5_mean(snapshot_candidate)

    delta_metrics = {
        "delta_r10_all": r10_cand - r10_base,
        "delta_hit5_all": hit5_cand - hit5_base,
        "delta_file_r10_all": cand_file_mean - base_file_mean,
    }

    deltas_with_strata = _inject_n_gt_repos(per_task_delta, snapshot_baseline)
    v2_payload = decide_verdict_v2(delta_metrics, deltas_with_strata)
    return {"verdict_v1": v1_payload, "verdict_v2": v2_payload}
