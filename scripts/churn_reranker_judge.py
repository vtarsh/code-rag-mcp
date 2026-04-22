#!/usr/bin/env python3
"""P1b.1 — Reranker-as-judge for churn_replay diff pairs (LOCAL, NO API).

Neutral judge = cross-encoder/ms-marco-MiniLM-L-6-v2 (different architecture
and different pretraining than the gte-modernbert lineage used for base/v8;
not fine-tuned on our Jira distribution). For each diff pair:

  1. Fetch first `chunks.content` snippet for each (repo_name, file_path,
     chunk_type) in base_top10 and v8_top10 (20 fetches per pair).
  2. Build (query, f"{repo} {file} {snippet}") pairs -> 20 docs per pair.
  3. Score all 20 through the neutral CrossEncoder.
  4. winner = sign(mean(v8_scores) - mean(base_scores)); tie if |margin| < t.

Replaces the Haiku-based `churn_llm_judge.py` (this project is local-only,
`autoresearch_loop.py` comment: "no LLM"). Reproducible and free.

Cost: ~50 pairs * 20 docs * MiniLM-L-6 throughput ~= 2-5 minutes on MPS, 0 USD.

Usage:
  python3.12 scripts/churn_reranker_judge.py
  python3.12 scripts/churn_reranker_judge.py --max-pairs 10  # smoke test
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def fetch_snippet(cur: sqlite3.Cursor, repo: str, fp: str, ct: str, max_chars: int) -> str:
    """Return one chunk content for (repo, file, chunk_type), fallback to any chunk."""
    cur.execute(
        "SELECT content FROM chunks WHERE repo_name=? AND file_path=? AND chunk_type=? LIMIT 1",
        (repo, fp, ct),
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            "SELECT content FROM chunks WHERE repo_name=? AND file_path=? LIMIT 1",
            (repo, fp),
        )
        row = cur.fetchone()
    return (row[0] if row else "")[:max_chars]


def score_list(judge, query, items, cur, *, max_chars, batch_size):
    docs = []
    for it in items:
        snip = fetch_snippet(
            cur, it.get("repo_name", ""), it.get("file_path", ""), it.get("chunk_type", ""), max_chars
        )
        docs.append(f"{it.get('repo_name', '')} {it.get('file_path', '')} {snip}")
    if not docs:
        return 0.0, []
    pairs = [(query, d) for d in docs]
    scores = judge.predict(pairs, batch_size=batch_size)
    return float(sum(scores) / len(scores)), [float(s) for s in scores]


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--input", type=Path, default=Path("profiles/pay-com/churn_replay/diff_pairs.jsonl"))
    p.add_argument("--output", type=Path, default=Path("profiles/pay-com/churn_replay/judge_reranker_v8_vs_base.jsonl"))
    p.add_argument("--summary", type=Path, default=Path("profiles/pay-com/churn_replay/judge_reranker_summary.json"))
    p.add_argument("--db", type=Path, default=Path("db/knowledge.db"))
    p.add_argument("--judge-model", default="cross-encoder/ms-marco-MiniLM-L-6-v2",
                   help="Neutral reranker. Must NOT be in the v8 FT lineage.")
    p.add_argument("--max-pairs", type=int, default=50)
    p.add_argument("--max-chars", type=int, default=1200, help="Snippet char cap (MiniLM 512-token cap)")
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--tie-threshold", type=float, default=0.03,
                   help="abs(mean_v8 - mean_base) below this -> tie")
    args = p.parse_args()

    if not args.input.exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1
    if not args.db.exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 1

    from sentence_transformers import CrossEncoder

    pairs = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    pairs = pairs[: args.max_pairs]
    if not pairs:
        print("ERROR: no pairs loaded", file=sys.stderr)
        return 1
    print(f"loaded {len(pairs)} diff pairs", flush=True)

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    print(f"loading judge: {args.judge_model}", flush=True)
    t0 = time.perf_counter()
    judge = CrossEncoder(args.judge_model, max_length=256)
    print(f"loaded in {time.perf_counter() - t0:.1f}s", flush=True)

    counts = {"a": 0, "b": 0, "tie": 0}
    margins: dict[str, list[float]] = {"a": [], "b": [], "tie": []}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    out_f = args.output.open("w", encoding="utf-8")
    try:
        for i, pair in enumerate(pairs, 1):
            t = time.perf_counter()
            q = pair.get("query") or ""
            base_mean, base_scores = score_list(judge, q, pair.get("base_top10") or [], cur,
                                                 max_chars=args.max_chars, batch_size=args.batch_size)
            v8_mean, v8_scores = score_list(judge, q, pair.get("v8_top10") or [], cur,
                                             max_chars=args.max_chars, batch_size=args.batch_size)
            margin = v8_mean - base_mean
            if abs(margin) < args.tie_threshold:
                verdict = "tie"
            elif margin > 0:
                verdict = "b"
            else:
                verdict = "a"
            counts[verdict] += 1
            margins[verdict].append(margin)

            entry = {
                "pair_idx": i, "query": q, "overlap_at_10": pair.get("overlap_at_10"),
                "base_mean": round(base_mean, 4), "v8_mean": round(v8_mean, 4),
                "margin": round(margin, 4),
                "base_scores": [round(s, 4) for s in base_scores],
                "v8_scores": [round(s, 4) for s in v8_scores],
                "verdict": verdict,
            }
            out_f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out_f.flush()
            lat = time.perf_counter() - t
            if i % 10 == 0 or i == len(pairs):
                print(f"[{i}/{len(pairs)}] verdict={verdict} margin={margin:+.3f} lat={lat:.1f}s", flush=True)
    finally:
        out_f.close()
        conn.close()

    n = len(pairs)
    all_margins = [m for lst in margins.values() for m in lst]
    summary = {
        "n": n, "judge_model": args.judge_model, "tie_threshold": args.tie_threshold,
        "base_wins": counts["a"], "v8_wins": counts["b"], "ties": counts["tie"],
        "base_win_rate": round(counts["a"] / n, 4),
        "v8_win_rate": round(counts["b"] / n, 4),
        "tie_rate": round(counts["tie"] / n, 4),
        "net_direction": round((counts["b"] - counts["a"]) / n, 4),
        "mean_margin_overall": round(sum(all_margins) / n, 4) if all_margins else 0.0,
        "mean_margin_by_verdict": {
            "a": round(sum(margins["a"]) / len(margins["a"]), 4) if margins["a"] else None,
            "b": round(sum(margins["b"]) / len(margins["b"]), 4) if margins["b"] else None,
            "tie": round(sum(margins["tie"]) / len(margins["tie"]), 4) if margins["tie"] else None,
        },
    }
    args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== SUMMARY ===", flush=True)
    for k, v in summary.items():
        print(f"  {k}: {v}", flush=True)
    print(f"\noutput: {args.output}", flush=True)
    print(f"summary: {args.summary}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
