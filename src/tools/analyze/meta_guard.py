"""Meta-guard section — warns when analyze_task query closely matches a stored task.

When an analyze_task query has high overlap with an existing task_history row,
the returned "relevant context" is actually memoized lookup, not generalization.
This section emits a warning so the caller knows their results are dominated by
a single historical task.

Detection strategy:
  1. Tokenize query (len>=4 tokens, hyphens stripped).
  2. Compute document frequency (DF) of each query token across task_history.
  3. Keep only RARE tokens (0 < df < RARE_DF_THRESHOLD) — these are distinctive
     terms like provider names (payper, interac) that uniquely identify a task.
  4. For each task, score = (rare query tokens present in task) / (total rare).
  5. If top task's score >= SIMILARITY_THRESHOLD and we have enough rare tokens,
     emit the warning.

This cleanly handles:
  - Iugu/Pix/Brazilian (0 corpus occurrences) → not rare-matched → no warning.
  - Payper/Interac (1-3 corpus occurrences) → rare-matched → warning if matches
    concentrate on one stored task.

The `exclude_task_id` from AnalysisContext excludes that task from scoring (used
by the blind eval harness).
"""

from __future__ import annotations

import re

from .base import AnalysisContext

# Tokens with corpus DF below this count are considered distinctive (rare).
# With 987 tasks, 20 ≈ 2% — filters generic terms (provider, payment, webhook)
# but keeps provider names (payper=1, interac=3, trustly=4).
RARE_DF_THRESHOLD = 20

# Minimum number of rare tokens required in the query to even consider matching.
# With only 1 rare token we get trivial 100% matches on any task containing it.
MIN_RARE_TOKENS = 2

# Score threshold (fraction of rare tokens matched) to trigger the warning.
SIMILARITY_THRESHOLD = 0.7

_STOP_WORDS = frozenset(
    {
        "should", "which", "where", "their", "about", "these", "those",
        "would", "could", "check", "start", "needs", "with", "that", "this",
        "from", "into", "when", "have", "been", "also", "will", "need",
    }
)


def _normalize(text: str) -> str:
    """Lowercase and strip hyphens/underscores so 'e-Transfer' ~= 'etransfer'."""
    return re.sub(r"[-_]+", "", text.lower())


def _extract_query_tokens(description: str) -> set[str]:
    """Extract distinctive tokens from query (len>=4, not stopword)."""
    normalized = _normalize(description)
    tokens = set(re.findall(r"[a-z]{4,}", normalized))
    return tokens - _STOP_WORDS


def _extract_jira_ids(description: str) -> list[str]:
    """Extract Jira-style ticket IDs (e.g. PI-60, CORE-2408) from the raw query."""
    return [m.upper() for m in re.findall(r"\b([A-Z]+-\d+)\b", description)]


def section_meta_guard(ctx: AnalysisContext) -> str:
    """Emit warning if query overlaps heavily with a single stored task.

    Returns markdown section or empty string.
    """
    # Fetch all tasks (id + normalized searchable text)
    try:
        rows = ctx.conn.execute(
            "SELECT ticket_id, summary, description FROM task_history"
        ).fetchall()
    except Exception:
        return ""

    if not rows:
        return ""

    exclude_id = (ctx.exclude_task_id or "").upper()

    # Jira-ID short-circuit: if the query literally names a task that exists
    # in task_history, this is a direct lookup — warn immediately regardless
    # of rare-token analysis. Handles "PI-60" / "Related to CORE-2408" cases.
    known_ids = {row[0].upper() for row in rows}
    query_ids = [tid for tid in _extract_jira_ids(ctx.description)
                 if tid in known_ids and tid != exclude_id]
    if query_ids:
        ids_list = ", ".join(f"`{t}`" for t in query_ids)
        return (
            "## :warning: Memoization Warning\n\n"
            f"**Query directly references stored task(s): {ids_list}.**\n\n"
            "Results are a retrospective lookup of these tasks, not a prediction. "
            "For proactive planning, describe the task without citing ticket IDs.\n\n"
        )

    query_tokens = _extract_query_tokens(ctx.description)
    if not query_tokens:
        return ""

    # Normalize all task texts once and compute DF per query token.
    token_df: dict[str, int] = dict.fromkeys(query_tokens, 0)
    normalized_tasks: list[tuple[str, str]] = []
    for tid, summary, desc in rows:
        text = f"{summary or ''} {desc or ''}"
        norm = _normalize(text)
        normalized_tasks.append((tid, norm))
        for tok in query_tokens:
            if tok in norm:
                token_df[tok] += 1

    # Keep only rare tokens (distinctive enough to signal memoization).
    rare_tokens = [tok for tok, df in token_df.items() if 0 < df < RARE_DF_THRESHOLD]
    if len(rare_tokens) < MIN_RARE_TOKENS:
        return ""

    # Score each task (excluding the one marked for blind eval).
    best_score = 0.0
    best_tid = ""
    best_matches: list[str] = []
    rare_count = len(rare_tokens)
    for tid, norm in normalized_tasks:
        if tid.upper() == exclude_id:
            continue
        matches = [tok for tok in rare_tokens if tok in norm]
        score = len(matches) / rare_count
        if score > best_score:
            best_score = score
            best_tid = tid
            best_matches = matches

    if best_score < SIMILARITY_THRESHOLD or not best_tid:
        return ""

    matched_list = ", ".join(f"`{t}`" for t in sorted(best_matches))
    output = "## :warning: Memoization Warning\n\n"
    output += (
        f"**Query closely matches stored task `{best_tid}`** "
        f"(similarity {best_score:.2f}, rare terms: {matched_list}).\n\n"
    )
    output += (
        "Results include memoized data from this task, not generalizations. "
        "For proactive planning, rephrase without keywords from stored tasks.\n\n"
    )
    return output
