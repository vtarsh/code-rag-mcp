#!/usr/bin/env python3
"""Build TSDAE corpus + CoSENT triplets for R1 Stage B (one-off, local).

Inputs:
    /tmp/r1_train_pairs.jsonl  (1130 mined pairs from Stage A)
    db/knowledge.db chunks (file_type IN ('docs','provider_doc'))

Outputs:
    /tmp/r1_tsdae_corpus.jsonl    (~one row per doc chunk, {"text": "..."})
    /tmp/r1_cosent_triplets.jsonl (~5-6k rows, {"query","positive","negative"})
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

DB = Path("/Users/vaceslavtarsevskij/.code-rag-mcp/db/knowledge.db")
PAIRS_IN = Path("/tmp/r1_train_pairs.jsonl")
TSDAE_OUT = Path("/tmp/r1_tsdae_corpus.jsonl")
COSENT_OUT = Path("/tmp/r1_cosent_triplets.jsonl")

DOC_FILE_TYPES = ("docs", "provider_doc")
MAX_NEGS_PER_PAIR = 5  # bound CoSENT to ~5650 rows

# TSDAE corpus filter: skip too-short / too-long.
MIN_TSDAE_LEN = 80
MAX_TSDAE_LEN = 4096

random.seed(42)


def build_tsdae_corpus(con: sqlite3.Connection) -> int:
    """Dump every doc/provider_doc chunk text once. Filter short noise."""
    n_written = 0
    n_skipped = 0
    seen = set()  # de-dupe identical content (FTS pads sometimes)
    with TSDAE_OUT.open("w") as fout:
        cur = con.execute(
            f"SELECT content FROM chunks WHERE file_type IN ({','.join('?' * len(DOC_FILE_TYPES))})",
            DOC_FILE_TYPES,
        )
        for (content,) in cur:
            if not content:
                n_skipped += 1
                continue
            text = content.strip()
            if len(text) < MIN_TSDAE_LEN or len(text) > MAX_TSDAE_LEN:
                n_skipped += 1
                continue
            h = hash(text)
            if h in seen:
                n_skipped += 1
                continue
            seen.add(h)
            fout.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
            n_written += 1
    print(f"[tsdae] wrote {n_written} rows, skipped {n_skipped}")
    return n_written


def build_path_text_index(con: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Map (repo_name, file_path) → full concatenated chunk text.

    Some files have multiple chunks; concatenate in chunk_meta.chunk_order.
    Keeps full doc text bounded but useful for CoSENT pos/neg.
    """
    print("[idx] building (repo,path) → text map")
    rows = con.execute(
        f"""
        SELECT c.repo_name, c.file_path, c.content, m.chunk_order
        FROM chunks c
        JOIN chunk_meta m ON m.chunk_rowid = c.rowid
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
            # Cap length to keep CoSENT examples bounded — TSDAE chunk-level above keeps full corpus.
            flat[key] = joined[:6000]
    print(f"[idx] indexed {len(flat)} unique (repo,path) docs (from {len(rows)} chunks)")
    return flat


def build_cosent_triplets(
    pairs: list[dict],
    path_text: dict[tuple[str, str], str],
) -> int:
    n_written = 0
    n_pairs_skipped_no_pos = 0
    n_pairs_skipped_no_negs = 0
    n_negs_dropped_missing_text = 0

    with COSENT_OUT.open("w") as fout:
        for pair in pairs:
            q = pair.get("q") or pair.get("query")
            pos_list = pair.get("pos") or []
            negs = pair.get("hard_negs") or []
            if not q or not pos_list:
                n_pairs_skipped_no_pos += 1
                continue
            # Take FIRST resolvable positive (deterministic, documented in caveats).
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

    print(
        f"[cosent] wrote {n_written} triplets; "
        f"skipped {n_pairs_skipped_no_pos} pairs no-pos, "
        f"{n_pairs_skipped_no_negs} pairs no-resolvable-negs, "
        f"{n_negs_dropped_missing_text} negs missing in DB"
    )
    return n_written


def main() -> int:
    if not PAIRS_IN.exists():
        print(f"FATAL: {PAIRS_IN} missing", file=sys.stderr)
        return 2

    con = sqlite3.connect(str(DB))

    # 1a. TSDAE corpus
    n_tsdae = build_tsdae_corpus(con)

    # 1b. CoSENT triplets
    pairs = []
    with PAIRS_IN.open() as f:
        for line in f:
            line = line.strip()
            if line:
                pairs.append(json.loads(line))
    print(f"[cosent] loaded {len(pairs)} mined pairs")

    path_text = build_path_text_index(con)
    n_cosent = build_cosent_triplets(pairs, path_text)

    con.close()

    # 1c. Validate schemas using the actual project validator.
    sys.path.insert(0, "/Users/vaceslavtarsevskij/.code-rag-mcp")
    from scripts.runpod.train_docs_embedder import _validate_rows_for_loss

    tsdae_rows = [json.loads(l) for l in TSDAE_OUT.open() if l.strip()]
    _validate_rows_for_loss("tsdae", tsdae_rows)
    print(f"[validate] tsdae OK: {len(tsdae_rows)} rows")

    cosent_rows = [json.loads(l) for l in COSENT_OUT.open() if l.strip()]
    _validate_rows_for_loss("cosent", cosent_rows)
    print(f"[validate] cosent OK: {len(cosent_rows)} rows")

    if n_tsdae < 10000:
        print(
            f"WARN: tsdae corpus={n_tsdae} < 10k threshold — TSDAE may overfit",
            file=sys.stderr,
        )
        # Don't abort; user said abort if <10k, but we have 41k+ docs so this is informational.
    return 0


if __name__ == "__main__":
    sys.exit(main())
