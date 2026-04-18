#!/usr/bin/env python3
"""Prepare CrossEncoder fine-tune data from Jira ground-truth tasks.

Input:
    - db/tasks.db / task_history  -> PI tasks with files_changed + repos_changed
    - db/knowledge.db / chunks FTS5 -> code chunks to use as positives / mined negatives

Output (under --out, default profiles/pay-com/finetune_data/):
    - train.jsonl    positives + mined negatives + random negatives (label 0/1)
    - test.jsonl     positives only (no negatives -- eval set)
    - manifest.json  run metadata + dataset statistics

Positives:
    For every file in task.files_changed we take up to 5 chunks (longest first)
    and emit (task.summary, chunk.content[:1000], label=1).

Negatives (train only):
    - mined:  FTS5 top-50 for sanitize_fts_query(task.summary), filtered to drop
              any chunk whose file is in task.files_changed.
    - random: chunks from repos NOT in task.repos_changed.

Anti-leakage:
    - train/test ticket sets are disjoint.
    - no chunk from any file in any test task's files_changed appears in train.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.search.fts import sanitize_fts_query  # noqa: E402

# FTS5 MATCH treats []{}()":, as syntax; strip so summaries like
# "[APM] - Nuvei" don't cause sqlite3.OperationalError.
_FTS_PRECLEAN = re.compile(r"[\[\]{}():\"',;]")


def preclean_for_fts(text: str) -> str:
    return _FTS_PRECLEAN.sub(" ", text)


_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
TASKS_DB = _BASE / "db" / "tasks.db"
KNOWLEDGE_DB = _BASE / "db" / "knowledge.db"

MAX_CHUNKS_PER_FILE = 5
CONTENT_CHAR_LIMIT = 1000
FTS_TOP_FOR_MINING = 50


# ---------- date helpers --------------------------------------------------- #

def _parse_changelog_dates(raw: str | None) -> list[datetime]:
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    out: list[datetime] = []
    for e in entries or []:
        d = e.get("date") if isinstance(e, dict) else None
        if not d:
            continue
        try:
            out.append(datetime.fromisoformat(d))
        except ValueError:
            continue
    return out


def _resolution_date(raw: str | None) -> datetime | None:
    """Latest (max) status transition = closest approximation of 'resolution'."""
    dates = _parse_changelog_dates(raw)
    return max(dates) if dates else None


# ---------- task loading --------------------------------------------------- #

def load_gt_tasks(db_path: Path) -> list[dict]:
    """Load all PI tasks that have non-empty files_changed."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    rows = conn.execute(
        "SELECT ticket_id, summary, repos_changed, files_changed, status_changelog "
        "FROM task_history "
        "WHERE ticket_id LIKE 'PI-%' "
        "AND files_changed IS NOT NULL AND files_changed != '[]'"
    ).fetchall()
    conn.close()

    tasks: list[dict] = []
    for r in rows:
        try:
            files = json.loads(r["files_changed"] or "[]")
            repos = json.loads(r["repos_changed"] or "[]")
        except json.JSONDecodeError:
            continue
        if not files:
            continue
        tasks.append({
            "ticket_id": r["ticket_id"],
            "summary": (r["summary"] or "").strip(),
            "repos_changed": list(repos),
            "files_changed": list(files),
            "resolution": _resolution_date(r["status_changelog"]),
        })
    return tasks


# ---------- test-task selection ------------------------------------------- #

def pick_test_tasks(
    tasks: list[dict],
    requested_ids: list[str] | None,
) -> tuple[list[dict], list[dict]]:
    by_id = {t["ticket_id"]: t for t in tasks}

    if requested_ids:
        missing = [tid for tid in requested_ids if tid not in by_id]
        if missing:
            sys.exit(f"ERROR: test tasks not in DB (or empty files_changed): {missing}")
        test = [by_id[tid] for tid in requested_ids]
    else:
        # Auto: PI-54 pinned + 2 most-recent others (by resolution date desc).
        pinned = by_id.get("PI-54")
        if pinned is None:
            sys.exit("ERROR: PI-54 (required pinned test task) not in DB or has no files_changed")
        others = [t for t in tasks if t["ticket_id"] != "PI-54" and t["resolution"] is not None]
        others.sort(key=lambda t: t["resolution"], reverse=True)
        test = [pinned] + others[:2]

    if len(tasks) <= 3:
        sys.exit(f"ERROR: only {len(tasks)} GT tasks -- not enough to split train/test")

    test_ids = {t["ticket_id"] for t in test}
    train = [t for t in tasks if t["ticket_id"] not in test_ids]
    return train, test


# ---------- chunk lookup --------------------------------------------------- #

def fetch_chunks_for_file(
    conn: sqlite3.Connection,
    repo_name: str,
    file_path: str,
    limit: int = MAX_CHUNKS_PER_FILE,
) -> list[dict]:
    """Return up to `limit` chunks for (repo_name, file_path), longest first."""
    rows = conn.execute(
        "SELECT rowid, content, repo_name, file_path "
        "FROM chunks WHERE repo_name = ? AND file_path = ? "
        "ORDER BY LENGTH(content) DESC LIMIT ?",
        (repo_name, file_path, limit),
    ).fetchall()
    return [{"rowid": r["rowid"], "content": r["content"] or "",
             "repo_name": r["repo_name"] or "", "file_path": r["file_path"] or ""}
            for r in rows]


