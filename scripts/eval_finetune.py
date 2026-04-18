#!/usr/bin/env python3
"""P5 step 4: eval baseline vs fine-tuned CrossEncoder reranker on PI GT tasks.

For each PI ticket in task_history with non-empty repos_changed:
  1. FTS5 top-N candidates for the task summary (same sanitize path as
     scripts/benchmark_rerank_ab.py).
  2. Rerank with (a) baseline HF model and (b) fine-tuned checkpoint.
  3. Compute recall@10, recall@25, rank_of_first_gt (1-indexed) after
     dedup-by-repo, using the same helpers as benchmark_rerank_ab.

Writes a regression-tracking snapshot JSON (per-task baseline + ft,
per-task delta, aggregate train/test split, regressions list) and prints a
console report ending with PROMOTE / HOLD / REJECT verdict. Does NOT swap
any config.

Daemon is paused via /admin/unload (pattern: embed_missing_vectors.py,
finetune_reranker.py) so we don't hold 2 rerankers + daemon's MiniLM at
once during the 40-task run.
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.benchmark_rerank_ab import (  # noqa: E402
    compute_recall,
    fetch_fts_candidates,
    percentile,
    top_k_repos,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eval_finetune")


_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
TASKS_DB = _BASE / "db" / "tasks.db"
KNOWLEDGE_DB = _BASE / "db" / "knowledge.db"
DAEMON_PORT = int(os.getenv("CODE_RAG_DAEMON_PORT", "8742"))


def pause_daemon(port: int = DAEMON_PORT, timeout: float = 5.0) -> bool:
    """POST /admin/unload so daemon frees its ML models before eval.

    Eval loads 2 CrossEncoders sequentially over 40 tasks x 200 candidates;
    daemon's ~1 GB resident MiniLM + MPS buffers would push us into Jetsam
    territory on 16 GB Mac. Daemon exits and launchd restarts it fresh
    after eval finishes.
    """
    url = f"http://127.0.0.1:{port}/admin/unload"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        log.info("daemon on :%d unloaded + exiting for launchd restart", port)
        return True
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        if isinstance(reason, OSError) and reason.errno in {61, 111}:
            return False
        log.info("daemon unload failed: %s; continuing", reason)
        return False
    except Exception as e:
        log.info("daemon unload error: %s; continuing", e)
        return False


def load_all_pi_tasks(db_path: Path) -> list[dict]:
    """Load every PI ticket with non-empty repos_changed (no sampling)."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    rows = conn.execute(
        "SELECT ticket_id, summary, repos_changed FROM task_history "
        "WHERE ticket_id LIKE 'PI-%' AND repos_changed IS NOT NULL "
        "AND repos_changed != '[]' ORDER BY ticket_id"
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
        tasks.append({
            "ticket_id": r["ticket_id"],
            "summary": r["summary"] or "",
            "expected_repos": list(repos),
        })
    return tasks


def rerank_with_latency(model, query: str, chunks: list[dict],
                        *, batch_size: int = 4) -> tuple[list[dict], float]:
    pairs = [(query, c["content"][:1000]) for c in chunks]
    t0 = time.perf_counter()
    scores = model.predict(pairs, batch_size=batch_size)
    lat = time.perf_counter() - t0
    order = sorted(range(len(chunks)), key=lambda i: float(scores[i]), reverse=True)
    return [chunks[i] for i in order], lat


def rank_of_first_gt(ranked_repos: list[str], expected: set[str]) -> int | None:
    for i, r in enumerate(ranked_repos, start=1):
        if r in expected:
            return i
    return None


