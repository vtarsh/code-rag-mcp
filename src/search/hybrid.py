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

import re

from src.config import GOTCHAS_BOOST, KEYWORD_WEIGHT, REFERENCE_BOOST, RRF_K
from src.container import db_connection, get_reranker
from src.search.fts import fts_search
from src.search.vector import vector_search


def rerank(query: str, results: list[dict], limit: int = 10) -> list[dict]:
    """Rerank search results using cross-encoder for better relevance.

    Takes RRF-fused results and reranks by comparing each snippet
    directly against the query (pairwise scoring).

    Combines: 70% reranker score + 30% normalized RRF score.
    """
    if not results or len(results) <= 1:
        return results

    reranker_model, err = get_reranker()
    if err or reranker_model is None:
        return results  # Fallback: return original order

    # Build query-document pairs for cross-encoder
    pairs: list[tuple[str, str]] = []
    for r in results:
        doc = re.sub(r">>>|<<<|\.\.\.|\[Repo: [^\]]+\]", "", r.get("snippet", ""))
        doc = f"{r['repo_name']} {r['file_path']} {doc}"
        pairs.append((query, doc))

    scores = reranker_model.predict(pairs)

    # Combine: reranker score (70%) + original RRF score (30%)
    max_rrf = max(r["score"] for r in results) if results else 1
    min_rrf = min(r["score"] for r in results) if results else 0
    rrf_range = max_rrf - min_rrf if max_rrf != min_rrf else 1

    for i, r in enumerate(results):
        rrf_norm = (r["score"] - min_rrf) / rrf_range
        r["rerank_score"] = float(scores[i])
        r["combined_score"] = 0.7 * float(scores[i]) + 0.3 * rrf_norm

    results.sort(key=lambda x: x["combined_score"], reverse=True)
    return results[:limit]


def hybrid_search(
    query: str,
    repo: str = "",
    file_type: str = "",
    exclude_file_types: str = "",
    limit: int = 10,
) -> tuple[list[dict], str | None, int]:
    """Hybrid search: combine FTS5 keyword + vector similarity via RRF.

    Keyword results get 2x weight because exact term matches are more
    reliable for code search than semantic similarity alone.

    Returns (ranked_results, vector_error | None, total_candidates).
    """
    K = RRF_K
    KW_WEIGHT = KEYWORD_WEIGHT

    # 1. Keyword search (FTS5) — large pool, no per-repo cap
    keyword_results = fts_search(query, repo, file_type, exclude_file_types, limit=100)

    # 2. Vector search
    vector_results, vec_err = vector_search(query, repo, file_type, exclude_file_types, limit=50)

    # 3. RRF fusion
    scores: dict[int, dict] = {}  # rowid → merged result dict

    for rank_idx, sr in enumerate(keyword_results):
        rid = sr.rowid
        rrf_score = KW_WEIGHT / (K + rank_idx + 1)
        if rid not in scores:
            scores[rid] = {
                "score": 0,
                "repo_name": sr.repo_name,
                "file_path": sr.file_path,
                "file_type": sr.file_type,
                "chunk_type": sr.chunk_type,
                "snippet": sr.snippet,
                "sources": [],
            }
        scores[rid]["score"] += rrf_score
        scores[rid]["sources"].append("keyword")

    for rank_idx, vrow in enumerate(vector_results):
        rid = vrow["rowid"]
        rrf_score = 1.0 / (K + rank_idx + 1)
        if rid not in scores:
            scores[rid] = {
                "score": 0,
                "repo_name": vrow["repo_name"],
                "file_path": vrow["file_path"],
                "file_type": vrow["file_type"],
                "chunk_type": vrow["chunk_type"],
                "snippet": vrow.get("content_preview", ""),
                "sources": [],
            }
        scores[rid]["score"] += rrf_score
        scores[rid]["sources"].append("vector")

    # Apply content-type boosts — curated knowledge ranks higher
    TASK_BOOST = {
        "task_decisions": 1.1,
        "task_plan": 1.1,
        "task_api_spec": 1.05,
        "task_gotchas": 1.1,
        "task_description": 1.05,
        "task_metadata": 0.95,
        "task_progress": 0.7,
        "task_section": 1.0,
    }
    for _rid, data in scores.items():
        ft = data.get("file_type", "")
        if ft == "gotchas":
            data["score"] *= GOTCHAS_BOOST
        elif ft == "task":
            data["score"] *= TASK_BOOST.get(data.get("chunk_type", ""), 1.0)
        elif ft == "reference":
            data["score"] *= REFERENCE_BOOST

    total_candidates = len(scores)

    # Sort by RRF score, take top candidates for reranking
    ranked = sorted(scores.values(), key=lambda x: x["score"], reverse=True)[: limit * 2]

    # Rerank with cross-encoder
    ranked = rerank(query, ranked, limit)

    # Expand top results with sibling chunks for context
    ranked = _expand_siblings(ranked)

    # Inject similar repo annotations
    ranked = _annotate_similar_repos(ranked)

    return ranked, vec_err, total_candidates


