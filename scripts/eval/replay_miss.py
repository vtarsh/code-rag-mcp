"""Replay per-step queries from a bench_steps_to_find result to expose which
non-GT files outranked GT and triggered the miss cascade.

Reads queries_used from a saved bench JSON, re-runs hybrid_search exactly as
the bench did (same preprocessing, env vars from CODE_RAG_NO_RERANK), and
prints the TOP-10 ranked files per step with flags:
  [GT]  expected-paths member
  [NEW] not yet in agent's simulated read-set (would be added by the bench)
  [r]   in agent's read-set already

Usage:
    # Replay specific tasks from baseline (rerank ON)
    python3 scripts/eval/replay_miss.py \\
        --bench=bench_runs/improve/s2f_v2_n665_baseline/full_s2f.json \\
        --ids=BO-1491,CORE-2328,BO-1109

    # Replay with rerank OFF (set env)
    CODE_RAG_NO_RERANK=1 python3 scripts/eval/replay_miss.py \\
        --bench=bench_runs/improve/s2f_v2_n665_norerank/full_s2f.json \\
        --ids=BO-1491,CORE-2328

Env vars match diagnose_recall.py / bench_steps_to_find.py:
- CODE_RAG_NO_RERANK=1  reranker stubbed
- CODE_RAG_NO_VECTOR=1  vector leg stubbed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _norm(p: str) -> str:
    return (p or "").strip().lstrip("/")


def _key(repo: str, path: str) -> tuple[str, str]:
    return (_norm(repo), _norm(path))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bench", type=Path, required=True, help="bench_steps_to_find JSON output")
    p.add_argument("--ids", type=str, required=True, help="comma-separated task IDs to replay")
    p.add_argument("--top-k", type=int, default=10, help="top-K to display per step")
    p.add_argument("--pool-limit", type=int, default=200)
    args = p.parse_args()

    if os.getenv("CODE_RAG_NO_VECTOR", "0") == "1":
        import src.search.hybrid as _H

        _H.vector_search = lambda *args, **kwargs: ([], None)
        print("[CODE_RAG_NO_VECTOR] vector leg stubbed", flush=True)

    if os.getenv("CODE_RAG_NO_RERANK", "0") == "1":
        import src.search.hybrid as _H

        _H.rerank = lambda query, ranked, limit, **kwargs: ranked[:limit]
        print("[CODE_RAG_NO_RERANK] reranker stubbed (raw RRF order)", flush=True)

    from src.search.fts import expand_query
    from src.search.hybrid import hybrid_search
    from src.search.service import _USE_EXPAND_QUERY, _detect_intent_adjustments, preprocess_query

    bench = json.loads(args.bench.read_text())
    by_id = {q["id"]: q for q in bench["eval_per_query"]}
    exclude_file_types = os.environ.get("CODE_RAG_DEFAULT_EXCLUDE", "")
    use_expand = _USE_EXPAND_QUERY

    ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    for qid in ids:
        if qid not in by_id:
            print(f"\n!! {qid} not in bench file", flush=True)
            continue
        row = by_id[qid]
        expected_set = {
            _key(p.split("/")[0], "/".join(p.split("/")[1:]))
            for p in []  # placeholder, see below
        }
        # The bench JSON's "found_at" only has hits; original eval file holds expected_paths.
        # Load eval to recover GT.
        eval_path = REPO_ROOT / "profiles" / "pay-com" / "eval" / "jira_eval_clean_v2.jsonl"
        for line in eval_path.open():
            r = json.loads(line)
            if r.get("id") == qid:
                expected_set = {_key(ep["repo_name"], ep["file_path"]) for ep in r.get("expected_paths", [])}
                break

        queries = row.get("queries_used", [])
        print(f"\n{'=' * 80}")
        print(
            f"{qid}  n_expected={len(expected_set)}  "
            f"first_hit={row.get('steps_to_first_hit')}  "
            f"terminal_recall={row.get('terminal_recall'):.2f}"
        )
        print(f"  query: {row.get('query')}")
        print("  GT files:")
        for k in sorted(expected_set):
            print(f"    {k[0]}/{k[1]}")

        read_set: set[tuple[str, str]] = set()
        for step_idx, q in enumerate(queries, 1):
            expanded = expand_query(q) if use_expand else q
            processed_query, entities = preprocess_query(q)
            use_entity_boost = len(q.split()) >= 6 and bool(entities)
            if os.getenv("CODE_RAG_QUERY_V2", "0") == "1" and len(entities) < 3:
                use_entity_boost = False
            search_query = processed_query if use_entity_boost else expanded
            repo_boost, repo_prefix_boost, *_ = _detect_intent_adjustments(q)
            try:
                ranked, *_ = hybrid_search(
                    search_query,
                    "",
                    "",
                    exclude_file_types,
                    args.pool_limit,
                    cross_provider=False,
                    docs_index=None,
                    entity_boost=1.3 if use_entity_boost else 1.0,
                    repo_boost=repo_boost,
                    repo_prefix_boost=repo_prefix_boost,
                )
                if use_entity_boost and len(ranked) < 5:
                    ranked, *_ = hybrid_search(
                        expanded,
                        "",
                        "",
                        exclude_file_types,
                        args.pool_limit,
                        cross_provider=False,
                        docs_index=None,
                        repo_boost=repo_boost,
                        repo_prefix_boost=repo_prefix_boost,
                    )
            except Exception as exc:
                print(f"  step{step_idx}: ERROR {exc}")
                continue

            print(f"\n  --- step {step_idx} ---")
            print(f"  query: {q[:140]}")
            print(f"  pool_size: {len(ranked)}")
            top = ranked[: args.top_k]
            new_count = 0
            for i, r in enumerate(top, 1):
                k = _key(r["repo_name"], r["file_path"])
                flags = []
                if k in expected_set:
                    flags.append("GT")
                if k in read_set:
                    flags.append("seen")
                else:
                    flags.append("NEW")
                    if new_count < 3:  # bench K_READ=3
                        new_count += 1
                snippet_excerpt = (r.get("snippet", "") or "")[:80].replace("\n", " ")
                print(f"    rank {i:2d} [{'/'.join(flags):12s}] {r['repo_name']}/{r['file_path'][:60]}")
                if snippet_excerpt:
                    print(f"           snippet: {snippet_excerpt}")
            # Also find GT files below top-K and report their rank
            for i, r in enumerate(ranked, 1):
                k = _key(r["repo_name"], r["file_path"])
                if k in expected_set and i > args.top_k:
                    print(f"    GT-in-pool rank {i}: {r['repo_name']}/{r['file_path'][:60]}")

            # Update read_set with top-K_READ NEW (mimic bench)
            added = 0
            for r in ranked:
                k = _key(r["repo_name"], r["file_path"])
                if k not in read_set:
                    read_set.add(k)
                    added += 1
                    if added >= 3:
                        break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