def eval_one_model(
    model_name_or_path: str,
    label: str,
    tasks: list[dict],
    fts_conn: sqlite3.Connection,
    *,
    fts_limit: int,
    batch_size: int,
    max_length: int,
) -> tuple[dict[str, dict], list[float]]:
    """Return (per_task_dict_keyed_by_ticket, latencies_list)."""
    from sentence_transformers import CrossEncoder

    log.info("[%s] loading model: %s", label, model_name_or_path)
    t0 = time.perf_counter()
    model = CrossEncoder(
        model_name_or_path,
        trust_remote_code=True,
        max_length=max_length,
    )
    log.info("[%s] loaded in %.1fs", label, time.perf_counter() - t0)

    per_task: dict[str, dict] = {}
    latencies: list[float] = []

    for task in tasks:
        ticket = task["ticket_id"]
        expected = set(task["expected_repos"])
        entry: dict = {
            "recall_at_10": 0.0,
            "recall_at_25": 0.0,
            "rank_of_first_gt": None,
            "n_gt_repos": len(expected),
            "top_10_repos": [],
            "latency_s": None,
            "error": None,
        }
        try:
            chunks = fetch_fts_candidates(fts_conn, task["summary"], limit=fts_limit)
        except ValueError as e:
            entry["error"] = f"empty_query: {e}"
            per_task[ticket] = entry
            log.info("[%s] %s: empty_query", label, ticket)
            continue

        if not chunks:
            entry["error"] = "no_fts_candidates"
            per_task[ticket] = entry
            log.info("[%s] %s: no_fts_candidates", label, ticket)
            continue

        try:
            ranked, lat = rerank_with_latency(
                model, task["summary"], chunks, batch_size=batch_size,
            )
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            per_task[ticket] = entry
            log.info("[%s] %s: rerank error %s", label, ticket, type(e).__name__)
            continue

        # Rank by repo (dedup) for recall + first-GT rank.
        ranked_repos_full = top_k_repos(ranked, len(ranked))
        top10 = ranked_repos_full[:10]
        top25 = ranked_repos_full[:25]
        entry.update({
            "recall_at_10": compute_recall(top10, expected, 10),
            "recall_at_25": compute_recall(top25, expected, 25),
            "rank_of_first_gt": rank_of_first_gt(ranked_repos_full, expected),
            "top_10_repos": top10,
            "latency_s": lat,
        })
        latencies.append(lat)
        per_task[ticket] = entry
        log.info(
            "[%s] %s: r@10=%.2f r@25=%.2f first_gt=%s lat=%.2fs",
            label, ticket,
            entry["recall_at_10"], entry["recall_at_25"],
            entry["rank_of_first_gt"], lat,
        )

    del model
    gc.collect()
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    return per_task, latencies


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def aggregate(per_task: dict[str, dict], tickets: list[str]) -> dict:
    r10 = [per_task[t]["recall_at_10"] for t in tickets if t in per_task]
    r25 = [per_task[t]["recall_at_25"] for t in tickets if t in per_task]
    return {"r10_mean": mean(r10), "r25_mean": mean(r25), "n": len(r10)}


