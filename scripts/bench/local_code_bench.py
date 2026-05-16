"""Local code-intent bench for an FT'd reranker.

Pod cost is dominated by training; the bench step is GPU-light. The code
vector index (db/vectors.lance.coderank, 88 GB) lives only on Mac, so
it's wasteful to scp it to a pod. Instead, after the train pod finishes
HF-pushing the FT'd model, run *this* script locally on Mac to bench
the reranker against code_intent_eval_v1.jsonl (80 queries) using the
local code vector index.

Thin wrapper over scripts/benchmark_doc_intent.py — same metric
(file-level Recall@10), same --rerank-model-path semantics, but swaps
--model=coderank and --eval=code_intent_eval to exercise the code
retrieval path. The eval JSONL schema matches doc_intent_eval_v3 (both
have ``query`` + ``expected_paths=[{repo_name, file_path}]``).

Usage::

    python3 scripts/local_code_bench.py \\
        --candidate-tag=rerank-mxbai \\
        --rerank-model-path=Tarshevskiy/pay-com-rerank-mxbai-ft-run1
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate-tag", required=True)
    p.add_argument(
        "--rerank-model-path",
        required=True,
        help="HF Hub id (Tarshevskiy/...) OR local path to FT'd reranker dir.",
    )
    p.add_argument(
        "--eval",
        type=Path,
        default=REPO_ROOT / "profiles" / "pay-com" / "code_intent_eval_v1.jsonl",
    )
    p.add_argument("--model", default="coderank")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    if not args.eval.is_file():
        sys.exit(f"--eval missing: {args.eval}")

    out_path = args.out or REPO_ROOT / "bench_runs" / f"{args.candidate_tag}_code_bench.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "benchmark_doc_intent.py"),
        f"--eval={args.eval}",
        f"--model={args.model}",
        "--rerank-on",
        f"--rerank-model-path={args.rerank_model_path}",
        f"--out={out_path}",
    ]

    env = {**os.environ, "CODE_RAG_HOME": str(REPO_ROOT)}
    print(f"[{time.strftime('%H:%M:%S')}] code bench {args.candidate_tag}", flush=True)
    print(f"  eval: {args.eval}")
    print(f"  retrieval: {args.model}")
    print(f"  reranker: {args.rerank_model_path}")
    print(f"  out: {out_path}")
    t0 = time.time()
    cp = subprocess.run(cmd, env=env)
    elapsed = time.time() - t0
    print(f"[{time.strftime('%H:%M:%S')}] done in {elapsed:.0f}s rc={cp.returncode}", flush=True)
    return cp.returncode


if __name__ == "__main__":
    sys.exit(main())
