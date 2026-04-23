"""Knowledge Base daemon — persistent HTTP server holding ML models in memory.

Runs as a single long-lived process (via launchd). All MCP tool calls are
proxied here by the thin mcp_server.py stdio wrapper. This avoids loading
~1.4 GB of ML models per Claude Code session.

Endpoints:
  POST /tool/<tool_name>  — execute a tool, body = JSON args
  GET  /health            — quick liveness check
  POST /admin/unload      — drop model refs, idempotent, reversible (next tool call reloads lazily)
  POST /admin/shutdown    — drain in-flight then exit for launchd restart
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if "CODE_RAG_HOME" not in os.environ:
    os.environ["CODE_RAG_HOME"] = str(Path(__file__).resolve().parent)

sys.path.insert(0, str(Path(__file__).parent))

from datetime import UTC

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
    health_check_tool,
    list_repos_tool,
    repo_overview_tool,
    trace_internal_tool,
)
from src.tools.shadow_types import provider_type_map_tool

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

TOOLS: dict[str, Callable[[dict[str, Any]], str]] = {
    "search": lambda args: search_tool(
        args.get("query", ""),
        args.get("repo", ""),
        args.get("file_type", ""),
        args.get("exclude_file_types", ""),
        args.get("limit", 10),
        brief=args.get("brief", False),
        cross_provider=args.get("cross_provider", False),
    ),
    "find_dependencies": lambda args: find_dependencies_tool(args["repo_name"]),
    "trace_impact": lambda args: trace_impact_tool(
        args.get("repo_name") or args.get("target", ""), args.get("max_depth", 2)
    ),
    "trace_flow": lambda args: (
        trace_flow_tool(args["source"], args["target"], args.get("max_depth", 5))
        if "source" in args and "target" in args
        else "Error: trace_flow requires 'source' and 'target' parameters (repo names). "
        'Example: {"source": "grpc-payment-gateway", "target": "grpc-apm-payper"}'
    ),
    "trace_chain": lambda args: trace_chain_tool(
        args["start"], args.get("direction", "both"), args.get("max_depth", 4)
    ),
    "repo_overview": lambda args: repo_overview_tool(args["repo_name"]),
    "list_repos": lambda args: list_repos_tool(args.get("type", ""), args.get("has_dep", ""), args.get("limit", 30)),
    "analyze_task": lambda args: analyze_task_tool(
        args["description"],
        args.get("provider", ""),
        exclude_task_id=args.get("exclude_task_id", ""),
    ),
    "context_builder": lambda args: context_builder_tool(
        args["query"],
        args.get("repo", ""),
        args.get("include_deps", True),
        args.get("include_proto", True),
        args.get("search_limit", 8),
    ),
    "health_check": lambda args: health_check_tool(),
    "trace_internal": lambda args: trace_internal_tool(args["repo_name"], args.get("method", "")),
    "trace_field": lambda args: trace_field_tool(args["field"], args.get("provider", ""), args.get("mode", "trace")),
    "provider_type_map": lambda args: provider_type_map_tool(
        args["provider"], args.get("method", ""), args.get("mode", "overview")
    ),
}

class DaemonHandler(BaseHTTPRequestHandler):
    """Handle /tool/<name> and /health requests."""

    def do_GET(self) -> None:
        if self.path == "/health":
            from src.embedding_provider import (
                _embedding_provider,
                _reranker_provider,
                loaded_provider_names,
            )

            providers_ready = is_model_loaded() and is_reranker_loaded()
            emb_name = _embedding_provider.provider_name if _embedding_provider else "not initialized"
            rer_name = _reranker_provider.provider_name if _reranker_provider else "not initialized"
            # Two-tower: expose each resident embedding tower separately so an
            # operator can see whether the docs tower got loaded (it is lazy —
            # first load happens on first doc-intent query).
            emb_loaded = loaded_provider_names()
            # Report "shutting_down" if /admin/shutdown has been triggered so
            # operators can tell a drain apart from a stuck request.
            status = "shutting_down" if _shutting_down.is_set() else ("ok" if providers_ready else "ready")
            self._json_response(
                200,
                {
                    "status": status,
                    "embedding_provider": emb_name,
                    "embedding_providers_loaded": emb_loaded,
                    "reranker_provider": rer_name,
                    "uptime": time.time() - _start_time,
                    "pid": os.getpid(),
                    "inflight": _inflight_requests.get(),
                },
            )
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        # ---- /admin/unload: idempotent, reversible ----
        # Drop resident model refs + empty MPS cache. Process keeps running.
        # Next /tool/... call triggers a lazy reload via get_*_provider().
        # Safe to call repeatedly. Intended for short-term memory pressure
        # relief where a full restart is NOT wanted (e.g. a sibling job
        # about to load its own copy for a bounded task).
        if self.path == "/admin/unload":
            from src.embedding_provider import reset_providers

            reset_providers()
            with contextlib.suppress(Exception):
                import torch

                if torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            log.info("Admin: unloaded resident models (reversible; lazy reload on next tool call)")
            self._json_response(200, {"status": "unloaded", "will_exit": False})
            return

        # ---- /admin/shutdown: irreversible, drain + exit ----
        # Flip _shutting_down, reject new traffic with 503, wait briefly for
        # in-flight requests to finish, then os._exit(0) so launchd KeepAlive
        # restarts us as a fresh low-RSS process.
        if self.path == "/admin/shutdown":
            if _shutting_down.is_set():
                self._json_response(200, {"status": "already_shutting_down", "will_exit": True})
                return

            _shutting_down.set()
            with contextlib.suppress(Exception):
                from src.embedding_provider import reset_providers

                reset_providers()
            with contextlib.suppress(Exception):
                import torch

                if torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            log.info("Admin: shutdown requested; draining in-flight then exiting for launchd restart")
            self._json_response(200, {"status": "shutting_down", "will_exit": True})

            def _exit_after_drain() -> None:
                # Wait up to ~1s for in-flight tool calls to finish.
                # ThreadingHTTPServer serves each request in its own thread;
                # we don't track them explicitly, so sleep-poll the counter.
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and _inflight_requests.get() > 0:
                    time.sleep(0.02)
                os._exit(0)

            threading.Thread(target=_exit_after_drain, daemon=True).start()
            return

        # ---- Reject new traffic during shutdown drain ----
        if _shutting_down.is_set():
            self._json_response(503, {"error": "daemon shutting down"})
            return

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

        _inflight_requests.inc()
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
        finally:
            _inflight_requests.dec()

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
_FULL_CALLS_LOG = _LOG_DIR / "tool_calls_full.jsonl"
_FULL_LOG_ENABLED = os.environ.get("CODE_RAG_FULL_TOOL_LOG", "").lower() in ("1", "true", "yes", "on")

_JSONL_MAX_BYTES = int(os.environ.get("CODE_RAG_JSONL_MAX_BYTES", str(50 * 1024 * 1024)))
_JSONL_BACKUPS = 3  # keep .1/.2/.3 like RotatingFileHandler

_shutting_down = threading.Event()

class _InflightCounter:
    """Thread-safe counter for in-flight /tool/ requests (drain gate)."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def inc(self) -> None:
        with self._lock:
            self._n += 1

    def dec(self) -> None:
        with self._lock:
            self._n -= 1

    def get(self) -> int:
        with self._lock:
            return self._n

