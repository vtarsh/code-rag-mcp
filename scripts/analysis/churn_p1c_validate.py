#!/usr/bin/env python3
"""P1c validation — re-run v8 on the 19 base-win queries with P1c-enabled pipeline.

Compares the NEW v8 top-10 (with extended _DOC_QUERY_RE + _CI_PATH_RE) to the
STORED v8 top-10 from profiles/pay-com/churn_replay/v8_vs_base.json (pre-P1c).
For each of the 19 queries we check two things:

  1. Did the ci/deploy.yml / k8s/.github/workflows/* files get pushed out of
     top-10? (Expected on pair #2, "ach provider service integration repo".)
  2. Did a canonical doc-artifact file (reference/*.md, known per-query) come
     back into top-10? (Expected on the 8 doc-intent queries.)

We also diff top-10 keys and print the overlap — if P1c flipped rankings, the
overlap drops.

NOTE: Runs v8 ONLY, not base. Cheaper than a full churn replay
(~3-5 min on MPS vs ~84 min).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("ACTIVE_PROFILE", "pay-com")
os.environ.setdefault("CODE_RAG_HOME", str(REPO_ROOT))

# Pair idx -> (query, expected_doc_repo_or_ci_fix, description)
# Source: profiles/pay-com/churn_replay/judge_opus_v8_vs_base.jsonl
EXPECTED = [
    (
        2,
        "ach provider service integration repo",
        ("ci", None),
        "expect ci/deploy.yml + k8s/.github/workflows/* pushed out of top-10",
    ),
    (
        4,
        "impact audit severity verification",
        ("doc", "impact-audit-rules"),
        "expect impact-audit-rules or impact-audit-catalog doc back in top-10",
    ),
    (
        5,
        "new APM provider integration recipe checklist boilerplate",
        ("doc", "provider-integration-checklist"),
        "expect provider-integration-checklist doc back in top-10",
    ),
    (
        10,
        "provider integration checklist service setup webhooks lifecycle",
        ("doc", "provider-integration-checklist"),
        "expect provider-integration-checklist doc back",
    ),
    (
        13,
        "openfinance APM integration documentation",
        ("doc", "openfinance"),
        "expect openfinance docs back (any docs/docs/*.md under grpc-apm-openfinance)",
    ),
    (
        19,
        "investigation framework",
        ("doc", "investigation-framework"),
        "expect investigation-framework doc back in top-1 or top-3",
    ),
    (
        38,
        "payper sandbox testing amount magic values test",
        ("doc", "payper"),
        "expect payper reference_sandbox or silverflow_sandbox doc back",
    ),
    (
        42,
        "provider integration checklist APM_TYPES connections next-web",
        ("doc", "provider-integration-checklist"),
        "expect provider-integration-checklist promoted to top-1",
    ),
    (
        46,
        "provider code rules subtle cross-service webhook auth",
        ("doc", "risk-rules"),
        "expect risk-rules or auth-related file back (softer assertion)",
    ),
]

CI_PATH_MARKERS = ("ci/deploy.yml", "ci/deploy.yaml", "k8s/.github/workflows/")


def has_ci_path(keys: list[str]) -> bool:
    for k in keys:
        for marker in CI_PATH_MARKERS:
            if marker in k:
                return True
    return False


def has_doc_for_substring(keys: list[str], needle: str) -> bool:
    """Match if any key contains `needle` AND (.md or /docs/ in path)."""
    needle_l = needle.lower()
    for k in keys:
        if needle_l not in k.lower():
            continue
        if k.endswith(".md") or "/docs/" in k or k.endswith(".yaml") or k.endswith(".yml"):
            return True
    return False


class _CrossEncoderAdapter:
    def __init__(self, model, *, batch_size: int = 2):
        self._model = model
        self._batch_size = batch_size

    @property
    def provider_name(self) -> str:
        return "p1c_validate_adapter"

    def rerank(self, query: str, documents: list[str], limit: int = 10) -> list[float]:
        pairs = [(query, doc) for doc in documents]
        scores = self._model.predict(pairs, batch_size=self._batch_size)
        return [float(s) for s in scores]


def _key(r: dict) -> str:
    return f"{r.get('repo_name', '')}::{r.get('file_path', '')}"


def _top_keys(results: list[dict], k: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in results:
        kk = _key(r)
        if kk in seen:
            continue
        seen.add(kk)
        out.append(kk)
        if len(out) >= k:
            break
    return out


def _overlap(a: list[str], b: list[str], k: int) -> float:
    if k <= 0:
        return 0.0
    return len(set(a[:k]) & set(b[:k])) / k


def _load_stored_v8_top10(churn_json_path: Path, queries: list[str]) -> dict[str, list[str]]:
    with churn_json_path.open() as f:
        data = json.load(f)
    wanted = set(queries)
    out: dict[str, list[str]] = {}
    for rec in data.get("per_query", []):
        q = rec.get("query")
        if q in wanted:
            out[q] = rec.get("v8_top10_keys") or []
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--stored", type=Path, default=Path("profiles/pay-com/churn_replay/v8_vs_base.json"))
    p.add_argument("--v8-model", default="profiles/pay-com/models/reranker_ft_gte_v8")
    p.add_argument("--output", type=Path, default=Path("profiles/pay-com/churn_replay/p1c_validation.json"))
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--max-length", type=int, default=256)
    args = p.parse_args()

    if not args.stored.exists():
        print(f"ERROR: stored results not found: {args.stored}", file=sys.stderr)
        return 1

    queries = [q for (_, q, _, _) in EXPECTED]
    stored = _load_stored_v8_top10(args.stored, queries)
    missing = [q for q in queries if q not in stored]
    if missing:
        print(f"ERROR: {len(missing)} queries missing from stored v8_vs_base.json:", file=sys.stderr)
        for q in missing:
            print(f"  - {q}", file=sys.stderr)
        return 1

    from sentence_transformers import CrossEncoder

    from src.search.hybrid import hybrid_search

    print(f"loading v8 model: {args.v8_model}", flush=True)
    t0 = time.perf_counter()
    model = CrossEncoder(args.v8_model, trust_remote_code=True, max_length=args.max_length)
    adapter = _CrossEncoderAdapter(model, batch_size=args.batch_size)
    print(f"loaded in {time.perf_counter() - t0:.1f}s", flush=True)

    results: list[dict] = []
    ci_fixed = 0
    docs_recovered = 0
    unchanged = 0

    for i, (idx, q, (kind, needle), desc) in enumerate(EXPECTED, 1):
        t = time.perf_counter()
        try:
            ranked = hybrid_search(q, limit=args.top_k, reranker_override=adapter)[0]
        except Exception as e:  # pragma: no cover
            print(f"[{i}/{len(EXPECTED)}] pair #{idx} ERROR: {e}", flush=True)
            results.append({"pair_idx": idx, "query": q, "error": str(e)})
            continue
        lat = time.perf_counter() - t

        new_keys = _top_keys(ranked or [], args.top_k)
        old_keys = stored.get(q, [])
        overlap = _overlap(old_keys, new_keys, args.top_k)

        old_has_ci = has_ci_path(old_keys)
        new_has_ci = has_ci_path(new_keys)
        ci_fix = kind == "ci" and old_has_ci and not new_has_ci

        doc_recovered = False
        old_has_doc = False
        new_has_doc = False
        if kind == "doc" and needle:
            old_has_doc = has_doc_for_substring(old_keys, needle)
            new_has_doc = has_doc_for_substring(new_keys, needle)
            doc_recovered = (not old_has_doc) and new_has_doc

        if ci_fix:
            ci_fixed += 1
            outcome = "CI_FIXED"
        elif doc_recovered:
            docs_recovered += 1
            outcome = "DOC_RECOVERED"
        elif kind == "doc" and new_has_doc and old_has_doc:
            outcome = "DOC_ALREADY_PRESENT"
        elif overlap == 1.0:
            unchanged += 1
            outcome = "UNCHANGED"
        else:
            outcome = "SHUFFLED_NO_TARGET"

        entry = {
            "pair_idx": idx,
            "query": q,
            "expected": desc,
            "outcome": outcome,
            "overlap_at_10_vs_stored": round(overlap, 2),
            "old_v8_top10": old_keys,
            "new_v8_top10": new_keys,
            "old_has_ci": old_has_ci,
            "new_has_ci": new_has_ci,
            "old_has_doc": old_has_doc if kind == "doc" else None,
            "new_has_doc": new_has_doc if kind == "doc" else None,
            "latency_s": round(lat, 2),
        }
        results.append(entry)

        mark = (
            "✓" if outcome in ("CI_FIXED", "DOC_RECOVERED") else ("~" if outcome in ("DOC_ALREADY_PRESENT",) else "·")
        )
        print(
            f"[{i}/{len(EXPECTED)}] {mark} pair#{idx} overlap={overlap:.2f} outcome={outcome} lat={lat:.1f}s",
            flush=True,
        )

    summary = {
        "n": len(EXPECTED),
        "ci_fixed": ci_fixed,
        "docs_recovered": docs_recovered,
        "unchanged": unchanged,
        "recovered_rate": round((ci_fixed + docs_recovered) / len(EXPECTED), 4),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"summary": summary, "per_query": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n=== SUMMARY ===", flush=True)
    for k, v in summary.items():
        print(f"  {k}: {v}", flush=True)
    print(f"\noutput: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
