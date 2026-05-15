#!/usr/bin/env python3
"""Generate gold-label CANDIDATES for v12 FT from real MCP queries.

This script does NOT produce training data directly. It produces a labeling
queue: for each selected query it runs the current hybrid_search (v8 + P1c)
and surfaces top-15 candidates grouped by type (doc / code / CI-yml / tests /
other). A human reviewer then annotates each candidate `label in {+, -, ?}`
to build the v12 gold set.

Why candidates not labels: per `feedback_code_rag_judge_bias.md`, no off-the-
shelf judge is neutral on the docs↔code axis. Only human labels give a
trusted v12 gold. Per `feedback_pretrain_sample_check.md`, we MUST hand-check
5 train + 5 test positives before training; this script materializes that
step.

Output:
  profiles/pay-com/v12_candidates.jsonl — one JSON object per (query, candidate)
  with empty `label` field for the reviewer to fill with "+" / "-" / "?".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("ACTIVE_PROFILE", "pay-com")
os.environ.setdefault("CODE_RAG_HOME", str(REPO_ROOT))

_DOC_QUERY_RE = re.compile(
    r"\b(test|tests|spec|specs|docs?|documentation|readme|guide|guides|tutorial|"
    r"checklist|framework|matrix|severity|sandbox|overview|reference|rules)\b",
    re.IGNORECASE,
)
_REPO_QUERY_RE = re.compile(r"\b(repo|deploy|ci|workflow|config)\b", re.IGNORECASE)

_CI_PATH_RE = re.compile(r"(?:^|/)(?:ci/deploy\.ya?ml|k8s/\.github/workflows/)", re.IGNORECASE)
_TEST_PATH_RE = re.compile(r"(?:\.spec\.(?:js|ts|tsx|jsx)$|\.test\.(?:js|ts|tsx|jsx|py)$|_test\.py$|/tests?/)")


def classify_file(file_path: str, file_type: str) -> str:
    if _CI_PATH_RE.search(file_path or ""):
        return "ci-yml"
    if _TEST_PATH_RE.search(file_path or ""):
        return "test"
    if (file_path or "").endswith(".md") or file_type in {
        "doc",
        "docs",
        "reference",
        "dictionary",
        "gotchas",
        "task",
        "provider_doc",
    }:
        return "doc"
    if (file_path or "").endswith((".yml", ".yaml")):
        return "config-yaml"
    return "code"


def select_queries(pool, n_doc, n_repo, n_general):
    doc = [q for q in pool if _DOC_QUERY_RE.search(q)]
    repo = [q for q in pool if _REPO_QUERY_RE.search(q) and not _DOC_QUERY_RE.search(q)]
    short = [q for q in pool if len(q.split()) <= 4 and not _DOC_QUERY_RE.search(q) and not _REPO_QUERY_RE.search(q)]
    general = [q for q in pool if not _DOC_QUERY_RE.search(q) and not _REPO_QUERY_RE.search(q) and len(q.split()) > 4]

    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag, bucket, k in [
        ("doc-intent", doc, n_doc),
        ("repo-intent", repo + short, n_repo),
        ("general", general, n_general),
    ]:
        picked = 0
        for q in bucket:
            if q in seen:
                continue
            seen.add(q)
            selected.append((tag, q))
            picked += 1
            if picked >= k:
                break
    return selected


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--queries", type=Path, default=Path("profiles/pay-com/real_queries/sampled.jsonl"))
    p.add_argument("--output", type=Path, default=Path("profiles/pay-com/v12_candidates.jsonl"))
    p.add_argument("--top-k", type=int, default=15)
    p.add_argument("--n-doc", type=int, default=20)
    p.add_argument("--n-repo", type=int, default=15)
    p.add_argument("--n-general", type=int, default=15)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    if not args.queries.exists():
        print(f"ERROR: queries file not found: {args.queries}", file=sys.stderr)
        return 1

    import random

    rng = random.Random(args.seed)
    queries_raw = [json.loads(line)["query"] for line in args.queries.read_text().splitlines() if line.strip()]
    rng.shuffle(queries_raw)
    selected = select_queries(queries_raw, args.n_doc, args.n_repo, args.n_general)
    print(
        f"selected {len(selected)} queries ({args.n_doc} doc + {args.n_repo} repo + {args.n_general} general)",
        flush=True,
    )

    from src.search.hybrid import hybrid_search

    args.output.parent.mkdir(parents=True, exist_ok=True)
    n_rows = 0
    with args.output.open("w", encoding="utf-8") as out:
        for i, (tag, q) in enumerate(selected, 1):
            t = time.perf_counter()
            try:
                ranked = hybrid_search(q, limit=args.top_k)[0]
            except Exception as e:  # pragma: no cover
                print(f"[{i}/{len(selected)}] ERROR {e}", flush=True)
                continue
            lat = time.perf_counter() - t
            seen_keys: set[str] = set()
            kept: list[dict] = []
            for r in ranked or []:
                key = f"{r.get('repo_name', '')}::{r.get('file_path', '')}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                kept.append(r)
                if len(kept) >= args.top_k:
                    break

            for rank, r in enumerate(kept, 1):
                row = {
                    "query": q,
                    "query_tag": tag,
                    "rank": rank,
                    "repo_name": r.get("repo_name", ""),
                    "file_path": r.get("file_path", ""),
                    "file_type": r.get("file_type", ""),
                    "chunk_type": r.get("chunk_type", ""),
                    "combined_score": r.get("combined_score"),
                    "rerank_score": r.get("rerank_score"),
                    "penalty": r.get("penalty", 0.0),
                    "category": classify_file(r.get("file_path", ""), r.get("file_type", "")),
                    "label": "",
                    "note": "",
                }
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                n_rows += 1

            print(f"[{i}/{len(selected)}] tag={tag} k={len(kept)} lat={lat:.1f}s q={q[:60]!r}", flush=True)

    print(f"\nwrote {n_rows} candidate rows -> {args.output}", flush=True)
    print("Next: review each row, set `label` to '+', '-' or '?', save, then feed", flush=True)
    print("      into a v12 data prep script that reads positive/negative pairs.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