def _expand_siblings(results: list[dict], max_siblings: int = 2) -> list[dict]:
    """For top results, append prev/next chunks from the same file as context.

    This helps reconstruct function bodies that span multiple chunks.
    Sibling chunks are appended to the snippet text, not added as separate results.
    """
    if not results:
        return results

    try:
        with db_connection() as conn:
            # Check if chunk_meta table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunk_meta'"
            ).fetchone()
            if not table_check:
                return results

            for result in results[:max_siblings]:
                repo = result["repo_name"]
                file_path = result["file_path"]

                # Find all chunks for this (repo, file) with ordering
                rows = conn.execute(
                    """SELECT c.rowid, c.content, cm.chunk_order
                       FROM chunks c
                       JOIN chunk_meta cm ON cm.chunk_rowid = c.rowid
                       WHERE c.repo_name = ? AND c.file_path = ?
                       ORDER BY cm.chunk_order""",
                    (repo, file_path),
                ).fetchall()

                if len(rows) <= 1:
                    continue

                # Find which chunk in the sequence matches our result
                snippet_text = result.get("snippet", "")
                current_order = None
                for row in rows:
                    # Match by content overlap
                    content = row[1] if isinstance(row, tuple) else row["content"]
                    if content and snippet_text and content[:100] in snippet_text[:200]:
                        current_order = row[2] if isinstance(row, tuple) else row["chunk_order"]
                        break

                if current_order is None:
                    continue

                # Collect adjacent chunks
                siblings = []
                for row in rows:
                    order = row[2] if isinstance(row, tuple) else row["chunk_order"]
                    content = row[1] if isinstance(row, tuple) else row["content"]
                    if order == current_order - 1 or order == current_order + 1:
                        siblings.append((order, content))

                if siblings:
                    siblings.sort(key=lambda x: x[0])
                    context_parts = []
                    for order, content in siblings:
                        label = "prev" if order < current_order else "next"
                        # Truncate sibling content to avoid huge results
                        truncated = content[:1000] if len(content) > 1000 else content
                        context_parts.append(f"\n--- [{label} chunk from same file] ---\n{truncated}")

                    result["snippet"] += "".join(context_parts)
                    result["has_siblings"] = True

            return results
    except Exception:
        return results


def _annotate_similar_repos(results: list[dict]) -> list[dict]:
    """Check if any result repos have similar_repo edges and add annotations.

    If a repo in results has a similar_repo edge to another repo NOT in results,
    inject an annotation so the user knows about the similar repo.
    """
    if not results:
        return results

    try:
        with db_connection() as conn:
            # Check if graph_edges table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_edges'"
            ).fetchone()
            if not table_check:
                return results

            result_repos = {r["repo_name"] for r in results}

            # Find similar_repo edges for repos in results
            if not result_repos:
                return results

            placeholders = ",".join("?" * len(result_repos))
            similar_rows = conn.execute(
                f"SELECT source, target, detail FROM graph_edges "
                f"WHERE edge_type = 'similar_repo' AND source IN ({placeholders})",
                list(result_repos),
            ).fetchall()

            if not similar_rows:
                return results

            # Group by source repo
            similar_map: dict[str, list[tuple[str, str]]] = {}
            for source, target, detail in similar_rows:
                similar_map.setdefault(source, []).append((target, detail))

            # Annotate results that have similar repos NOT already in results
            for result in results:
                repo = result["repo_name"]
                if repo in similar_map:
                    missing_similar = [
                        (target, detail) for target, detail in similar_map[repo] if target not in result_repos
                    ]
                    if missing_similar:
                        annotations = []
                        for target, detail in missing_similar[:3]:
                            annotations.append(f"{target} ({detail})")
                        result["snippet"] += "\n\n--- Similar repos (may be confused) ---\n" + "\n".join(
                            f"  - {a}" for a in annotations
                        )
                        result["has_similar_repos"] = True

            return results
    except Exception:
        return results
