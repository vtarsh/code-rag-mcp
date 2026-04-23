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

Daemon is paused via /admin/shutdown (pattern: embed_missing_vectors.py,
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
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.benchmark_rerank_ab import (
    compute_recall,
    fetch_fts_candidates,
    percentile,
    top_k_repos,
)
from scripts.eval_verdict import verdict_from_snapshot
from scripts.prepare_finetune_data import build_query_text, preclean_for_fts

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
    """POST /admin/shutdown so daemon frees its ML models before eval.

    Eval loads 2 CrossEncoders sequentially over 40 tasks x 200 candidates;
    daemon's ~1 GB resident MiniLM + MPS buffers would push us into Jetsam
    territory on 16 GB Mac. Daemon drains in-flight then exits; launchd
    restarts it fresh after eval finishes.
    """
    url = f"http://127.0.0.1:{port}/admin/shutdown"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        log.info("daemon on :%d shutdown requested; launchd will restart fresh", port)
        return True
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        if isinstance(reason, OSError) and reason.errno in {61, 111}:
            return False
        log.info("daemon shutdown failed: %s; continuing", reason)
        return False
    except Exception as e:
        log.info("daemon shutdown error: %s; continuing", e)
        return False

def load_all_gt_tasks(db_path: Path, projects: list[str] | None = None) -> list[dict]:
    """Load every ticket with non-empty repos_changed (no sampling).

    projects: list of Jira project prefixes ("PI","BO",...) or None = all.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if projects:
        placeholders = " OR ".join(f"ticket_id LIKE '{p}-%'" for p in projects)
        where = f"({placeholders}) AND repos_changed IS NOT NULL AND repos_changed != '[]'"
    else:
        where = "repos_changed IS NOT NULL AND repos_changed != '[]'"
    rows = conn.execute(
        "SELECT ticket_id, summary, description, jira_comments, repos_changed, "
        "files_changed FROM task_history "
        f"WHERE {where} ORDER BY ticket_id"
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
        comments = []
        raw_comments = r["jira_comments"]
        if raw_comments:
            try:
                comments = json.loads(raw_comments) or []
            except (TypeError, json.JSONDecodeError):
                comments = []
        # File-level GT (proposal §2): load alongside repos_changed. Missing /
        # malformed rows default to [] so callers never see KeyError and the
        # file-recall helpers can fold to 0.0 via `if not expected_files`.
        expected_files: list[str] = []
        raw_files = r["files_changed"] if "files_changed" in r.keys() else None
        if raw_files:
            try:
                parsed = json.loads(raw_files) or []
                expected_files = sorted({str(f) for f in parsed if f})
            except (TypeError, json.JSONDecodeError):
                expected_files = []
        tasks.append(
            {
                "ticket_id": r["ticket_id"],
                "summary": r["summary"] or "",
                "description": r["description"] or "",
                "jira_comments": comments,
                "expected_repos": list(repos),
                "expected_files": expected_files,
            }
        )
    return tasks

def _resolve_eval_query(task: dict, mode: str) -> str:
    """Pick query string for eval based on mode.

    mode=summary: raw Jira summary only (backward-compat with pre-2026-04-20 runs).
    mode=enriched: same composition as build_query_text in prepare_finetune_data.py
        (summary + description + fallback comments). Matches training distribution.
    """
    if mode == "enriched":
        return build_query_text(task, use_description=True) or task["summary"]
    return task["summary"]

def rerank_with_latency(model, query: str, chunks: list[dict], *, batch_size: int = 4) -> tuple[list[dict], float]:
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

# --- File-level GT upgrade (docs/eval_file_level_gt_proposal.md §2, §6) ---
#
# Gate schema versions — snapshots written before the file-level gate shipped
# carry `gate_version == GATE_VERSION_V1` and are evaluated with the legacy
# repo-only primary; new runs carry `GATE_VERSION_V2` and add
# `file_recall@10` as a co-primary with `DELTA_FILE_R10_THRESHOLD` as the
# minimum Δ for PROMOTE. Threshold is 0.01 (vs 0.02 on repo r@10) because
# file-level recall is strictly harder than repo-level and absolute deltas
# compress.
GATE_VERSION_V1 = "v1"
GATE_VERSION_V2 = "v2"
DELTA_FILE_R10_THRESHOLD = 0.01


def top_k_files(ranked: list[dict], k: int) -> list[tuple[str, str]]:
    """Return top-k unique (repo_name, file_path) tuples from a ranked chunk list.

    Analogue of `top_k_repos` at the file granularity. Dedup key is
    `(repo_name, file_path)` so two chunks from the same file collapse to one
    entry. Chunks missing either field are skipped.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for c in ranked:
        repo = c.get("repo_name") or ""
        fpath = c.get("file_path") or ""
        if not repo or not fpath:
            continue
        key = (repo, fpath)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= k:
            break
    return out


def compute_file_recall(
    ranked_files: list[tuple[str, str]],
    expected_files: list[str],
    k: int,
) -> float:
    """Fraction of `expected_files` present in the first `k` ranked file paths.

    Mirrors `compute_recall` but at file granularity. `ranked_files` is a
    list of `(repo_name, file_path)` tuples (typically from `top_k_files`);
    match is by `file_path` only because `task_history.files_changed` records
    paths without repo prefixes. Returns 0.0 when `expected_files` is empty
    (no ZeroDivisionError — matches the repo-level convention).
    """
    if not expected_files:
        return 0.0
    expected_set = set(expected_files)
    ranked_paths = {fp for _repo, fp in ranked_files[:k]}
    return len(expected_set & ranked_paths) / len(expected_set)


def _lpt_schedule(
    tasks: list[dict],
    latency_profile: dict[str, float],
    n_shards: int,
    shard_index: int,
) -> list[dict]:
    """Longest-Processing-Time greedy shard split.

    Sorts tasks by descending estimated latency, then assigns each to the
    currently least-loaded shard. Result: each shard gets a roughly equal sum
    of latency estimates rather than equal ticket count. Shard bound becomes
    ~max_ticket + avg_sum_per_shard instead of worst-case (sum of unlucky
    heavy tail on one shard).

    Tickets missing from the profile fall back to the median of known
    estimates (robust to outliers), or 10s if the profile is empty.

    First-time runs (no profile) should use the per-prefix stride path.
    """
    known = [v for v in latency_profile.values() if v is not None and v > 0]
    default = sorted(known)[len(known) // 2] if known else 10.0

    def estimate(task: dict) -> float:
        v = latency_profile.get(task["ticket_id"])
        return float(v) if v is not None and v > 0 else default

    sorted_tasks = sorted(tasks, key=estimate, reverse=True)

    shard_loads = [0.0] * n_shards
    shard_tasks: list[list[dict]] = [[] for _ in range(n_shards)]
    for task in sorted_tasks:
        min_shard = min(range(n_shards), key=lambda s: shard_loads[s])
        shard_loads[min_shard] += estimate(task)
        shard_tasks[min_shard].append(task)

    return shard_tasks[shard_index]

class _CrossEncoderAdapter:
    """Adapt a sentence_transformers.CrossEncoder to RerankerProvider.rerank(...).

    P0a: hybrid.rerank() expects `reranker.rerank(query, documents, limit)`
    returning a list of floats. Eval scripts instantiate raw CrossEncoder
    objects whose predict(pairs) returns a numpy array. This shim bridges the
    two so hybrid_search can drive the production pipeline with the eval
    model instead of the daemon-loaded one.
    """

    def __init__(self, model, *, batch_size: int = 4):
        self._model = model
        self._batch_size = batch_size

    @property
    def provider_name(self) -> str:
        return "eval_adapter"

    def rerank(self, query: str, documents: list[str], limit: int = 10) -> list[float]:
        pairs = [(query, doc) for doc in documents]
        scores = self._model.predict(pairs, batch_size=self._batch_size)
        return [float(s) for s in scores]

def eval_one_model_hybrid(
    model_name_or_path: str,
    label: str,
    tasks: list[dict],
    *,
    fts_limit: int,
    batch_size: int,
    max_length: int,
    query_mode: str = "summary",
    fts_fallback_enrich: bool = False,
) -> tuple[dict[str, dict], list[float]]:
    """P0a: eval variant that routes candidates through src.search.hybrid.

    Matches the production pipeline:
      FTS5 (150) + vector (50) → RRF → code_facts boost/inject + env_vars boost
      → content-type boosts → top RERANK_POOL_SIZE (default 200) → reranker
      (the eval model here, via _CrossEncoderAdapter).

    `fts_limit` is routed to hybrid.RERANK_POOL_SIZE via env var so the
    reranker sees the same candidate count the FTS-only path did (default 200).

    `fts_fallback_enrich` is accepted for call-site symmetry with
    `eval_one_model` but is a no-op here (see investigation notes at the
    retry site). 2026-04-22: neither conditional (cand≤50) nor unconditional
    enriched retries universally help — they swap ~5 fixed tickets for ~10
    newly-regressed ones on the partial hybrid subset.
    """
    from sentence_transformers import CrossEncoder

    from src.search.hybrid import hybrid_search

    os.environ["CODE_RAG_RERANK_POOL_SIZE"] = str(fts_limit)

    log.info("[%s] loading model (hybrid path): %s", label, model_name_or_path)
    t0 = time.perf_counter()
    model = CrossEncoder(
        model_name_or_path,
        trust_remote_code=True,
        max_length=max_length,
    )
    adapter = _CrossEncoderAdapter(model, batch_size=batch_size)
    log.info("[%s] loaded in %.1fs", label, time.perf_counter() - t0)

    per_task: dict[str, dict] = {}
    latencies: list[float] = []
    n_fallback_rescued = 0

    for task in tasks:
        ticket = task["ticket_id"]
        expected = set(task["expected_repos"])
        expected_files: list[str] = task.get("expected_files", []) or []
        entry: dict = {
            "recall_at_10": 0.0,
            "recall_at_25": 0.0,
            "rank_of_first_gt": None,
            "n_gt_repos": len(expected),
            "n_gt_files": len(expected_files),
            "file_recall_at_10": 0.0,
            "top_10_repos": [],
            "latency_s": None,
            "error": None,
            "fallback_used": False,
            "retrieval": "hybrid",
        }
        query = _resolve_eval_query(task, query_mode)
        query_clean = preclean_for_fts(query) if query_mode == "enriched" else query
        try:
            t_q = time.perf_counter()
            ranked, vec_err, total_candidates = hybrid_search(
                query_clean,
                limit=fts_limit,
                reranker_override=adapter,
            )
            lat = time.perf_counter() - t_q
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            per_task[ticket] = entry
            log.info("[%s] %s: hybrid_search error %s", label, ticket, type(e).__name__)
            continue

        _ = fts_fallback_enrich  # intentionally unused here
        _ = n_fallback_rescued  # unused; kept to preserve function shape

        if not ranked:
            entry["error"] = "no_hybrid_candidates"
            if vec_err:
                entry["error"] += f" (vec_err={vec_err})"
            per_task[ticket] = entry
            log.info("[%s] %s: no_hybrid_candidates (total=%d)", label, ticket, total_candidates)
            continue

        ranked_repos_full: list[str] = []
        seen_repos: set[str] = set()
        for r in ranked:
            repo_name = r.get("repo_name", "")
            if repo_name and repo_name not in seen_repos:
                seen_repos.add(repo_name)
                ranked_repos_full.append(repo_name)

        top10 = ranked_repos_full[:10]
        top25 = ranked_repos_full[:25]
        # File-level GT co-primary (proposal §2): dedup by (repo, file_path)
        # on the same `ranked` chunk list, then intersect with
        # task_history.files_changed. We keep chunks not truncated here —
        # `top_k_files(ranked, len(ranked))` is idempotent on already-sorted
        # input and lets compute_file_recall slice to k itself.
        ranked_files_full = top_k_files(ranked, len(ranked))
        entry.update(
            {
                "recall_at_10": compute_recall(top10, expected, 10),
                "recall_at_25": compute_recall(top25, expected, 25),
                "rank_of_first_gt": rank_of_first_gt(ranked_repos_full, expected),
                "file_recall_at_10": compute_file_recall(ranked_files_full, expected_files, 10),
                "top_10_repos": top10,
                "latency_s": lat,
            }
        )
        latencies.append(lat)
        per_task[ticket] = entry
        log.info(
            "[%s] %s: r@10=%.2f r@25=%.2f file_r@10=%.2f first_gt=%s lat=%.2fs (hybrid, cand=%d)",
            label,
            ticket,
            entry["recall_at_10"],
            entry["recall_at_25"],
            entry["file_recall_at_10"],
            entry["rank_of_first_gt"],
            lat,
            total_candidates,
        )

    del model, adapter
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

def eval_one_model(
    model_name_or_path: str,
    label: str,
    tasks: list[dict],
    fts_conn: sqlite3.Connection,
    *,
    fts_limit: int,
    batch_size: int,
    max_length: int,
    query_mode: str = "summary",
    fts_fallback_enrich: bool = False,
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
    n_fallback_rescued = 0

    for task in tasks:
        ticket = task["ticket_id"]
        expected = set(task["expected_repos"])
        expected_files: list[str] = task.get("expected_files", []) or []
        entry: dict = {
            "recall_at_10": 0.0,
            "recall_at_25": 0.0,
            "rank_of_first_gt": None,
            "n_gt_repos": len(expected),
            "n_gt_files": len(expected_files),
            "file_recall_at_10": 0.0,
            "top_10_repos": [],
            "latency_s": None,
            "error": None,
            "fallback_used": False,
        }
        query = _resolve_eval_query(task, query_mode)
        # Enriched query often has "Alias:" / colons / quotes from Jira
        # descriptions; FTS5 treats `word:` as a column-specifier and crashes
        # with "no such column". preclean_for_fts strips those reserved
        # punctuation chars — same function the training pipeline uses.
        fts_query = preclean_for_fts(query) if query_mode == "enriched" else query
        try:
            chunks = fetch_fts_candidates(fts_conn, fts_query, limit=fts_limit)
        except ValueError as e:
            entry["error"] = f"empty_query: {e}"
            per_task[ticket] = entry
            log.info("[%s] %s: empty_query", label, ticket)
            continue

        # Conditional enriched fallback: summary-mode tickets whose short Jira
        # title yields 0 FTS hits get a retry with build_query_text (summary +
        # description + fallback comments). ~8.5% of eval set (77/909) is pure
        # no_fts_candidates; most have non-empty descriptions. The blanket
        # enriched mode tested on PI lost ~15pp on tickets that already had
        # candidates (FTS pool drift from 80-char → 500+-char queries). The
        # CONDITIONAL form only triggers where summary already failed — can't
        # regress those tickets, can only rescue them.
        if not chunks and query_mode == "summary" and fts_fallback_enrich:
            enriched_query = build_query_text(task, use_description=True)
            enriched_fts_query = preclean_for_fts(enriched_query)
            if enriched_fts_query.strip():
                try:
                    chunks = fetch_fts_candidates(fts_conn, enriched_fts_query, limit=fts_limit)
                except ValueError:
                    chunks = []
                if chunks:
                    # Reranker was trained on enriched queries (build_query_text in
                    # prepare_finetune_data.py). Feeding it the enriched form
                    # matches training distribution; summary would be OOD.
                    query = enriched_query
                    entry["fallback_used"] = True
                    n_fallback_rescued += 1
                    log.info("[%s] %s: fallback→enriched rescued %d chunks", label, ticket, len(chunks))

        if not chunks:
            entry["error"] = "no_fts_candidates"
            per_task[ticket] = entry
            log.info("[%s] %s: no_fts_candidates", label, ticket)
            continue

        try:
            ranked, lat = rerank_with_latency(
                model,
                query,
                chunks,
                batch_size=batch_size,
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
        # File-level GT co-primary (proposal §2): parallel dedup on (repo,
        # file_path). Zero cost when a ticket has no expected_files
        # (compute_file_recall short-circuits to 0.0).
        ranked_files_full = top_k_files(ranked, len(ranked))
        entry.update(
            {
                "recall_at_10": compute_recall(top10, expected, 10),
                "recall_at_25": compute_recall(top25, expected, 25),
                "rank_of_first_gt": rank_of_first_gt(ranked_repos_full, expected),
                "file_recall_at_10": compute_file_recall(ranked_files_full, expected_files, 10),
                "top_10_repos": top10,
                "latency_s": lat,
            }
        )
        latencies.append(lat)
        per_task[ticket] = entry
        log.info(
            "[%s] %s: r@10=%.2f r@25=%.2f file_r@10=%.2f first_gt=%s lat=%.2fs",
            label,
            ticket,
            entry["recall_at_10"],
            entry["recall_at_25"],
            entry["file_recall_at_10"],
            entry["rank_of_first_gt"],
            lat,
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

    if fts_fallback_enrich and n_fallback_rescued:
        log.info("[%s] fallback→enriched rescued %d/%d tickets", label, n_fallback_rescued, len(tasks))

    return per_task, latencies

def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0

def aggregate(per_task: dict[str, dict], tickets: list[str]) -> dict:
    r10 = [per_task[t]["recall_at_10"] for t in tickets if t in per_task]
    r25 = [per_task[t]["recall_at_25"] for t in tickets if t in per_task]
    return {"r10_mean": mean(r10), "r25_mean": mean(r25), "n": len(r10)}

def build_delta(base: dict[str, dict], ft: dict[str, dict]) -> dict[str, dict]:
    """Per-ticket deltas. Format aligned with merge_eval_shards.build_delta
    post-2026-04-20 fix: key `rank_of_first_gt_delta` (was ambiguously named
    `rank_of_first_gt`). None if either baseline or ft didn't find GT.
    Negative rank delta = ft moved GT up (improvement).

    2026-04-22: added `file_recall_at_10` delta alongside — v2 gate primary.
    None if either side lacks the field (legacy per-task entries).
    """
    out: dict[str, dict] = {}
    for ticket in base:
        if ticket not in ft:
            continue
        b = base[ticket]
        f = ft[ticket]
        b_rank = b.get("rank_of_first_gt")
        f_rank = f.get("rank_of_first_gt")
        rank_delta: int | None
        if b_rank is None or f_rank is None:
            rank_delta = None
        else:
            rank_delta = f_rank - b_rank
        b_file = b.get("file_recall_at_10")
        f_file = f.get("file_recall_at_10")
        if b_file is None or f_file is None:
            file_delta: float | None = None
        else:
            file_delta = float(f_file) - float(b_file)
        out[ticket] = {
            "recall_at_10": f["recall_at_10"] - b["recall_at_10"],
            "recall_at_25": f["recall_at_25"] - b["recall_at_25"],
            "rank_of_first_gt_delta": rank_delta,
            "file_recall_at_10": file_delta,
        }
    return out

def find_regressions(delta: dict[str, dict], *, r10_drop_5pp: float = -0.05, r10_drop_10pp: float = -0.10) -> dict:
    drops_5 = [t for t, d in delta.items() if d["recall_at_10"] <= r10_drop_5pp]
    drops_10 = [t for t, d in delta.items() if d["recall_at_10"] <= r10_drop_10pp]
    drops_5.sort(key=lambda t: delta[t]["recall_at_10"])
    return {
        "count_r10_drop_gte_5pp": len(drops_5),
        "count_r10_drop_gte_10pp": len(drops_10),
        "tickets_regressed": [{"ticket": t, "r10_delta": round(delta[t]["recall_at_10"], 4)} for t in drops_5],
    }

# `decide_verdict` lives in scripts/eval_verdict.py — single source of truth.
# The old test-only gate (delta_test r@10, n=5 tickets) was replaced 2026-04-20
# with a full-eval gate (Δr@10 + ΔHit@5 + net_improved). See eval_verdict module.

def print_console_report(
    *,
    n_tasks: int,
    n_train: int,
    n_test: int,
    agg: dict,
    regressions: dict,
    base_lat: list[float],
    ft_lat: list[float],
    verdict: str,
    verdict_reason: str,
) -> None:
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
            print(f"  {row['ticket']}: r@10 delta {row['r10_delta'] * 100:+.1f} pp")
    print()
    print("Latency:")
    print(f"  baseline p50={percentile(base_lat, 50):.2f}s p95={percentile(base_lat, 95):.2f}s")
    print(f"  ft_v1    p50={percentile(ft_lat, 50):.2f}s p95={percentile(ft_lat, 95):.2f}s")
    print()
    print(f"Verdict: {verdict}")
    print(f"  {verdict_reason}")

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P5 eval: baseline vs FT reranker")
    p.add_argument("--base-model", default="cross-encoder/ms-marco-MiniLM-L-6-v2")
    p.add_argument("--ft-model", required=True, help="Path or HF id for fine-tuned reranker")
    p.add_argument("--history-out", type=Path, required=True, help="Snapshot JSON for regression tracking")
    p.add_argument("--fts-limit", type=int, default=200)
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--max-length", type=int, default=256)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--no-pause-daemon", action="store_true")
    p.add_argument("--projects", default="", help="Comma-separated Jira prefixes (PI,BO,CORE,HS). Empty = all.")
    p.add_argument("--manifest", type=Path, default=Path("profiles/pay-com/finetune_data/manifest.json"))
    p.add_argument(
        "--training-summary",
        type=Path,
        default=None,
        help="Override path; defaults to <ft-model>/training_summary.json",
    )
    p.add_argument(
        "--shard-index", type=int, default=0, help="0-based shard index; shard-mode enabled when shard-total > 1."
    )
    p.add_argument(
        "--shard-total",
        type=int,
        default=1,
        help="Total number of shards to run in parallel. Each process slices "
        "tasks[shard_index::shard_total] and writes a partial history-out "
        "file — no aggregate/verdict computed in shard mode.",
    )
    p.add_argument(
        "--reuse-baseline-from",
        type=Path,
        default=None,
        help="Path to a previous history_out JSON that shares base_model, "
        "fts_limit, seed. Its per_task_baseline is loaded directly and "
        "the baseline pass is skipped. 2x speedup when baseline "
        "hasn't changed between runs.",
    )
    p.add_argument(
        "--eval-query-mode",
        choices=["summary", "enriched"],
        default="summary",
        help="Query composition for eval. 'summary' = raw Jira summary "
        "(pre-2026-04-20 default; mismatches training distribution). "
        "'enriched' = build_query_text(summary+description+comments), "
        "matches prepare_finetune_data.py training query.",
    )
    p.add_argument(
        "--fts-fallback-enrich",
        action="store_true",
        default=False,
        help="When query_mode=summary and the candidate pool is effectively "
        "empty (0 FTS hits in fts_only mode), retry with build_query_text "
        "(summary+description) and use the enriched query for rerank. "
        "Targets the ~8.5%% of tickets (77/909) with summaries too "
        "short/generic for FTS. Applies symmetrically to baseline and FT "
        "so relative delta is fair. No-op in hybrid retrieval mode "
        "(investigation showed neither conditional nor unconditional "
        "enriched retries universally help — see eval_one_model_hybrid).",
    )
    p.add_argument(
        "--latency-profile",
        type=Path,
        default=None,
        help="Path to a previous history_out JSON used to LPT-balance shard "
        "assignment. The `per_task_baseline[*].latency_s` field drives a "
        "greedy longest-processing-time-first split instead of per-prefix "
        "stride. Eliminates heavy-tail bunching on slow shards (eval_v8_hybrid "
        "saw shard0 finish baseline while shard2 still on 145/175 BO due to "
        "20-25s outliers). Tickets missing from the profile get the median "
        "estimate. Absent flag → falls back to per-prefix stride.",
    )
    p.add_argument(
        "--use-hybrid-retrieval",
        action="store_true",
        default=False,
        help="P0a: route candidates through src.search.hybrid.hybrid_search "
        "(FTS + vector RRF + code_facts/env_vars wiring + content-type "
        "boosts) instead of FTS-only. Aligns the eval pool to the "
        "production serving pool. `--fts-limit` is passed through as "
        "CODE_RAG_RERANK_POOL_SIZE so the reranker still sees the same "
        "candidate count. Slower per ticket due to vector search but "
        "measures what production actually does.",
    )
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

    # ---- Load all GT tasks (projects filter optional) ----
    projects = [x.strip() for x in args.projects.split(",") if x.strip()] or None
    tasks = load_all_gt_tasks(TASKS_DB, projects=projects)
    log.info("loaded %d GT tasks (projects=%s)", len(tasks), projects or "all")

    # ---- Shard slicing (horizontal parallelism across multiple processes) ----
    # When --latency-profile is given, use LPT scheduling (balances wall time);
    # otherwise fall back to per-prefix stride (balances ticket count — OK for
    # first-ever hybrid run where no latency profile exists yet).
    if args.shard_total > 1:
        if not (0 <= args.shard_index < args.shard_total):
            raise ValueError(f"shard-index={args.shard_index} outside [0,{args.shard_total})")

        if args.latency_profile is not None:
            # LPT-balanced split using a previous snapshot's per-ticket latency.
            prof_path = args.latency_profile
            if not prof_path.is_absolute():
                prof_path = Path(__file__).resolve().parents[1] / prof_path
            prof_data = json.loads(prof_path.read_text(encoding="utf-8"))
            latency_map: dict[str, float] = {}
            for ticket_id, entry in (prof_data.get("per_task_baseline") or {}).items():
                lat = entry.get("latency_s") if isinstance(entry, dict) else None
                if lat is not None and lat > 0:
                    latency_map[ticket_id] = float(lat)
            log.info(
                "latency profile: loaded %d entries from %s (total tasks=%d)",
                len(latency_map),
                prof_path.name,
                len(tasks),
            )
            tasks = _lpt_schedule(tasks, latency_map, args.shard_total, args.shard_index)
            est_load = sum(latency_map.get(t["ticket_id"], 0.0) for t in tasks)
            log.info(
                "SHARD %d/%d -> processing %d tasks (LPT-balanced, est_load=%.1fs)",
                args.shard_index,
                args.shard_total,
                len(tasks),
                est_load,
            )
        else:
            # Per-prefix stride fallback (first-ever run, no profile available).
            # Shuffle once + stride within each ticket-id prefix group so each
            # shard gets roughly an equal share of every project. Heavy-tail
            # still clusters by latency — use --latency-profile on repeat runs.
            import random as _rnd
            from collections import defaultdict as _dd

            _rng = _rnd.Random(args.seed)
            _rng.shuffle(tasks)
            by_prefix: dict[str, list[dict]] = _dd(list)
            for t in tasks:
                pfx = t["ticket_id"].split("-", 1)[0]
                by_prefix[pfx].append(t)
            sharded: list[dict] = []
            for pfx in sorted(by_prefix):
                sharded.extend(by_prefix[pfx][args.shard_index :: args.shard_total])
            tasks = sharded
            log.info(
                "SHARD %d/%d -> processing %d tasks (per-prefix stride)",
                args.shard_index,
                args.shard_total,
                len(tasks),
            )

    fts_conn = sqlite3.connect(str(KNOWLEDGE_DB), timeout=30)
    fts_conn.row_factory = sqlite3.Row
    fts_conn.execute("PRAGMA query_only = ON")

    try:
        t_eval_start = time.time()

        if args.reuse_baseline_from is not None:
            reuse_path = args.reuse_baseline_from
            if not reuse_path.is_absolute():
                reuse_path = Path(__file__).resolve().parents[1] / reuse_path
            reuse = json.loads(reuse_path.read_text(encoding="utf-8"))
            if reuse.get("base_model") != args.base_model:
                raise ValueError(f"base_model mismatch: reuse={reuse.get('base_model')!r} current={args.base_model!r}")
            # Check every eval_config field that can change ranking. Previously
            # only fts_limit was compared, so a batch_size or max_length tweak
            # would silently mix old baseline numbers with fresh FT numbers.
            reuse_cfg = reuse.get("eval_config") or {}
            # Pre-2026-04-21 snapshots have no query_mode — it defaulted to
            # summary behaviour. Treat missing key as "summary" for back-compat.
            # Pre-2026-04-21-pm snapshots have no fts_fallback_enrich either — treat as False.
            # Pre-P0a snapshots have no retrieval_mode — treat as "fts_only".
            reuse_cfg_for_check = {
                "query_mode": "summary",
                "fts_fallback_enrich": False,
                "retrieval_mode": "fts_only",
                **reuse_cfg,
            }
            current_cfg = {
                "fts_limit": args.fts_limit,
                "batch_size": args.batch_size,
                "max_length": args.max_length,
                "seed": args.seed,
                "query_mode": args.eval_query_mode,
                "fts_fallback_enrich": args.fts_fallback_enrich,
                "retrieval_mode": "hybrid" if args.use_hybrid_retrieval else "fts_only",
            }
            for key, want in current_cfg.items():
                if reuse_cfg_for_check.get(key) != want:
                    raise ValueError(
                        f"eval_config.{key} mismatch: reuse={reuse_cfg_for_check.get(key)!r} "
                        f"current={want!r}. Re-run baseline fresh or align configs."
                    )
            # Keep only tickets that survive the current shard slice.
            wanted_ids = {t["ticket_id"] for t in tasks}
            base_per_task = {tid: v for tid, v in reuse["per_task_baseline"].items() if tid in wanted_ids}
            base_lat = [v.get("latency_s", 0.0) for v in base_per_task.values()]
            missing = wanted_ids - set(base_per_task)
            log.info(
                "=== baseline reused from %s (%d/%d tickets; %d missing) ===",
                reuse_path.name,
                len(base_per_task),
                len(wanted_ids),
                len(missing),
            )
            if missing:
                log.warning("reuse missing %d tickets — falling back to fresh baseline for them", len(missing))
                missing_tasks = [t for t in tasks if t["ticket_id"] in missing]
                if args.use_hybrid_retrieval:
                    extra_per_task, extra_lat = eval_one_model_hybrid(
                        args.base_model,
                        "baseline",
                        missing_tasks,
                        fts_limit=args.fts_limit,
                        batch_size=args.batch_size,
                        max_length=args.max_length,
                        query_mode=args.eval_query_mode,
                        fts_fallback_enrich=args.fts_fallback_enrich,
                    )
                else:
                    extra_per_task, extra_lat = eval_one_model(
                        args.base_model,
                        "baseline",
                        missing_tasks,
                        fts_conn,
                        fts_limit=args.fts_limit,
                        batch_size=args.batch_size,
                        max_length=args.max_length,
                        query_mode=args.eval_query_mode,
                        fts_fallback_enrich=args.fts_fallback_enrich,
                    )
                base_per_task.update(extra_per_task)
                base_lat.extend(extra_lat)
        else:
            retrieval_tag = "hybrid" if args.use_hybrid_retrieval else "fts_only"
            log.info("=== baseline pass (query_mode=%s, retrieval=%s) ===", args.eval_query_mode, retrieval_tag)
            if args.use_hybrid_retrieval:
                base_per_task, base_lat = eval_one_model_hybrid(
                    args.base_model,
                    "baseline",
                    tasks,
                    fts_limit=args.fts_limit,
                    batch_size=args.batch_size,
                    max_length=args.max_length,
                    query_mode=args.eval_query_mode,
                    fts_fallback_enrich=args.fts_fallback_enrich,
                )
            else:
                base_per_task, base_lat = eval_one_model(
                    args.base_model,
                    "baseline",
                    tasks,
                    fts_conn,
                    fts_limit=args.fts_limit,
                    batch_size=args.batch_size,
                    max_length=args.max_length,
                    query_mode=args.eval_query_mode,
                    fts_fallback_enrich=args.fts_fallback_enrich,
                )

        retrieval_tag = "hybrid" if args.use_hybrid_retrieval else "fts_only"
        log.info("=== ft_v1 pass (query_mode=%s, retrieval=%s) ===", args.eval_query_mode, retrieval_tag)
        if args.use_hybrid_retrieval:
            ft_per_task, ft_lat = eval_one_model_hybrid(
                args.ft_model,
                "ft_v1",
                tasks,
                fts_limit=args.fts_limit,
                batch_size=args.batch_size,
                max_length=args.max_length,
                query_mode=args.eval_query_mode,
                fts_fallback_enrich=args.fts_fallback_enrich,
            )
        else:
            ft_per_task, ft_lat = eval_one_model(
                args.ft_model,
                "ft_v1",
                tasks,
                fts_conn,
                fts_limit=args.fts_limit,
                batch_size=args.batch_size,
                max_length=args.max_length,
                query_mode=args.eval_query_mode,
                fts_fallback_enrich=args.fts_fallback_enrich,
            )

        eval_dur = time.time() - t_eval_start
        log.info("eval total: %.1fs (%.1f min)", eval_dur, eval_dur / 60.0)
    finally:
        fts_conn.close()

    # ---- Shard mode: write partial snapshot + exit (no aggregate/verdict) ----
    if args.shard_total > 1:
        shard_path = args.history_out.with_suffix(f".shard{args.shard_index}of{args.shard_total}.json")
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_snapshot = {
            "run_id": run_id,
            "shard": {"index": args.shard_index, "total": args.shard_total},
            "base_model": args.base_model,
            "ft_model_path": args.ft_model,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hyperparams": hyperparams,
            "eval_config": {
                "fts_limit": args.fts_limit,
                "batch_size": args.batch_size,
                "max_length": args.max_length,
                "seed": args.seed,
                "query_mode": args.eval_query_mode,
                "fts_fallback_enrich": args.fts_fallback_enrich,
                "retrieval_mode": "hybrid" if args.use_hybrid_retrieval else "fts_only",
            },
            "evaluated_tickets": [t["ticket_id"] for t in tasks],
            "per_task_baseline": base_per_task,
            "per_task_ft_v1": ft_per_task,
            "latency_baseline": base_lat,
            "latency_ft_v1": ft_lat,
        }
        shard_path.write_text(
            json.dumps(shard_snapshot, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("shard snapshot written: %s", shard_path)
        return 0

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

    # ---- Delta + regressions + verdict (full eval, not 5-ticket test split) ----
    per_task_delta = build_delta(base_per_task, ft_per_task)
    regressions_all = find_regressions(per_task_delta)

    verdict_result = verdict_from_snapshot(base_per_task, ft_per_task, per_task_delta)
    verdict = verdict_result.verdict
    reason = verdict_result.reason

    # ---- Build snapshot ----
    snapshot = {
        "run_id": run_id,
        "base_model": args.base_model,
        "ft_model_path": args.ft_model,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hyperparams": hyperparams,
        "eval_config": {
            "fts_limit": args.fts_limit,
            "batch_size": args.batch_size,
            "max_length": args.max_length,
            "seed": args.seed,
            "query_mode": args.eval_query_mode,
            "fts_fallback_enrich": args.fts_fallback_enrich,
            "retrieval_mode": "hybrid" if args.use_hybrid_retrieval else "fts_only",
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
        "verdict_metrics": verdict_result.metrics,
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
