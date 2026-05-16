"""P1c follow-up — Local MiniLM judge for v12 candidate labeling.

Second of two independent judges for v12 candidate rows. This one is a small
cross-encoder (ms-marco-MiniLM-L-6-v2) that emits a 0..1 sigmoid relevance
score per (query, chunk) pair. No LLM reasoning — pure local model.

Expected bias: prose-leaning (docs > code). That's deliberate — the Opus
judge in parallel is code-biased (see `feedback_code_rag_judge_bias.md`).
Rows where the two judges disagree are the ones a human arbitrates.

Threshold map (fixed, decided upstream):
  score >= 0.45  -> "+"
  score <  0.15  -> "-"
  otherwise      -> "?"   (ambiguous band)

Chunk resolution:
  1. filesystem: `extracted/{repo_name}/{file_path}` (primary per spec)
  2. filesystem: `raw/{repo_name}/{file_path}` (fallback — some chunkers use raw)
  3. sqlite:    `chunks.content` for (repo_name, file_path) (doc-scraped repos
     like nuvei-docs/paynearme-docs have no filesystem copy — content only
     lives in the DB). `note: "db fallback"`.
  4. If none of the above: `label=?`, `note: "file not found"`.

Usage:
  python3.12 scripts/label_v12_candidates_minilm.py
  python3.12 scripts/label_v12_candidates_minilm.py --max-rows 50   # smoke test
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts._common import setup_paths

setup_paths()

MAX_CHARS = 4000
POS_THRESHOLD = 0.45
NEG_THRESHOLD = 0.15


def load_chunk_text(
    repo_name: str,
    file_path: str,
    *,
    repo_root: Path,
    db_cur: sqlite3.Cursor | None,
    max_chars: int = MAX_CHARS,
) -> tuple[str, str | None]:
    """Return (chunk_text, note). Note is None on filesystem hit.

    Resolution order: extracted/ -> raw/ -> sqlite fallback.
    """
    for base in ("extracted", "raw"):
        p = repo_root / base / repo_name / file_path
        if p.exists() and p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="replace")[:max_chars], None
            except OSError:
                pass
    if db_cur is not None:
        db_cur.execute(
            "SELECT content FROM chunks WHERE repo_name=? AND file_path=? LIMIT 1",
            (repo_name, file_path),
        )
        row = db_cur.fetchone()
        if row and row[0]:
            return str(row[0])[:max_chars], "db fallback"
    return "", "file not found"


def label_from_score(score: float) -> str:
    if score >= POS_THRESHOLD:
        return "+"
    if score < NEG_THRESHOLD:
        return "-"
    return "?"


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--input",
        type=Path,
        default=Path("profiles/pay-com/v12_candidates_regen.jsonl"),
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("profiles/pay-com/v12_candidates_regen_labeled_minilm.jsonl"),
    )
    p.add_argument("--db", type=Path, default=Path("db/knowledge.db"))
    p.add_argument(
        "--judge-model",
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
    )
    p.add_argument("--max-rows", type=int, default=0, help="0 = all rows")
    p.add_argument("--max-chars", type=int, default=MAX_CHARS)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = p.parse_args()

    if not args.input.exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1

    rows = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    if args.max_rows > 0:
        rows = rows[: args.max_rows]
    if not rows:
        print("ERROR: no rows loaded", file=sys.stderr)
        return 1
    print(f"loaded {len(rows)} candidate rows from {args.input}", flush=True)

    import torch
    from sentence_transformers import CrossEncoder

    print(f"loading judge: {args.judge_model}", flush=True)
    t0 = time.perf_counter()
    judge = CrossEncoder(args.judge_model, max_length=512)
    print(f"loaded in {time.perf_counter() - t0:.1f}s", flush=True)
    sigmoid = torch.nn.Sigmoid()

    db_cur: sqlite3.Cursor | None = None
    db_conn: sqlite3.Connection | None = None
    if args.db.exists():
        db_conn = sqlite3.connect(args.db)
        db_cur = db_conn.cursor()

    queries: list[str] = []
    texts: list[str] = []
    notes: list[str | None] = []
    t_load = time.perf_counter()
    for r in rows:
        text, note = load_chunk_text(
            r.get("repo_name", ""),
            r.get("file_path", ""),
            repo_root=args.repo_root,
            db_cur=db_cur,
            max_chars=args.max_chars,
        )
        queries.append(r.get("query") or "")
        texts.append(text)
        notes.append(note)
    print(f"loaded chunk texts in {time.perf_counter() - t_load:.1f}s", flush=True)

    pairs = [(q, t if t else " ") for q, t in zip(queries, texts, strict=False)]
    print(f"scoring {len(pairs)} pairs, batch_size={args.batch_size} ...", flush=True)
    t_score = time.perf_counter()
    raw_scores = judge.predict(pairs, batch_size=args.batch_size, activation_fn=sigmoid)
    score_time = time.perf_counter() - t_score
    print(f"scored in {score_time:.1f}s ({score_time / len(pairs) * 1000:.1f} ms/pair)", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    label_counts: dict[str, int] = {"+": 0, "-": 0, "?": 0}
    missing = 0
    used_scores: list[float] = []

    with args.output.open("w", encoding="utf-8") as out_f:
        for r, score, note in zip(rows, raw_scores, notes, strict=False):
            s = float(score)
            if note == "file not found":
                label = "?"
                missing += 1
            else:
                label = label_from_score(s)
                used_scores.append(s)
            label_counts[label] = label_counts.get(label, 0) + 1

            out = dict(r)  # preserve all input fields
            out["label"] = label
            out["minilm_score"] = round(s, 6)
            out["judge"] = "minilm-L6"
            if note:
                out["note"] = note
            elif "note" in out:
                pass
            out_f.write(json.dumps(out, ensure_ascii=False) + "\n")

    total_time = time.perf_counter() - t0
    mean = statistics.mean(used_scores) if used_scores else 0.0
    median = statistics.median(used_scores) if used_scores else 0.0
    stdev = statistics.stdev(used_scores) if len(used_scores) > 1 else 0.0

    print("\n=== SUMMARY ===", flush=True)
    print(f"output: {args.output}", flush=True)
    print(f"rows: {len(rows)}", flush=True)
    print(f"label_distribution: {label_counts}", flush=True)
    print(
        f"score_distribution: mean={mean:.4f} median={median:.4f} stdev={stdev:.4f} "
        f"(n={len(used_scores)}, excludes file-not-found)",
        flush=True,
    )
    print(f"file_not_found: {missing}", flush=True)
    print(f"total_runtime: {total_time:.1f}s", flush=True)

    if db_conn is not None:
        db_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