def build_delta(base: dict[str, dict], ft: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ticket in base:
        if ticket not in ft:
            continue
        b = base[ticket]
        f = ft[ticket]
        # rank delta: smaller rank = better. None handling: if either is None,
        # emit null (can't compare).
        b_rank = b.get("rank_of_first_gt")
        f_rank = f.get("rank_of_first_gt")
        if b_rank is None or f_rank is None:
            rank_delta = None
        else:
            # Negative = ft moved GT up (better).
            rank_delta = f_rank - b_rank
        out[ticket] = {
            "recall_at_10": f["recall_at_10"] - b["recall_at_10"],
            "recall_at_25": f["recall_at_25"] - b["recall_at_25"],
            "rank_of_first_gt": rank_delta,
        }
    return out


def find_regressions(delta: dict[str, dict],
                     *, r10_drop_5pp: float = -0.05,
                     r10_drop_10pp: float = -0.10) -> dict:
    drops_5 = [t for t, d in delta.items() if d["recall_at_10"] <= r10_drop_5pp]
    drops_10 = [t for t, d in delta.items() if d["recall_at_10"] <= r10_drop_10pp]
    drops_5.sort(key=lambda t: delta[t]["recall_at_10"])
    return {
        "count_r10_drop_gte_5pp": len(drops_5),
        "count_r10_drop_gte_10pp": len(drops_10),
        "tickets_regressed": [
            {"ticket": t, "r10_delta": round(delta[t]["recall_at_10"], 4)}
            for t in drops_5
        ],
    }


def decide_verdict(agg_test_delta_r10: float, regressions: dict) -> tuple[str, str]:
    n_5pp = regressions["count_r10_drop_gte_5pp"]
    if agg_test_delta_r10 < 0 or n_5pp >= 3:
        return ("REJECT",
                f"delta_test r@10={agg_test_delta_r10:+.3f}, "
                f"{n_5pp} regressions >=5pp")
    if agg_test_delta_r10 >= 0.03 and n_5pp == 0:
        return ("PROMOTE",
                f"delta_test r@10={agg_test_delta_r10:+.3f}, no regressions")
    return ("HOLD",
            f"delta_test r@10={agg_test_delta_r10:+.3f}, "
            f"{n_5pp} minor regressions")


def print_console_report(*,
                         n_tasks: int, n_train: int, n_test: int,
                         agg: dict, regressions: dict,
                         base_lat: list[float], ft_lat: list[float],
                         verdict: str, verdict_reason: str) -> None:
    print()
    print("===== P5 eval: baseline vs ft_v1 =====")
    print(f"Tickets evaluated: {n_tasks} ({n_train} train + {n_test} test)")
    print()
    b = agg["baseline"]
    f = agg["ft_v1"]
    print("Aggregate (train):")
    print(f"  baseline  r@10={b['r10_mean_train']:.3f}  r@25={b['r25_mean_train']:.3f}")
    dtr10 = (f["r10_mean_train"] - b["r10_mean_train"]) * 100
    dtr25 = (f["r25_mean_train"] - b["r25_mean_train"]) * 100
    print(
        f"  ft_v1     r@10={f['r10_mean_train']:.3f}  r@25={f['r25_mean_train']:.3f}  "
        f"(Δ r@10={dtr10:+.1f} pp, Δ r@25={dtr25:+.1f} pp)"
    )
    print()
    print("Aggregate (test):")
    print(f"  baseline  r@10={b['r10_mean_test']:.3f}  r@25={b['r25_mean_test']:.3f}")
    dte10 = (f["r10_mean_test"] - b["r10_mean_test"]) * 100
    dte25 = (f["r25_mean_test"] - b["r25_mean_test"]) * 100
    print(
        f"  ft_v1     r@10={f['r10_mean_test']:.3f}  r@25={f['r25_mean_test']:.3f}  "
        f"(Δ r@10={dte10:+.1f} pp, Δ r@25={dte25:+.1f} pp)"
    )
    print()
    print("Regressions (r@10 dropped >=5pp):")
    if not regressions["tickets_regressed"]:
        print("  (none)")
    else:
        for row in regressions["tickets_regressed"]:
            print(f"  {row['ticket']}: r@10 delta {row['r10_delta']*100:+.1f} pp")
    print()
    print("Latency:")
    print(
        f"  baseline p50={percentile(base_lat, 50):.2f}s "
        f"p95={percentile(base_lat, 95):.2f}s"
    )
    print(
        f"  ft_v1    p50={percentile(ft_lat, 50):.2f}s "
        f"p95={percentile(ft_lat, 95):.2f}s"
    )
    print()
    print(f"Verdict: {verdict}")
    print(f"  {verdict_reason}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P5 eval: baseline vs FT reranker")
    p.add_argument("--base-model",
                   default="cross-encoder/ms-marco-MiniLM-L-6-v2")
    p.add_argument("--ft-model", required=True,
                   help="Path or HF id for fine-tuned reranker")
    p.add_argument("--history-out", type=Path, required=True,
                   help="Snapshot JSON for regression tracking")
    p.add_argument("--fts-limit", type=int, default=200)
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--max-length", type=int, default=256)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--no-pause-daemon", action="store_true")
    p.add_argument("--manifest", type=Path,
                   default=Path("profiles/pay-com/finetune_data/manifest.json"))
    p.add_argument("--training-summary", type=Path, default=None,
                   help="Override path; defaults to <ft-model>/training_summary.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    run_id = args.history_out.stem  # e.g. "v1"

    if not args.no_pause_daemon:
        pause_daemon()

    # ---- Load manifest train/test tickets ----
    manifest_path = args.manifest
    if not manifest_path.is_absolute():
        manifest_path = Path(__file__).resolve().parents[1] / manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    train_tickets: list[str] = list(manifest.get("train_tickets", []))
    test_tickets: list[str] = list(manifest.get("test_tickets", []))
    log.info("manifest: train=%d test=%d", len(train_tickets), len(test_tickets))

    # ---- Load training hyperparams ----
    ts_path = args.training_summary
    if ts_path is None:
        ts_path = Path(args.ft_model) / "training_summary.json"
    hyperparams: dict = {}
    if ts_path.is_file():
        try:
            hyperparams = json.loads(ts_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.info("training_summary read failed: %s", e)

    # ---- Load all PI tasks ----
    tasks = load_all_pi_tasks(TASKS_DB)
    log.info("loaded %d PI tasks with GT", len(tasks))

    fts_conn = sqlite3.connect(str(KNOWLEDGE_DB), timeout=30)
    fts_conn.row_factory = sqlite3.Row
    fts_conn.execute("PRAGMA query_only = ON")

    try:
        t_eval_start = time.time()

        log.info("=== baseline pass ===")
        base_per_task, base_lat = eval_one_model(
            args.base_model, "baseline", tasks, fts_conn,
            fts_limit=args.fts_limit,
            batch_size=args.batch_size,
            max_length=args.max_length,
        )

        log.info("=== ft_v1 pass ===")
        ft_per_task, ft_lat = eval_one_model(
            args.ft_model, "ft_v1", tasks, fts_conn,
            fts_limit=args.fts_limit,
            batch_size=args.batch_size,
            max_length=args.max_length,
        )

        eval_dur = time.time() - t_eval_start
        log.info("eval total: %.1fs (%.1f min)", eval_dur, eval_dur / 60.0)
    finally:
        fts_conn.close()

    # ---- Aggregates (train vs test split) ----
    evaluated_tickets = [t["ticket_id"] for t in tasks]
    train_in_eval = [t for t in train_tickets if t in evaluated_tickets]
    test_in_eval = [t for t in test_tickets if t in evaluated_tickets]

    agg_base_train = aggregate(base_per_task, train_in_eval)
    agg_base_test = aggregate(base_per_task, test_in_eval)
    agg_ft_train = aggregate(ft_per_task, train_in_eval)
    agg_ft_test = aggregate(ft_per_task, test_in_eval)

    agg = {
        "baseline": {
            "r10_mean_train": round(agg_base_train["r10_mean"], 4),
            "r25_mean_train": round(agg_base_train["r25_mean"], 4),
            "r10_mean_test": round(agg_base_test["r10_mean"], 4),
            "r25_mean_test": round(agg_base_test["r25_mean"], 4),
            "n_train_evaluated": agg_base_train["n"],
            "n_test_evaluated": agg_base_test["n"],
        },
        "ft_v1": {
            "r10_mean_train": round(agg_ft_train["r10_mean"], 4),
            "r25_mean_train": round(agg_ft_train["r25_mean"], 4),
            "r10_mean_test": round(agg_ft_test["r10_mean"], 4),
            "r25_mean_test": round(agg_ft_test["r25_mean"], 4),
            "n_train_evaluated": agg_ft_train["n"],
            "n_test_evaluated": agg_ft_test["n"],
        },
        "delta_train": {
            "r10": round(agg_ft_train["r10_mean"] - agg_base_train["r10_mean"], 4),
            "r25": round(agg_ft_train["r25_mean"] - agg_base_train["r25_mean"], 4),
        },
        "delta_test": {
            "r10": round(agg_ft_test["r10_mean"] - agg_base_test["r10_mean"], 4),
            "r25": round(agg_ft_test["r25_mean"] - agg_base_test["r25_mean"], 4),
        },
    }

    # ---- Delta + regressions (test-set focus for verdict, but overall for snapshot) ----
    per_task_delta = build_delta(base_per_task, ft_per_task)
    regressions_all = find_regressions(per_task_delta)

    verdict, reason = decide_verdict(agg["delta_test"]["r10"], regressions_all)

    # ---- Build snapshot ----
    snapshot = {
        "run_id": run_id,
        "base_model": args.base_model,
        "ft_model_path": args.ft_model,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hyperparams": hyperparams,
        "eval_config": {
            "fts_limit": args.fts_limit,
            "batch_size": args.batch_size,
            "max_length": args.max_length,
            "seed": args.seed,
        },
        "train_tickets": train_tickets,
        "test_tickets": test_tickets,
        "evaluated_tickets": evaluated_tickets,
        "per_task_baseline": base_per_task,
        "per_task_ft_v1": ft_per_task,
        "per_task_delta": per_task_delta,
        "aggregate": agg,
        "regressions": regressions_all,
        "latency": {
            "baseline_p50_s": round(percentile(base_lat, 50), 3),
            "baseline_p95_s": round(percentile(base_lat, 95), 3),
            "ft_v1_p50_s": round(percentile(ft_lat, 50), 3),
            "ft_v1_p95_s": round(percentile(ft_lat, 95), 3),
        },
        "verdict": verdict,
        "verdict_reason": reason,
    }

    args.history_out.parent.mkdir(parents=True, exist_ok=True)
    args.history_out.write_text(
        json.dumps(snapshot, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("snapshot written: %s", args.history_out)

    print_console_report(
        n_tasks=len(evaluated_tickets),
        n_train=len(train_in_eval),
        n_test=len(test_in_eval),
        agg=agg,
        regressions=regressions_all,
        base_lat=base_lat,
        ft_lat=ft_lat,
        verdict=verdict,
        verdict_reason=reason,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
