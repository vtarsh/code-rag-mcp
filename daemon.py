#!/usr/bin/env python3
"""Knowledge Base daemon — persistent HTTP server holding ML models in memory.

Runs as a single long-lived process (via launchd). All MCP tool calls are
proxied here by the thin mcp_server.py stdio wrapper. This avoids loading
~1.4 GB of ML models per Claude Code session.

Endpoints:
  POST /tool/<tool_name>  — execute a tool, body = JSON args
  GET  /health            — quick liveness check
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import os
import sys
import time
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Auto-detect CODE_RAG_HOME from script location (daemon lives in project root)
if "CODE_RAG_HOME" not in os.environ:
    os.environ["CODE_RAG_HOME"] = str(Path(__file__).resolve().parent)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.container import is_model_loaded, is_reranker_loaded
from src.graph.service import (
    find_dependencies_tool,
    trace_chain_tool,
    trace_flow_tool,
    trace_impact_tool,
)
from src.search.service import search_tool
from src.tools.analyze import analyze_task_tool
from src.tools.context import context_builder_tool
from src.tools.fields import trace_field_tool
from src.tools.service import (
    diff_provider_config_tool,
    health_check_tool,
    list_repos_tool,
    repo_overview_tool,
    search_task_history_tool,
    visualize_graph_tool,
)

PORT = int(os.environ.get("CODE_RAG_PORT", os.environ.get("PAY_KNOWLEDGE_PORT", "8742")))
PID_FILE = Path(__file__).parent / "daemon.pid"

_LOG_FORMAT = "%(asctime)s [daemon] %(levelname)s %(message)s"
_LOG_DIR = Path(os.environ.get("CODE_RAG_HOME", Path(__file__).parent)) / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "daemon.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=3,
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))

_stderr_handler = logging.StreamHandler()
_stderr_level = getattr(logging, os.environ.get("DAEMON_STDERR_LEVEL", "CRITICAL"), logging.CRITICAL)
_stderr_handler.setLevel(_stderr_level)
_stderr_handler.setFormatter(logging.Formatter(_LOG_FORMAT))

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[_file_handler, _stderr_handler],
)
log = logging.getLogger(__name__)

# --- Tool registry ---
TOOLS: dict[str, Callable[[dict[str, Any]], str]] = {
    "search": lambda args: search_tool(
        args["query"],
        args.get("repo", ""),
        args.get("file_type", ""),
        args.get("exclude_file_types", ""),
        args.get("limit", 10),
    ),
    "find_dependencies": lambda args: find_dependencies_tool(args["repo_name"]),
    "trace_impact": lambda args: trace_impact_tool(args["repo_name"], args.get("max_depth", 2)),
    "trace_flow": lambda args: trace_flow_tool(args["source"], args["target"], args.get("max_depth", 5)),
    "trace_chain": lambda args: trace_chain_tool(
        args["start"], args.get("direction", "both"), args.get("max_depth", 4)
    ),
    "repo_overview": lambda args: repo_overview_tool(args["repo_name"]),
    "list_repos": lambda args: list_repos_tool(args.get("type", ""), args.get("has_dep", ""), args.get("limit", 30)),
    "analyze_task": lambda args: analyze_task_tool(args["description"], args.get("provider", "")),
    "context_builder": lambda args: context_builder_tool(
        args["query"],
        args.get("repo", ""),
        args.get("include_deps", True),
        args.get("include_proto", True),
        args.get("search_limit", 8),
    ),
    "health_check": lambda args: health_check_tool(),
    "visualize_graph": lambda args: visualize_graph_tool(args.get("repo", ""), args.get("edge_type", "")),
    "diff_provider_config": lambda args: diff_provider_config_tool(args["provider_a"], args["provider_b"]),
    "search_task_history": lambda args: search_task_history_tool(
        args["query"], args.get("developer", ""), args.get("limit", 10)
    ),
    "trace_field": lambda args: trace_field_tool(args["field"], args.get("provider", ""), args.get("mode", "trace")),
}


class DaemonHandler(BaseHTTPRequestHandler):
    """Handle /tool/<name> and /health requests."""

    def do_GET(self) -> None:
        if self.path == "/health":
            from src.embedding_provider import _embedding_provider, _reranker_provider
            from src.api_costs import get_daily_cost

            providers_ready = is_model_loaded() and is_reranker_loaded()
            emb_name = _embedding_provider.provider_name if _embedding_provider else "not initialized"
            rer_name = _reranker_provider.provider_name if _reranker_provider else "not initialized"
            self._json_response(200, {
                "status": "ok" if providers_ready else "ready",
                "embedding_provider": emb_name,
                "reranker_provider": rer_name,
                "daily_api_cost_usd": round(get_daily_cost(), 4),
                "uptime": time.time() - _start_time,
                "pid": os.getpid(),
            })
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/tool/"):
            self._json_response(404, {"error": "not found"})
            return

        tool_name = self.path[6:]  # strip "/tool/"
        if tool_name not in TOOLS:
            self._json_response(404, {"error": f"unknown tool: {tool_name}"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            args = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return

        # Detect caller source and session from headers
        ua = self.headers.get("User-Agent", "")
        source = "cli" if "cli.py" in ua else "mcp" if "mcp_server" in ua else "direct"
        session_id = self.headers.get("X-Session-ID", "")

        try:
            t0 = time.time()
            result = TOOLS[tool_name](args)
            duration_ms = (time.time() - t0) * 1000
            log.info(f"tool={tool_name} source={source} duration={duration_ms:.0f}ms")
            _log_call(tool_name, args, result, duration_ms, source=source, session=session_id)
            self._json_response(200, {"result": result})
        except Exception as e:
            duration_ms = (time.time() - t0) * 1000
            log.error(f"tool={tool_name} error: {traceback.format_exc()}")
            _log_call(tool_name, args, str(e), duration_ms, error=str(e), source=source, session=session_id)
            self._json_response(500, {"error": str(e)})

    def _json_response(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default stderr logging — we use our own logger."""
        pass


_start_time = time.time()
_CALLS_LOG = _LOG_DIR / "tool_calls.jsonl"


def _log_call(
    tool_name: str, args: dict, result: str, duration_ms: float,
    error: str | None = None, source: str = "unknown", session: str = "",
) -> None:
    """Append tool call record to JSONL log. Never raises."""
    try:
        from datetime import datetime, timezone

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "args": args,
            "duration_ms": round(duration_ms),
            "result_len": len(result),
            "result_preview": result[:300].replace("\n", " "),
            "error": error,
            "source": source,
            "session": session,
        }
        with open(_CALLS_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def write_pid() -> None:
    """Write PID file for management scripts."""
    PID_FILE.write_text(str(os.getpid()))


def cleanup_pid() -> None:
    """Remove PID file on shutdown."""
    with contextlib.suppress(OSError):
        PID_FILE.unlink(missing_ok=True)


def main() -> None:
    log.info(f"Starting daemon on port {PORT} (pid={os.getpid()})")

    # No model preloading — providers initialize lazily on first query
    # API providers (Gemini) need no preload; local fallback loads on demand
    write_pid()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), DaemonHandler)
    try:
        log.info(f"Daemon ready at http://127.0.0.1:{PORT}")
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        server.server_close()
        cleanup_pid()


if __name__ == "__main__":
    main()
