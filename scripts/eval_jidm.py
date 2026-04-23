"""IM-NDCG (Intent-Match NDCG) — judge-independent direction metric.

Background (2026-04-22):
  - Opus and MiniLM judges disagreed by 64pp on the same v8-vs-base diff
    pairs (project_p1b_opus_judge_verdict.md). Both are biased:
    Opus prose-prefers code files, MiniLM prefers prose. Neither is a
    stable direction signal for "did the ranker improve".
  - IM-NDCG is a deterministic, judge-free proxy that scores a ranked list
    against the INTENT of the query (doc/code/mixed) using an asymmetric
    relevance matrix over result file kinds.

Metric:
  IM-NDCG@k = (DCG / IDCG) * diversity_multiplier
    diversity_multiplier = unique(repo::file)_in_top_k / k

  DCG@k = sum_{i=1..k} REL[intent][kind_i] / log2(i + 1)
  IDCG@k = sorted-ideal DCG of the same ranked list (relevance values
    sorted descending — best-case achievable with the candidates present).

Input:
  Snapshot JSON — one of:
    (a) churn_replay format: per_query[].{query, base_top10, v8_top10}
        where each result has {repo_name, file_path, file_type, ...}
    (b) eval_finetune format: per_task_baseline/per_task_ft_v1[ticket]
        with top_10_repos (repo names only — file-level IM-NDCG cannot
        be computed from this; script warns and skips).

Output:
  Per-query IM-NDCG@k for each ranker plus overall mean, plus Spearman
  rho vs Jira r@10 when the snapshot carries both signals.

Usage:
  python scripts/eval_jidm.py \
    --snapshot profiles/pay-com/churn_replay/v8_vs_base.json \
    --k 10
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Reuse production regex so the metric stays coherent with the ranker.
from src.search.hybrid import _CI_PATH_RE, _DOC_QUERY_RE  # noqa: E402

# Code-ish tokens in the query: snake_case, CamelCase of length 3+, function
# calls, file extensions. Spec is from V-C agent (2026-04-22), kept verbatim.
_CODE_QUERY_RE = re.compile(
    r"(?:"
    r"\b[a-z][a-zA-Z0-9]*\([^)]*\)"           # fn_call(...)
    r"|\b[A-Z][A-Z0-9_]{2,}\b"                 # SCREAMING_CASE
    r"|[a-z]+_[a-z_]+"                         # snake_case
    r"|\.(?:js|ts|py|go|proto)\b"              # file ext
    r")"
)

# Known repo name patterns in pay-com profile. Treated as code-intent signal
# because repo tokens usually narrow to service code, not prose.
_REPO_TOKEN_RE = re.compile(
    r"\b(grpc-[a-z]+|express-api-[a-z]+|k8s-[a-z]+|next-web-[a-z]+)\b",
    re.IGNORECASE,
)


def query_intent(q: str) -> str:
    """Classify query as "doc" | "code" | "mixed".

    Reuses _DOC_QUERY_RE (same regex hybrid.py uses to disable penalties),
    so a query that disables penalties in prod is classified "doc" here.
    """
    has_doc = bool(_DOC_QUERY_RE.search(q or ""))
    has_code = bool(_CODE_QUERY_RE.search(q or ""))
    has_repo_tok = bool(_REPO_TOKEN_RE.search(q or ""))
    if has_doc and not has_code:
        return "doc"
    if has_code or has_repo_tok:
        return "code"
    return "mixed"


# Asymmetric relevance matrix. "ci" is 0 for doc/code queries (P1c finding:
# CI yaml files are noise on service-code queries) but 0.2 on mixed queries
# because ambiguous natural-language queries sometimes do want deploy context.
REL: dict[str, dict[str, float]] = {
    "doc":   {"doc": 1.0, "code": 0.3, "config": 0.5, "proto": 0.5, "ci": 0.0, "test": 0.3},
    "code":  {"doc": 0.3, "code": 1.0, "config": 0.7, "proto": 0.8, "ci": 0.0, "test": 0.3},
    "mixed": {"doc": 0.7, "code": 1.0, "config": 0.7, "proto": 0.8, "ci": 0.2, "test": 0.3},
}


# Known doc-ish file_type values in the index (matches src.search.hybrid
# _DOC_FILE_TYPES plus "docs"). Kept local so this script stays standalone
# for callers that don't import the full hybrid module.
_DOC_FILE_TYPES: frozenset[str] = frozenset(
    {"doc", "docs", "task", "gotchas", "reference", "dictionary", "provider_doc", "flow_annotation"}
)

_TEST_PATH_RE = re.compile(r"(?:\.spec\.|\.test\.|/tests?/)")


def kind_of(file_type: str, file_path: str) -> str:
    """Classify a result into {doc, code, config, proto, ci, test}.

    Priority (strongest wins):
      1. ci yaml / k8s workflow path -> "ci"
      2. test path (.spec. / .test. / /tests/) -> "test"
      3. doc-ish file_type (doc/docs/gotchas/reference/task/...) -> "doc"
      4. file_type == "env" OR path ends with .yaml/.yml/.json -> "config"
      5. file_type == "proto" OR path ends with .proto -> "proto"
      6. fallback -> "code"
    """
    ft = (file_type or "").strip()
    fp = (file_path or "").strip()
    if _CI_PATH_RE.search(fp):
        return "ci"
    if _TEST_PATH_RE.search(fp):
        return "test"
    if ft in _DOC_FILE_TYPES:
        return "doc"
    if ft == "env" or fp.endswith((".yaml", ".yml", ".json")):
        return "config"
    if ft == "proto" or fp.endswith(".proto"):
        return "proto"
    return "code"


def im_ndcg_at_k(query: str, ranked_results: list[dict], k: int = 10) -> float:
    """IM-NDCG@k for one query.

    ranked_results: list of dicts with at least {file_path, file_type}.
      Order matters — index 0 = rank 1. file_path is used for uniqueness
      (repo::file dedup already assumed upstream, but we do it again here
      as defensive programming — chunk-expansion may produce duplicates).

    Returns 0.0 if the ranked list is empty.
    """
    if not ranked_results or k <= 0:
        return 0.0
    intent = query_intent(query)
    rel_map = REL[intent]

    # Take top-k positions; compute DCG on those.
    top = ranked_results[:k]
    rels: list[float] = []
    for r in top:
        kind = kind_of(r.get("file_type", ""), r.get("file_path", ""))
        rels.append(rel_map.get(kind, 0.0))

    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))

    # IDCG = same relevances sorted descending (best-case ordering of what
    # we actually retrieved). This is "ranker quality given this candidate
    # set" — not "ranker quality vs perfect oracle retrieval". The diversity
    # multiplier penalizes unranked duplicates.
    ideal_rels = sorted(rels, reverse=True)
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal_rels))
    if idcg == 0:
        return 0.0

    # Diversity: unique (repo, file) pairs in top-k / k.
    seen: set[tuple[str, str]] = set()
    for r in top:
        seen.add((r.get("repo_name", ""), r.get("file_path", "")))
    diversity = len(seen) / k

    return (dcg / idcg) * diversity


def _spearman_rho(xs: list[float], ys: list[float]) -> float | None:
    """Stdlib-only Spearman rho — avoids scipy dep at metric-compute time.

    Ties are broken with average-rank (standard behavior). Returns None
    if arrays are empty or variance is zero (constant input).
    """
    if not xs or not ys or len(xs) != len(ys):
        return None
    n = len(xs)
    if n < 2:
        return None

    def _rank(vs: list[float]) -> list[float]:
        # Sort indices by value; assign fractional rank to ties.
        order = sorted(range(n), key=lambda i: vs[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0  # 1-based average rank
            for m in range(i, j + 1):
                ranks[order[m]] = avg
            i = j + 1
        return ranks

    rx = _rank(xs)
    ry = _rank(ys)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    cov = sum((rx[i] - mean_x) * (ry[i] - mean_y) for i in range(n))
    vx = math.sqrt(sum((rx[i] - mean_x) ** 2 for i in range(n)))
    vy = math.sqrt(sum((ry[i] - mean_y) ** 2 for i in range(n)))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def _eval_snapshot(snapshot: dict, k: int) -> dict:
    """Compute IM-NDCG@k for every ranker series present in the snapshot.

    Supports:
      churn_replay: per_query[].{query, base_top10, v8_top10}
      (eval_finetune snapshots lack per-result file_path — skipped with
      a warning in the caller.)
    """
    out: dict = {"format": None, "k": k, "per_query": [], "summary": {}}
    per_query = snapshot.get("per_query")
    if not isinstance(per_query, list) or not per_query:
        return out

    # Detect which series are present. churn_replay has base_top10/v8_top10.
    sample = per_query[0]
    series: list[str] = [name for name in ("base_top10", "v8_top10") if name in sample]
    if not series:
        # Some snapshots might have other keys; look for any list-of-dict
        # with file_path on the first element.
        for k2, v2 in sample.items():
            if (
                isinstance(v2, list)
                and v2
                and isinstance(v2[0], dict)
                and "file_path" in v2[0]
            ):
                series.append(k2)

    out["format"] = "churn_replay" if {"base_top10", "v8_top10"}.issubset(sample) else "generic"
    out["series"] = series

    per_query_rows: list[dict] = []
    for item in per_query:
        q = item.get("query") or ""
        row: dict = {"query": q, "intent": query_intent(q)}
        for s in series:
            ranked = item.get(s) or []
            row[s] = im_ndcg_at_k(q, ranked, k)
        per_query_rows.append(row)

    # Summary — mean per series.
    summary: dict = {}
    for s in series:
        vals = [r[s] for r in per_query_rows if r.get(s) is not None]
        summary[s] = sum(vals) / len(vals) if vals else 0.0
    out["per_query"] = per_query_rows
    out["summary"]["mean_im_ndcg"] = {s: round(summary[s], 4) for s in series}
    out["summary"]["n_queries"] = len(per_query_rows)

    # Intent distribution — useful sanity check.
    from collections import Counter

    intents = Counter(r["intent"] for r in per_query_rows)
    out["summary"]["intent_distribution"] = dict(intents)

    return out


def _maybe_spearman_vs_jira(result: dict, snapshot: dict, k: int) -> dict | None:
    """If the snapshot also carries a Jira r@10 per query, compute rho.

    churn_replay has only file-level rankings — it doesn't persist Jira
    recall. So this typically returns None for churn snapshots.

    eval_finetune per_task entries carry recall_at_10 but no file_path;
    those are filtered upstream. If a future snapshot combines both, this
    function will compute rho on whichever IM-NDCG series is present.
    """
    # Hook for future snapshots that carry per-query Jira recall alongside
    # file-level rankings. Match query string or ticket id.
    per_query = snapshot.get("per_query") or []
    recall_map: dict[str, float] = {}
    for item in per_query:
        for key_field in ("query", "ticket_id", "ticket"):
            q = item.get(key_field)
            if q and "recall_at_10" in item:
                recall_map[q] = float(item["recall_at_10"])
                break
    if not recall_map:
        return None

    rho_map: dict[str, float | None] = {}
    for s in result.get("series", []):
        xs: list[float] = []
        ys: list[float] = []
        for row in result["per_query"]:
            q = row.get("query")
            if q in recall_map:
                xs.append(row[s])
                ys.append(recall_map[q])
        rho_map[s] = _spearman_rho(xs, ys) if xs else None
    return rho_map


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--snapshot", type=Path, required=True, help="path to snapshot JSON")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--output", type=Path, default=None, help="optional JSON output path")
    ap.add_argument("--sample", type=int, default=5, help="print N sample per-query rows")
    args = ap.parse_args()

    if not args.snapshot.exists():
        print(f"ERROR: snapshot not found: {args.snapshot}", file=sys.stderr)
        return 2

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))

    # Detect eval_finetune format (repo-only data) and warn.
    if "per_task_baseline" in snapshot and "per_query" not in snapshot:
        print(
            "ERROR: eval_finetune snapshot only carries top_10_repos (no file_path). "
            "IM-NDCG is file-kind-level and cannot be computed.",
            file=sys.stderr,
        )
        return 3

    result = _eval_snapshot(snapshot, args.k)
    if not result.get("per_query"):
        print("ERROR: snapshot has no per_query entries with file-level data", file=sys.stderr)
        return 4

    rho = _maybe_spearman_vs_jira(result, snapshot, args.k)
    if rho is not None:
        result["summary"]["spearman_rho_vs_recall_at_10"] = {
            s: (round(r, 4) if r is not None else None) for s, r in rho.items()
        }

    # Print human-readable digest.
    print(f"snapshot: {args.snapshot}")
    print(f"format: {result['format']}")
    print(f"series: {result['series']}")
    print(f"n_queries: {result['summary']['n_queries']}")
    print(f"k: {result['k']}")
    print(f"intent distribution: {result['summary']['intent_distribution']}")
    print(f"mean IM-NDCG@{args.k}: {result['summary']['mean_im_ndcg']}")
    if "spearman_rho_vs_recall_at_10" in result["summary"]:
        print(f"Spearman rho vs Jira r@10: {result['summary']['spearman_rho_vs_recall_at_10']}")
    else:
        print("Spearman rho vs Jira r@10: N/A (snapshot has no per-query recall_at_10)")
    if args.sample > 0:
        print(f"\nfirst {args.sample} rows:")
        for row in result["per_query"][: args.sample]:
            print(f"  [{row['intent']:>5}] {row['query'][:70]!r}")
            for s in result["series"]:
                print(f"      {s}: {row[s]:.4f}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\noutput: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
