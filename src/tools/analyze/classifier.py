"""Task classifier — detect domain (PI, CORE-risk, CORE-api, etc.) from description."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from src.config import DOMAIN_PATTERNS

from .pi_analyzer import _AMBIGUOUS_PROVIDER_NAMES, count_matching_providers, detect_provider


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
    # If 3+ provider names mentioned, treat as bulk PI (no specific provider)
    # so section_bulk_providers can fire instead of single-provider analysis.
    # 1. Provider detection → PI
    # Skip ambiguous provider names for CORE/BO/HS tasks (e.g., "checkout" as a field name)
    prefix_hint = re.search(r"(PI|CORE|BO|HS)-?\d+", description, re.IGNORECASE)
    is_non_pi_prefix = prefix_hint and prefix_hint.group(1).upper() != "PI"

    provider = explicit_provider
    if not provider:
        if count_matching_providers(conn, words) >= 3:
            return TaskClassification(domain="pi", provider="", confidence=1.0)
        provider = detect_provider(conn, words)
        # Suppress provider detection for non-PI tasks when:
        # 1. Provider name is ambiguous (checkout, ach, iris, etc.) — always suppress
        # 2. Provider name is real BUT description has strong CORE domain keywords
        #    (risk, settlement, audit, migration) — suppress to avoid PI misclassification
        if provider and is_non_pi_prefix:
            if provider in _AMBIGUOUS_PROVIDER_NAMES:
                provider = ""
            else:
                # Check if description has strong non-PI domain signals
                _core_signals = {
                    "risk",
                    "settlement",
                    "audit",
                    "migration",
                    "migrate",
                    "workflow",
                    "schema",
                    "field",
                    "column",
                }
                if len(words & _core_signals) >= 2:
                    provider = ""
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

    # For BO/HS prefix: use as primary domain but still run keyword matching
    # to find secondary domains whose seed repos should also be included.
    # Example: BO-1598 "High Risk Override Reason Logic" → BO + core-risk seeds.
    prefix_domain = prefix_bias.get(task_prefix)
    prefix_seeds: list[str] = []
    if prefix_domain:
        pattern = DOMAIN_PATTERNS.get(prefix_domain, {})
        prefix_seeds = list(pattern.get("seed_repos", []))

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

    # If prefix_domain set (BO/HS) but no keyword matches, return with prefix domain
    if prefix_domain and not scores:
        return TaskClassification(
            domain=prefix_domain,
            provider="",
            confidence=0.7,
            seed_repos=prefix_seeds,
        )

    if not scores and not prefix_domain:
        return TaskClassification(domain="unknown", provider="", confidence=0.0)

    # Sort by score, take best
    scores.sort(key=lambda x: x[1], reverse=True)

    if prefix_domain:
        # BO/HS prefix: use prefix as primary, merge keyword-matched domains as secondary
        best_domain = prefix_domain
        all_keywords: list[str] = []
        all_seeds = list(prefix_seeds)
        secondary_domains: list[str] = []
        for domain_name, _score, matched, seeds in scores:
            if domain_name != prefix_domain:
                secondary_domains.append(domain_name)
            for s in seeds:
                if s not in all_seeds:
                    all_seeds.append(s)
            for kw in matched:
                if kw not in all_keywords:
                    all_keywords.append(kw)
        confidence = 0.7
    else:
        best_domain, best_score, best_matched, best_seeds = scores[0]

        # Multi-domain: if other domains score ≥50% of best, union their seed_repos
        all_keywords = list(best_matched)
        all_seeds = list(best_seeds)
        secondary_domains = []
        for domain_name, score, matched, seeds in scores[1:]:
            if score >= best_score * 0.5:
                secondary_domains.append(domain_name)
                for s in seeds:
                    if s not in all_seeds:
                        all_seeds.append(s)
                for kw in matched:
                    if kw not in all_keywords:
                        all_keywords.append(kw)

        # Confidence: normalize score (cap at 1.0)
        confidence = min(best_score / 4.0, 1.0)

        # If CORE prefix, boost CORE domains
        if task_prefix == "CORE" and best_domain.startswith("core-"):
            confidence = min(confidence + 0.2, 1.0)

    domain_label = best_domain
    if secondary_domains:
        domain_label = f"{best_domain}+{'+'.join(secondary_domains[:2])}"

    return TaskClassification(
        domain=domain_label,
        provider="",
        confidence=confidence,
        matched_keywords=all_keywords,
        seed_repos=all_seeds,
    )