def split_qualified_path(qualified: str) -> tuple[str, str] | None:
    """'grpc-apm-trustly/methods/verify.js' -> ('grpc-apm-trustly', 'methods/verify.js')."""
    if "/" not in qualified:
        return None
    repo, rest = qualified.split("/", 1)
    return repo, rest


# ---------- positives ------------------------------------------------------ #

def build_positives(task: dict, conn: sqlite3.Connection) -> list[dict]:
    pairs: list[dict] = []
    for qualified in task["files_changed"]:
        split = split_qualified_path(qualified)
        if not split:
            continue
        repo, rel_path = split
        chunks = fetch_chunks_for_file(conn, repo, rel_path)
        for c in chunks:
            pairs.append({
                "ticket_id": task["ticket_id"],
                "query": task["summary"],
                "document": c["content"][:CONTENT_CHAR_LIMIT],
                "label": 1,
                "repo_name": c["repo_name"],
                "file_path": c["file_path"],
                "chunk_rowid": c["rowid"],
                "chunk_file": f"{c['repo_name']}/{c['file_path']}",
                "negative_type": None,
            })
    return pairs


# ---------- negatives ------------------------------------------------------ #

def fts_top_chunks(
    conn: sqlite3.Connection,
    query_text: str,
    limit: int = FTS_TOP_FOR_MINING,
) -> list[dict]:
    sanitized = sanitize_fts_query(preclean_for_fts(query_text))
    if not sanitized.strip():
        return []
    try:
        rows = conn.execute(
            "SELECT rowid, content, repo_name, file_path FROM chunks "
            "WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
            (sanitized, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"rowid": r["rowid"], "content": r["content"] or "",
             "repo_name": r["repo_name"] or "", "file_path": r["file_path"] or ""}
            for r in rows]


def mine_negatives(
    task: dict,
    conn: sqlite3.Connection,
    n_needed: int,
) -> list[dict]:
    if n_needed <= 0:
        return []
    gt_files = set(task["files_changed"])
    chunks = fts_top_chunks(conn, task["summary"], limit=FTS_TOP_FOR_MINING)
    out: list[dict] = []
    for c in chunks:
        qualified = f"{c['repo_name']}/{c['file_path']}"
        if qualified in gt_files:
            continue
        out.append({
            "ticket_id": task["ticket_id"],
            "query": task["summary"],
            "document": c["content"][:CONTENT_CHAR_LIMIT],
            "label": 0,
            "repo_name": c["repo_name"],
            "file_path": c["file_path"],
            "chunk_rowid": c["rowid"],
            "chunk_file": qualified,
            "negative_type": "mined",
        })
        if len(out) >= n_needed:
            break
    return out


def random_negatives(
    task: dict,
    conn: sqlite3.Connection,
    n_needed: int,
    rng: random.Random,
) -> list[dict]:
    if n_needed <= 0:
        return []
    repos = task["repos_changed"]
    if repos:
        placeholders = ",".join("?" * len(repos))
        sql = (
            f"SELECT rowid, content, repo_name, file_path FROM chunks "
            f"WHERE repo_name NOT IN ({placeholders}) "
            f"ORDER BY RANDOM() LIMIT ?"
        )
        # random seed for SQLite RANDOM() cannot be set; we additionally shuffle
        # the over-fetched pool with our seeded rng for determinism.
        rows = conn.execute(sql, (*repos, n_needed * 4)).fetchall()
    else:
        rows = conn.execute(
            "SELECT rowid, content, repo_name, file_path FROM chunks "
            "ORDER BY RANDOM() LIMIT ?",
            (n_needed * 4,),
        ).fetchall()
    pool = [dict(r) for r in rows]
    rng.shuffle(pool)
    out: list[dict] = []
    for c in pool[:n_needed]:
        out.append({
            "ticket_id": task["ticket_id"],
            "query": task["summary"],
            "document": (c["content"] or "")[:CONTENT_CHAR_LIMIT],
            "label": 0,
            "repo_name": c["repo_name"] or "",
            "file_path": c["file_path"] or "",
            "chunk_rowid": c["rowid"],
            "chunk_file": f"{c['repo_name'] or ''}/{c['file_path'] or ''}",
            "negative_type": "random",
        })
    return out


# ---------- writing -------------------------------------------------------- #

def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def verify_no_leakage(train_records: list[dict], test_tasks: list[dict]) -> None:
    """Assert no train record references a file that any test task touched.

    Checks both positives (would be false labels if model saw them) AND
    negatives (the model still gets trained on these chunks, so they must
    not be test-set files — otherwise the evaluator effectively re-sees
    what the trainer saw).
    """
    test_files = {qf for t in test_tasks for qf in t["files_changed"]}
    for r in train_records:
        assert r["chunk_file"] not in test_files, (
            f"LEAK: train {('positive' if r['label'] == 1 else r.get('negative_type', 'neg'))} "
            f"uses test file {r['chunk_file']} (ticket {r['ticket_id']})"
        )


