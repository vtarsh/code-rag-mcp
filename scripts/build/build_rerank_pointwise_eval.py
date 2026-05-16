#!/usr/bin/env python3
"""Build the v1 pointwise reranker eval (Bug 2 fix from NEXT_SESSION_PROMPT.md).

Bug 2 background: the legacy pointwise eval (`finetune_data_v8/test_pointwise.jsonl`)
collapses to 4 unique queries with positives -> all 3 reranker candidates tied at
0.6337 R@10 last cycle (saturated, eval too small to discriminate).

This script derives a discriminating eval from the calibrated docs intent eval
(`profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl`, n=192 scoreable). For each
scoreable query:
    - positives = expected_paths (label=1)
    - hard-negatives = FTS5 top-50 paths over the docs corpus minus expected_paths
                       (label=0)

Output schema (one row per (query, doc_path) pair):
    {
        "query": str,
        "doc_path": str,         # "<repo_name>::<file_path>"
        "doc_text": str,         # first chunk content (<= ~1500 chars)
        "label": 0 | 1,
        "query_id": str,
        "stratum": str,
    }

Acceptance bar (Bug 2):
    >= 30 unique queries with >= 3 positives each (median); discrimination spread
    of vanilla L-6 R@10 vs random-rank baseline >= 0.10.

Usage:
    python3.12 scripts/build_rerank_pointwise_eval.py
    python3.12 scripts/build_rerank_pointwise_eval.py --check-only
    python3.12 scripts/build_rerank_pointwise_eval.py --no-discrim
"""

from __future__ import annotations

import argparse
import json
import os
import random
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

EVAL_PATH = ROOT / "profiles" / "pay-com" / "doc_intent_eval_v3_n200_v2.jsonl"
DB_PATH = ROOT / "db" / "knowledge.db"
OUT_PATH = ROOT / "profiles" / "pay-com" / "rerank_pointwise_eval_v1.jsonl"
DISCRIM_OUT = Path("/tmp/pointwise_eval_v1_check.txt")

# Docs corpus file_types — anything outside this set is code/config noise
# from a reranker-quality standpoint.
DOC_FILE_TYPES = (
    "provider_doc",
    "docs",
    "reference",
    "gotchas",
    "flow_annotation",
    "dictionary",
    "domain_registry",
)

FTS_PER_TYPE_LIMIT = 10  # 10 * 7 file_types -> up to 70 raw hits, dedupe to ~50
TARGET_NEG_PER_QUERY = 50
DOC_TEXT_CLIP_CHARS = 1500


def load_eval(path: Path) -> list[dict]:
    """Load doc_intent eval rows. Drop rows without expected_paths (unscoreable)."""
    if not path.exists():
        sys.exit(f"ERROR: eval not found at {path}")
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ep = row.get("expected_paths") or []
            if not ep:
                continue
            rows.append(row)
    return rows


