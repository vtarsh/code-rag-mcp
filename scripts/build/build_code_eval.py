#!/usr/bin/env python3
"""Build the v1 code-intent eval (NEW for honest reranker A/B comparison).

Reranker runs on BOTH code and docs queries in production, but the only eval
we have today is docs-side (`doc_intent_eval_v3_n200_v2.jsonl`). Without a
code-side eval, reranker candidates can pass the docs gate but blow up on the
code-search workload.

This script harvests real code-intent queries from production MCP call logs
(`logs/tool_calls.jsonl`), then for each query:
    1. Re-runs FTS5 over the code corpus (file_types NOT in the docs set).
    2. Heuristically labels each top-50 hit as positive (relevant) or not, via
       three orthogonal signals — path-token overlap, repo-token overlap, and
       chunk-content word overlap. A path is marked positive when overlap_score
       >= POSITIVE_THRESHOLD.

Output schema mirrors the docs eval (`doc_intent_eval_v3_n200_v2.jsonl`) so the
existing bench harness (`benchmark_doc_intent.py`) can be pointed at it later
with minimal plumbing:

    {
        "query": str,
        "query_id": str,
        "expected_paths": [{"repo_name": str, "file_path": str}],
        "stratum": str,
        "labeler": "heuristic-code-v1",
        "labeler_metadata": {"n_positives": int, "n_candidates_scored": 50},
    }

Strata (heuristic):
    - lookup       — query is mostly identifiers (camelCase / snake_case tokens)
    - trace        — query mentions flow/webhook/signal/workflow keywords
    - audit        — query mentions audit/check/verify/validation
    - debug        — query mentions error/bug/fix/missing/broken
    - integration  — query mentions provider/integration/grpc-apm-*
    - other        — none of the above

Acceptance bar:
    >= 30 unique queries with >= 3 positives each (median).

Usage:
    python3.12 scripts/build_code_eval.py
    python3.12 scripts/build_code_eval.py --max-queries=80
    python3.12 scripts/build_code_eval.py --tool-calls-log=path/to/log.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".code-rag-mcp")))
os.environ.setdefault("CODE_RAG_HOME", str(ROOT))
os.environ.setdefault("ACTIVE_PROFILE", "pay-com")
sys.path.insert(0, str(ROOT))

from src.search.fts import fts_search  # noqa: E402
from src.search.hybrid import _query_wants_docs  # noqa: E402

TOOL_CALLS_LOG = ROOT / "logs" / "tool_calls.jsonl"
DB_PATH = ROOT / "db" / "knowledge.db"
OUT_PATH = ROOT / "profiles" / "pay-com" / "eval" / "code_intent_eval_v1.jsonl"

# Doc file_types to EXCLUDE — anything not in here is treated as code-corpus.
# This matches the production split used by the docs vector tower.
DOC_FILE_TYPES = (
    "provider_doc",
    "docs",
    "reference",
    "gotchas",
    "flow_annotation",
    "dictionary",
    "domain_registry",
)
DOC_FILE_TYPES_SET = frozenset(DOC_FILE_TYPES)

FTS_LIMIT = 50  # candidates per query
POSITIVE_THRESHOLD = 0.30  # heuristic relevance >= this -> label=1 candidate
POSITIVE_TOP_K = 15  # cap on positives kept per query (top by score)
MIN_QUERY_LEN = 3  # tokens — skip "x", "abc"-style noise
MAX_QUERY_LEN = 20  # tokens — skip pasted long blocks
TARGET_QUERIES = 80  # collect more than the 30 minimum to leave headroom

# Stratum keyword maps — applied first-match-wins in declared order.
STRATUM_KEYWORDS = (
    ("debug", re.compile(r"\b(error|bug|fix|missing|broken|fail|crash|stack)\b", re.IGNORECASE)),
    ("audit", re.compile(r"\b(audit|check|verify|validate|validation|review|consistent)\b", re.IGNORECASE)),
    ("trace", re.compile(r"\b(flow|webhook|signal|workflow|trace|chain|signalWithStart|process)\b", re.IGNORECASE)),
    ("integration", re.compile(r"\b(grpc-apm-|grpc-providers-|integration|provider)\b", re.IGNORECASE)),
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|[_\-\.\/]")


def _split_tokens(text: str) -> set[str]:
    """Lowercase split on word boundaries + camelCase split. Returns token set."""
    if not text:
        return set()
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(text):
        out.add(raw.lower())
        for part in _CAMEL_SPLIT_RE.split(raw):
            if part and len(part) >= 3:
                out.add(part.lower())
    return out


def _stratum_for(query: str) -> str:
    """Classify query by first-match keyword regex. Falls back to 'lookup'/'other'.

    Lookup is the implicit default for short identifier-only queries that hit
    none of the keyword strata.
    """
    for label, regex in STRATUM_KEYWORDS:
        if regex.search(query):
            return label
    # Lookup heuristic: dominated by single-token identifiers.
    tokens = query.split()
    if len(tokens) <= 4:
        return "lookup"
    return "other"


def harvest_queries(log_path: Path) -> list[str]:
    """Pull unique code-intent queries from tool_calls.jsonl.

    Filters:
    - tool == 'search'
    - query non-empty, length in [MIN_QUERY_LEN, MAX_QUERY_LEN] tokens
    - `_query_wants_docs(query) == False` (router decided code)

    Returns insertion-ordered unique queries (Python's dict preserves it).
    """
    if not log_path.exists():
        sys.exit(f"ERROR: log not found at {log_path}")
    seen: dict[str, None] = {}
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("tool") != "search":
                continue
            args = row.get("args") or {}
            q = (args.get("query") or "").strip()
            if not q:
                continue
            tokens = q.split()
            if not (MIN_QUERY_LEN <= len(tokens) <= MAX_QUERY_LEN):
                continue
            if _query_wants_docs(q):
                continue
            if q not in seen:
                seen[q] = None
    return list(seen.keys())


def fts_pool_code(query: str, limit: int = FTS_LIMIT) -> list[dict]:
    """FTS5 hits restricted to NON-doc file types. Returns up to `limit` results.

    The live `fts_search` API takes a single file_type filter; for the code
    corpus we use `exclude_file_types` (comma-joined doc types) which it accepts.
    """
    excl = ",".join(DOC_FILE_TYPES)
    try:
        hits = fts_search(query, exclude_file_types=excl, limit=limit)
    except Exception as exc:
        print(f"  [fts err] {exc}", file=sys.stderr)
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for h in hits or []:
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


def fetch_first_chunk(
    db: sqlite3.Connection,
    repo_name: str,
    file_path: str,
) -> str:
    """Return the first chunk content (clipped to 1500 chars) for a file."""
    cur = db.execute(
        """
        SELECT content
          FROM chunks
         WHERE repo_name = ? AND file_path = ?
         ORDER BY rowid ASC
         LIMIT 1
        """,
        (repo_name, file_path),
    )
    row = cur.fetchone()
    if row is None:
        return ""
    return (row[0] or "")[:1500]


def relevance_score(
    query_tokens: set[str],
    cand: dict,
    chunk_text: str,
) -> float:
    """Three-signal heuristic — returns score in [0, 1].

    Signals (each weighted, sum capped at 1.0):
        - path token overlap   (0.35)
        - repo token overlap   (0.25)
        - content word overlap (0.40, computed on snippet+first-chunk text)

    Each signal is `|q ∩ source_tokens| / |q|`. We do NOT count the file
    extension and we do strip stop-tokens via _split_tokens (length >= 3).
    """
    if not query_tokens:
        return 0.0
    path_tokens = _split_tokens(cand.get("file_path", ""))
    repo_tokens = _split_tokens(cand.get("repo_name", ""))
    content_tokens = _split_tokens((cand.get("snippet") or "") + " " + chunk_text)

    n_q = max(len(query_tokens), 1)
    s_path = len(query_tokens & path_tokens) / n_q
    s_repo = len(query_tokens & repo_tokens) / n_q
    s_text = len(query_tokens & content_tokens) / n_q

    # Cap each at 1.0 to avoid double-credit when query_tokens is small.
    return min(1.0, 0.35 * s_path + 0.25 * s_repo + 0.40 * s_text)


def label_query(
    query: str,
    candidates: list[dict],
    db: sqlite3.Connection,
) -> list[dict]:
    """Score every candidate, return the top-N positives.

    A candidate is "positive" when its heuristic relevance score >=
    POSITIVE_THRESHOLD AND it is in the top POSITIVE_TOP_K by score. This
    matches how a human labeller would scan the FTS top-50 for a code query —
    pick the best handful, ignore the long tail of marginal matches.

    Each output entry is `{repo_name, file_path}` for downstream eval-format
    parity with `doc_intent_eval_v3_n200_v2.jsonl`.
    """
    qtokens = _split_tokens(query)
    scored: list[tuple[float, dict]] = []
    for c in candidates:
        chunk_text = fetch_first_chunk(db, c["repo_name"], c["file_path"])
        score = relevance_score(qtokens, c, chunk_text)
        if score >= POSITIVE_THRESHOLD:
            scored.append((score, c))
    # Sort by score desc, dedupe by file_path keeping highest-scoring chunk.
    scored.sort(key=lambda x: -x[0])
    seen_paths: set[tuple[str, str]] = set()
    out: list[dict] = []
    for _score, c in scored:
        key = (c["repo_name"], c["file_path"])
        if key in seen_paths:
            continue
        seen_paths.add(key)
        out.append({"repo_name": c["repo_name"], "file_path": c["file_path"]})
        if len(out) >= POSITIVE_TOP_K:
            break
    return out


def build_eval(queries: list[str], max_queries: int) -> list[dict]:
    """Score each query, keep ones with >= 3 positives until we hit max_queries."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows: list[dict] = []
    n_skipped_few_positives = 0
    n_skipped_no_candidates = 0
    t0 = time.time()
    for i, q in enumerate(queries):
        cands = fts_pool_code(q)
        if not cands:
            n_skipped_no_candidates += 1
            continue
        positives = label_query(q, cands, db)
        if len(positives) < 3:
            n_skipped_few_positives += 1
            continue
        stratum = _stratum_for(q)
        rows.append(
            {
                "query": q,
                "query_id": f"code_v1_{len(rows):04d}",
                "expected_paths": positives,
                "stratum": stratum,
                "strata": [stratum],
                "labeler": "heuristic-code-v1",
                "labeler_metadata": {
                    "n_positives": len(positives),
                    "n_candidates_scored": len(cands),
                    "positive_threshold": POSITIVE_THRESHOLD,
                },
            }
        )
        if len(rows) >= max_queries:
            break
        if (i + 1) % 50 == 0:
            print(
                f"  scanned {i + 1} queries, kept {len(rows)} "
                f"(skip_no_cand={n_skipped_no_candidates} "
                f"skip_few_pos={n_skipped_few_positives}) "
                f"elapsed={time.time() - t0:.1f}s"
            )
    db.close()
    print(
        f"build_eval done: kept {len(rows)} of {len(queries)} queries "
        f"(skip_no_cand={n_skipped_no_candidates} "
        f"skip_few_pos={n_skipped_few_positives})"
    )
    return rows


def write_rows(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} eval rows -> {out}")


def stats(rows: list[dict]) -> dict:
    pos_per_q = [len(r["expected_paths"]) for r in rows]
    by_stratum: dict[str, int] = defaultdict(int)
    for r in rows:
        by_stratum[r["stratum"]] += 1
    return {
        "n_queries": len(rows),
        "median_positives": statistics.median(pos_per_q) if pos_per_q else 0,
        "min_positives": min(pos_per_q) if pos_per_q else 0,
        "max_positives": max(pos_per_q) if pos_per_q else 0,
        "by_stratum": dict(by_stratum),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--tool-calls-log",
        default=str(TOOL_CALLS_LOG),
        help="Source production MCP call log.",
    )
    p.add_argument(
        "--out",
        default=str(OUT_PATH),
        help="Output eval JSONL path.",
    )
    p.add_argument(
        "--max-queries",
        type=int,
        default=TARGET_QUERIES,
        help="Stop after this many positively-labeled queries.",
    )
    args = p.parse_args()

    queries = harvest_queries(Path(args.tool_calls_log))
    print(f"harvested {len(queries)} unique code-intent queries")
    rows = build_eval(queries, args.max_queries)
    write_rows(rows, Path(args.out))

    s = stats(rows)
    print()
    print("=" * 50)
    print("STATS")
    print("=" * 50)
    for k, v in s.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
