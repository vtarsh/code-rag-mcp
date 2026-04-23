#!/usr/bin/env python3
"""Regenerate v12 candidates for doc-intent queries only.

Rationale (V-A audit 2026-04-22): `v12_candidates.jsonl` has <2 `category=doc`
rows in the top-15 for several doc-intent queries (e.g. "provider docs":
0 docs / 15 code). The v8 FT reranker outranks docs even when the P1c
penalty layer is disabled — see `project_p1c_validation_measured.md`:
"the v8 raw rerank score on code is simply higher than on the doc, and
no penalty tuning will recover those — they are explicit v12 FT training
targets." As a result, ~20% of the labeling pool has structurally zero
doc positives REACHABLE, so human labeling alone cannot build a v12
training set that balances the docs↔code axis.

This script regenerates candidates for doc-intent queries using:
  1. CODE_RAG_DISABLE_PENALTIES=1 — turns `_classify_penalty` into a no-op
     (zeroes GUIDE / TEST / CI / DOC penalties).
  2. Baseline reranker (`Alibaba-NLP/gte-reranker-modernbert-base`) injected
     via `hybrid_search(..., reranker_override=...)` instead of the prod
     v8 FT reranker. The base ModernBERT reranker is docs-neutral; the v8
     FT checkpoint was trained on Jira diff/chunk pairs and learned a
     code-biased ordering.
  3. Full hybrid retrieval pool (FTS5 + vector + code_facts + env_vars) —
     we do NOT drop lexical because short queries ("provider docs")
     return too few vector hits on their own.

Non-doc queries are copied unchanged from `v12_candidates.jsonl` so the
labeler's existing work (if any) is preserved.

Output:
  profiles/pay-com/v12_candidates_regen.jsonl — new file, alongside the
  existing `v12_candidates.jsonl`. Operator compares side-by-side before
  overwriting.

Usage:
  python3.12 scripts/v12_candidates_regen_doc.py
  # or with custom paths:
  python3.12 scripts/v12_candidates_regen_doc.py \
      --input profiles/pay-com/v12_candidates.jsonl \
      --output profiles/pay-com/v12_candidates_regen.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import OrderedDict, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Env overrides — MUST be set before importing src.search.hybrid (module-level
# _DISABLE_PENALTIES is read once at import time).
os.environ.setdefault("ACTIVE_PROFILE", "pay-com")
os.environ.setdefault("CODE_RAG_HOME", str(REPO_ROOT))
os.environ["CODE_RAG_DISABLE_PENALTIES"] = "1"

# Keep code_facts/env_vars enabled — they widen the doc-retrieval surface too
# (e.g. code_facts_fts indexes doc-section facts in provider_doc pages).

# Use production `_DOC_QUERY_RE` from hybrid.py as single source of truth.
# Any query that matches is treated as doc-intent.
_DOC_QUERY_RE = re.compile(
    r"\b("
    r"test|tests|spec|specs|"
    r"docs?|documentation|readme|guide|guides|tutorial|"
    r"checklist|framework|matrix|severity|sandbox|overview|reference|rules"
    r")\b",
    re.IGNORECASE,
)

_CI_PATH_RE = re.compile(r"(?:^|/)(?:ci/deploy\.ya?ml|k8s/\.github/workflows/)", re.IGNORECASE)
_TEST_PATH_RE = re.compile(r"(?:\.spec\.(?:js|ts|tsx|jsx)$|\.test\.(?:js|ts|tsx|jsx|py)$|_test\.py$|/tests?/)")


def classify_file(file_path: str, file_type: str) -> str:
    """Same category logic as `scripts/v12_candidates.py`."""
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


def load_existing(path: Path) -> "OrderedDict[str, list[dict]]":
    """Load existing candidates grouped by query, preserving input order."""
    by_q: OrderedDict[str, list[dict]] = OrderedDict()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        by_q.setdefault(row["query"], []).append(row)
    return by_q


def _cat_breakdown(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.get("category", "?")] += 1
    return dict(counts)


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--input",
        type=Path,
        default=Path("profiles/pay-com/v12_candidates.jsonl"),
        help="Existing candidate file to read (doc queries will be regenerated, others copied).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("profiles/pay-com/v12_candidates_regen.jsonl"),
        help="Output file. Writes alongside input — does NOT overwrite by default.",
    )
    p.add_argument("--top-k", type=int, default=15)
    p.add_argument(
        "--base-reranker",
        default="Alibaba-NLP/gte-reranker-modernbert-base",
        help="HF model id used as neutral reranker for the doc-intent pass.",
    )
    args = p.parse_args()

    if not args.input.exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1

    # Import AFTER env is set so _DISABLE_PENALTIES=True in the module.
    from src.embedding_provider import LocalRerankerProvider
    from src.search.hybrid import _DISABLE_PENALTIES, hybrid_search

    assert _DISABLE_PENALTIES, "CODE_RAG_DISABLE_PENALTIES=1 did not take effect"

    by_q = load_existing(args.input)
    doc_queries: list[str] = [q for q in by_q if _DOC_QUERY_RE.search(q)]
    non_doc_queries: list[str] = [q for q in by_q if not _DOC_QUERY_RE.search(q)]
    print(
        f"loaded {len(by_q)} queries from {args.input}: "
        f"doc-intent={len(doc_queries)}, non-doc={len(non_doc_queries)}",
        flush=True,
    )
    print("doc-intent queries:", flush=True)
    for q in doc_queries:
        before = _cat_breakdown(by_q[q])
        print(f"  before: doc={before.get('doc', 0):2d} code={before.get('code', 0):2d} | {q}", flush=True)

    # Instantiate neutral reranker once, inject via override.
    print(f"\nloading baseline reranker: {args.base_reranker} ...", flush=True)
    neutral_rr = LocalRerankerProvider(model_name=args.base_reranker)
    # Warm the model so per-query latency reflects inference, not load.
    _ = neutral_rr.rerank("warmup", ["hello world"], limit=1)
    print("reranker ready\n", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    delta_report: list[dict] = []
    zero_doc_after: list[str] = []
    n_rows_total = 0

    with args.output.open("w", encoding="utf-8") as out:
        # Preserve input query order — iterate by_q.
        for q, old_rows in by_q.items():
            if q in doc_queries:
                # Regenerate.
                tag = old_rows[0].get("query_tag", "doc-intent")
                t = time.perf_counter()
                try:
                    ranked, _vec_err, _total = hybrid_search(
                        q,
                        limit=args.top_k,
                        reranker_override=neutral_rr,
                    )
                except Exception as e:
                    print(f"  ERROR {q[:60]!r}: {e}", file=sys.stderr, flush=True)
                    # Fall back to keeping old rows so the labeler loses nothing.
                    for row in old_rows:
                        out.write(json.dumps(row, ensure_ascii=False) + "\n")
                        n_rows_total += 1
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

                new_rows: list[dict] = []
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
                        "regen_source": "neutral_reranker_no_penalties",
                    }
                    new_rows.append(row)
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    n_rows_total += 1

                before = _cat_breakdown(old_rows)
                after = _cat_breakdown(new_rows)
                delta_report.append(
                    {
                        "query": q,
                        "before_doc": before.get("doc", 0),
                        "before_total": len(old_rows),
                        "after_doc": after.get("doc", 0),
                        "after_total": len(new_rows),
                        "lat_s": round(lat, 2),
                    }
                )
                if after.get("doc", 0) == 0:
                    zero_doc_after.append(q)
                print(
                    f"  [doc] {q[:70]!r:72s}  "
                    f"before={before.get('doc', 0):2d}/{len(old_rows):2d}  "
                    f"after={after.get('doc', 0):2d}/{len(new_rows):2d}  "
                    f"({lat:.1f}s)",
                    flush=True,
                )
            else:
                # Copy as-is, stripping any stale regen marker.
                for row in old_rows:
                    row.pop("regen_source", None)
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    n_rows_total += 1
                print(f"  [skip] {q[:70]!r}  ({len(old_rows)} rows unchanged)", flush=True)

    # Summary
    print(f"\nwrote {n_rows_total} rows -> {args.output}", flush=True)
    print("\n=== delta report (doc-intent only) ===", flush=True)
    print(f"{'before_doc/total':>18}  {'after_doc/total':>18}  query", flush=True)
    n_recovered = 0
    for d in delta_report:
        bstr = f"{d['before_doc']}/{d['before_total']}"
        astr = f"{d['after_doc']}/{d['after_total']}"
        if d["after_doc"] >= 2 and d["before_doc"] < 2:
            n_recovered += 1
        print(f"{bstr:>18}  {astr:>18}  {d['query']}", flush=True)
    print(
        f"\nrecovered (before<2, after>=2): {n_recovered}/"
        f"{sum(1 for d in delta_report if d['before_doc'] < 2)} queries with <2 docs",
        flush=True,
    )
    if zero_doc_after:
        print("\nWARNING — still ZERO docs after regen (blind-spot candidates):", flush=True)
        for q in zero_doc_after:
            print(f"  - {q}", flush=True)
    print(
        "\nNext: diff profiles/pay-com/v12_candidates.jsonl vs v12_candidates_regen.jsonl,"
        "\n      spot-check top-3 per doc query, commit when satisfied.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
