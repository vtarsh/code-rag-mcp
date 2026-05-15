#!/usr/bin/env python3
"""Post-process scripts/churn_replay output.

Slices the per-query metrics by query characteristics (length, token shape,
top-1 changed) and emits:
  1. Human-readable Markdown summary to stdout (or --report).
  2. JSONL of the most interesting diff pairs, for LLM-as-judge
     (scripts/churn_llm_judge.py or manual review).

No new dependencies — stdlib + the churn JSON produced by churn_replay.py.

Usage:
  python3.12 scripts/analyze_churn.py \
    --input profiles/pay-com/churn_replay/v8_vs_base.json \
    --diff-pairs profiles/pay-com/churn_replay/diff_pairs.jsonl \
    --report profiles/pay-com/churn_replay/report.md

Query slices:
  - length: short (<=3 tokens) / medium (4-8) / long (>8)
  - uppercase_id: query contains an UPPERCASE_IDENTIFIER token (env var / enum)
  - jira_prefix: BO-XXX / PI-XX / CORE-XXX prefix → "already-seen in finetune data"
  - doc_keyword: query contains docs/test/guide/readme/tutorial

Diff-pair export ranks queries by churn magnitude (1 - overlap@10) so the top
export contains the highest-leverage examples for LLM-judge or human review.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections.abc import Iterable
from pathlib import Path

_UPPER_TOK_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_JIRA_PREFIX_RE = re.compile(r"\b(BO|PI|CORE|HS)-\d+\b")
_DOC_KW_RE = re.compile(r"\b(docs?|test|tests|spec|specs|guide|guides|tutorial|readme)\b", re.IGNORECASE)


def _classify(query: str) -> dict:
    toks = query.split()
    return {
        "length_bucket": "short" if len(toks) <= 3 else ("long" if len(toks) > 8 else "medium"),
        "has_uppercase_id": bool(_UPPER_TOK_RE.search(query)),
        "has_jira_prefix": bool(_JIRA_PREFIX_RE.search(query)),
        "has_doc_keyword": bool(_DOC_KW_RE.search(query)),
        "n_tokens": len(toks),
    }


def _overlap_at(q: dict, k: int) -> float | None:
    v = q.get("overlap_at_k", {})
    # JSON stringifies int keys — handle both forms.
    return v.get(str(k), v.get(k))


def _slice_stats(entries: list[dict], pred, label: str, k: int) -> dict:
    subset = [e for e in entries if pred(e)]
    if not subset:
        return {"label": label, "n": 0}
    overlaps = [_overlap_at(e, k) for e in subset]
    overlaps = [o for o in overlaps if o is not None]
    top1 = [1 for e in subset if e.get("top1_changed")]
    return {
        "label": label,
        "n": len(subset),
        f"mean_overlap_at_{k}": round(statistics.fmean(overlaps), 4) if overlaps else None,
        f"median_overlap_at_{k}": round(statistics.median(overlaps), 4) if overlaps else None,
        "pct_top1_changed": round(100.0 * sum(top1) / len(subset), 2),
    }


def _aggregate_slices(per_query: list[dict], top_k: int) -> list[dict]:
    enriched = []
    for e in per_query:
        e2 = dict(e)
        e2["_class"] = _classify(e["query"])
        enriched.append(e2)

    slices: list[dict] = []

    # Overall
    slices.append(_slice_stats(enriched, lambda _: True, "overall", top_k))

    # By length bucket
    for bucket in ("short", "medium", "long"):
        slices.append(
            _slice_stats(enriched, lambda e, b=bucket: e["_class"]["length_bucket"] == b, f"length={bucket}", top_k)
        )

    # Boolean flags
    for flag, label in [
        ("has_uppercase_id", "uppercase_id"),
        ("has_jira_prefix", "jira_prefix"),
        ("has_doc_keyword", "doc_keyword"),
    ]:
        slices.append(_slice_stats(enriched, lambda e, f=flag: e["_class"][f], f"{label}=True", top_k))
        slices.append(_slice_stats(enriched, lambda e, f=flag: not e["_class"][f], f"{label}=False", top_k))

    return slices


def _top_diff_pairs(per_query: list[dict], top_k: int, limit: int) -> list[dict]:
    def churn_magnitude(e: dict) -> float:
        o = _overlap_at(e, top_k)
        return 1.0 - (o if o is not None else 1.0)

    ranked = sorted(per_query, key=churn_magnitude, reverse=True)
    out = []
    for e in ranked[:limit]:
        out.append(
            {
                "query": e["query"],
                "overlap_at_10": _overlap_at(e, top_k),
                "top1_changed": e.get("top1_changed"),
                "base_top10": e.get("base_top10", []),
                "v8_top10": e.get("v8_top10", []),
                "base_top10_keys": e.get("base_top10_keys", []),
                "v8_top10_keys": e.get("v8_top10_keys", []),
            }
        )
    return out


def _fmt_slice_table(slices: list[dict], k: int) -> str:
    mean_key = f"mean_overlap_at_{k}"
    median_key = f"median_overlap_at_{k}"
    header = f"| slice | n | mean_overlap@{k} | median_overlap@{k} | pct_top1_changed |\n|---|---:|---:|---:|---:|"
    rows = []
    for s in slices:
        n = s.get("n", 0)
        if n == 0:
            rows.append(f"| {s['label']} | 0 | - | - | - |")
            continue
        rows.append(
            f"| {s['label']} | {n} | {s.get(mean_key, '-')} | {s.get(median_key, '-')} | {s.get('pct_top1_changed', '-')} |"
        )
    return header + "\n" + "\n".join(rows)


def _fmt_diff_pairs_md(pairs: Iterable[dict], top_n: int) -> str:
    out = []
    for i, p in enumerate(list(pairs)[:top_n], 1):
        out.append(f"### #{i} — overlap@10={p['overlap_at_10']}, top1_changed={p['top1_changed']}\n")
        out.append(f"**Query:** `{p['query']}`\n")
        out.append("**base top-5:**")
        for j, key in enumerate(p.get("base_top10_keys", [])[:5], 1):
            out.append(f"  {j}. {key}")
        out.append("\n**v8 top-5:**")
        for j, key in enumerate(p.get("v8_top10_keys", [])[:5], 1):
            out.append(f"  {j}. {key}")
        out.append("\n---\n")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--input", type=Path, default=Path("profiles/pay-com/churn_replay/v8_vs_base.json"))
    p.add_argument("--diff-pairs", type=Path, default=Path("profiles/pay-com/churn_replay/diff_pairs.jsonl"))
    p.add_argument("--report", type=Path, default=None, help="write markdown report to path (default: stdout)")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--diff-limit", type=int, default=50, help="top N most-churn queries to export")
    p.add_argument("--report-preview", type=int, default=10, help="how many diff pairs to show in report")
    args = p.parse_args()

    if not args.input.exists():
        raise SystemExit(f"ERROR: input not found: {args.input}")

    data = json.loads(args.input.read_text(encoding="utf-8"))
    per_query = data.get("per_query") or []
    summary = data.get("summary") or {}
    config = data.get("config") or {}
    if not per_query:
        raise SystemExit("ERROR: no per_query entries in input")

    slices = _aggregate_slices(per_query, args.top_k)
    pairs = _top_diff_pairs(per_query, args.top_k, args.diff_limit)

    # Write diff pairs JSONL
    args.diff_pairs.parent.mkdir(parents=True, exist_ok=True)
    with args.diff_pairs.open("w", encoding="utf-8") as f:
        for p_ in pairs:
            f.write(json.dumps(p_, ensure_ascii=False) + "\n")

    # Report
    report_parts: list[str] = []
    report_parts.append("# Churn Replay Analysis\n")
    report_parts.append(f"**Source:** `{args.input}`")
    report_parts.append(f"**n_queries:** {config.get('n_queries', len(per_query))}")
    report_parts.append(f"**base:** `{config.get('base_model', '?')}`")
    report_parts.append(f"**v8:** `{config.get('v8_model', '?')}`\n")

    report_parts.append("## Overall Summary\n")
    for k, v in summary.items():
        report_parts.append(f"- **{k}**: {v}")
    report_parts.append("")

    report_parts.append("## Slices\n")
    report_parts.append(_fmt_slice_table(slices, args.top_k))
    report_parts.append("")

    report_parts.append(f"## Top {args.report_preview} Diff Pairs (highest churn)\n")
    report_parts.append(_fmt_diff_pairs_md(pairs, args.report_preview))

    report_parts.append("\n## Outputs\n")
    report_parts.append(f"- Diff pairs JSONL: `{args.diff_pairs}` ({len(pairs)} entries)")

    report = "\n".join(report_parts) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"report written to: {args.report}")
    else:
        print(report)

    print(f"diff pairs: {args.diff_pairs} ({len(pairs)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
