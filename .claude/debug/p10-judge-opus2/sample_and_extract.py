#!/usr/bin/env python3.12
"""Judge #2 sampler + bundle builder for P10 A2 verification.

- Stratified sample of 30 queries from doc_intent_eval_v3_n200 (with expected_paths)
- seed=42
- OFF strata bucket: nuvei, aircash, trustly, webhook, refund (rerank-skipped in A2)
- KEEP strata bucket: interac, provider (rerank kept in A2)
- UNKNOWN strata bucket: tail, payout, method
- Pull top-10 per query from BOTH /tmp/p10_a2_stratum_gated.json and
  /tmp/p10_rerank_on_parity.json
- Snippets from db/knowledge.db, capped 1500 chars
- Persist:
    /tmp/p10_a2_judge_query_ids_opus2.json  (sampled query_ids)
    /tmp/p10_a2_judge_bundle_opus2.json     (full judging bundle)
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

ROOT = Path("/Users/vaceslavtarsevskij/.code-rag-mcp")
EVAL_V3 = ROOT / "profiles" / "pay-com" / "doc_intent_eval_v3_n200.jsonl"
A2 = Path("/tmp/p10_a2_stratum_gated.json")
ON = Path("/tmp/p10_rerank_on_parity.json")
DB = ROOT / "db" / "knowledge.db"
SNIPPET_CAP = 1500

OFF_STRATA = {"nuvei", "aircash", "trustly", "webhook", "refund"}
KEEP_STRATA = {"interac", "provider"}
UNK_STRATA = {"tail", "payout", "method"}

# Per-bucket targets (sum to 30)
TARGETS = {"OFF": 15, "KEEP": 6, "UNK": 9}
# Per-stratum targets within bucket (proportional, deterministic order)
PER_STRATUM = {
    # OFF (15): nuvei 5, refund 3, webhook 3, aircash 2, trustly 2 → 15
    "nuvei": 5,
    "refund": 3,
    "webhook": 3,
    "aircash": 2,
    "trustly": 2,
    # KEEP (6): provider 3, interac 3 → 6
    "provider": 3,
    "interac": 3,
    # UNK (9): tail 4, payout 3, method 2 → 9
    "tail": 4,
    "payout": 3,
    "method": 2,
}


def primary_stratum(strata):
    if not strata:
        return None
    return strata[0]


def pick_queries():
    rng = random.Random(42)
    rows = [json.loads(l) for l in open(EVAL_V3)]
    rows = [r for r in rows if r.get("expected_paths")]
    by_stratum = {}
    for r in rows:
        s = primary_stratum(r.get("strata", []))
        if s is None:
            continue
        by_stratum.setdefault(s, []).append(r)
    picked = []
    for stratum, n in PER_STRATUM.items():
        bucket = by_stratum.get(stratum, [])
        if not bucket:
            print(f"WARN no rows in stratum {stratum}")
            continue
        # Sort for determinism
        bucket = sorted(bucket, key=lambda r: r["query_id"])
        if len(bucket) <= n:
            chosen = bucket[:]
        else:
            chosen = rng.sample(bucket, n)
        picked.extend(chosen)
    print(f"picked {len(picked)} queries (target 30)")
    return picked


def fetch_snippet(conn, repo, fp):
    cur = conn.cursor()
    cur.execute(
        "SELECT content FROM chunks WHERE repo_name=? AND file_path=? ORDER BY length(content) DESC LIMIT 1",
        (repo, fp),
    )
    row = cur.fetchone()
    if not row:
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


def load_bench(path):
    j = json.load(open(path))
    return {r["query"]: r for r in j["eval_per_query"]}


def main():
    picked = pick_queries()
    ids = [r["query_id"] for r in picked]
    Path("/tmp/p10_a2_judge_query_ids_opus2.json").write_text(json.dumps(ids, indent=2))
    print("wrote IDs: /tmp/p10_a2_judge_query_ids_opus2.json")

    a2_bench = load_bench(A2)
    on_bench = load_bench(ON)
    print(f"loaded A2 bench: {len(a2_bench)}, ON bench: {len(on_bench)}")

    conn = sqlite3.connect(str(DB))
    bundle = []
    missing_a2 = 0
    missing_on = 0
    for r in picked:
        q = r["query"]
        a2_row = a2_bench.get(q)
        on_row = on_bench.get(q)
        if not a2_row:
            missing_a2 += 1
            continue
        if not on_row:
            missing_on += 1
            continue
        item = {
            "query_id": r["query_id"],
            "query": q,
            "stratum": primary_stratum(r.get("strata", [])),
            "strata": r.get("strata", []),
            "expected_paths": r.get("expected_paths", []),
            "a2_r10_heuristic": a2_row.get("recall_at_10"),
            "on_r10_heuristic": on_row.get("recall_at_10"),
            "a2": [],
            "on": [],
        }
        for tf in a2_row["top_files"][:10]:
            item["a2"].append(
                {
                    "rank": tf["rank"],
                    "repo_name": tf["repo_name"],
                    "file_path": tf["file_path"],
                    "snippet": fetch_snippet(conn, tf["repo_name"], tf["file_path"]),
                }
            )
        for tf in on_row["top_files"][:10]:
            item["on"].append(
                {
                    "rank": tf["rank"],
                    "repo_name": tf["repo_name"],
                    "file_path": tf["file_path"],
                    "snippet": fetch_snippet(conn, tf["repo_name"], tf["file_path"]),
                }
            )
        bundle.append(item)
    conn.close()
    Path("/tmp/p10_a2_judge_bundle_opus2.json").write_text(json.dumps(bundle, indent=2))
    print(f"wrote bundle: /tmp/p10_a2_judge_bundle_opus2.json (n={len(bundle)})")
    if missing_a2 or missing_on:
        print(f"WARN missing rows — a2:{missing_a2} on:{missing_on}")


if __name__ == "__main__":
    main()
