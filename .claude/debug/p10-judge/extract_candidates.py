#!/usr/bin/env python3.12
"""Extract (query, top-10 candidates with snippets) bundles for LLM judging.

Pulls bench artifacts that already contain top-10 paths per query, then
re-fetches the actual chunk text from `db/knowledge.db` (the largest
content-bearing chunk per file) so a human/LLM judge can score relevance.

Outputs:
- /tmp/p10_judge_g1_bundle.json  # 14 risk queries, both off/on top-10
- /tmp/p10_judge_g2_bundle.json  # 50 random queries (seed=42), off only
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
from pathlib import Path

ROOT = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".code-rag-mcp")))

# Bench artifacts (rerank-off and rerank-on on eval-v3-n200, model=docs)
BENCH_OFF = ROOT / "bench_runs" / "doc_intent_summary_20260426T005758.json"
BENCH_ON = ROOT / "bench_runs" / "doc_intent_summary_20260426T005625.json"
EVAL_V3 = ROOT / "profiles" / "pay-com" / "doc_intent_eval_v3_n200.jsonl"
NEG_FILE = Path("/tmp/p10_reorder_negatives.json")

DB = ROOT / "db" / "knowledge.db"

SNIPPET_CAP = 1500


def load_bench(path: Path) -> dict[str, list[dict]]:
    """Return {query: top_files (list of {repo_name,file_path,score,rank})}."""
    rows = json.load(open(path))[0]["eval_per_query"]
    return {r["query"]: r for r in rows}


def fetch_snippet(conn: sqlite3.Connection, repo: str, fp: str) -> str:
    """Return the longest content chunk for (repo, file_path), capped."""
    cur = conn.cursor()
    cur.execute(
        "SELECT content FROM chunks WHERE repo_name=? AND file_path=? ORDER BY length(content) DESC LIMIT 1",
        (repo, fp),
    )
    row = cur.fetchone()
    if not row:
        # Try fuzzier match
        cur.execute(
            "SELECT content FROM chunks WHERE repo_name=? AND file_path LIKE ? ORDER BY length(content) DESC LIMIT 1",
            (repo, "%" + fp.split("/")[-1]),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return ""
    text = row[0]
    if len(text) > SNIPPET_CAP:
        text = text[:SNIPPET_CAP] + "...[truncated]"
    return text


def build_g1():
    """14 risk queries from p10-quickwin-report (off>0, on<off, ie negatives).

    These are the ones where disabling the reranker LOSES ranking.
    """
    negs = json.load(open(NEG_FILE))
    # Top 14 by absolute on_r10 (from the report's table)
    # The report cites 14 specific queries; we replicate by ranking on_r10 desc
    # then by delta (we want "lost docs that have value").
    # Actually report says "14 queries that hit@10 ONLY thanks to reranker" =
    # rows where on_r10 > 0 and off_r10 = 0. Filter accordingly.
    off_bench = load_bench(BENCH_OFF)
    on_bench = load_bench(BENCH_ON)
    risk = []
    for n in negs:
        q = n["query"]
        if q not in off_bench or q not in on_bench:
            continue
        off_r = n["off_r10"]
        on_r = n["on_r10"]
        # The report's risk-table = "currently hit@10 ONLY thanks to reranker"
        # i.e. on_r10 > 0 and off_r10 = 0. The negs file is queries where
        # rerank-off is BETTER (off > on). Looking at neg list, none of them
        # match "off=0 on>0". So the 14 risk queries must be in a DIFFERENT
        # file — let me recompute from bench: queries where on_r10 > off_r10.
        pass
    return None


def find_risk_queries():
    """Recompute the 14 risk queries: on_r10 > off_r10 (reranker gains)."""
    off_bench = load_bench(BENCH_OFF)
    on_bench = load_bench(BENCH_ON)
    risks = []
    for q, off_row in off_bench.items():
        on_row = on_bench.get(q)
        if not on_row:
            continue
        off_r = off_row["recall_at_10"]
        on_r = on_row["recall_at_10"]
        if on_r > off_r:
            risks.append(
                {
                    "query": q,
                    "off_r10": off_r,
                    "on_r10": on_r,
                    "delta_on_minus_off": round(on_r - off_r, 4),
                    "strata": off_row.get("strata", []),
                    "expected_paths": off_row.get("expected_paths", []),
                }
            )
    risks.sort(key=lambda r: (-r["on_r10"], -r["delta_on_minus_off"]))
    return risks


def build_judging_bundle(queries: list[str], off_bench: dict, on_bench: dict, conn: sqlite3.Connection, label: str):
    """For each query, build the judging bundle with snippets."""
    bundle = []
    for q in queries:
        off_row = off_bench.get(q)
        on_row = on_bench.get(q) if on_bench else None
        if not off_row:
            continue
        item = {
            "query": q,
            "strata": off_row.get("strata", []),
            "expected_paths": [tuple(p) if isinstance(p, list) else p for p in off_row.get("expected_paths", [])],
            "off": [],
            "on": [],
        }
        if off_row:
            item["off_r10_heuristic"] = off_row.get("recall_at_10")
            for tf in off_row["top_files"][:10]:
                snippet = fetch_snippet(conn, tf["repo_name"], tf["file_path"])
                item["off"].append(
                    {
                        "rank": tf["rank"],
                        "repo_name": tf["repo_name"],
                        "file_path": tf["file_path"],
                        "in_expected": [tf["repo_name"], tf["file_path"]]
                        in [list(p) if isinstance(p, tuple) else p for p in item["expected_paths"]],
                        "snippet": snippet,
                    }
                )
        if on_row:
            item["on_r10_heuristic"] = on_row.get("recall_at_10")
            for tf in on_row["top_files"][:10]:
                snippet = fetch_snippet(conn, tf["repo_name"], tf["file_path"])
                item["on"].append(
                    {
                        "rank": tf["rank"],
                        "repo_name": tf["repo_name"],
                        "file_path": tf["file_path"],
                        "in_expected": [tf["repo_name"], tf["file_path"]]
                        in [list(p) if isinstance(p, tuple) else p for p in item["expected_paths"]],
                        "snippet": snippet,
                    }
                )
        bundle.append(item)
    return bundle


def main():
    off_bench = load_bench(BENCH_OFF)
    on_bench = load_bench(BENCH_ON)
    print(f"loaded off: {len(off_bench)} queries, on: {len(on_bench)} queries")

    risks = find_risk_queries()
    print(f"found {len(risks)} risk queries (on_r10 > off_r10)")

    conn = sqlite3.connect(str(DB))

    # G1 bundle: top 14 by on_r10
    top14 = [r["query"] for r in risks[:14]]
    g1 = build_judging_bundle(top14, off_bench, on_bench, conn, "G1")
    Path("/tmp/p10_judge_g1_bundle.json").write_text(json.dumps(g1, indent=2))
    print(f"wrote G1 bundle: {len(g1)} queries x 20 candidates each")

    # G2 calibration: 50 random queries (seed=42)
    rng = random.Random(42)
    all_queries = list(off_bench.keys())
    sample_50 = rng.sample(all_queries, 50)
    g2 = build_judging_bundle(sample_50, off_bench, None, conn, "G2")
    Path("/tmp/p10_judge_g2_bundle.json").write_text(json.dumps(g2, indent=2))
    print(f"wrote G2 bundle: {len(g2)} queries x 10 candidates each")

    conn.close()


if __name__ == "__main__":
    main()
