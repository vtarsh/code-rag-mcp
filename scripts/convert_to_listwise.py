"""Convert pointwise train.jsonl (query/document/label) into listwise format
for LambdaLoss / ListNetLoss / RankNetLoss training.

Input row:  {"query": str, "document": str, "label": 0.0 | 1.0, ...}
Output row: {"query": str, "docs": [str, ...], "labels": [float, ...]}

Grouping is by exact `query` string. For each query we keep ALL positives
and sample up to `max_negs` negatives (default 31 → total docs ≤ 32 which
is within LambdaLoss's comfortable batch range). Using deterministic seed
so the output is reproducible.

Usage:
    python3.12 scripts/convert_to_listwise.py \
      --in profiles/pay-com/finetune_data_v8/train.jsonl \
      --out profiles/pay-com/finetune_data_v12a/train.jsonl \
      --max-negs 31 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="inp", type=Path, required=True,
                   help="Pointwise JSONL with {query, document, label}")
    p.add_argument("--out", type=Path, required=True,
                   help="Listwise JSONL with {query, docs, labels}")
    p.add_argument("--max-negs", type=int, default=31,
                   help="Max negatives to keep per query (positives always kept)")
    p.add_argument("--max-docs-per-group", type=int, default=32,
                   help="Hard cap on total docs (pos+neg) per group. "
                        "Prevents OOM on listwise forward pass (each group = one step). "
                        "If group exceeds cap, oversampled positives are downsampled too.")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Read pointwise rows, group by query.
    groups: dict[str, dict] = defaultdict(lambda: {"pos_docs": [], "neg_docs": []})
    skipped = 0
    total_in = 0
    with args.inp.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_in += 1
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            q = r.get("query")
            d = r.get("document")
            lbl = r.get("label")
            if q is None or d is None or lbl is None:
                skipped += 1
                continue
            label = float(lbl)
            slot = "pos_docs" if label >= 0.5 else "neg_docs"
            groups[q][slot].append(str(d))

    # Emit listwise rows with positives + sampled negatives.
    rng = random.Random(args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    n_groups = 0
    n_emitted_rows = 0
    n_degenerate = 0  # skip queries missing either positives or negatives
    pos_sizes: list[int] = []
    neg_sizes: list[int] = []
    final_sizes: list[int] = []

    with args.out.open("w", encoding="utf-8") as f:
        # Deterministic iteration order for reproducibility
        for query in sorted(groups.keys()):
            g = groups[query]
            pos = g["pos_docs"]
            neg = g["neg_docs"]
            if not pos or not neg:
                n_degenerate += 1
                continue
            # Sample negatives (deterministic per query via seeded rng).
            if len(neg) > args.max_negs:
                rng.shuffle(neg)
                neg = neg[: args.max_negs]

            # Hard cap on total group size — listwise forward passes all docs
            # per step; a 500-doc group would OOM the GPU. Prefer to keep a
            # mix of positives and negatives when downsampling rather than
            # taking just the head of either list.
            if len(pos) + len(neg) > args.max_docs_per_group:
                # Keep at least 1 positive + 1 negative; split remaining slots
                # proportionally but lean slightly toward positives (at least 1/3).
                cap = args.max_docs_per_group
                n_pos = max(1, min(len(pos), max(cap // 3, cap - len(neg))))
                n_neg = max(1, cap - n_pos)
                if n_neg > len(neg):
                    n_neg = len(neg)
                    n_pos = max(1, cap - n_neg)
                rng.shuffle(pos)
                rng.shuffle(neg)
                pos = pos[:n_pos]
                neg = neg[:n_neg]

            docs = pos + neg
            labels = [1.0] * len(pos) + [0.0] * len(neg)
            f.write(json.dumps(
                {"query": query, "docs": docs, "labels": labels},
                ensure_ascii=False,
            ) + "\n")
            n_groups += 1
            n_emitted_rows += len(docs)
            pos_sizes.append(len(pos))
            neg_sizes.append(len(neg))
            final_sizes.append(len(docs))

    def _stats(xs: list[int]) -> str:
        if not xs:
            return "n=0"
        xs_sorted = sorted(xs)
        return (f"n={len(xs)} min={xs_sorted[0]} "
                f"median={xs_sorted[len(xs)//2]} "
                f"p90={xs_sorted[int(len(xs)*0.9)]} "
                f"max={xs_sorted[-1]}")

    print(f"input rows: {total_in} (skipped malformed: {skipped})")
    print(f"unique queries: {len(groups)}")
    print(f"degenerate (no-pos or no-neg) queries skipped: {n_degenerate}")
    print(f"output listwise groups: {n_groups}")
    print(f"total docs in output: {n_emitted_rows}")
    print(f"positives/group: {_stats(pos_sizes)}")
    print(f"negatives/group (after sampling, max={args.max_negs}): {_stats(neg_sizes)}")
    print(f"docs/group (total): {_stats(final_sizes)}")
    print(f"wrote: {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
