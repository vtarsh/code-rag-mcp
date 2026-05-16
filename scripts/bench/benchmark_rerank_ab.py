#!/usr/bin/env python3
"""Blind A/B benchmark of CrossEncoder reranker models on PI tasks.

Pipeline per (task, model): FTS5 top-50 chunks -> CrossEncoder rerank ->
top-K repo deduplication -> recall@{10,25} vs task.repos_changed.
Models are exposed in output JSON under shuffled labels model_A..model_D;
real names are only written under the `_reveal` key at the end.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.search.fts import sanitize_fts_query

# FTS5 MATCH treats []{}()":, as syntax; strip so prefixes like
# "[APM] - Nuvei" or "Bancomat, satispay" don't error out.
_FTS_PRECLEAN = re.compile(r"[\[\]{}():\"',;]")


def preclean_for_fts(text: str) -> str:
    return _FTS_PRECLEAN.sub(" ", text)


DEFAULT_MODELS = [
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "cross-encoder/ms-marco-MiniLM-L-12-v2",
    "Alibaba-NLP/gte-reranker-modernbert-base",
    "BAAI/bge-reranker-v2-m3",
]
_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
TASKS_DB = _BASE / "db" / "tasks.db"
KNOWLEDGE_DB = _BASE / "db" / "knowledge.db"


def load_pi_tasks(db_path: Path, n: int, seed: int, prefix: str = "PI") -> list[dict]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    rows = conn.execute(
        "SELECT ticket_id, summary, repos_changed FROM task_history "
        "WHERE ticket_id LIKE ? AND repos_changed IS NOT NULL AND repos_changed != '[]'",
        (f"{prefix}-%",),
    ).fetchall()
    conn.close()

    tasks: list[dict] = []
    for r in rows:
        try:
            repos = json.loads(r["repos_changed"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not repos:
            continue
        tasks.append(
            {
                "ticket_id": r["ticket_id"],
                "summary": r["summary"] or "",
                "expected_repos": list(repos),
            }
        )

    rng = random.Random(seed)
    rng.shuffle(tasks)
    return tasks[:n]


def fetch_fts_candidates(conn: sqlite3.Connection, query_text: str, limit: int = 50) -> list[dict]:
    sanitized = sanitize_fts_query(preclean_for_fts(query_text))
    if not sanitized.strip():
        raise ValueError("empty_query_after_sanitize")
    try:
        rows = conn.execute(
            "SELECT rowid, content, repo_name, file_path FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
            (sanitized, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS5 syntax error (e.g. bracket chars) -> treat as no candidates
        return []
    return [
        {
            "rowid": r["rowid"],
            "content": r["content"] or "",
            "repo_name": r["repo_name"] or "",
            "file_path": r["file_path"] or "",
        }
        for r in rows
    ]


def rerank_and_rank(model, query: str, chunks: list[dict]) -> tuple[list[dict], float]:
    """Rerank chunks with CrossEncoder; return (sorted_chunks_desc, latency_s)."""
    pairs = [(query, c["content"][:1000]) for c in chunks]
    t0 = time.perf_counter()
    scores = model.predict(pairs, batch_size=16)
    latency = time.perf_counter() - t0
    order = sorted(range(len(chunks)), key=lambda i: float(scores[i]), reverse=True)
    return [chunks[i] for i in order], latency


def top_k_repos(ranked: list[dict], k: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in ranked:
        repo = c["repo_name"]
        if not repo or repo in seen:
            continue
        seen.add(repo)
        out.append(repo)
        if len(out) >= k:
            break
    return out


def compute_recall(ranked_repos: list[str], expected: set[str], k: int) -> float:
    if not expected:
        return 0.0
    return len(expected & set(ranked_repos[:k])) / len(expected)


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * (p / 100.0)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def save_results(out_path: Path, results: dict) -> None:
    with out_path.open("w") as f:
        json.dump(results, f, indent=2, default=str)


def benchmark_model(
    model_name: str,
    label: str,
    tasks: list[dict],
    fts_conn: sqlite3.Connection,
    fts_limit: int = 50,
) -> dict:
    from sentence_transformers import CrossEncoder

    entry: dict = {
        "cold_load_s": None,
        "recall_at_10_mean": 0.0,
        "recall_at_25_mean": 0.0,
        "p50_latency_s": 0.0,
        "p95_latency_s": 0.0,
        "per_task": [],
    }
    try:
        t0 = time.perf_counter()
        # trust_remote_code required for modernbert-based rerankers
        model = CrossEncoder(model_name, trust_remote_code=True)
        entry["cold_load_s"] = time.perf_counter() - t0
    except Exception as e:
        entry["load_failed"] = f"{type(e).__name__}: {e}"
        print(f"[{label}] load_failed: {type(e).__name__}", flush=True)
        return entry

    latencies: list[float] = []
    r10s: list[float] = []
    r25s: list[float] = []

    for task in tasks:
        ticket = task["ticket_id"]
        expected = set(task["expected_repos"])
        per: dict = {
            "ticket_id": ticket,
            "recall_at_10": None,
            "recall_at_25": None,
            "latency_s": None,
            "top_10_repos": [],
            "error": None,
        }
        try:
            chunks = fetch_fts_candidates(fts_conn, task["summary"], limit=fts_limit)
            if not chunks:
                per["error"] = "no_fts_candidates"
                per["recall_at_10"] = 0.0
                per["recall_at_25"] = 0.0
                entry["per_task"].append(per)
                r10s.append(0.0)
                r25s.append(0.0)
                print(f"[{label}] {ticket}: no_fts_candidates", flush=True)
                continue
            ranked, lat = rerank_and_rank(model, task["summary"], chunks)
            top25 = top_k_repos(ranked, 25)
            top10 = top25[:10]
            r10 = compute_recall(top10, expected, 10)
            r25 = compute_recall(top25, expected, 25)
            per.update(
                {
                    "recall_at_10": r10,
                    "recall_at_25": r25,
                    "latency_s": lat,
                    "top_10_repos": top10,
                }
            )
            latencies.append(lat)
            r10s.append(r10)
            r25s.append(r25)
            print(f"[{label}] {ticket}: r@10={r10:.2f} r@25={r25:.2f} lat={lat:.2f}s", flush=True)
        except Exception as e:
            per["error"] = f"{type(e).__name__}: {e}"
            print(f"[{label}] {ticket}: error {type(e).__name__}", flush=True)
        entry["per_task"].append(per)

    if r10s:
        entry["recall_at_10_mean"] = sum(r10s) / len(r10s)
        entry["recall_at_25_mean"] = sum(r25s) / len(r25s)
    if latencies:
        entry["p50_latency_s"] = percentile(latencies, 50)
        entry["p95_latency_s"] = percentile(latencies, 95)

    del model
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=50)
    ap.add_argument("--out", type=Path, default=Path("./rerank_ab_results.json"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--models", type=str, default=None, help="CSV of model names; default = all 4")
    ap.add_argument(
        "--ticket-prefix", type=str, default="PI", help="task_history.ticket_id prefix to sample (e.g. PI, CORE, BO)"
    )
    ap.add_argument("--fts-limit", type=int, default=50, help="FTS5 candidate pool size per task before rerank")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",")] if args.models else list(DEFAULT_MODELS)

    rng = random.Random(args.seed)
    shuffled = models[:]
    rng.shuffle(shuffled)
    labels = [f"model_{c}" for c in "ABCDEFGH"[: len(shuffled)]]
    label_to_name = dict(zip(labels, shuffled, strict=False))

    if args.dry_run:
        labels = labels[:1]
        label_to_name = {labels[0]: label_to_name[labels[0]]}

    tasks = load_pi_tasks(TASKS_DB, args.tasks, args.seed, args.ticket_prefix)
    if args.dry_run:
        tasks = tasks[:2]
    print(f"loaded {len(tasks)} tasks (seed={args.seed})", flush=True)

    results: dict = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seed": args.seed,
        "n_tasks": len(tasks),
        "fts_limit": args.fts_limit,
        "tasks": [
            {"ticket_id": t["ticket_id"], "summary": t["summary"], "expected_repos": t["expected_repos"]} for t in tasks
        ],
        "models": {},
    }
    save_results(args.out, results)

    fts_conn = sqlite3.connect(str(KNOWLEDGE_DB), timeout=30)
    fts_conn.row_factory = sqlite3.Row
    fts_conn.execute("PRAGMA query_only = ON")

    try:
        for label in labels:
            name = label_to_name[label]
            print(f"[{label}] starting (fts_limit={args.fts_limit})", flush=True)
            entry = benchmark_model(name, label, tasks, fts_conn, fts_limit=args.fts_limit)
            results["models"][label] = entry
            # intermediate save to survive crashes
            save_results(args.out, results)
    finally:
        fts_conn.close()

    results["_reveal"] = label_to_name
    save_results(args.out, results)
    print(f"done: {args.out}", flush=True)


if __name__ == "__main__":
    main()
