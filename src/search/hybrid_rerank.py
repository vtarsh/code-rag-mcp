"""Hybrid search — RRF fusion of FTS5 + vector results + CrossEncoder reranking.

Pipeline:
  1. FTS5 keyword search (2x weight, 100 candidates, NO per-repo cap)
  2. LanceDB vector search (50 candidates)
  3. RRF (Reciprocal Rank Fusion) to merge both lists
  4. CrossEncoder reranker (70% rerank + 30% RRF) for final ordering

Per-repo diversity is NOT applied here — candidates must survive fusion
and reranking on merit. Diversity capping happens only at the
presentation layer (search tool output) to control output size.
"""

from __future__ import annotations

import logging
import os
import re

from src.config import CI_PENALTY, DOC_PENALTY, GUIDE_PENALTY, TEST_PENALTY
from src.container import get_reranker
from src.search.hybrid_query import (
    _DOC_RERANK_OFF_STRATA,
    _detect_stratum,
    _query_wants_docs,
)

_DISABLE_PENALTIES = os.getenv("CODE_RAG_DISABLE_PENALTIES", "0") == "1"

_logger = logging.getLogger(__name__)

_DOC_FILE_TYPES: frozenset[str] = frozenset(
    {"doc", "docs", "task", "gotchas", "reference", "dictionary", "provider_doc", "flow_annotation"}
)

_TEST_PATH_RE = re.compile(r"(?:\.spec\.(?:js|ts|tsx|jsx)$|\.test\.(?:js|ts|tsx|jsx|py)$|_test\.py$|/tests?/)")
_GUIDE_PATH_RE = re.compile(
    r"(?:/AI-CODING-GUIDE\.md$|/CLAUDE\.md$|/README\.md$|^AI-CODING-GUIDE\.md$|^CLAUDE\.md$|^README\.md$)",
    re.IGNORECASE,
)
# P1c 2026-04-22: CI/deploy yaml files are neither docs nor service code. v8
# FT reranker systematically surfaces them on short repo queries (e.g. "ach
# provider service integration repo" -> 5x workflow-ach-*::ci/deploy.yml).
# Treat them as doc-level noise on code-intent queries.
_CI_PATH_RE = re.compile(
    r"(?:^|/)(?:ci/deploy\.ya?ml|k8s/\.github/workflows/)",
    re.IGNORECASE,
)


# P10 Phase A2-revise (2026-04-25 late): stratum-gated rerank-skip — INVERTED.
#
# Original A2 (2026-04-26 stratum map) was inverted vs true reranker behavior.
# v2 LLM-calibrated eval (10 Opus agents, ~2200 judgments, n=192 across 10
# strata in `profiles/pay-com/eval/doc_intent_eval_v3_n200_v2.jsonl`) revealed the
# correct direction. Per-stratum R@10 deltas (A2 with skip-on-OFF vs full
# rerank-on baseline):
#
#   reranker HURTS (skip → larger R@10 lift):
#     webhook +3.35pp, trustly +2.68pp, method +1.30pp, payout +1.11pp
#
#   reranker HELPS (must keep rerank to avoid regression):
#     nuvei -7.58pp, aircash -8.78pp, refund -14.51pp
#
#   flat / small loss → conservative KEEP rerank:
#     interac 0.00, provider -0.24pp, tail -1.96pp
#
def _merge_two_towers(
    code_results: list[dict],
    docs_results: list[dict],
    code_err: str | None,
    docs_err: str | None,
) -> tuple[list[dict], str | None]:
    """Merge code-tower + docs-tower vector results, deduping by rowid.

    Code tower comes first so its ranking wins on collisions (chunks live in
    the same SQLite `chunks` table; only the embeddings differ between
    towers). Keeping the first occurrence preserves the better-ranked
    position from whichever tower surfaced the chunk first, matching the
    `if key not in scores` behaviour in the RRF loop downstream.
    """
    seen_rowids: set = set()
    merged: list[dict] = []
    for vrow in list(code_results) + list(docs_results):
        rid = vrow.get("rowid")
        if rid in seen_rowids:
            continue
        seen_rowids.add(rid)
        merged.append(vrow)
    return merged, (code_err or docs_err)


def _should_skip_rerank(query: str, is_doc_intent: bool) -> bool:
    """Stratum-gated rerank-skip decision.

    Default behavior:
      - non-doc-intent → False (run reranker, existing behavior preserved)
      - env `CODE_RAG_DOC_RERANK_OFF=1` → True (kill-switch back-compat with
        the P10 Phase 1 quickwin; disables reranker for ALL doc-intent queries)
      - doc-intent + stratum in OFF set → True (reranker hurts these strata)
      - doc-intent + stratum in KEEP set → False (reranker helps these strata)
      - doc-intent + unknown stratum → False (conservative fallback: reranker
        runs on queries the gate can't classify)
    """
    if not is_doc_intent:
        return False
    if os.getenv("CODE_RAG_DOC_RERANK_OFF", "0") == "1":
        return True
    stratum = _detect_stratum(query)
    if stratum is None:
        return False
    return stratum in _DOC_RERANK_OFF_STRATA


