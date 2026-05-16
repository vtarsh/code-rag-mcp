#!/usr/bin/env python3
"""Benchmark large embedding models (>3 GB RAM) on the doc-intent eval set.

Skeleton — to be implemented when we test stella_en_1.5B_v5 / linq-embed-mistral
or similar models that don't fit on the 16 GB Mac.

Reuses methodology from scripts/benchmark_doc_intent.py: bypass router +
reranker, file-level Recall@10, dual-judge unlabeled top-10.
"""

from __future__ import annotations

import argparse
import sys
from typing import Final

CANDIDATES: Final[dict[str, str]] = {
    "stella_en_1.5B_v5":  "dunzhang/stella_en_1.5B_v5",
    "linq-embed-mistral": "Linq-AI-Research/Linq-Embed-Mistral",
    "qwen3-embedding-8b": "Qwen/Qwen3-Embedding-8B",
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Bench large embedding models on RunPod"
    )
    p.add_argument("--model", choices=list(CANDIDATES), required=True)
    p.add_argument(
        "--eval-set", required=True,
        help="JSONL with {query, expected_path} rows",
    )
    p.add_argument("--top-k", type=int, default=10)
    args = p.parse_args(argv)

    print(
        f"[bench] model={args.model} ({CANDIDATES[args.model]}) "
        f"eval_set={args.eval_set} k={args.top_k}"
    )
    print("[bench] not yet implemented — pod must have model downloaded first")
    return 1


if __name__ == "__main__":
    sys.exit(main())
