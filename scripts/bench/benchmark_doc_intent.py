#!/usr/bin/env python3
"""Doc-intent retrieval bench: file-level Recall@10 across docs-tower candidates.

DEFAULT MODE (rerank-off): bypasses router (`src/search/hybrid.py::_query_wants_docs`)
and reranker (`cross-encoder/ms-marco-MiniLM-L-6-v2`) — measures the raw vector
tower in isolation. That is the only fair signal for "is the new docs model
better?" because router + reranker would mask retrieval-side gains.

E2E MODE (--rerank-on, P9 2026-04-25): retrieves top-50 from the bi-encoder,
runs the production CrossEncoder reranker over those candidates, then scores
R@10 on the reranked top-10. This measures the FULL production pipeline
(docs-tower → reranker → top-10) for deploy decisions. Router is still
bypassed (we already know we want the docs tower for this eval set).

Inputs:
    profiles/pay-com/eval/doc_intent_eval_v1.jsonl   (frozen pseudo-gold seed, n≈40)
    db/vectors.lance.docs[.<key>]/chunks         (per-model LanceDB tables)

Output: bench_runs/doc_intent_<key>_<ts>.json (one per model)
        bench_runs/doc_intent_summary_<ts>.json (cross-model comparison)

Metrics (per model):
    - recall_at_10 = mean over rows of |E ∩ top10| / min(|E|, 10)
    - ndcg_at_10   = mean over rows of DCG@10 / IDCG@10 (binary relevance)
    - hit_at_5     = mean over rows of [|E ∩ top5| > 0]
    - hit_at_10    = mean over rows of [|E ∩ top10| > 0]
    - per_stratum_recall = recall_at_10 grouped by stratum tag
                           (payout/provider/nuvei/webhook/method/interac/refund/trustly/aircash)
    - latency_p95_ms      = p95 over per-query (encode + retrieve) ms

Eval rows from doc_intent_eval_v1.jsonl carry `gold=False` + `expected_paths`
(labeler=auto-heuristic-v1). We treat every row as scoreable pseudo-gold and
log `n_gold_rows` separately so the labeler-trust caveat is visible.

The prod-only top-K dump (rows without expected_paths) is preserved for
diagnostic hand-inspection.

Sequential model loads only. After each candidate the model is del'd, gc'd,
and MPS cache cleared. Pre-flight default requires sys-avail >= 3.5 GB
(was 5.0; lowered because daemon co-residency squeezes the budget). Pass
`--no-pre-flight` to bypass entirely.

Usage:
    python3 scripts/benchmark_doc_intent.py
    python3 scripts/benchmark_doc_intent.py --only docs-gte-large
    python3 scripts/benchmark_doc_intent.py --models docs,docs-gte-large
    python3 scripts/benchmark_doc_intent.py --eval=profiles/pay-com/eval/doc_intent_eval_v1.jsonl --model=docs
    python3 scripts/benchmark_doc_intent.py --no-pre-flight
    python3 scripts/benchmark_doc_intent.py --compare baseline.json candidate.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import gc
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".code-rag-mcp")))
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "db" / "knowledge.db"
EVAL_PATH = ROOT / "profiles" / "pay-com" / "eval" / "doc_intent_eval_v1.jsonl"
BENCH_DIR = ROOT / "bench_runs"

PREFLIGHT_AVAIL_HARD_GB = 3.5  # was 5.0 — daemon co-residency made the gate too tight
TOP_K = 10

# E2E mode (P9 2026-04-25): top-K from bi-encoder fed into the reranker. 50
# matches the production hybrid pipeline (`src/search/hybrid.py::vector_search`
# limit=50), so the reranker sees the same candidate pool size as live traffic.
E2E_RETRIEVAL_K = 50

# Production reranker. Default is the production CrossEncoder; can be
# overridden via `CODE_RAG_BENCH_RERANKER=<hf-id>` for A/B testing alternative
# rerankers (P10 A4 V3 swap experiment). When overridden, the manifest's
# `rerank_model` field reflects the override so downstream tooling sees which
# model produced each result JSON.
E2E_RERANKER_MODEL = os.getenv(
    "CODE_RAG_BENCH_RERANKER",
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
)

# AND-gate thresholds (used by --compare; see eval-methodology-verdict.md F4).
GATE_RECALL_LIFT = 0.10  # candidate.recall_at_10 >= baseline + 0.10
GATE_NDCG_LIFT = 0.05  # candidate.ndcg_at_10  >= baseline + 0.05
GATE_PER_STRATUM_DROP = -0.15  # no stratum drop > 15 pp
GATE_HIT5_DROP = -0.05  # hit_at_5 floor: not worse than baseline - 5 pp
GATE_LATENCY_RATIO = 2.0  # candidate.latency_p95_ms < 2x baseline

DEFAULT_MODELS: tuple[str, ...] = (
    "docs",  # incumbent baseline (nomic-embed-text-v1.5)
    "docs-gte-large",
    "docs-arctic-l-v2",
    "docs-bge-m3-dense",
)


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _avail_gb() -> float:
    import psutil

    return psutil.virtual_memory().available / 1024**3


def load_eval(path: Path) -> list[dict]:
    if not path.exists():
        print(
            f"ERROR: eval set not found at {path}. Pass --eval=<path> with an existing JSONL.",
            file=sys.stderr,
        )
        sys.exit(2)
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _row_expected(row: dict) -> list[tuple[str, str]]:
    """Return list of (repo_name, file_path) tuples for a row.

    Tolerates both legacy (`expected_files`) and current (`expected_paths`) keys.
    Current eval JSONLs use `expected_paths`; the legacy bench used
    `expected_files` and `gold=True` — we accept both.
    """
    raw = row.get("expected_paths") or row.get("expected_files") or []
    out: list[tuple[str, str]] = []
    for entry in raw:
        repo = entry.get("repo_name")
        path = entry.get("file_path")
        if repo and path:
            out.append((repo, path))
    return out


def _row_strata(row: dict) -> list[str]:
    """Return list of stratum tags for a row, or ['__none__'] if absent."""
    strata = row.get("strata") or []
    if isinstance(strata, list) and strata:
        return [str(s) for s in strata]
    return ["__none__"]


def _percentile(samples: list[float], p: float) -> float:
    """Plain-Python percentile (no numpy dependency).

    `p` in [0,1]. Linear interpolation between order statistics.
    Returns 0.0 on empty input.
    """
    if not samples:
        return 0.0
    s = sorted(samples)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _recall_at_k(expected: set[tuple[str, str]], retrieved_in_order: list[tuple[str, str]], k: int) -> float:
    """True Recall@K = |E ∩ top_k| / min(|E|, K). Returns 0.0 if expected empty."""
    if not expected:
        return 0.0
    top_k = set(retrieved_in_order[:k])
    return len(expected & top_k) / min(len(expected), k)


def _ndcg_at_k(expected: set[tuple[str, str]], retrieved_in_order: list[tuple[str, str]], k: int) -> float:
    """Binary-relevance nDCG@K. IDCG = sum(1/log2(i+1)) for i=1..min(|E|,k)."""
    if not expected:
        return 0.0
    dcg = 0.0
    for i, key in enumerate(retrieved_in_order[:k], start=1):
        if key in expected:
            dcg += 1.0 / math.log2(i + 1)
    ideal_n = min(len(expected), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_n + 1))
    return dcg / idcg if idcg > 0 else 0.0


def _hit_at_k(expected: set[tuple[str, str]], retrieved_in_order: list[tuple[str, str]], k: int) -> int:
    """Binary Hit@K (1 if any expected appears in top-k, else 0)."""
    if not expected:
        return 0
    top_k = set(retrieved_in_order[:k])
    return 1 if (expected & top_k) else 0


def open_table(lance_dir: Path):
    """Open the per-model LanceDB chunks table. Raise if missing."""
    import lancedb

    if not lance_dir.exists():
        raise FileNotFoundError(f"{lance_dir} missing — build it via scripts/benchmark_doc_indexing_ab.py")
    db = lancedb.connect(str(lance_dir))
    if "chunks" not in db.table_names():
        raise RuntimeError(f"{lance_dir} has no 'chunks' table")
    return db.open_table("chunks")


def load_st(cfg):
    """Load SentenceTransformer on best device (mps > cuda > cpu).

    Also applies the U1 patch (`_fix_gte_persistent_false_buffers`) so any
    `gte-*-en-v1.5` candidate has its rotary + position_ids buffers re-seeded
    after `from_pretrained` (transformers >= 5 + accelerate lazy-init drops
    persistent=False buffer values silently). No-op on non-new-impl bases.
    """
    import torch
    from sentence_transformers import SentenceTransformer

    device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    model = SentenceTransformer(cfg.name, trust_remote_code=cfg.trust_remote_code, device=device)
    try:
        from src.index.builders.docs_vector_indexer import (
            _fix_gte_persistent_false_buffers,
        )

        _fix_gte_persistent_false_buffers(model)
    except Exception as e:  # pragma: no cover - bench safety net
        print(f"[bench] WARN _fix_gte_persistent_false_buffers skipped: {e}")
    return model, device


def load_reranker(model_name: str = E2E_RERANKER_MODEL):
    """Load the production CrossEncoder reranker.

    Returns the loaded model. Used by the --rerank-on path to rerank top-50
    bi-encoder candidates down to top-10 before scoring R@10. Mirrors the
    production wiring in `src.embedding_provider.LocalRerankerProvider`, but
    is kept independent of the daemon container so the bench is hermetic.
    """
    from sentence_transformers import CrossEncoder

    return CrossEncoder(model_name)


def rerank_candidates(
    reranker,
    query: str,
    candidates: list[dict],
    limit: int = TOP_K,
) -> list[dict]:
    """Rerank a list of bi-encoder candidates using the CrossEncoder.

    Candidates is a list of dicts with at least `repo_name`, `file_path`, and
    `content_preview` (the text snippet from the LanceDB chunks table). We
    score each (query, "<repo> <path> <snippet>") pair and re-sort descending.

    This is intentionally pure-rerank (no RRF blending), because in the bench
    the bi-encoder ranking already comes from a vector-only pool (no FTS leg).
    Production blends 70% rerank + 30% RRF, but since RRF here is a single-
    source ranking it would just preserve the bi-encoder order — pure rerank
    is the cleaner head-to-head.
    """
    if not candidates:
        return []
    if reranker is None:
        return candidates[:limit]

    docs: list[str] = []
    for c in candidates:
        snippet = c.get("content_preview", "") or ""
        repo = c.get("repo_name", "") or ""
        path = c.get("file_path", "") or ""
        docs.append(f"{repo} {path} {snippet}")

    pairs = [(query, d) for d in docs]
    scores = reranker.predict(pairs)

    # Attach scores; sort by reranker score desc; return top-`limit`.
    scored = list(zip(candidates, [float(s) for s in scores], strict=False))
    scored.sort(key=lambda x: x[1], reverse=True)
    out = []
    for c, s in scored[:limit]:
        c2 = dict(c)
        c2["rerank_score"] = s
        out.append(c2)
    return out


def evaluate_model(
    key: str,
    eval_rows: list[dict],
    common: dict,
    pre_flight: bool = True,
    rerank_on: bool = False,
    reranker=None,
    stratum_gated: bool = False,
    dedupe: bool = False,
) -> dict:
    """Run TOP_K vector search per query, score multi-metric, log prod top-K.

    `rerank_on` (P9 2026-04-25): when True, retrieve E2E_RETRIEVAL_K from the
    bi-encoder, rerank with `reranker`, then keep top TOP_K for scoring. The
    `reranker` argument must be a loaded CrossEncoder (use `load_reranker()`).
    Caller passes a single shared reranker so it's loaded once per `main()`
    invocation, not once per model.

    `stratum_gated` (P10 A2 2026-04-26): when True (and `rerank_on` is True),
    consult `src.search.hybrid._should_skip_rerank` per query. Skips the
    reranker on OFF strata (nuvei/aircash/trustly/webhook/refund) and runs it
    on KEEP strata (interac/provider) + unknown strata. This measures the
    A2 deploy candidate.

    `dedupe` (P10 A2 verification 2026-04-26): when True, dedupe the post-rank
    candidate list by (repo_name, file_path) keeping the first (highest-rank)
    occurrence per file BEFORE truncating to TOP_K. This measures "recall over
    top-10 UNIQUE documents" — the LLM-judge-corroborated user-experience
    metric. Both LLM judges (`a2-verification/llm_judge_opus.md`,
    `a2-verification/llm_judge_opus2.md`) flagged that ~80% of eval rows have
    duplicate file_paths in top-10 (avg 7.14 unique vs 7 max for A2; 6.78 for
    rerank-on). Dedupe makes the bench match how a user actually experiences
    the result list (one slot = one document).
    """
    from src.models import EMBEDDING_MODELS

    if key not in EMBEDDING_MODELS:
        return {
            "model_key": key,
            "skipped_reason": "not_registered",
        }

    cfg = EMBEDDING_MODELS[key]
    lance_dir = ROOT / "db" / cfg.lance_dir

    print()
    print("=" * 60)
    print(f"EVAL: {key}  ({cfg.name})")
    print(f"  lance_dir   = {lance_dir}")
    print(f"  query_pref  = {cfg.query_prefix!r}")
    print("=" * 60)

    avail = _avail_gb()
    if pre_flight and avail < PREFLIGHT_AVAIL_HARD_GB:
        print(
            f"  SKIP {key}: sys-avail={avail:.2f}G < {PREFLIGHT_AVAIL_HARD_GB}G",
            file=sys.stderr,
        )
        return {
            "model_key": key,
            "skipped_reason": "low_memory",
            "avail_gb": round(avail, 2),
        }

    try:
        table = open_table(lance_dir)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"  SKIP {key}: {exc}", file=sys.stderr)
        return {"model_key": key, "skipped_reason": "no_table", "detail": str(exc)}

    t0 = time.time()
    model, device = load_st(cfg)
    load_s = time.time() - t0
    print(f"  loaded model on {device} in {load_s:.1f}s")

    eval_results: list[dict] = []  # rows that have expected_paths (scoreable)
    prod_results: list[dict] = []  # rows with no labels (diagnostic only)
    per_query_latency_ms: list[float] = []

    # Per-row metric accumulators.
    sum_recall = 0.0
    sum_ndcg = 0.0
    sum_hit5 = 0
    sum_hit10 = 0
    n_eval_rows = 0
    n_gold_rows = 0  # informational tag; not used for filtering

    # Per-stratum accumulators: stratum -> [n_rows, sum_recall].
    strat_acc: dict[str, list[float]] = {}

    # In E2E mode pull a wider candidate pool so the reranker has something
    # to reorder. TOP_K (10) would leave it nothing to do.
    # When `dedupe` is on, we ALWAYS pull E2E_RETRIEVAL_K so we have headroom
    # to fill 10 unique files even if the top-10 chunks collapse to fewer
    # distinct file_paths after dedup.
    retrieval_k = E2E_RETRIEVAL_K if (rerank_on or dedupe) else TOP_K

    # P10 A2 (2026-04-26): per-query rerank-skip telemetry. Tracks how many
    # queries the stratum gate skipped vs reranked, broken down by stratum.
    rerank_skipped_n = 0
    rerank_ran_n = 0
    skipped_by_stratum: dict[str, int] = {}

    # Lazy import: hybrid wires SQLite/LanceDB at module import which we don't
    # need outside the gate, but importing the symbol is cheap.
    if stratum_gated:
        from src.search.hybrid import _detect_stratum, _should_skip_rerank
    else:
        _detect_stratum = None
        _should_skip_rerank = None

    enc_t0 = time.time()
    for row in eval_rows:
        q_text = f"{cfg.query_prefix}{row['query']}"
        per_query_t0 = time.time()
        try:
            # NOTE: do NOT normalize_embeddings — the docs index was built
            # via src/embedding_provider.py which calls model.encode(texts)
            # WITHOUT normalize_embeddings. Mismatched normalization between
            # query and index → distance space drift → 0% recall (caught
            # 2026-04-25 during loop Iteration 12 baseline diagnosis; the
            # eval-v2 labeler used vector_search via the same provider, so
            # benchmark must match its normalization to be apples-to-apples).
            vecs = model.encode([q_text], show_progress_bar=False)
            q_vec = vecs[0]
            hits = table.search(q_vec).limit(retrieval_k).to_list()
            if rerank_on and reranker is not None and hits:
                # P10 A2: stratum gate decides per query whether to invoke the
                # CrossEncoder. The gate matches the production decision in
                # `src/search/hybrid.py::_should_skip_rerank` (is_doc_intent
                # is True for the doc-intent eval set by construction).
                skip_for_this_query = False
                if stratum_gated and _should_skip_rerank is not None:
                    skip_for_this_query = _should_skip_rerank(row["query"], is_doc_intent=True)
                if skip_for_this_query:
                    # No reranker call — keep the bi-encoder ordering, truncate.
                    # When dedupe is on, defer truncation until after dedup so
                    # we have headroom to fill 10 unique files.
                    hits = hits if dedupe else hits[:TOP_K]
                    rerank_skipped_n += 1
                    if _detect_stratum is not None:
                        s = _detect_stratum(row["query"]) or "unknown"
                        skipped_by_stratum[s] = skipped_by_stratum.get(s, 0) + 1
                else:
                    # The reranker takes the raw query (no embedding prefix),
                    # so we pass the user-facing `row['query']` rather than
                    # `q_text` which still has the bi-encoder's `search_query:`
                    # prefix baked in. Production rerank in `hybrid.py::rerank`
                    # also uses the unprefixed query.
                    # When dedupe is on, rerank the FULL pool so the top-10
                    # unique are selected from a complete reordering.
                    rerank_limit = retrieval_k if dedupe else TOP_K
                    hits = rerank_candidates(reranker, row["query"], hits, limit=rerank_limit)
                    rerank_ran_n += 1
        except Exception as exc:
            print(f"  ERROR on {row.get('id', '?')}: {exc}", file=sys.stderr)
            continue
        per_query_latency_ms.append(1000.0 * (time.time() - per_query_t0))

        # Dedup pass — keep first (highest-rank) occurrence per (repo, file_path).
        # Then truncate to TOP_K for scoring. This makes R@10 measure "recall
        # over top-10 unique documents", matching user-experience.
        if dedupe:
            seen_paths: set[tuple[str, str]] = set()
            deduped_hits = []
            for h in hits:
                repo = h.get("repo_name") or ""
                fp = h.get("file_path") or ""
                key_pair = (repo, fp)
                if key_pair not in seen_paths:
                    seen_paths.add(key_pair)
                    deduped_hits.append(h)
                    if len(deduped_hits) >= TOP_K:
                        break
            hits = deduped_hits

        top_files = [
            {
                "rank": i + 1,
                "repo_name": h.get("repo_name"),
                "file_path": h.get("file_path"),
                "file_type": h.get("file_type"),
                # In rerank-off mode `score` is the bi-encoder distance. In
                # rerank-on mode the candidate has been resorted by the
                # CrossEncoder and `_distance` is no longer meaningful as a
                # rank — surface `rerank_score` so downstream tooling sees
                # which signal drove the ordering.
                "score": float(h.get("rerank_score", h.get("_distance", 0.0))),
            }
            for i, h in enumerate(hits)
        ]
        retrieved_ordered: list[tuple[str, str]] = [(t["repo_name"], t["file_path"]) for t in top_files]

        expected_list = _row_expected(row)
        if expected_list:
            n_eval_rows += 1
            if row.get("gold") is True:
                n_gold_rows += 1

            expected_set = set(expected_list)
            r10 = _recall_at_k(expected_set, retrieved_ordered, TOP_K)
            ndcg10 = _ndcg_at_k(expected_set, retrieved_ordered, TOP_K)
            h5 = _hit_at_k(expected_set, retrieved_ordered, 5)
            h10 = _hit_at_k(expected_set, retrieved_ordered, TOP_K)

            sum_recall += r10
            sum_ndcg += ndcg10
            sum_hit5 += h5
            sum_hit10 += h10

            for stratum in _row_strata(row):
                acc = strat_acc.setdefault(stratum, [0.0, 0.0])
                acc[0] += 1
                acc[1] += r10

            eval_results.append(
                {
                    "id": row.get("id"),
                    "query": row.get("query"),
                    "strata": row.get("strata", []),
                    "gold": bool(row.get("gold", False)),
                    "n_expected": len(expected_set),
                    "recall_at_10": round(r10, 4),
                    "ndcg_at_10": round(ndcg10, 4),
                    "hit_at_5": h5,
                    "hit_at_10": h10,
                    "expected_paths": sorted(expected_set),
                    "top_files": top_files,
                }
            )
        else:
            prod_results.append(
                {
                    "id": row.get("id"),
                    "query": row.get("query"),
                    "intent_score": row.get("intent_score"),
                    "top_files": top_files,
                }
            )
    enc_s = time.time() - enc_t0

    if n_eval_rows == 0:
        recall_at_10 = None
        ndcg_at_10 = None
        hit_at_5 = None
        hit_at_10 = None
    else:
        recall_at_10 = sum_recall / n_eval_rows
        ndcg_at_10 = sum_ndcg / n_eval_rows
        hit_at_5 = sum_hit5 / n_eval_rows
        hit_at_10 = sum_hit10 / n_eval_rows

    per_stratum_recall = {
        stratum: round(acc[1] / acc[0], 4) if acc[0] else 0.0 for stratum, acc in sorted(strat_acc.items())
    }
    per_stratum_n = {stratum: int(acc[0]) for stratum, acc in sorted(strat_acc.items())}

    latency_p95_ms = round(_percentile(per_query_latency_ms, 0.95), 2)
    latency_p50_ms = round(_percentile(per_query_latency_ms, 0.50), 2)

    manifest = {
        "model_key": key,
        "model_name": cfg.name,
        "dim": cfg.dim,
        "query_prefix": cfg.query_prefix,
        "device": device,
        # `rerank_off` retained as an alias for backwards-compat consumers.
        # `rerank_on` is the new authoritative flag.
        "rerank_off": not rerank_on,
        "rerank_on": rerank_on,
        "rerank_model": E2E_RERANKER_MODEL if rerank_on else None,
        # P10 A2 telemetry. When stratum_gated is False these are zero/empty.
        "stratum_gated": stratum_gated,
        "rerank_skipped_n": rerank_skipped_n,
        "rerank_ran_n": rerank_ran_n,
        "skipped_by_stratum": dict(sorted(skipped_by_stratum.items())),
        # P10 A2 verification 2026-04-26: dedup mode flag — when True, top-10
        # contains unique file_paths only. Bench JSONs from before this flag
        # are with dedupe=False (heuristic R@10 over chunk slots).
        "dedupe": dedupe,
        "retrieval_k": retrieval_k,
        "router_bypassed": True,
        "top_k": TOP_K,
        "n_eval_rows": n_eval_rows,
        "n_gold_rows": n_gold_rows,
        "n_prod_queries": len(prod_results),
        "recall_at_10": round(recall_at_10, 4) if recall_at_10 is not None else None,
        "ndcg_at_10": round(ndcg_at_10, 4) if ndcg_at_10 is not None else None,
        "hit_at_5": round(hit_at_5, 4) if hit_at_5 is not None else None,
        "hit_at_10": round(hit_at_10, 4) if hit_at_10 is not None else None,
        "per_stratum_recall": per_stratum_recall,
        "per_stratum_n": per_stratum_n,
        "latency_p50_ms": latency_p50_ms,
        "latency_p95_ms": latency_p95_ms,
        "load_seconds": round(load_s, 1),
        "encode_seconds": round(enc_s, 1),
        "eval_per_query": eval_results,
        "prod_per_query": prod_results,
        **common,
    }

    # 1-line summary to stdout (always, regardless of JSON sink).
    summary_line = (
        f"SUMMARY {key}: "
        f"recall@10={manifest['recall_at_10']!s} "
        f"ndcg@10={manifest['ndcg_at_10']!s} "
        f"hit@5={manifest['hit_at_5']!s} "
        f"n_eval={n_eval_rows} (gold={n_gold_rows}) "
        f"p95={latency_p95_ms}ms enc={enc_s:.1f}s"
    )
    print(summary_line)

    # Cleanup before next candidate.
    del model
    gc.collect()
    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass
    return manifest


def _evaluate_gate(baseline: dict, candidate: dict) -> dict:
    """Compute AND-gate decision per eval-methodology-verdict.md F4.

    Returns dict with `deploy` (bool), `reasons` (list of failure strings),
    `details` (per-condition pass/fail).
    """
    reasons: list[str] = []
    details: dict[str, dict] = {}

    b_recall = baseline.get("recall_at_10") or 0.0
    c_recall = candidate.get("recall_at_10") or 0.0
    cond_recall = (c_recall - b_recall) >= GATE_RECALL_LIFT
    details["recall_at_10"] = {
        "baseline": b_recall,
        "candidate": c_recall,
        "delta": round(c_recall - b_recall, 4),
        "threshold": f">= +{GATE_RECALL_LIFT}",
        "pass": cond_recall,
    }
    if not cond_recall:
        reasons.append(f"recall@10 lift {c_recall - b_recall:+.4f} < +{GATE_RECALL_LIFT}")

    b_ndcg = baseline.get("ndcg_at_10") or 0.0
    c_ndcg = candidate.get("ndcg_at_10") or 0.0
    cond_ndcg = (c_ndcg - b_ndcg) >= GATE_NDCG_LIFT
    details["ndcg_at_10"] = {
        "baseline": b_ndcg,
        "candidate": c_ndcg,
        "delta": round(c_ndcg - b_ndcg, 4),
        "threshold": f">= +{GATE_NDCG_LIFT}",
        "pass": cond_ndcg,
    }
    if not cond_ndcg:
        reasons.append(f"ndcg@10 lift {c_ndcg - b_ndcg:+.4f} < +{GATE_NDCG_LIFT}")

    b_strat = baseline.get("per_stratum_recall") or {}
    c_strat = candidate.get("per_stratum_recall") or {}
    strat_violations: dict[str, float] = {}
    for s in sorted(set(b_strat) | set(c_strat)):
        delta = (c_strat.get(s) or 0.0) - (b_strat.get(s) or 0.0)
        if delta < GATE_PER_STRATUM_DROP:
            strat_violations[s] = round(delta, 4)
    cond_strat = not strat_violations
    details["per_stratum_recall"] = {
        "violations": strat_violations,
        "threshold": f"every stratum delta >= {GATE_PER_STRATUM_DROP}",
        "pass": cond_strat,
    }
    if not cond_strat:
        reasons.append(f"per-stratum drops > {-GATE_PER_STRATUM_DROP}: {strat_violations}")

    b_hit5 = baseline.get("hit_at_5") or 0.0
    c_hit5 = candidate.get("hit_at_5") or 0.0
    cond_hit5 = (c_hit5 - b_hit5) >= GATE_HIT5_DROP
    details["hit_at_5"] = {
        "baseline": b_hit5,
        "candidate": c_hit5,
        "delta": round(c_hit5 - b_hit5, 4),
        "threshold": f">= {GATE_HIT5_DROP}",
        "pass": cond_hit5,
    }
    if not cond_hit5:
        reasons.append(f"hit@5 drop {c_hit5 - b_hit5:+.4f} < {GATE_HIT5_DROP}")

    b_p95 = baseline.get("latency_p95_ms") or 0.0
    c_p95 = candidate.get("latency_p95_ms") or 0.0
    if b_p95 <= 0:
        # Baseline has no measured latency — skip the gate (treat as pass).
        cond_lat = True
        ratio: float | None = None
    else:
        ratio = c_p95 / b_p95
        cond_lat = ratio < GATE_LATENCY_RATIO
    details["latency_p95_ms"] = {
        "baseline": b_p95,
        "candidate": c_p95,
        "ratio": round(ratio, 4) if ratio is not None else None,
        "threshold": f"< {GATE_LATENCY_RATIO}x baseline",
        "pass": cond_lat,
    }
    if not cond_lat:
        reasons.append(f"latency_p95 ratio {ratio:.2f}x >= {GATE_LATENCY_RATIO}x")

    deploy = cond_recall and cond_ndcg and cond_strat and cond_hit5 and cond_lat
    return {
        "deploy": deploy,
        "reasons": reasons,
        "details": details,
    }


def _cmd_compare(baseline_path: Path, candidate_path: Path) -> int:
    """Print DEPLOY: yes/no based on AND-gate over baseline vs candidate JSON."""
    if not baseline_path.exists():
        print(f"ERROR: baseline file not found: {baseline_path}", file=sys.stderr)
        return 2
    if not candidate_path.exists():
        print(f"ERROR: candidate file not found: {candidate_path}", file=sys.stderr)
        return 2
    baseline = json.loads(baseline_path.read_text())
    candidate = json.loads(candidate_path.read_text())
    # If the inputs are summary lists (cross-model dump), pick the first non-skip entry.
    if isinstance(baseline, list):
        baseline = next((m for m in baseline if "skipped_reason" not in m), {})
    if isinstance(candidate, list):
        candidate = next((m for m in candidate if "skipped_reason" not in m), {})

    gate = _evaluate_gate(baseline, candidate)
    verdict = "yes" if gate["deploy"] else "no"
    print(f"DEPLOY: {verdict}")
    print(
        f"  baseline:  {baseline.get('model_key')} "
        f"recall={baseline.get('recall_at_10')} ndcg={baseline.get('ndcg_at_10')} "
        f"hit@5={baseline.get('hit_at_5')} p95={baseline.get('latency_p95_ms')}ms"
    )
    print(
        f"  candidate: {candidate.get('model_key')} "
        f"recall={candidate.get('recall_at_10')} ndcg={candidate.get('ndcg_at_10')} "
        f"hit@5={candidate.get('hit_at_5')} p95={candidate.get('latency_p95_ms')}ms"
    )
    if gate["reasons"]:
        print("  failures:")
        for reason in gate["reasons"]:
            print(f"    - {reason}")
    print(f"  details: {json.dumps(gate['details'], indent=2)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--models",
        help="Comma-separated model keys (default: all 4 = docs + 3 candidates)",
    )
    p.add_argument(
        "--model",
        help="Single model key (alias for --only with one value)",
    )
    p.add_argument("--only", action="append", help="Single key (repeatable)")
    p.add_argument(
        "--eval",
        help=f"Path to eval JSONL (default: {EVAL_PATH})",
    )
    p.add_argument(
        "--no-pre-flight",
        action="store_true",
        help=f"Skip the >={PREFLIGHT_AVAIL_HARD_GB} GB available-RAM gate.",
    )
    p.add_argument(
        "--compare",
        nargs=2,
        metavar=("BASELINE_RUN", "CANDIDATE_RUN"),
        help=(
            "Compare two per-model JSONs (or summary lists) and print DEPLOY: yes/no based on the 5-condition AND-gate."
        ),
    )
    p.add_argument(
        "--probe",
        type=int,
        default=0,
        help=(
            "If >0, slice the eval set to first N scoreable rows (kill-gate "
            "smoke). Wider SE than full bench — only use for -3pp early-out."
        ),
    )
    p.add_argument(
        "--out",
        help=("Optional explicit output path for the per-model JSON. If unset, uses bench_runs/<key>_<ts>.json."),
    )
    p.add_argument(
        "--rerank-on",
        action="store_true",
        help=(
            "P9 E2E mode: retrieve top-50 from the bi-encoder, rerank with "
            "the production CrossEncoder, score R@10 on the reranked top-10. "
            "Default off preserves the rerank-off bi-encoder-only behaviour."
        ),
    )
    p.add_argument(
        "--rerank-model-path",
        default=None,
        help=(
            "Override reranker by path or HF id (alternative to "
            "CODE_RAG_BENCH_RERANKER env var). When set, the manifest's "
            "rerank_model field reflects the override; assert in CI that "
            "the value contains the expected FT tag. Takes precedence over "
            "the env var when both are present."
        ),
    )
    p.add_argument(
        "--stratum-gated",
        action="store_true",
        help=(
            "P10 A2 (2026-04-26): only effective with --rerank-on. Per query, "
            "consult `src.search.hybrid._should_skip_rerank`; skip the "
            "reranker on OFF strata (nuvei/aircash/trustly/webhook/refund) "
            "and run it elsewhere. Measures the A2 deploy candidate."
        ),
    )
    p.add_argument(
        "--dedupe",
        action="store_true",
        help=(
            "P10 A2 verification (2026-04-26): dedupe top-10 by "
            "(repo_name, file_path), pulling from the wider candidate pool "
            "(retrieval_k=50) to fill 10 unique files. Makes R@10 measure "
            "'recall over 10 unique documents' which matches user-experienced "
            "result quality. Both LLM judges (~80%% of eval rows have "
            "duplicate file_paths in default top-10) flagged this as a "
            "measurement bias of the un-deduped bench. Default OFF preserves "
            "historical bench JSONs comparable."
        ),
    )
    args = p.parse_args()

    # Bug 1 fix (2026-04-26): plumb --rerank-model-path through to both the
    # loader and the manifest. We rebind the module-level global because
    # `evaluate_model` reads E2E_RERANKER_MODEL at call time when building
    # the manifest's `rerank_model` field. Flag takes precedence over the
    # CODE_RAG_BENCH_RERANKER env var (the env var was already baked into the
    # global at module import time, so an explicit CLI value wins here).
    if args.rerank_model_path:
        globals()["E2E_RERANKER_MODEL"] = args.rerank_model_path

    if args.stratum_gated and not args.rerank_on:
        print(
            "WARN: --stratum-gated has no effect without --rerank-on; ignoring.",
            file=sys.stderr,
        )

    if args.compare:
        baseline_path = Path(args.compare[0])
        candidate_path = Path(args.compare[1])
        return _cmd_compare(baseline_path, candidate_path)

    if args.only:
        models = list(args.only)
    elif args.model:
        models = [args.model]
    elif args.models:
        models = [k.strip() for k in args.models.split(",") if k.strip()]
    else:
        models = list(DEFAULT_MODELS)

    eval_path = Path(args.eval) if args.eval else EVAL_PATH

    avail = _avail_gb()
    if not args.no_pre_flight and avail < PREFLIGHT_AVAIL_HARD_GB:
        print(
            f"ABORT: sys-avail={avail:.2f}G < {PREFLIGHT_AVAIL_HARD_GB}G (rerun with --no-pre-flight to override)",
            file=sys.stderr,
        )
        return 2

    md5 = _md5_file(DB_PATH) if DB_PATH.exists() else "missing"
    common = {
        "knowledge_db_md5": md5,
        "preflight_avail_gb": round(avail, 2),
        "pre_flight_skipped": bool(args.no_pre_flight),
        "eval_path": str(eval_path),
    }
    eval_rows = load_eval(eval_path)
    if args.probe and args.probe > 0:
        # Slice to the first N rows that have expected_paths so the probe
        # actually stresses the retrieval — unscoreable prod rows would
        # waste the budget without contributing to the kill-gate signal.
        sliced: list[dict] = []
        for r in eval_rows:
            if _row_expected(r):
                sliced.append(r)
                if len(sliced) >= args.probe:
                    break
        print(f"PROBE mode: sliced eval to {len(sliced)} scoreable rows (of {len(eval_rows)} total)")
        eval_rows = sliced
    n_with_expected = sum(1 for r in eval_rows if _row_expected(r))
    n_gold_flag = sum(1 for r in eval_rows if r.get("gold") is True)
    print(
        f"Loaded {len(eval_rows)} eval queries from {eval_path.name} "
        f"({n_with_expected} have expected_paths, {n_gold_flag} flagged gold)"
    )
    print(f"  knowledge.db md5 = {md5}")

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")

    # Load the reranker exactly once and reuse across models. Avoids paying
    # the ~150 MB / ~2 s load cost per candidate. Only fired when --rerank-on
    # so the default rerank-off path does not regress. Pass the
    # (potentially overridden) global explicitly because `load_reranker`'s
    # default kwarg was bound at definition time and would otherwise miss
    # the --rerank-model-path override.
    reranker = None
    if args.rerank_on:
        print(f"Loading reranker {E2E_RERANKER_MODEL} ...")
        t0 = time.time()
        reranker = load_reranker(E2E_RERANKER_MODEL)
        print(f"  reranker loaded in {time.time() - t0:.1f}s")

    summary: list[dict] = []
    for key in models:
        result = evaluate_model(
            key,
            eval_rows,
            common,
            pre_flight=not args.no_pre_flight,
            rerank_on=args.rerank_on,
            reranker=reranker,
            stratum_gated=args.stratum_gated and args.rerank_on,
            dedupe=args.dedupe,
        )
        summary.append(result)
        # --out only applies when exactly one model is being benched (single
        # explicit destination). Fall back to the timestamped path otherwise.
        if args.out and len(models) == 1:
            per_path = Path(args.out)
            per_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            per_path = BENCH_DIR / f"{key}_{ts}.json"
        per_path.write_text(json.dumps(result, indent=2))
        print(f"  wrote {per_path}")

    summary_path = BENCH_DIR / f"doc_intent_summary_{ts}.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    print()
    print("=" * 60)
    print(f"SUMMARY ({summary_path.name})")
    print(f"  {'model':<22s} {'recall@10':>10s} {'ndcg@10':>9s} {'hit@5':>7s} {'n_eval':>6s} {'p95_ms':>7s}")
    for m in summary:
        if "skipped_reason" in m:
            print(f"  {m['model_key']:<22s} SKIP {m['skipped_reason']}")
            continue
        print(
            f"  {m['model_key']:<22s} {m['recall_at_10']!s:>10s} "
            f"{m['ndcg_at_10']!s:>9s} {m['hit_at_5']!s:>7s} "
            f"{m['n_eval_rows']:>6d} {m['latency_p95_ms']!s:>7s}"
        )
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
