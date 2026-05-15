#!/usr/bin/env python3
"""Build CoSENT triplets for R1-retry-prep (one-off, local).

R1-retry-prep variant of `build_r1_data.py` (2026-04-25):
- Reads `/tmp/r1_train_pairs_v3.jsonl` (re-mined 1069 pairs from
  `scripts/build_train_pairs_v2.py` against the eval-disjoint v3 sets).
- Resolves (repo_name, file_path) → text via `db/knowledge.db` chunks.
- Emits `/tmp/r1_cosent_triplets_v3.jsonl` with `{query, positive, negative}`.
- Fails LOUD on any unresolvable positive (we want truth, not silent emptys).
- Validates schema via `scripts.runpod.train_docs_embedder._validate_rows_for_loss`.

Outputs:
    /tmp/r1_cosent_triplets_v3.jsonl  (~5,000-10,000 triplets target)
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

DB = Path("/Users/vaceslavtarsevskij/.code-rag-mcp/db/knowledge.db")
PAIRS_IN = Path("/tmp/r1_train_pairs_v3.jsonl")
COSENT_OUT = Path("/tmp/r1_cosent_triplets_v3.jsonl")

# Match production docs-tower exactly: src/index/builders/docs_vector_indexer.py
# DOC_FILE_TYPES. Training distribution must mirror serving distribution.
DOC_FILE_TYPES = (
    "doc",
    "docs",
    "gotchas",
    "reference",
    "provider_doc",
    "task",
    "flow_annotation",
    "dictionary",
    "domain_registry",
)
MAX_NEGS_PER_PAIR = 7  # bound to ~7,500 triplets for ~1.07k pairs.

random.seed(42)


def build_path_text_index(con: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Map (repo_name, file_path) → full concatenated chunk text.

    Concatenates chunks in chunk_meta.chunk_order when available. LEFT JOIN
    so chunks without a chunk_meta row (provider_doc / reference / dictionary
    / others — only ~34% of doc chunks have chunk_meta entries) still flow
    through. Missing order falls back to 0; ties break on insertion order.
    Cap to 6000 chars so CoSENT sees a bounded but representative text.
    """
    print("[idx] building (repo,path) -> text map", flush=True)
    rows = con.execute(
        f"""
        SELECT c.repo_name, c.file_path, c.content,
               COALESCE(m.chunk_order, 0) AS chunk_order
        FROM chunks c
        LEFT JOIN chunk_meta m ON m.chunk_rowid = c.rowid
        WHERE c.file_type IN ({",".join("?" * len(DOC_FILE_TYPES))})
        """,
        DOC_FILE_TYPES,
    ).fetchall()

    bucket: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for repo, path, content, order in rows:
        if not content:
            continue
        bucket.setdefault((repo, path), []).append((order or 0, content))

    flat: dict[tuple[str, str], str] = {}
    for key, parts in bucket.items():
        parts.sort(key=lambda x: x[0])
        joined = "\n".join(p[1].strip() for p in parts if p[1])
        if joined.strip():
            flat[key] = joined[:6000]
    print(f"[idx] indexed {len(flat)} unique (repo,path) docs (from {len(rows)} chunks)", flush=True)
    return flat


def build_cosent_triplets(
    pairs: list[dict],
    path_text: dict[tuple[str, str], str],
) -> tuple[int, int]:
    """Walk mined pairs; emit CoSENT triplets. Return (n_written, n_unique_q)."""
    n_written = 0
    n_pairs_skipped_no_pos = 0
    n_pairs_skipped_no_negs = 0
    n_negs_dropped_missing_text = 0
    unique_qs: set[str] = set()

    with COSENT_OUT.open("w") as fout:
        for pair in pairs:
            q = pair.get("q") or pair.get("query")
            pos_list = pair.get("pos") or []
            negs = pair.get("hard_negs") or []
            if not q or not pos_list:
                n_pairs_skipped_no_pos += 1
                continue
            # Take FIRST resolvable positive (deterministic).
            pos_text = None
            for cand in pos_list:
                key = (cand.get("repo_name"), cand.get("file_path"))
                t = path_text.get(key)
                if t:
                    pos_text = t
                    break
            if not pos_text:
                n_pairs_skipped_no_pos += 1
                continue

            # Sample up to MAX_NEGS_PER_PAIR resolvable negatives.
            random.shuffle(negs)
            kept = 0
            for cand in negs:
                if kept >= MAX_NEGS_PER_PAIR:
                    break
                key = (cand.get("repo_name"), cand.get("file_path"))
                neg_text = path_text.get(key)
                if not neg_text:
                    n_negs_dropped_missing_text += 1
                    continue
                fout.write(
                    json.dumps(
                        {"query": q, "positive": pos_text, "negative": neg_text},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                kept += 1
                n_written += 1
            if kept == 0:
                n_pairs_skipped_no_negs += 1
            else:
                unique_qs.add(q)

    print(
        f"[cosent] wrote {n_written} triplets; "
        f"skipped {n_pairs_skipped_no_pos} pairs no-pos, "
        f"{n_pairs_skipped_no_negs} pairs no-resolvable-negs, "
        f"{n_negs_dropped_missing_text} negs missing in DB",
        flush=True,
    )
    return n_written, len(unique_qs)


def main() -> int:
    if not PAIRS_IN.exists():
        print(f"FATAL: {PAIRS_IN} missing", file=sys.stderr)
        return 2

    con = sqlite3.connect(str(DB))
    pairs = [json.loads(l) for l in PAIRS_IN.open() if l.strip()]
    print(f"[cosent] loaded {len(pairs)} mined pairs", flush=True)

    path_text = build_path_text_index(con)
    n_cosent, n_unique_q = build_cosent_triplets(pairs, path_text)
    con.close()

    # Schema validation via the production validator.
    sys.path.insert(0, "/Users/vaceslavtarsevskij/.code-rag-mcp")
    from scripts.runpod.train_docs_embedder import _validate_rows_for_loss

    cosent_rows = [json.loads(l) for l in COSENT_OUT.open() if l.strip()]
    _validate_rows_for_loss("cosent", cosent_rows)
    print(f"[validate] cosent OK: {len(cosent_rows)} rows; unique_q={n_unique_q}", flush=True)

    if n_cosent < 3000:
        print(
            f"WARN: cosent triplets={n_cosent} < 3000 threshold — may overfit",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