_inflight_requests = _InflightCounter()

# Serialise local writes so multiple threads in the same process can't
# interleave on the same buffer. fcntl.LOCK_EX handles cross-process.
_log_write_lock = threading.Lock()

def _append_jsonl_locked(path: Path, record: dict) -> None:
    """Append a single JSON record as one line, atomically.

    - threading lock: serialises same-process threads
    - fcntl.LOCK_EX: serialises against other daemons / scripts writing the
      same file (defensive — shouldn't happen, but cheap insurance)
    - single write() of the complete line: the kernel either writes the
      whole line or none, so concurrent readers never see half-lines
    - fsync(): flush kernel buffers so a crash right after /admin/shutdown
      doesn't lose the last record that motivated the shutdown
    - size-triggered rotation to .1/.2/.3
    """
    import fcntl

    line = json.dumps(record, ensure_ascii=False) + "\n"
    data = line.encode("utf-8")

    with _log_write_lock:
        _rotate_if_needed(path)
        with open(path, "ab") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(data)
                f.flush()
                with contextlib.suppress(OSError):
                    os.fsync(f.fileno())
            finally:
                with contextlib.suppress(Exception):
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def _rotate_if_needed(path: Path) -> None:
    """Rotate path → path.1 → path.2 → path.3, dropping the oldest.

    Best-effort; rotation failure falls through so logging never throws.
    Called with _log_write_lock held so no concurrent writer can race.
    """
    try:
        if not path.exists() or path.stat().st_size < _JSONL_MAX_BYTES:
            return
    except OSError:
        return
    try:
        # Build explicit paths: tool_calls.jsonl, tool_calls.jsonl.1, .2, .3
        # Concat via string so we don't fight pathlib.suffix for multi-dot names.
        base = str(path)
        backups = [Path(f"{base}.{i}") for i in range(1, _JSONL_BACKUPS + 1)]
        # Drop the oldest if present.
        with contextlib.suppress(OSError):
            if backups[-1].exists():
                backups[-1].unlink()
        # Shift .N-1 → .N, ..., .1 → .2
        for i in range(_JSONL_BACKUPS - 1, 0, -1):
            src = backups[i - 1]
            dst = backups[i]
            if src.exists():
                with contextlib.suppress(OSError):
                    src.replace(dst)
        # Primary → .1
        with contextlib.suppress(OSError):
            path.replace(backups[0])
    except Exception:
        pass

def _log_call(
    tool_name: str,
    args: dict,
    result: str,
    duration_ms: float,
    error: str | None = None,
    source: str = "unknown",
    session: str = "",
) -> None:
    """Append tool call record to JSONL log. Never raises.

    Writes preview (300 / 3000 chars) to tool_calls.jsonl (always).
    Writes FULL result to tool_calls_full.jsonl when CODE_RAG_FULL_TOOL_LOG=1
    (opt-in, used for blind-test audit to catch leaks past the preview cutoff).

    Concurrency: each line is emitted under a thread lock + fcntl.LOCK_EX so
    multi-threaded ThreadingHTTPServer handlers can't interleave on the same
    buffer. Size-triggered rotation prevents runaway bench runs from filling
    the disk.
    """
    try:
        from datetime import datetime

        ts = datetime.now(UTC).isoformat()
        preview_limit = 3000 if tool_name == "analyze_task" else 300
        base = {
            "ts": ts,
            "tool": tool_name,
            "args": args,
            "duration_ms": round(duration_ms),
            "result_len": len(result),
            "error": error,
            "source": source,
            "session": session,
        }
        _append_jsonl_locked(_CALLS_LOG, {**base, "result_preview": result[:preview_limit].replace("\n", " ")})

        if _FULL_LOG_ENABLED:
            _append_jsonl_locked(_FULL_CALLS_LOG, {**base, "result": result})
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
