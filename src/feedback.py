"""Ranking feedback logger — logs search queries and results to JSONL.

Captures what was searched, what was returned, and in what order.
Used for offline analysis to tune RRF weights and CrossEncoder thresholds.

Log format (one JSON object per line):
  {timestamp, tool, query, params, results: [{repo, file, score, rank}], total_candidates}

Analysis script: scripts/analyze_feedback.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock

_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
_LOG_DIR = _BASE / "logs"
_LOG_FILE = _LOG_DIR / "search_feedback.jsonl"
_MAX_LOG_SIZE = 50 * 1024 * 1024  # 50MB rotation threshold
_lock = Lock()


def _rotate_if_needed() -> None:
    """Rotate log file if it exceeds max size."""
    try:
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _MAX_LOG_SIZE:
            rotated = _LOG_FILE.with_suffix(".jsonl.1")
            if rotated.exists():
                rotated.unlink()
            _LOG_FILE.rename(rotated)
    except OSError:
        pass


def log_search(
    tool: str,
    query: str,
    params: dict,
    results: list[dict],
    total_candidates: int = 0,
) -> None:
    """Log a search call with its results for offline analysis.

    Args:
        tool: "search" or "semantic_search"
        query: The expanded query string
        params: {repo, file_type, limit}
        results: List of result dicts with repo_name, file_path, score fields
        total_candidates: Total candidates before final ranking
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": tool,
        "query": query,
        "params": {k: v for k, v in params.items() if v},
        "results": [
            {
                "rank": i + 1,
                "repo": r.get("repo_name", ""),
                "file": r.get("file_path", ""),
                "score": round(r.get("combined_score", r.get("score", 0)), 4),
                "sources": r.get("sources", []),
            }
            for i, r in enumerate(results)
        ],
        "total_candidates": total_candidates,
        "result_count": len(results),
    }

    with _lock:
        try:
            _rotate_if_needed()
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(_LOG_FILE, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            pass  # Never fail a search because of logging
