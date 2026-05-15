#!/usr/bin/env python3
"""Benchmark runner for ``bench_v2.yaml`` (proposal §4).

Loads the labeled YAML, runs hybrid search on every row where
``answerable in {yes, partial}``, and computes per-query / per-stratum:

  * file_recall@10  = |gt_files ∩ top-10 returned| / |gt_files|
  * file_hit@5      = 1 if any gt_file in top-5 else 0
  * file_mrr        = 1 / rank of first gt hit (top-20, else 0)
  * keyword_recall  = |gt_symbols in top-10 snippets| / |gt_symbols|

Outputs:

  * ``$CODE_RAG_HOME/benchmark_bench_v2_results.json`` (primary)
  * Optional markdown summary via ``--markdown <path>``.

Exit code is always 0 on successful run — gating is ``bench_v2_gate.py``'s job.
Usage::

    python3 scripts/benchmark_bench_v2.py \
        --input profiles/pay-com/bench/bench_v2.yaml \
        --out ~/.code-rag/benchmark_bench_v2_results.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Top-5 providers we separately report strata for (proposal §5).
TOP5_PROVIDERS = ("payper", "nuvei", "interac", "trustly", "paynearme")


# ---------------------------------------------------------------------------
# Metric primitives
# ---------------------------------------------------------------------------


def _normalize_gt(gt: str) -> str:
    """GT format is ``repo::path``; tolerate leading/trailing whitespace."""
    return (gt or "").strip().lower()


def _result_key(result: dict) -> str:
    """Key used for intersection with gt_files — must match GT format."""
    repo = (result.get("repo_name") or "").strip().lower()
    path = (result.get("file_path") or "").strip().lower()
    return f"{repo}::{path}"


def file_recall_at_10(gt_files: list[str], results: list[dict]) -> float:
    gt = {_normalize_gt(f) for f in gt_files if f}
    if not gt:
        return 0.0
    top = {_result_key(r) for r in results[:10]}
    return len(gt & top) / len(gt)


def file_hit_at_5(gt_files: list[str], results: list[dict]) -> int:
    gt = {_normalize_gt(f) for f in gt_files if f}
    if not gt:
        return 0
    top = {_result_key(r) for r in results[:5]}
    return 1 if gt & top else 0


def file_mrr(gt_files: list[str], results: list[dict], limit: int = 20) -> float:
    gt = {_normalize_gt(f) for f in gt_files if f}
    if not gt:
        return 0.0
    for i, r in enumerate(results[:limit], 1):
        if _result_key(r) in gt:
            return 1.0 / i
    return 0.0


def keyword_recall(gt_symbols: list[str], results: list[dict], top_n: int = 10) -> float:
    symbols = [s for s in gt_symbols if s]
    if not symbols:
        return 1.0  # No symbols requested = trivially perfect (excluded from mean).
    snippets = " ".join((r.get("snippet") or "") for r in results[:top_n]).lower()
    hit = sum(1 for s in symbols if s.lower() in snippets)
    return hit / len(symbols)


# ---------------------------------------------------------------------------
# Hybrid search invocation (isolated so tests can stub)
# ---------------------------------------------------------------------------


def run_query(query: str, *, limit: int = 20) -> tuple[list[dict], str | None]:
    """Wrap ``hybrid_search`` — returns ranked results and optional error string."""
    try:
        from src.search.fts import expand_query
        from src.search.hybrid import hybrid_search

        expanded = expand_query(query)
        ranked, err, _total = hybrid_search(expanded, limit=limit)
        return (ranked or []), err
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _strata_keys(row: dict) -> list[str]:
    out = [
        f"intent:{row['intent']}",
        f"length:{row['length_bucket']}",
    ]
    prov = row.get("provider")
    if prov and prov in TOP5_PROVIDERS:
        out.append(f"provider:{prov}")
    if not prov:
        out.append("provider:none")
    return out


def aggregate(per_query: list[dict]) -> dict:
    """Build the stratum summary consumed by ``bench_v2_gate.py``."""
    overall_rec = [q["file_recall@10"] for q in per_query if q["counts"]]
    overall = {
        "file_recall@10": sum(overall_rec) / len(overall_rec) if overall_rec else 0.0,
        "file_hit@5": (sum(q["file_hit@5"] for q in per_query) / len(per_query) if per_query else 0.0),
        "file_mrr": (sum(q["file_mrr"] for q in per_query) / len(per_query) if per_query else 0.0),
        "keyword_recall": (sum(q["keyword_recall"] for q in per_query) / len(per_query) if per_query else 0.0),
        "count": len(per_query),
    }
    buckets: dict[str, list[dict]] = defaultdict(list)
    for q in per_query:
        for key in q["strata"]:
            buckets[key].append(q)
    strata: dict[str, dict] = {}
    for key, rows in buckets.items():
        rec = [r["file_recall@10"] for r in rows if r["counts"]]
        if not rec:
            continue
        strata[key] = {
            "file_recall@10": sum(rec) / len(rec),
            "file_hit@5": sum(r["file_hit@5"] for r in rows) / len(rows),
            "file_mrr": sum(r["file_mrr"] for r in rows) / len(rows),
            "count": len(rows),
        }
    return {"overall": overall, "strata": strata}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run(bench: dict, *, verbose: bool = False) -> dict:
    queries = bench.get("queries") or []

    # Hygiene counter: answerable=no rows whose top-10 contains any gt_file hint.
    unanswerable_hits = 0
    per_query: list[dict] = []
    skipped = Counter()

    for row in queries:
        answerable = row.get("answerable")
        if answerable not in ("yes", "partial"):
            skipped[str(answerable)] += 1
            # Hygiene: if labeler set answerable=no but attached gt_files AND we
            # still hit them, flag as dataset noise.
            if answerable == "no" and row.get("gt_files"):
                ranked, _err = run_query(row["query"])
                if ranked and file_recall_at_10(row["gt_files"], ranked) > 0:
                    unanswerable_hits += 1
            continue

        gt_files = row.get("gt_files") or []
        gt_symbols = row.get("gt_symbols") or []
        ranked, err = run_query(row["query"])
        if err and verbose:
            print(f"  [warn] {row['id']}: {err}", file=sys.stderr)

        rec = file_recall_at_10(gt_files, ranked) if gt_files else 0.0
        hit = file_hit_at_5(gt_files, ranked) if gt_files else 0
        mrr = file_mrr(gt_files, ranked) if gt_files else 0.0
        kw = keyword_recall(gt_symbols, ranked)

        per_query.append(
            {
                "id": row["id"],
                "query": row["query"],
                "answerable": answerable,
                "intent": row["intent"],
                "length_bucket": row["length_bucket"],
                "provider": row.get("provider"),
                "file_recall@10": rec,
                "file_hit@5": hit,
                "file_mrr": mrr,
                "keyword_recall": kw,
                "counts": bool(gt_files),  # only rows with gt_files drive file_recall mean
                "strata": _strata_keys(row),
                "err": err,
            }
        )

    report = aggregate(per_query)
    report["per_query"] = per_query
    report["skipped"] = dict(skipped)
    report["hygiene"] = {"unanswerable_hits": unanswerable_hits}
    report["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return report


def _markdown_summary(report: dict) -> str:
    ov = report["overall"]
    lines = [
        "# bench_v2 results",
        "",
        f"- Generated: {report['timestamp']}",
        f"- Rows evaluated: {ov['count']}",
        f"- Skipped: {report.get('skipped')}",
        "",
        "## Overall",
        "",
        f"- file_recall@10: **{ov['file_recall@10']:.3f}**",
        f"- file_hit@5:     {ov['file_hit@5']:.3f}",
        f"- file_mrr:       {ov['file_mrr']:.3f}",
        f"- keyword_recall: {ov['keyword_recall']:.3f}",
        "",
        "## Per stratum",
        "",
        "| stratum | count | file_recall@10 | file_hit@5 | file_mrr |",
        "|---|---:|---:|---:|---:|",
    ]
    for key, block in sorted(report["strata"].items()):
        lines.append(
            f"| {key} | {block['count']} | {block['file_recall@10']:.3f} | "
            f"{block['file_hit@5']:.3f} | {block['file_mrr']:.3f} |"
        )
    hyg = report.get("hygiene") or {}
    lines.append("")
    lines.append(f"Hygiene — unanswerable_hits: {hyg.get('unanswerable_hits', 0)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--input",
        type=Path,
        default=Path("profiles/pay-com/bench/bench_v2.yaml"),
    )
    default_out = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".code-rag"))) / "benchmark_bench_v2_results.json"
    p.add_argument("--out", type=Path, default=default_out)
    p.add_argument("--markdown", type=Path, default=None)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if not args.input.exists():
        print(f"ERROR: bench yaml not found: {args.input}", file=sys.stderr)
        return 1
    bench = yaml.safe_load(args.input.read_text(encoding="utf-8")) or {}
    if (bench.get("version") or 0) != 2:
        print(f"WARN: expected version: 2 in {args.input}, got {bench.get('version')!r}", file=sys.stderr)

    t0 = time.time()
    report = run(bench, verbose=args.verbose)
    report["elapsed_s"] = round(time.time() - t0, 2)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {args.out} ({report['overall']['count']} rows in {report['elapsed_s']}s)")

    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(_markdown_summary(report), encoding="utf-8")
        print(f"wrote {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