def fts_pool(query: str) -> list[tuple[str, str]]:
    """FTS5 BM25 hits filtered to doc file types. Mirrors v3 eval-builder strategy.

    We loop over each doc-like file_type because the live `fts_search` API
    accepts a single file_type filter. Dedupe across types keeping first-seen
    ordering (BM25 rank within each type is preserved).
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ft in DOC_FILE_TYPES:
        try:
            hits = fts_search(query, file_type=ft, limit=FTS_PER_TYPE_LIMIT)
        except Exception as exc:
            print(f"  [fts err {ft}] {exc}", file=sys.stderr)
            continue
        for h in hits or []:
            key = (h.repo_name, h.file_path)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def fetch_doc_text(
    db: sqlite3.Connection,
    repo_name: str,
    file_path: str,
) -> str:
    """Return the first chunk content for the given file (clipped to ~1500 chars).

    Reranker pointwise eval needs SOME text per doc to score; we use the first
    chunk because that's what the production hybrid pipeline feeds the reranker
    (snippet-level, not full file). Falls back to empty string if missing.
    """
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
    txt = row[0] or ""
    return txt[:DOC_TEXT_CLIP_CHARS]


def build_pairs(eval_rows: list[dict]) -> list[dict]:
    """Build (query, doc, label) pairs. Returns the full pair list."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    pairs: list[dict] = []
    n_skipped_no_neg = 0
    n_skipped_no_pos_text = 0
    t0 = time.time()
    for i, row in enumerate(eval_rows):
        query = row.get("query") or ""
        query_id = row.get("query_id") or f"row{i}"
        stratum = row.get("stratum") or "__none__"
        expected = [
            (e["repo_name"], e["file_path"])
            for e in (row.get("expected_paths") or [])
            if e.get("repo_name") and e.get("file_path")
        ]
        if not expected:
            continue
        pos_set = set(expected)
        # Pull FTS5 candidates over docs corpus.
        cands = fts_pool(query)
        # Hard-negs = FTS hits that are NOT in expected_paths. We intentionally
        # take all of them (up to TARGET_NEG_PER_QUERY) — discrimination needs
        # signal-rich negatives, not random ones.
        negs: list[tuple[str, str]] = []
        for c in cands:
            if c in pos_set:
                continue
            negs.append(c)
            if len(negs) >= TARGET_NEG_PER_QUERY:
                break
        if not negs:
            # Without negatives the row is degenerate (cannot discriminate).
            n_skipped_no_neg += 1
            continue

        # Emit positives first.
        added_pos = 0
        for repo, path in expected:
            txt = fetch_doc_text(db, repo, path)
            if not txt:
                # Positive doc not in the corpus — skip but log; do NOT count
                # this query as scoreable if NO positives have text.
                continue
            pairs.append(
                {
                    "query": query,
                    "doc_path": f"{repo}::{path}",
                    "doc_text": txt,
                    "label": 1,
                    "query_id": query_id,
                    "stratum": stratum,
                }
            )
            added_pos += 1
        if added_pos == 0:
            n_skipped_no_pos_text += 1
            # Roll back the negs we'd add for this query — cannot score.
            continue

        # Emit hard-negs.
        for repo, path in negs:
            txt = fetch_doc_text(db, repo, path)
            if not txt:
                continue
            pairs.append(
                {
                    "query": query,
                    "doc_path": f"{repo}::{path}",
                    "doc_text": txt,
                    "label": 0,
                    "query_id": query_id,
                    "stratum": stratum,
                }
            )

        if (i + 1) % 25 == 0:
            elapsed = time.time() - t0
            print(
                f"  [{i + 1}/{len(eval_rows)}] pairs={len(pairs)} "
                f"skipped_no_neg={n_skipped_no_neg} "
                f"skipped_no_pos_text={n_skipped_no_pos_text} "
                f"elapsed={elapsed:.1f}s"
            )

    db.close()
    print(
        f"build_pairs done: pairs={len(pairs)} "
        f"skipped_no_neg={n_skipped_no_neg} "
        f"skipped_no_pos_text={n_skipped_no_pos_text}"
    )
    return pairs


def write_pairs(pairs: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"wrote {len(pairs)} pairs -> {out}")


def stats(pairs: list[dict]) -> dict:
    """Compute summary stats for the pair set."""
    by_qid: dict[str, dict] = defaultdict(lambda: {"pos": 0, "neg": 0})
    for p in pairs:
        bucket = by_qid[p["query_id"]]
        if p["label"] == 1:
            bucket["pos"] += 1
        else:
            bucket["neg"] += 1
    qids_with_pos = [q for q, b in by_qid.items() if b["pos"] > 0]
    pos_counts = [b["pos"] for q, b in by_qid.items() if b["pos"] > 0]
    median_pos = statistics.median(pos_counts) if pos_counts else 0
    return {
        "n_pairs": len(pairs),
        "n_queries": len(by_qid),
        "n_queries_with_pos": len(qids_with_pos),
        "median_pos_per_query": median_pos,
        "min_pos": min(pos_counts) if pos_counts else 0,
        "max_pos": max(pos_counts) if pos_counts else 0,
        "total_pos": sum(b["pos"] for b in by_qid.values()),
        "total_neg": sum(b["neg"] for b in by_qid.values()),
    }


# ---------------------------------------------------------------- discrimination


def _recall_at_10(ranked_indices: list[int], pos_indices: set[int]) -> float:
    """Recall@10 for ONE query = |top10 ∩ pos| / min(|pos|, 10)."""
    if not pos_indices:
        return 0.0
    top10 = set(ranked_indices[:10])
    return len(top10 & pos_indices) / min(len(pos_indices), 10)


