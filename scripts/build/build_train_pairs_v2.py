#!/usr/bin/env python3
"""Build (query, positive, hard_negatives) train pairs from prod tool_calls log.

P7 Phase 1.4 — recipe-architect R1 / CM1 (HN-A reranker-mined). Reads prod
search/analyze_task queries from logs/tool_calls.jsonl, filters to doc-intent,
runs FTS5 baseline retrieval, scores top-100 with a CrossEncoder reranker,
keeps ranks 1-3 as silver positives and ranks 11-30 as hard negatives. Drops
queries that overlap eval-v3 (query-disjoint) and paths that overlap eval
expected_paths (path-disjoint).

Usage:
    python3.12 scripts/build_train_pairs_v2.py \
      --queries=logs/tool_calls.jsonl \
      --filter=doc-intent \
      --eval-disjoint=profiles/pay-com/eval/doc_intent_eval_v3_n150.jsonl \
      --eval-disjoint=profiles/pay-com/eval/doc_intent_eval_v3.jsonl \
      --reranker=cross-encoder/ms-marco-MiniLM-L-6-v2 \
      --positives-rank=1-3 --hard-neg-rank=11-30 \
      --max-pairs=12000 --seed=42 \
      --out=/tmp/train_v2.jsonl

The script prefers a programmatic CrossEncoder load (sentence_transformers).
It does NOT spawn a daemon — if the daemon is not running, FTS5 is the only
candidate source (vector_search would require the embedding provider, which
is daemon-loaded). This is documented as the chosen tradeoff (see top of
build_pair_rows for the candidate-source contract).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

ROOT = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".code-rag-mcp")))
os.environ.setdefault("CODE_RAG_HOME", str(ROOT))
os.environ.setdefault("ACTIVE_PROFILE", "pay-com")
sys.path.insert(0, str(ROOT))

# Mirror src/search/hybrid.py::_query_wants_docs (regex copy — keep in sync).
_DOC_QUERY_RE = re.compile(
    r"\b(test|tests|spec|specs|docs?|documentation|readme|guide|guides|tutorial|"
    r"checklist|framework|matrix|severity|sandbox|overview|reference|rules)\b",
    re.IGNORECASE,
)
_CODE_SIG_RE = re.compile(
    r"(?:\b[a-z][a-zA-Z0-9]*\([^)]*\)|\b[A-Z][A-Z0-9_]{2,}\b|"
    r"[a-z]+_[a-z_]+|\.(?:js|ts|py|go|proto)\b)"
)
_REPO_TOKEN_RE = re.compile(
    r"\b(?:grpc-|express-|next-web-|workflow-|k8s-)[a-z0-9-]+\b",
    re.IGNORECASE,
)


def _query_wants_docs(query: str) -> bool:
    """Doc-intent classifier — exact mirror of src/search/hybrid.py."""
    if _DOC_QUERY_RE.search(query or ""):
        return True
    if not query:
        return False
    if _CODE_SIG_RE.search(query) or _REPO_TOKEN_RE.search(query):
        return False
    tokens = query.split()
    return 2 <= len(tokens) <= 15


def _norm_query(q: str) -> str:
    return (q or "").strip().lower()


def load_prod_queries(path: Path, filter_func: Callable[[str], bool]) -> list[tuple[str, int]]:
    """Return [(query, freq), ...] of unique queries that pass filter_func.

    Stable order: sorted by freq desc, then query asc — gives deterministic
    output for the same input. Reads `tool` in {search, analyze_task} from
    tool_calls.jsonl (matches grow_doc_intent_eval_v3 conventions).
    """
    if not path.exists():
        raise FileNotFoundError(f"queries file not found: {path}")
    freq: Counter = Counter()
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("tool") not in ("search", "analyze_task"):
                continue
            args = d.get("args") or {}
            q = (args.get("query") or args.get("task_description") or args.get("description") or "").strip()
            if not q or not filter_func(q):
                continue
            freq[q] += 1
    return sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))


def load_eval_disjoint_set(eval_paths: tuple[Path, ...]) -> tuple[set[str], set[tuple[str, str]]]:
    """Return (queries_lower_set, expected_paths_set). Union over all eval files."""
    qset: set[str] = set()
    pset: set[tuple[str, str]] = set()
    for p in eval_paths:
        if not p.exists():
            raise FileNotFoundError(f"eval-disjoint file not found: {p}")
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                q = row.get("query") or ""
                if q:
                    qset.add(_norm_query(q))
                for ep in row.get("expected_paths") or []:
                    repo = ep.get("repo_name") or ""
                    fp = ep.get("file_path") or ""
                    if repo and fp:
                        pset.add((repo, fp))
    return qset, pset


def query_disjoint(q: str, eval_queries: set[str]) -> bool:
    return _norm_query(q) not in eval_queries


def path_disjoint(repo: str, fp: str, eval_paths: set[tuple[str, str]]) -> bool:
    return (repo, fp) not in eval_paths


def _parse_rank_range(spec: str) -> tuple[int, int]:
    """'1-3' -> (1, 3). Inclusive bounds."""
    a, b = spec.split("-")
    lo, hi = int(a), int(b)
    if lo < 1 or hi < lo:
        raise ValueError(f"invalid rank range: {spec}")
    return lo, hi


def retrieve_candidates(query: str, limit: int = 100) -> list[dict]:
    """FTS5-only candidate pool — vector_search needs daemon's embedding provider.

    Returns list of dicts with keys {repo_name, file_path, file_type, snippet}.
    """
    from src.search.fts import fts_search  # lazy: src module loads container

    hits = fts_search(query, limit=limit)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for h in hits:
        key = (h.repo_name, h.file_path)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "repo_name": h.repo_name,
                "file_path": h.file_path,
                "file_type": h.file_type,
                "snippet": h.snippet or "",
            }
        )
    return out


def score_with_reranker(query: str, candidates: list[dict], reranker) -> list[dict]:
    """Score (query, snippet) pairs with the reranker; return list sorted desc.

    Each returned dict gains a `_score` field. `reranker` must expose `.predict`.
    """
    if not candidates:
        return []
    pairs = [(query, c.get("snippet") or c.get("file_path") or "") for c in candidates]
    scores = reranker.predict(pairs, batch_size=32)
    scored = [{**c, "_score": float(s)} for c, s in zip(candidates, scores, strict=False)]
    scored.sort(key=lambda r: -r["_score"])
    return scored


def pick_positives_and_hard_negatives(
    ranked: list[dict],
    pos_ranks: tuple[int, int],
    hn_ranks: tuple[int, int],
    eval_paths: set[tuple[str, str]],
) -> tuple[list[dict], list[dict]]:
    """Slice ranks (1-indexed). Drop anything whose path is in eval_paths."""

    def _filter(items: list[dict]) -> list[dict]:
        return [i for i in items if path_disjoint(i["repo_name"], i["file_path"], eval_paths)]

    pos_lo, pos_hi = pos_ranks
    hn_lo, hn_hi = hn_ranks
    pos = _filter(ranked[pos_lo - 1 : pos_hi])
    hn = _filter(ranked[hn_lo - 1 : hn_hi])
    return pos, hn


def build_pair_row(query: str, freq: int, pos: list[dict], hn: list[dict]) -> dict:
    return {
        "q": query,
        "pos": [{"repo_name": p["repo_name"], "file_path": p["file_path"]} for p in pos],
        "hard_negs": [{"repo_name": n["repo_name"], "file_path": n["file_path"]} for n in hn],
        "query_freq": freq,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries", type=Path, default=ROOT / "logs/tool_calls.jsonl")
    ap.add_argument("--filter", choices=("doc-intent", "all"), default="doc-intent")
    ap.add_argument(
        "--eval-disjoint",
        type=Path,
        action="append",
        default=[],
        help="Eval JSONL to disjoint against (queries+expected_paths). Repeatable.",
    )
    ap.add_argument("--reranker", default="cross-encoder/ms-marco-MiniLM-L-6-v2")
    ap.add_argument("--positives-rank", default="1-3", type=_parse_rank_range)
    ap.add_argument("--hard-neg-rank", default="11-30", type=_parse_rank_range)
    ap.add_argument("--max-pairs", type=int, default=12000)
    ap.add_argument("--candidate-pool", type=int, default=100, help="FTS5 top-N candidates per query before reranking")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--probe", type=int, default=0, help="Process only N queries (smoke mode); 0 = no limit")
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)

    filter_func = _query_wants_docs if args.filter == "doc-intent" else (lambda q: bool(q))
    queries = load_prod_queries(args.queries, filter_func)
    print(f"loaded {len(queries)} unique {args.filter} prod queries", file=sys.stderr)

    eval_qs: set[str] = set()
    eval_ps: set[tuple[str, str]] = set()
    if args.eval_disjoint:
        eval_qs, eval_ps = load_eval_disjoint_set(tuple(args.eval_disjoint))
        print(f"  eval-disjoint: {len(eval_qs)} queries, {len(eval_ps)} paths", file=sys.stderr)

    # Drop eval-overlapping queries up-front.
    queries = [(q, c) for q, c in queries if query_disjoint(q, eval_qs)]
    print(f"  after query-disjoint: {len(queries)} queries", file=sys.stderr)

    if args.probe and args.probe > 0:
        queries = queries[: args.probe]
        print(f"  probe mode: limited to {len(queries)} queries", file=sys.stderr)

    # Determinism: shuffle once with seed, then iterate. Output order is then
    # input-shuffled-then-iterated, which is reproducible for the same seed.
    rng.shuffle(queries)

    # Lazy reranker load — keeps tests fast (they patch retrieve+score).
    print(f"loading reranker: {args.reranker}", file=sys.stderr)
    from sentence_transformers import CrossEncoder

    reranker = CrossEncoder(args.reranker, max_length=512)

    rows: list[dict] = []
    n_no_pos = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as outf:
        for i, (q, c) in enumerate(queries, start=1):
            cands = retrieve_candidates(q, limit=args.candidate_pool)
            ranked = score_with_reranker(q, cands, reranker)
            pos, hn = pick_positives_and_hard_negatives(
                ranked,
                args.positives_rank,
                args.hard_neg_rank,
                eval_ps,
            )
            if not pos:
                n_no_pos += 1
                continue
            row = build_pair_row(q, c, pos, hn)
            outf.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows.append(row)
            if i % 50 == 0:
                print(f"  {i}/{len(queries)} pairs={len(rows)} skipped_no_pos={n_no_pos}", file=sys.stderr)
            if len(rows) >= args.max_pairs:
                print(f"  hit max-pairs={args.max_pairs}", file=sys.stderr)
                break

    print(
        f"wrote {len(rows)} rows to {args.out}; skipped {n_no_pos} (no positives)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