def _classify_penalty(file_type: str, file_path: str) -> float:
    """Return the penalty delta (in normalized score units) for a result.

    Priority order (strongest penalty wins):
      1. Guide-like paths (AI-CODING-GUIDE.md / CLAUDE.md / README.md) -> GUIDE_PENALTY
      2. Test paths (*.spec.js, *.test.py, /tests/...) -> TEST_PENALTY
      3. CI yaml paths (ci/deploy.yml, k8s/.github/workflows/*) -> CI_PENALTY
         (stronger than DOC_PENALTY — v8 surfaces 5+ CI files on short repo
         queries, and DOC_PENALTY=0.15 was insufficient on pair #2).
      4. Doc-ish file_type (doc, task, gotchas, reference) -> DOC_PENALTY
    Returns 0.0 for production code (unchanged).

    Eval A/B: CODE_RAG_DISABLE_PENALTIES=1 short-circuits to 0.0 so penalties
    can be isolated as a cause when hybrid-mode eval drops GT repos.
    """
    if _DISABLE_PENALTIES:
        return 0.0
    path = file_path or ""
    if _GUIDE_PATH_RE.search(path):
        return GUIDE_PENALTY
    if _TEST_PATH_RE.search(path):
        return TEST_PENALTY
    if _CI_PATH_RE.search(path):
        return CI_PENALTY
    if (file_type or "") in _DOC_FILE_TYPES:
        return DOC_PENALTY
    return 0.0


def rerank(
    query: str,
    results: list[dict],
    limit: int = 10,
    *,
    reranker_override=None,
) -> list[dict]:
    """Rerank search results with the local CrossEncoder provider.

    Takes RRF-fused results and reranks by scoring each snippet
    against the query. Combines: 70% reranker score + 30% normalized RRF score.

    `reranker_override` (P0a): any object with a `rerank(query, documents, limit)`
    method replaces `get_reranker()` for the duration of this call. Used by
    `scripts/eval_finetune.py --use-hybrid-retrieval` to score the same RRF pool
    with an arbitrary CrossEncoder so eval shares the production candidate set.
    """
    if not results or len(results) <= 1:
        return results

    if reranker_override is not None:
        reranker, err = reranker_override, None
    else:
        # Run 2 routing (2026-04-27): use code-tuned l12 FT for code-intent queries,
        # default L6 for docs. Verified on jira n=908: l12 +3.31pp top-10 vs L6 on
        # code (POSITIVE bootstrap-confirmed). On docs: l12 -9pp top-10 (NEGATIVE).
        intent = "docs" if _query_wants_docs(query) else "code"
        reranker, err = get_reranker(intent=intent)
    if err or reranker is None:
        return results  # Fallback: return original order

    # Build document strings for reranker
    documents: list[str] = []
    for r in results:
        doc = re.sub(r">>>|<<<|\.\.\.|\[Repo: [^\]]+\]", "", r.get("snippet", ""))
        doc = f"{r['repo_name']} {r['file_path']} {doc}"
        documents.append(doc)

    scores = reranker.rerank(query, documents, limit=limit)

    if not scores:
        return results[:limit]

    # Normalize reranker scores to [0, 1]
    max_score = max(scores) if scores else 1
    min_score = min(scores) if scores else 0
    score_range = max_score - min_score if max_score != min_score else 1

    # Combine: reranker score (70%) + original RRF score (30%)
    max_rrf = max(r["score"] for r in results) if results else 1
    min_rrf = min(r["score"] for r in results) if results else 0
    rrf_range = max_rrf - min_rrf if max_rrf != min_rrf else 1

    # Skip doc/test penalties when the query explicitly asks for them.
    apply_penalties = not _query_wants_docs(query)

    for i, r in enumerate(results):
        rrf_norm = (r["score"] - min_rrf) / rrf_range
        rerank_norm = (scores[i] - min_score) / score_range if i < len(scores) else 0
        r["rerank_score"] = float(scores[i]) if i < len(scores) else 0
        combined = 0.7 * rerank_norm + 0.3 * rrf_norm

        # P4.1: down-weight doc/test/guide chunks so production code ranks higher
        # on code-related queries. Stored on result for observability.
        penalty = _classify_penalty(r.get("file_type", ""), r.get("file_path", "")) if apply_penalties else 0.0
        r["penalty"] = penalty
        r["combined_score"] = combined - penalty

    results.sort(key=lambda x: x["combined_score"], reverse=True)
    return results[:limit]
