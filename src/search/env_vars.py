"""Targeted retrieval over env_vars — UPPERCASE identifiers and repo boost.

env_vars is a flat (repo, var_name, raw_value, source, is_map) table with 4753
rows over 427 repos. Built by `scripts/build_env_index.py`. Was unread at query
time before P0c.

Activates only when the query contains an UPPERCASE_IDENTIFIER pattern
(≥3 chars, letters/digits/underscore). Returns matching rows so the hybrid
pipeline can boost repos where the identifier is defined.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from src.container import db_connection

# Match UPPERCASE_SNAKE_CASE tokens — typical for env var names.
# Requires at least one underscore or digit, OR length ≥5, to avoid matching
# common words like "URL", "JSON", "HTML" on their own.
_UPPER_IDENT_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def extract_upper_idents(query: str) -> list[str]:
    """Extract UPPERCASE identifiers from a query.

    Returns deduplicated list preserving first-occurrence order. Filters out
    short generic acronyms (URL, API, TLS, HTTP, JSON, HTML, XML, ...) that
    would otherwise trigger repo boost on every web/config query — a match
    requires an underscore or digit, OR length >= 5.
    """
    seen: set[str] = set()
    idents: list[str] = []
    for match in _UPPER_IDENT_RE.findall(query or ""):
        if match in seen:
            continue
        has_separator = "_" in match or any(c.isdigit() for c in match)
        # Skip 3-char generic acronyms (URL/API/TLS/XML/SQL/...) that would
        # spray env_var boost across every config/web query.
        if not has_separator and len(match) < 4:
            continue
        seen.add(match)
        idents.append(match)
    return idents


def env_var_search(query: str, limit: int = 30) -> list[dict[str, Any]]:
    """Return env_vars rows matching UPPERCASE tokens in the query.

    Returns list of dicts with: repo, var_name, raw_value, source.
    Empty list when query has no UPPERCASE identifiers, or on DB error.
    """
    idents = extract_upper_idents(query)
    if not idents:
        return []

    clauses = " OR ".join(["var_name LIKE ?"] * len(idents))
    params: list[Any] = [f"%{ident}%" for ident in idents]
    params.append(limit)

    with db_connection() as conn:
        try:
            rows = conn.execute(
                f"""SELECT repo, var_name, raw_value, source
                    FROM env_vars
                    WHERE {clauses}
                    LIMIT ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []
