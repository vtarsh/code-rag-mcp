#!/usr/bin/env python3
"""Build (query, positive) training JSONL for the docs-embedder fine-tune.

Stage C/D input. Loads labeled candidates, filters to doc-intent positives,
resolves content from knowledge.db, scrubs accidental secret matches, samples
a deterministic subset, and writes one JSONL row per (query, positive) pair.

Usage:
    python prepare_train_data.py --subset=10 --seed=42 --out=/tmp/train_v0.jsonl
    python prepare_train_data.py --full --seed=42  --out=/tmp/train_full.jsonl

The defaults point at the 172 MB ``db/knowledge.db`` — NEVER the 0-byte
``profiles/pay-com/knowledge.db`` shim.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path
from typing import Final

_BASE: Final = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
DEFAULT_DB: Final = _BASE / "db" / "knowledge.db"
DEFAULT_LABELED: Final = (
    _BASE / "profiles" / "pay-com" / "v12_candidates_regen_labeled_FINAL.jsonl"
)

# Eval files train pairs MUST be disjoint from. Both legacy v3 (n=100) and
# the expanded v3_n150 (n=143) — union the two. P7 Phase 1.2 (CM4 closure):
# without the query-disjoint check, silver-positive transduction can leak
# eval queries into training even when paths are disjoint.
DEFAULT_EVAL_FILES: Final = (
    _BASE / "profiles" / "pay-com" / "doc_intent_eval_v3.jsonl",
    _BASE / "profiles" / "pay-com" / "doc_intent_eval_v3_n150.jsonl",
)

# Regex scan — even though the labeled set has no hits today, we bake the
# scrub in so a future labeling pass that sucks in a secret-bearing chunk
# can't silently leak it into training.
_SECRET_RE: Final = re.compile(
    r"MerchantSecret|PrivateKey|BearerToken|X-Api-Key",
    re.IGNORECASE,
)


def load_labeled(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def filter_doc_intent_positives(rows: list[dict]) -> list[dict]:
    return [
        r for r in rows
        if r.get("query_tag") == "doc-intent" and r.get("label_final") == "+"
    ]


def dedupe_by_query_file(rows: list[dict]) -> list[dict]:
    """Keep first occurrence per (query, file_path)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for r in rows:
        key = (r.get("query", ""), r.get("file_path", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def resolve_content(
    conn: sqlite3.Connection,
    repo_name: str,
    file_path: str,
) -> str | None:
    """Return chunk content for (repo_name, file_path), or None if missing.

    LIMIT 1 — duplicate chunks in the same file are rare enough that the
    first row is representative for training.
    """
    row = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND file_path = ? LIMIT 1",
        (repo_name, file_path),
    ).fetchone()
    if row is None:
        return None
    content = row[0]
    return content if content else None


def contains_secret(text: str) -> bool:
    return bool(_SECRET_RE.search(text or ""))


# ----- eval-disjoint guards (CM4 + P7 Phase 1.2) ----------------------------

def _norm_query(q: str) -> str:
    """Normalize a query for disjoint comparison: lower-cased + stripped."""
    return (q or "").strip().lower()


def load_eval_queries(eval_paths: tuple[Path, ...]) -> set[str]:
    """Return the union of normalized eval queries from each path.

    Missing files raise FileNotFoundError — callers must explicitly opt out
    of an eval guard by passing an empty tuple.
    """
    queries: set[str] = set()
    for p in eval_paths:
        if not p.exists():
            raise FileNotFoundError(f"eval file not found: {p}")
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                q = row.get("query")
                if q:
                    queries.add(_norm_query(q))
    return queries


def load_eval_paths(eval_paths: tuple[Path, ...]) -> set[tuple[str, str]]:
    """Return the union of (repo_name, file_path) pairs from eval expected_paths.

    Same file-presence contract as load_eval_queries.
    """
    paths: set[tuple[str, str]] = set()
    for p in eval_paths:
        if not p.exists():
            raise FileNotFoundError(f"eval file not found: {p}")
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                for ep in row.get("expected_paths") or []:
                    repo = ep.get("repo_name") or ""
                    fp = ep.get("file_path") or ""
                    if repo and fp:
                        paths.add((repo, fp))
    return paths


def _assert_query_disjoint_from_eval(
    pairs: list[dict],
    eval_queries: set[str],
) -> None:
    """REFUSE to write if any train query (normalized) appears in eval set.

    P7 Phase 1.2 (CM4 closure). Recipe-architect's path-disjoint check left a
    silver-positive transduction leak: the same query can appear in training
    even with a different doc path. This guards the query side.
    """
    leaked = [
        p for p in pairs
        if _norm_query(p.get("query", "")) in eval_queries
    ]
    if leaked:
        sample = [p.get("query", "") for p in leaked[:5]]
        raise ValueError(
            f"REFUSE: {len(leaked)} train pairs collide with eval queries: {sample}"
        )


def _assert_path_disjoint_from_eval(
    pairs: list[dict],
    eval_paths: set[tuple[str, str]],
) -> None:
    """REFUSE to write if any train positive's path is an eval expected_path.

    Original CM4 spec from debate-recipes.md §CM4. Independent of the query
    check — both must pass.
    """
    leaked = [
        p for p in pairs
        if (p.get("_repo_name", ""), p.get("_file_path", "")) in eval_paths
    ]
    if leaked:
        sample = [
            f"{p.get('_repo_name', '')}:{p.get('_file_path', '')}"
            for p in leaked[:5]
        ]
        raise ValueError(
            f"REFUSE: {len(leaked)} train pairs collide with eval expected_paths: {sample}"
        )


def build_pairs(
    labeled_path: Path,
    db_path: Path,
) -> tuple[list[dict], int, int]:
    """Return (resolved_pairs, n_missing, n_scrubbed).

    Each resolved pair is ``{"query": ..., "positive": ..., "_repo_name": ...,
    "_file_path": ...}`` — the underscored keys help callers log provenance
    but are stripped before final JSONL write.

    Raises:
      FileNotFoundError: labeled or db file missing.
      ValueError: db_path is empty (0 bytes) — usually the profiles shim.
    """
    if not labeled_path.exists():
        raise FileNotFoundError(f"labeled file not found: {labeled_path}")
    if not db_path.exists():
        raise FileNotFoundError(f"knowledge db not found: {db_path}")
    if db_path.stat().st_size == 0:
        raise ValueError(
            f"knowledge db is empty (0 bytes): {db_path} — "
            "did you point at profiles/pay-com/knowledge.db? use db/knowledge.db"
        )

    rows = load_labeled(labeled_path)
    pos = filter_doc_intent_positives(rows)
    deduped = dedupe_by_query_file(pos)

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA query_only = ON")

    pairs: list[dict] = []
    n_missing = 0
    n_scrubbed = 0
    try:
        for r in deduped:
            repo = r.get("repo_name") or ""
            fp = r.get("file_path") or ""
            q = r.get("query") or ""
            content = resolve_content(conn, repo, fp)
            if content is None:
                n_missing += 1
                print(
                    f"[warn] missing content: repo={repo} file={fp}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if contains_secret(content) or contains_secret(q):
                n_scrubbed += 1
                print(
                    f"[warn] scrubbed secret-match: repo={repo} file={fp}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            pairs.append(
                {
                    "query": q,
                    "positive": content,
                    "_repo_name": repo,
                    "_file_path": fp,
                }
            )
    finally:
        conn.close()

    return pairs, n_missing, n_scrubbed


def sample_pairs(
    pairs: list[dict],
    subset: int,
    seed: int,
) -> list[dict]:
    """Deterministic sample of ``subset`` pairs using ``random.Random(seed)``."""
    if subset >= len(pairs):
        return list(pairs)
    rng = random.Random(seed)
    return rng.sample(pairs, subset)


def write_jsonl(path: Path, pairs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for p in pairs:
            row = {"query": p["query"], "positive": p["positive"]}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run(
    subset: int | None,
    seed: int,
    out: Path,
    db_path: Path,
    labeled_path: Path,
    eval_files: tuple[Path, ...] = DEFAULT_EVAL_FILES,
) -> int:
    """Return exit code. subset=None means --full (take every resolved pair).

    Pass ``eval_files=()`` to skip the eval-disjoint guards (tests only).
    """
    pairs, n_missing, n_scrubbed = build_pairs(labeled_path, db_path)
    resolved_count = len(pairs)

    if subset is None:
        selected = pairs
        requested = resolved_count
    else:
        if resolved_count < subset:
            print(
                f"ERROR: resolved {resolved_count} rows, requested {subset}; "
                "increase --subset or check db path",
                file=sys.stderr,
                flush=True,
            )
            return 2
        selected = sample_pairs(pairs, subset, seed)
        requested = subset

    if eval_files:
        eval_queries = load_eval_queries(eval_files)
        eval_paths = load_eval_paths(eval_files)
        _assert_query_disjoint_from_eval(selected, eval_queries)
        _assert_path_disjoint_from_eval(selected, eval_paths)

    write_jsonl(out, selected)
    print(
        f"wrote {len(selected)} rows to {out}; "
        f"skipped {n_missing} missing + {n_scrubbed} scrubbed "
        f"(requested={requested}, resolved={resolved_count})",
        file=sys.stderr,
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--subset", type=int,
        help="Sample N deterministic pairs (Stage C smoke/pilot)",
    )
    mode.add_argument(
        "--full", action="store_true",
        help="Emit every resolved pair (Stage D)",
    )
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--out", required=True, type=Path,
        help="Output JSONL path",
    )
    ap.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"knowledge.db path (default: {DEFAULT_DB})",
    )
    ap.add_argument(
        "--labeled", type=Path, default=DEFAULT_LABELED,
        help=f"labeled candidates JSONL (default: {DEFAULT_LABELED})",
    )
    args = ap.parse_args(argv)

    subset = None if args.full else args.subset
    if subset is not None and subset <= 0:
        print("ERROR: --subset must be > 0", file=sys.stderr, flush=True)
        return 2

    try:
        return run(
            subset=subset,
            seed=args.seed,
            out=args.out,
            db_path=args.db,
            labeled_path=args.labeled,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
