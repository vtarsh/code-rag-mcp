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
    """Return PROMOTE / HOLD / REJECT on the full eval set.

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
