"""Task classifier — detect domain (PI, CORE-risk, CORE-api, etc.) from description."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from src.config import DOMAIN_PATTERNS

from .pi_analyzer import detect_provider


@dataclass(frozen=True)
class TaskClassification:
    """Result of classifying a task into a domain."""

    domain: str  # "pi", "core-risk", "core-api", "core-3ds", "core-platform", "core-payment", "bo", "hs", "unknown"
    provider: str  # Non-empty only for PI
    confidence: float  # 0.0 - 1.0
    matched_keywords: list[str] = field(default_factory=list)
    seed_repos: list[str] = field(default_factory=list)


def classify_task(
    conn: sqlite3.Connection,
    description: str,
    explicit_provider: str,
    words: set[str],
) -> TaskClassification:
    """Classify task into domain. Returns classification with seed repos.

    Algorithm:
    1. If provider detected → PI
    2. Extract task ID prefix as signal (PI-, CORE-, BO-, HS-)
    3. Match keywords against domain_patterns from conventions.yaml
    4. Score each domain, return highest
    """
    # 1. Provider detection → PI
    provider = explicit_provider
    if not provider:
        provider = detect_provider(conn, words)
    if provider:
        return TaskClassification(domain="pi", provider=provider, confidence=1.0)

    # 2. Task ID prefix as signal
    prefix_match = re.search(r"(PI|CORE|BO|HS|PAY|FE|BE|INF)-?\d+", description, re.IGNORECASE)
    task_prefix = prefix_match.group(1).upper() if prefix_match else ""

    # Map prefix to domain bias
    prefix_bias: dict[str, str] = {
        "PI": "pi",
        "BO": "bo",
        "HS": "hs",
    }

    # If prefix is BO or HS, use that directly (low ambiguity)
    if task_prefix in prefix_bias:
        domain = prefix_bias[task_prefix]
        pattern = DOMAIN_PATTERNS.get(domain, {})
        return TaskClassification(
            domain=domain,
            provider="",
            confidence=0.7,
            seed_repos=pattern.get("seed_repos", []),
        )

    # 3. Keyword matching against domain patterns
    if not DOMAIN_PATTERNS:
        return TaskClassification(domain="unknown", provider="", confidence=0.0)

    desc_lower = description.lower()
    scores: list[tuple[str, float, list[str], list[str]]] = []

    for domain_name, pattern in DOMAIN_PATTERNS.items():
        keywords = pattern.get("keywords", [])
        repo_patterns = pattern.get("repo_patterns", [])
        seed_repos = pattern.get("seed_repos", [])

        matched: list[str] = []
        score = 0.0

        # Keyword matching
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in desc_lower:
                matched.append(kw)
                # Multi-word keywords score higher
                score += 2.0 if " " in kw else 1.0

        # Repo pattern matching against words (e.g., "risk" in words matches "grpc-risk-.*")
        for rp in repo_patterns:
            # Extract meaningful parts from regex (e.g., "grpc-risk-.*" → "risk")
            parts = re.sub(r"[.*?+\[\]()\\]", " ", rp).split("-")
            for part in parts:
                part = part.strip()
                if len(part) > 2 and part in words and part not in matched:
                    matched.append(part)
                    score += 0.5

        if score > 0:
            scores.append((domain_name, score, matched, seed_repos))

    if not scores:
        return TaskClassification(domain="unknown", provider="", confidence=0.0)

    # Sort by score, take best
    scores.sort(key=lambda x: x[1], reverse=True)
    best_domain, best_score, best_matched, best_seeds = scores[0]

    # Confidence: normalize score (cap at 1.0)
    confidence = min(best_score / 4.0, 1.0)

    # If CORE prefix, boost CORE domains
    if task_prefix == "CORE" and best_domain.startswith("core-"):
        confidence = min(confidence + 0.2, 1.0)

    return TaskClassification(
        domain=best_domain,
        provider="",
        confidence=confidence,
        matched_keywords=best_matched,
        seed_repos=best_seeds,
    )