# ---------- main ----------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-tasks", default="",
                    help="Comma-separated ticket IDs for test split. Empty = auto-pick.")
    ap.add_argument("--out", default="profiles/pay-com/finetune_data/",
                    help="Output directory.")
    ap.add_argument("--neg-mined-ratio", type=float, default=2.0,
                    help="Mined negatives per positive (train only).")
    ap.add_argument("--neg-random-ratio", type=float, default=1.0,
                    help="Random negatives per positive (train only).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--tasks-db", default=str(TASKS_DB))
    ap.add_argument("--knowledge-db", default=str(KNOWLEDGE_DB))
    args = ap.parse_args()

    rng = random.Random(args.seed)

    requested = [x.strip() for x in args.test_tasks.split(",") if x.strip()] or None

    tasks = load_gt_tasks(Path(args.tasks_db))
    if len(tasks) <= 3:
        sys.exit(f"ERROR: only {len(tasks)} PI tasks with ground truth -- aborting.")

    train_tasks, test_tasks = pick_test_tasks(tasks, requested)

    print(f"[info] GT tasks: {len(tasks)}  train: {len(train_tasks)}  test: {len(test_tasks)}")
    print("[test selection]")
    for t in test_tasks:
        res = t["resolution"].isoformat() if t["resolution"] else "unknown"
        print(f"  - {t['ticket_id']:<6} {res}  {t['summary']!r}")

    kconn = sqlite3.connect(args.knowledge_db, timeout=30)
    kconn.row_factory = sqlite3.Row
    kconn.execute("PRAGMA query_only = ON")

    # Test: positives only.
    test_records: list[dict] = []
    for t in test_tasks:
        test_records.extend(build_positives(t, kconn))

    # Train: positives + mined + random.
    # Drop any train positive whose chunk comes from a file that appears in
    # some test task's files_changed -- otherwise the model would see test
    # documents during training (anti-leakage).
    test_files_qualified = {qf for t in test_tasks for qf in t["files_changed"]}

    train_records: list[dict] = []
    n_pos = n_mined = n_rand = 0
    n_pos_dropped = 0
    n_neg_dropped = 0
    for t in train_tasks:
        raw_positives = build_positives(t, kconn)
        positives = [p for p in raw_positives if p["chunk_file"] not in test_files_qualified]
        n_pos_dropped += len(raw_positives) - len(positives)
        if not positives:
            continue
        raw_mined = mine_negatives(t, kconn,
                                   n_needed=int(round(len(positives) * args.neg_mined_ratio)))
        mined = [n for n in raw_mined if n["chunk_file"] not in test_files_qualified]
        raw_rand = random_negatives(t, kconn,
                                    n_needed=int(round(len(positives) * args.neg_random_ratio)),
                                    rng=rng)
        randneg = [n for n in raw_rand if n["chunk_file"] not in test_files_qualified]
        n_neg_dropped += (len(raw_mined) - len(mined)) + (len(raw_rand) - len(randneg))
        train_records.extend(positives)
        train_records.extend(mined)
        train_records.extend(randneg)
        n_pos += len(positives)
        n_mined += len(mined)
        n_rand += len(randneg)

    kconn.close()

    # Anti-leakage checks.
    train_ids = {t["ticket_id"] for t in train_tasks}
    test_ids = {t["ticket_id"] for t in test_tasks}
    assert train_ids.isdisjoint(test_ids), f"LEAK: overlapping ticket_ids {train_ids & test_ids}"
    verify_no_leakage(train_records, test_tasks)

    # Shuffle train (deterministic).
    rng.shuffle(train_records)

    out_dir = Path(args.out)
    train_path = out_dir / "train.jsonl"
    test_path = out_dir / "test.jsonl"
    manifest_path = out_dir / "manifest.json"

    write_jsonl(train_path, train_records)
    write_jsonl(test_path, test_records)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "neg_mined_ratio": args.neg_mined_ratio,
        "neg_random_ratio": args.neg_random_ratio,
        "tasks_db": str(Path(args.tasks_db).resolve()),
        "knowledge_db": str(Path(args.knowledge_db).resolve()),
        "train_tickets": sorted(train_ids),
        "train_tickets_effective": sorted({r["ticket_id"] for r in train_records}),
        "test_tickets": sorted(test_ids),
        "train_positives": n_pos,
        "train_positives_dropped_due_to_test_overlap": n_pos_dropped,
        "train_negatives_mined": n_mined,
        "train_negatives_random": n_rand,
        "train_negatives_dropped_due_to_test_overlap": n_neg_dropped,
        "train_total": len(train_records),
        "test_positives": len(test_records),
        "anti_leakage_verified": True,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    print(f"[out] {train_path}  ({len(train_records)} rows)")
    print(f"[out] {test_path}   ({len(test_records)} rows)")
    print(f"[out] {manifest_path}")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
