"""Per-query pipeline trace logger.

Emits one JSONL line per hybrid_search call to a log file when
`CODE_RAG_TRACE=1`. Used to catch silent pipeline bugs — historical examples:
the 28.4% silent FTS5 OperationalError (`_sanitize_fts_input` fix 2026-04-27),
the daemon CODE_RAG_DEFAULT_EXCLUDE leak (paypal-docs returned 0 for any
query, 2026-05-19). A per-query trace would have flagged both inside an hour.

Default OFF so production stdout / output stays clean. Override the log path
with `CODE_RAG_TRACE_LOG`; default is `$CODE_RAG_HOME/bench_runs/trace.jsonl`.

Failure-safe: any exception inside emit_trace is swallowed so a broken trace
sink can never break search.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def emit_trace(record: dict) -> None:
    """Append one JSONL trace line. Silent on failure — never breaks search."""
    if os.getenv("CODE_RAG_TRACE", "0") != "1":
        return
    try:
        out = {"ts": time.time(), **record}
        path = os.getenv("CODE_RAG_TRACE_LOG", "").strip()
        if not path:
            home = os.getenv("CODE_RAG_HOME", str(Path.home() / ".code-rag-mcp"))
            path = str(Path(home) / "bench_runs" / "trace.jsonl")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(out, default=str) + "\n")
    except Exception as exc:  # never break search on trace failure
        _LOGGER.debug("trace emit failed: %r", exc)