def discrim_check(pairs: list[dict]) -> dict:
    """Compute spread = vanilla MiniLM-L-6 R@10 - random R@10.

    For each query: rank its docs by (a) MiniLM-L-6 cross-encoder score,
    (b) random shuffle. Then R@10 is averaged across queries.

    spread >= 0.10 means the eval discriminates (rerankers can move the metric).

    Returns a dict with both numbers and the spread.
    """
    # Group pairs per query (preserve insertion order).
    by_qid: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        by_qid[p["query_id"]].append(p)

    print(f"  loading vanilla cross-encoder/ms-marco-MiniLM-L-6-v2 ...")
    from sentence_transformers import CrossEncoder
    ce = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

    rng = random.Random(42)
    minilm_recalls: list[float] = []
    random_recalls: list[float] = []
    for qid, qpairs in by_qid.items():
        if not qpairs:
            continue
        query = qpairs[0]["query"]
        docs = [p["doc_text"] for p in qpairs]
        labels = [p["label"] for p in qpairs]
        pos_idx = {i for i, lab in enumerate(labels) if lab == 1}
        if not pos_idx:
            continue

        # vanilla MiniLM ranking (descending score).
        scores = ce.predict([(query, d) for d in docs])
        order = sorted(range(len(docs)), key=lambda i: -float(scores[i]))
        minilm_recalls.append(_recall_at_10(order, pos_idx))

        # random ranking.
        order = list(range(len(docs)))
        rng.shuffle(order)
        random_recalls.append(_recall_at_10(order, pos_idx))

    minilm_r10 = (sum(minilm_recalls) / len(minilm_recalls)) if minilm_recalls else 0.0
    random_r10 = (sum(random_recalls) / len(random_recalls)) if random_recalls else 0.0
    return {
        "minilm_l6_r10": round(minilm_r10, 4),
        "random_r10": round(random_r10, 4),
        "spread": round(minilm_r10 - random_r10, 4),
        "n_queries_scored": len(minilm_recalls),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--eval",
        default=str(EVAL_PATH),
        help="Path to source doc-intent eval JSONL (must have expected_paths).",
    )
    p.add_argument(
        "--out",
        default=str(OUT_PATH),
        help="Output JSONL path for the pointwise pairs.",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="Run discrim check on the existing OUT_PATH file without rebuilding.",
    )
    p.add_argument(
        "--no-discrim",
        action="store_true",
        help=(
            "Skip discrimination check (faster, no MiniLM load). Useful for unit "
            "tests and CI smoke. Stats are still printed."
        ),
    )
    args = p.parse_args()

    eval_path = Path(args.eval)
    out_path = Path(args.out)

    if args.check_only:
        if not out_path.exists():
            sys.exit(f"--check-only set but {out_path} missing")
        pairs = []
        with out_path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    pairs.append(json.loads(line))
    else:
        eval_rows = load_eval(eval_path)
        print(f"loaded {len(eval_rows)} scoreable eval rows from {eval_path}")
        pairs = build_pairs(eval_rows)
        write_pairs(pairs, out_path)

    s = stats(pairs)
    print()
    print("=" * 50)
    print("STATS")
    print("=" * 50)
    for k, v in s.items():
        print(f"  {k}: {v}")

    if args.no_discrim:
        print()
        print("--no-discrim set; skipping spread check")
        return

    print()
    print("=" * 50)
    print("DISCRIMINATION CHECK (vanilla MiniLM-L-6 vs random)")
    print("=" * 50)
    d = discrim_check(pairs)
    for k, v in d.items():
        print(f"  {k}: {v}")
    DISCRIM_OUT.parent.mkdir(parents=True, exist_ok=True)
    with DISCRIM_OUT.open("w") as f:
        json.dump({"stats": s, "discrim": d}, f, indent=2)
    print(f"  wrote {DISCRIM_OUT}")
    if d["spread"] < 0.10:
        print(f"  WARN: spread {d['spread']} < 0.10 — eval may not discriminate")


if __name__ == "__main__":
    main()
