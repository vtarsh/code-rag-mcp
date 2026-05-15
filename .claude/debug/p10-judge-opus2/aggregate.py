#!/usr/bin/env python3.12
"""Aggregate Judge #2 scores into per-query and macro metrics."""

import json
import math
from collections import defaultdict

scores = json.load(open("/tmp/p10_a2_judge_scores_opus2.json"))["scores"]


def metrics(arr):
    rel = sum(1 for s in arr if s >= 2) / len(arr)
    direct = sum(1 for s in arr if s == 3) / len(arr)
    dcg = sum(s / math.log2(i + 2) for i, s in enumerate(arr))
    return rel, direct, dcg


per_query = []
for qid, row in scores.items():
    a2 = row["a2"]
    on = row["on"]
    a2_rel, a2_direct, a2_dcg = metrics(a2)
    on_rel, on_direct, on_dcg = metrics(on)
    per_query.append(
        {
            "qid": qid,
            "stratum": row["stratum"],
            "skipped": row["skipped"],
            "a2_rel": a2_rel,
            "on_rel": on_rel,
            "a2_direct": a2_direct,
            "on_direct": on_direct,
            "a2_dcg": a2_dcg,
            "on_dcg": on_dcg,
            "d_rel": a2_rel - on_rel,
            "d_direct": a2_direct - on_direct,
            "d_dcg": a2_dcg - on_dcg,
        }
    )

# Macro
n = len(per_query)
mean = lambda k: sum(r[k] for r in per_query) / n
print(f"Total queries: {n}")
print(f"Macro A2 rel_rate: {mean('a2_rel'):.4f}")
print(f"Macro ON rel_rate: {mean('on_rel'):.4f}")
print(f"Δ rel_rate (A2 - ON): {mean('d_rel'):+.4f}")
print()
print(f"Macro A2 direct_rate: {mean('a2_direct'):.4f}")
print(f"Macro ON direct_rate: {mean('on_direct'):.4f}")
print(f"Δ direct_rate: {mean('d_direct'):+.4f}")
print()
print(f"Macro A2 DCG: {mean('a2_dcg'):.3f}")
print(f"Macro ON DCG: {mean('on_dcg'):.3f}")
print(f"Δ DCG: {mean('d_dcg'):+.3f}")
print()

# Per-stratum
strata = defaultdict(list)
for r in per_query:
    strata[r["stratum"]].append(r)
print("Per-stratum lifts (A2 - ON):")
print(f"{'stratum':<10} {'n':>3} {'d_rel':>8} {'d_direct':>9} {'d_dcg':>7}")
for s, rows in strata.items():
    n_s = len(rows)
    drel = sum(r["d_rel"] for r in rows) / n_s
    ddirect = sum(r["d_direct"] for r in rows) / n_s
    ddcg = sum(r["d_dcg"] for r in rows) / n_s
    print(f"{s:<10} {n_s:>3} {drel:>+8.4f} {ddirect:>+9.4f} {ddcg:>+7.3f}")
print()

# OFF vs KEEP/UNK split
off = [r for r in per_query if r["skipped"]]
keep_unk = [r for r in per_query if not r["skipped"]]
print(f"OFF stratum (rerank skipped, n={len(off)}):")
print(f"  Δ rel = {sum(r['d_rel'] for r in off) / len(off):+.4f}")
print(f"  Δ direct = {sum(r['d_direct'] for r in off) / len(off):+.4f}")
print(f"  Δ DCG = {sum(r['d_dcg'] for r in off) / len(off):+.3f}")
print(f"KEEP/UNK stratum (rerank kept, n={len(keep_unk)}):")
print(f"  Δ rel = {sum(r['d_rel'] for r in keep_unk) / len(keep_unk):+.4f}")
print(f"  Δ direct = {sum(r['d_direct'] for r in keep_unk) / len(keep_unk):+.4f}")
print(f"  Δ DCG = {sum(r['d_dcg'] for r in keep_unk) / len(keep_unk):+.3f}")
print()

# Categorize
print("Categorisation (per query, threshold |Δ rel| >= 0.10 OR |Δ DCG| >= 1.0):")
real_a2 = []
real_on = []
ties = []
for r in per_query:
    if r["d_rel"] >= 0.10 or r["d_dcg"] >= 1.0:
        real_a2.append(r["qid"])
    elif r["d_rel"] <= -0.10 or r["d_dcg"] <= -1.0:
        real_on.append(r["qid"])
    else:
        ties.append(r["qid"])
print(f"  REAL A2 WIN: {len(real_a2)} -> {real_a2}")
print(f"  REAL BASELINE WIN: {len(real_on)} -> {real_on}")
print(f"  TIE: {len(ties)} -> {ties}")

json.dump(per_query, open("/tmp/p10_a2_judge_per_query_opus2.json", "w"), indent=2)
print("\nWrote /tmp/p10_a2_judge_per_query_opus2.json")
