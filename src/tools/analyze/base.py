"""Shared types and utilities for analyze_task modules."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import ClassVar

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
class Finding:
    """A single analysis finding — replaces raw (ftype, repo, confidence) tuples."""

    ftype: str
    repo: str
    confidence: str = "medium"


@dataclass
class AnalysisContext:
    """Shared state accumulated across analyzer sections."""

    conn: sqlite3.Connection
    description: str
    words: set[str]
    provider: str
    findings: list[Finding] = field(default_factory=list)
    # When set, exclude this task's own data from task_history lookups
    # (used by eval harness to prevent hint leakage during blind scoring)
    exclude_task_id: str = ""
    # When True, drop repeated preamble/disclaimer prose to reduce response size.
    # Section headers and body content are preserved.
    brief: bool = False

    _CONF_RANK: ClassVar[dict[str, int]] = {"high": 0, "medium": 1, "low": 2}

    def get_repos_by_confidence(self) -> dict[str, list[str]]:
        """Return unique repos grouped by their best confidence tier.

        Returns:
            {"high": [...], "medium": [...], "low": [...]}
        """
        best: dict[str, str] = {}
        for f in self.findings:
            prev = best.get(f.repo)
            if prev is None or self._CONF_RANK.get(f.confidence, 1) < self._CONF_RANK.get(prev, 1):
                best[f.repo] = f.confidence
        result: dict[str, list[str]] = {"high": [], "medium": [], "low": []}
        for repo, conf in best.items():
            if conf in result:
                result[conf].append(repo)
        return result

    def get_unique_repos(self) -> set[str]:
        """Return all unique repo names from findings."""
        return {f.repo for f in self.findings}


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
    """Extract task ID (e.g., PI-54, pi_60, CORE-2545) from description.

    Accepts: PI-60, pi-60, PI60, pi_60, pi 60. Returns canonical lower-case
    hyphen form (e.g. "pi-60") for backwards compatibility with existing
    consumers that compare against lowercased ids.
    """
    match = re.search(r"(PI|CORE|PAY|FE|BE|INF|BO|HS)[-_ ]?(\d+)", description, re.IGNORECASE)
    if not match:
        return ""
    return f"{match.group(1).lower()}-{match.group(2)}"
