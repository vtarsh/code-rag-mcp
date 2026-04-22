"""FTS5 full-text search + query sanitization + query expansion.

Handles:
- Abbreviation expansion via domain glossary
- FTS5 query sanitization (OR mode, dotted tokens, hyphenated terms)
- Keyword search with repo diversity capping
"""

from __future__ import annotations

import logging
import re
import sqlite3

from src.config import DOMAIN_GLOSSARY, PHRASE_GLOSSARY
from src.container import db_connection
from src.types import SearchResult


def expand_query(query: str) -> str:
    """Expand abbreviations in query using the domain glossary.

    Two-pass expansion:
    1. Single-token: looks up each token in DOMAIN_GLOSSARY
    2. Phrase-aware: checks if query contains ALL tokens from a phrase pattern,
       and appends expansion terms. This handles cases like
       "add method provider" → adds "boilerplate template"

    Example: "NT provider flow" -> "NT provider flow network token"
    """
    tokens = query.split()
    lower_tokens = {t.lower().strip(".,;:!?") for t in tokens}
    expansions = []

    # Pass 1: single-token glossary
    for token in tokens:
        key = token.lower().strip(".,;:!?")
        if key in DOMAIN_GLOSSARY:
            expansions.append(DOMAIN_GLOSSARY[key])

    # Pass 2: phrase-aware glossary (multi-token patterns)
    for pattern_tokens, expansion in PHRASE_GLOSSARY:
        if pattern_tokens.issubset(lower_tokens):
            expansions.append(expansion)

    if expansions:
        # Deduplicate expansion terms while preserving order
        seen: set[str] = set()
        unique_terms: list[str] = []
        for term in " ".join(expansions).split():
            if term.lower() not in seen:
                seen.add(term.lower())
                unique_terms.append(term)
        expanded = query + " " + " ".join(unique_terms)
        logging.debug("Query expanded: %r -> %r", query, expanded)
        return expanded
    return query


def _sanitize_fts_input(query: str) -> str:
    """Remove FTS5 special operators from user input.

    Uses word boundaries so leading/trailing operators (e.g. "AND foo" or
    "foo OR") are also stripped — the old ` {op} ` pattern missed them and
    the resulting FTS5 syntax error was silently swallowed, killing search.
    """
    for op in ("AND", "OR", "NOT", "NEAR"):
        query = re.sub(rf"\b{op}\b", " ", query)
    # Remove special characters
    query = re.sub(r'[*"()]', "", query)
    # Collapse whitespace
    return re.sub(r"\s+", " ", query).strip()


_STOP_WORDS = frozenset(
    {
        "add", "get", "set", "use", "new", "the", "for", "and", "with", "from",
        "how", "does", "what", "this", "that", "into", "make", "call",
        "need", "want", "help", "show", "find", "look", "check", "support",
        "change", "update", "create", "delete", "remove", "implement",
        "about", "where",
    }
)


def sanitize_fts_with_stop_words(
    query: str,
    stop_words: frozenset[str] | None = None,
) -> str:
    """Sanitize query for FTS5 with stop-word removal and OR mode.

    Used for proto/context searches where stop words like 'add', 'support'
    would match too broadly. Falls back to length-only filtering when all
    meaningful tokens are stop words.

    Args:
        query: Raw search query
        stop_words: Custom stop words set (defaults to built-in _STOP_WORDS)
    """
    sw = stop_words if stop_words is not None else _STOP_WORDS
    tokens = query.split()
    sanitized = [t for t in tokens if len(t) >= 3 and t.lower() not in sw]
    if not sanitized:
        sanitized = [t for t in tokens if len(t) >= 3]
    return " OR ".join(sanitized) if sanitized else ""


def sanitize_fts_query(query: str) -> str:
    """Sanitize query for FTS5. Uses OR to find partial matches.

    FTS5 default is AND (all terms must appear in same chunk).
    For code search, OR is better — finding "trustly" OR "verification"
    is more useful than requiring both in the same chunk.
    The reranker then sorts by actual relevance.
    """
    query = _sanitize_fts_input(query)
    tokens = query.split()
    sanitized: list[str] = []
    for token in tokens:
        if len(token) < 3:
            continue
        if "-" in token and not token.startswith('"'):
            sanitized.append(f'"{token}"')
        elif "." in token:
            parts = [p for p in token.split(".") if len(p) >= 3]
            sanitized.extend(parts)
            sanitized.append(f'"{token}"')
        else:
            sanitized.append(token)
    return " OR ".join(sanitized) if sanitized else query


def fts_search(
    query: str,
    repo: str = "",
    file_type: str = "",
    exclude_file_types: str = "",
    limit: int = 50,
) -> list[SearchResult]:
    """Run FTS5 keyword search — returns ALL candidates for fusion.

    No per-repo capping here. Diversity is applied AFTER RRF fusion and
    reranking in the presentation layer, so we never silently drop
    candidates that could be globally relevant.

    Args:
        query: Raw search query (will be sanitized for FTS5)
        repo: Optional repo name filter (partial match)
        file_type: Optional file type filter (exact match)
        exclude_file_types: Optional comma-separated file types to exclude (e.g. "gotchas,task")
        limit: Max candidates to fetch from DB (pool for fusion)

    Returns list of SearchResult sorted by FTS5 rank.
    """
    fts_query = sanitize_fts_query(query)

    with db_connection() as conn:
        where_clauses = ["chunks MATCH ?"]
        params: list[str | int] = [fts_query]
        if repo:
            where_clauses.append("repo_name LIKE ?")
            params.append(f"%{repo}%")
        if file_type:
            where_clauses.append("file_type = ?")
            params.append(file_type)
        if exclude_file_types:
            excluded = [ft.strip() for ft in exclude_file_types.split(",") if ft.strip()]
            if excluded:
                placeholders = ",".join("?" * len(excluded))
                where_clauses.append(f"file_type NOT IN ({placeholders})")
                params.extend(excluded)

        where = " AND ".join(where_clauses)
        params.append(limit)

        try:
            raw_rows = conn.execute(
                f"""
                SELECT rowid, repo_name, file_path, file_type, chunk_type,
                       snippet(chunks, 0, '>>>', '<<<', '...', 64) as snippet
                FROM chunks WHERE {where} ORDER BY rank LIMIT ?
            """,
                params,
            ).fetchall()

            return [
                SearchResult(
                    rowid=row["rowid"],
                    repo_name=row["repo_name"],
                    file_path=row["file_path"],
                    file_type=row["file_type"],
                    chunk_type=row["chunk_type"],
                    snippet=row["snippet"],
                )
                for row in raw_rows
            ]

        except sqlite3.OperationalError:
            return []
