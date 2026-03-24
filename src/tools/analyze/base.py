"""Shared types and utilities for analyze_task modules."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

_KEYWORD_STOP_WORDS = frozenset(
    {
        "should",
        "which",
        "where",
        "their",
        "about",
        "these",
        "those",
        "would",
        "could",
        "check",
        "start",
        "needs",
    }
)


@dataclass
class AnalysisContext:
    """Shared state accumulated across analyzer sections."""

    conn: sqlite3.Connection
    description: str
    words: set[str]
    provider: str
    findings: list[tuple[str, str, str]] = field(default_factory=list)
    # Each finding: (finding_type, repo_name, confidence)
    # confidence: "high", "medium", "low"


def useful_keywords(words: set[str]) -> list[str]:
    """Filter words to those useful for FTS queries."""
    return [w for w in words if len(w) > 4 and w not in _KEYWORD_STOP_WORDS]


def fts_queries(provider: str, words: set[str]) -> list[str]:
    """Build FTS query strings from provider + words."""
    queries = []
    if provider:
        queries.append(f'"{provider}"')
    for w in words:
        if len(w) > 5 and w not in _KEYWORD_STOP_WORDS:
            queries.append(f'"{w}"')
    return queries


def extract_task_id(description: str) -> str:
    """Extract task ID (e.g., PI-54, CORE-2545) from description."""
    match = re.search(r"(PI|CORE|PAY|FE|BE|INF|BO|HS)-?\d+", description, re.IGNORECASE)
    return match.group(0).lower().replace("_", "-") if match else ""
