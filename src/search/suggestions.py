"""Query suggestions when search returns 0 results.

Suggests similar terms from:
1. Domain glossary (abbreviation ↔ expansion fuzzy match)
2. Repo names (fuzzy substring match)
"""

from __future__ import annotations

from src.config import DOMAIN_GLOSSARY
from src.container import db_connection


def _fuzzy_match(query: str, candidates: list[str], max_results: int = 5) -> list[str]:
    """Simple fuzzy matching: score by longest common substring ratio."""
    query_lower = query.lower()
    scored: list[tuple[str, float]] = []

    for candidate in candidates:
        cand_lower = candidate.lower()
        # Direct substring match scores highest
        if query_lower in cand_lower or cand_lower in query_lower:
            scored.append((candidate, 1.0))
            continue
        # Token overlap
        q_tokens = set(query_lower.split())
        c_tokens = set(cand_lower.replace("-", " ").replace("_", " ").split())
        overlap = q_tokens & c_tokens
        if overlap:
            score = len(overlap) / max(len(q_tokens), len(c_tokens))
            scored.append((candidate, score))
            continue
        # Character trigram similarity
        q_trigrams = {query_lower[i : i + 3] for i in range(len(query_lower) - 2)} if len(query_lower) >= 3 else set()
        c_trigrams = {cand_lower[i : i + 3] for i in range(len(cand_lower) - 2)} if len(cand_lower) >= 3 else set()
        if q_trigrams and c_trigrams:
            similarity = len(q_trigrams & c_trigrams) / len(q_trigrams | c_trigrams)
            if similarity > 0.15:
                scored.append((candidate, similarity))

    scored.sort(key=lambda x: -x[1])
    return [s[0] for s in scored[:max_results]]


def suggest_queries(query: str, max_suggestions: int = 5) -> list[str]:
    """Generate query suggestions based on glossary and repo names.

    Returns a list of suggested search terms.
    """
    suggestions: list[str] = []

    # 1. Glossary matches — check both abbreviations and expansions
    glossary_terms: list[str] = []
    for abbr, expansion in DOMAIN_GLOSSARY.items():
        glossary_terms.append(abbr.upper())
        glossary_terms.extend(expansion.split())
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_terms: list[str] = []
    for t in glossary_terms:
        t_lower = t.lower()
        if t_lower not in seen and len(t) >= 3:
            seen.add(t_lower)
            unique_terms.append(t)
    glossary_matches = _fuzzy_match(query, unique_terms, max_results=3)
    suggestions.extend(glossary_matches)

    # 2. Repo name matches
    try:
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT name FROM graph_nodes WHERE node_type = 'repo' OR node_type IS NULL"
            ).fetchall()
            repo_names = [r["name"] for r in rows]
    except Exception:
        repo_names = []

    if repo_names:
        repo_matches = _fuzzy_match(query, repo_names, max_results=3)
        suggestions.extend(repo_matches)

    # Deduplicate and limit
    seen_suggestions: set[str] = set()
    result: list[str] = []
    for s in suggestions:
        s_lower = s.lower()
        if s_lower not in seen_suggestions and s_lower != query.lower():
            seen_suggestions.add(s_lower)
            result.append(s)
    return result[:max_suggestions]


def format_no_results(query: str, context: str = "") -> str:
    """Format a 'no results' message with suggestions.

    Args:
        query: The original search query
        context: Optional context like repo/file_type filters
    """
    suggestions = suggest_queries(query)
    msg = f"No results for '{query}'."
    if context:
        msg += f" {context}"

    if suggestions:
        msg += "\n\nDid you mean:\n"
        for s in suggestions:
            msg += f"  - {s}\n"
        msg += "\nTip: Try broader terms, remove filters, or use semantic_search for conceptual queries."
    else:
        msg += " Try different keywords or broader terms."

    return msg
