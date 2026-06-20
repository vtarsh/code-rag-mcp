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
from src.tools.shadow_fs import grep_shadow_tool, list_shadow_dir_tool, read_shadow_file_tool
from src.tools.shadow_types import provider_type_map_tool

PORT = int(os.environ.get("CODE_RAG_PORT", os.environ.get("PAY_KNOWLEDGE_PORT", "8742")))
# Bind address. Default loopback. To serve teammates over a VPN, set either an
# explicit interface IP or a CIDR like CODE_RAG_BIND=172.31.1.0/27 — the daemon
# resolves its own address inside that subnet at startup (VPN IPs change between
# connects). With a CIDR and no matching interface (VPN down) it falls back to
# loopback. A non-loopback bind always keeps a second loopback listener so the
# owner's local proxy is unaffected. Set CODE_RAG_TOKEN for remote callers.
# Env wins; falls back to the .bind / .secrets/team_token files so the daemon
# binds correctly no matter WHO starts it (manual run, launchd, or an MCP proxy
# auto-start whose own env predates the bind config).
_BIND_FILE = Path(__file__).parent / ".bind"
BIND = os.environ.get("CODE_RAG_BIND") or (_BIND_FILE.read_text().strip() if _BIND_FILE.exists() else "127.0.0.1")
# Optional shared secret: when set, every non-loopback /tool/ and /admin/
# request must carry the X-Auth-Token header with this value.
_TOKEN_FILE = Path(__file__).parent / ".secrets" / "team_token"
AUTH_TOKEN = os.environ.get("CODE_RAG_TOKEN") or (_TOKEN_FILE.read_text().strip() if _TOKEN_FILE.exists() else "")
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
    "grep_shadow": lambda args: grep_shadow_tool(
        args["pattern"],
        args.get("repo", ""),
        args.get("glob", ""),
        args.get("max_results", 100),
        args.get("context", 0),
        args.get("case_insensitive", False),
        args.get("fixed_string", False),
    ),
    "read_shadow_file": lambda args: read_shadow_file_tool(args["path"], args.get("offset", 1), args.get("limit", 200)),
    "list_shadow_dir": lambda args: list_shadow_dir_tool(args.get("path", "")),
}


class DaemonHandler(BaseHTTPRequestHandler):
    """Handle /tool/<name> and /health requests."""

    def do_GET(self) -> None:
        # ---- /client: serve the thin MCP proxy script for teammate installs ----
        # mcp_server.py is dual-mode: with CODE_RAG_HOST set it acts as a pure
        # remote client (never starts a local daemon), so the same file IS the
        # teammate client. One curl replaces "clone the repo".
        if self.path == "/client":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized: missing or wrong X-Auth-Token"})
                return
            script = Path(__file__).parent / "mcp_server.py"
            body = script.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/x-python")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

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

    def _authorized(self) -> bool:
        """Check the shared-secret header when CODE_RAG_TOKEN is set.

        Loopback clients always pass — the token only gates remote (VPN)
        callers, so the owner's local setup needs no extra config.
        """
        if not AUTH_TOKEN:
            return True
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        return self.headers.get("X-Auth-Token", "") == AUTH_TOKEN

    def do_POST(self) -> None:
        if not self._authorized():
            self._json_response(401, {"error": "unauthorized: missing or wrong X-Auth-Token"})
            return

        # ---- /admin/unload: idempotent, reversible ----
        # Drop resident model refs + empty MPS cache. Process keeps running.
        # Next /tool/... call triggers a lazy reload via get_*_provider().
        # Safe to call repeatedly. Intended for short-term memory pressure
        # relief where a full restart is NOT wanted (e.g. a sibling job
        # about to load its own copy for a bounded task).
        if self.path == "/admin/unload":
            before = _mem_snapshot()
            from src.embedding_provider import reset_providers

            reset_providers()
            with contextlib.suppress(Exception):
                import torch

                if torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            after = _mem_snapshot()
            log.info(
                "Admin: unloaded resident models (reversible; lazy reload on next tool call) "
                f"(footprint {before['footprint_mb']}→{after['footprint_mb']}MB, "
                f"rss {before['rss_mb']}→{after['rss_mb']}MB, "
                f"mps_drv {before['mps_drv_mb']}→{after['mps_drv_mb']}MB)"
            )
            _journal_unload("admin_unload", before, after)
            self._json_response(
                200, {"status": "unloaded", "will_exit": False, "mem_before": before, "mem_after": after}
            )
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

        t0 = time.time()
        mem_before = _mem_snapshot()
        _inflight_requests.inc()
        # Race guard vs the idle-restart watchdog: it sets _shutting_down THEN
        # re-reads inflight. By incrementing FIRST and re-checking the gate here,
        # we form a handshake that makes the two mutually exclusive — either the
        # watchdog sees our inflight and defers the restart, or it already set the
        # gate and we bail with a clean, retryable 503 instead of being killed
        # mid-call by os._exit(). (The earlier gate check is the fast path; this
        # is the one that closes the window between it and inc().)
        if _shutting_down.is_set():
            _inflight_requests.dec()
            self._json_response(503, {"error": "daemon restarting, please retry"})
            return
        try:
            try:
                result = TOOLS[tool_name](args)
            except KeyError as ke:
                # A tool dispatch lambda did args["X"] for a missing required
                # arg. Return a clean 400 with the arg name instead of an
                # opaque HTTP 500 (which looked like a server crash to callers).
                # Full traceback still logged so a genuine *internal* KeyError
                # stays debuggable rather than silently downgraded.
                duration_ms = (time.time() - t0) * 1000
                missing = str(ke).strip("'\"")
                log.warning(f"tool={tool_name} KeyError ({missing}):\n{traceback.format_exc()}")
                _log_call(
                    tool_name,
                    args,
                    "",
                    duration_ms,
                    error=f"missing arg: {missing}",
                    source=source,
                    session=session_id,
                    mem_before=mem_before,
                )
                self._json_response(400, {"error": f"missing required argument: {missing}"})
                return
            duration_ms = (time.time() - t0) * 1000
            log.info(f"tool={tool_name} source={source} duration={duration_ms:.0f}ms")
            _log_call(tool_name, args, result, duration_ms, source=source, session=session_id, mem_before=mem_before)
            self._json_response(200, {"result": result})
        except Exception as e:
            duration_ms = (time.time() - t0) * 1000
            log.error(f"tool={tool_name} error: {traceback.format_exc()}")
            _log_call(
                tool_name,
                args,
                str(e),
                duration_ms,
                error=str(e),
                source=source,
                session=session_id,
                mem_before=mem_before,
            )
            self._json_response(500, {"error": str(e)})
        finally:
            _inflight_requests.dec()
            _touch_activity()

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

_MIB = 1024 * 1024


def _mem_snapshot() -> dict:
    """Cheap in-process memory snapshot for the tool-call / unload journals.

    Three numbers separate "how much it *consumes*" from "how much it *needs*" —
    the exact confusion behind a 3.9 GB Activity-Monitor "Memory" reading while
    real RAM (RSS) sat at 45 MB:

    - ``rss_mb``       — resident RAM (psutil); pages the OS has in physical RAM
                         now. Drops to tens of MB when idle pages swap/compress.
    - ``footprint_mb`` — phys_footprint: the EXACT number macOS Activity Monitor
                         shows in its "Memory" column (folds in compressed/swapped
                         dirty pages + owned graphics). The user's mental model.
    - ``mps_drv_mb``   — MPS driver pool this process *reserves* (unified-memory
                         graphics footprint). The bulk of the footprint for us.
    - ``mps_cur_mb``   — MPS memory backing *live* tensors; the genuine working set.

    ``mps_drv_mb - mps_cur_mb`` is reclaimable cache (what ``empty_cache`` returns).
    Never raises — any probe that fails degrades to -1 for that field so the
    journal line is still written.
    """
    snap = {
        "rss_mb": -1,
        "footprint_mb": -1,
        "mps_drv_mb": -1,
        "mps_cur_mb": -1,
        "cpu_pct": -1,
        "swap_mb": -1,
        "models": is_model_loaded(),
        "rerank": is_reranker_loaded(),
    }
    with contextlib.suppress(Exception):
        import psutil

        snap["rss_mb"] = round(psutil.Process().memory_info().rss / _MIB)
    with contextlib.suppress(Exception):
        from src.index.builders._memguard import phys_footprint_bytes

        fp = phys_footprint_bytes()
        if fp > 0:
            snap["footprint_mb"] = round(fp / _MIB)
    with contextlib.suppress(Exception):
        from src.index.builders._memguard import mps_allocated_bytes, mps_current_bytes

        snap["mps_drv_mb"] = round(mps_allocated_bytes() / _MIB)
        snap["mps_cur_mb"] = round(mps_current_bytes() / _MIB)
    # System-wide stress: cpu% (non-blocking, since last snapshot) + swap used.
    # The before/after pair brackets the tool, so mem_after.cpu_pct ≈ load during
    # the call; swap_mb is the thrash signal (machine, not just this process).
    with contextlib.suppress(Exception):
        from src.index.builders._memguard import swap_used_bytes, system_cpu_percent

        snap["cpu_pct"] = round(system_cpu_percent(), 1)
        snap["swap_mb"] = round(swap_used_bytes() / _MIB)
    return snap


def _journal_unload(event: str, before: dict, after: dict, **extra) -> None:
    """Journal a model-unload reclaim (before/after + delta). Never raises.

    Answers the core question behind a high idle footprint: does
    ``reset_providers() + empty_cache()`` actually hand the MPS driver pool back?
    ``drv_freed_mb`` is the reserved graphics memory reclaimed; if it stays ~0
    while ``mps_drv_mb`` is large, the pool is stuck and only a process restart
    (``/admin/shutdown``) will return it.
    """
    with contextlib.suppress(Exception):
        from src.index.builders._memguard import journal

        journal(
            event,
            rss_before=before.get("rss_mb"),
            rss_after=after.get("rss_mb"),
            footprint_before=before.get("footprint_mb"),
            footprint_after=after.get("footprint_mb"),
            footprint_freed_mb=before.get("footprint_mb", 0) - after.get("footprint_mb", 0),
            mps_drv_before=before.get("mps_drv_mb"),
            mps_drv_after=after.get("mps_drv_mb"),
            mps_cur_before=before.get("mps_cur_mb"),
            mps_cur_after=after.get("mps_cur_mb"),
            drv_freed_mb=before.get("mps_drv_mb", 0) - after.get("mps_drv_mb", 0),
            **extra,
        )


# Idle auto-unload: drop resident ML models after N seconds of no /tool/ traffic.
# Reload is lazy (next /tool/ call triggers _ensure_model in embedding_provider).
# CODE_RAG_IDLE_UNLOAD_SEC=0 disables the unload tier.
_IDLE_UNLOAD_SEC = int(os.environ.get("CODE_RAG_IDLE_UNLOAD_SEC", "1800"))

# Idle auto-RESTART (tier 2): after a longer idle, exit the process so launchd
# (KeepAlive=true) respawns a fresh ~45 MB daemon. This is the ONLY thing that
# returns the MPS allocator pool — measured 2026-06-19: unload + empty_cache
# reclaims only ~1/3, ~2 GB of driver-reserved graphics memory stays stuck for
# the process lifetime (PyTorch MPS keeps it; watermark caps + empty_cache do
# not help while warm). NOT per-search churn: fires only once the daemon has
# gone quiet for the full window AND is still holding a stuck pool worth the
# cold-start cost. Default 0 = OFF (opt-in; assumes launchd KeepAlive — a
# manually-run daemon would NOT respawn). Set e.g. CODE_RAG_IDLE_RESTART_SEC=3600.
_IDLE_RESTART_SEC = int(os.environ.get("CODE_RAG_IDLE_RESTART_SEC", "0"))
# Don't restart a fresh/idle process that never accumulated a pool — only when the
# driver-reserved MPS pool is at least this large (MB). Skips pointless churn.
_IDLE_RESTART_MIN_MPS_MB = int(os.environ.get("CODE_RAG_IDLE_RESTART_MIN_MPS_MB", "512"))

_last_activity_ts = time.time()
_activity_lock = threading.Lock()


def _touch_activity() -> None:
    global _last_activity_ts
    with _activity_lock:
        _last_activity_ts = time.time()


def _idle_decision(idle_s: float, inflight: int, models_loaded: bool, held_mps_mb: int) -> str:
    """Pure policy: what should the idle watchdog do right now?

    Returns ``"restart"`` | ``"unload"`` | ``"none"``. Extracted from the loop so
    the two-tier thresholds are unit-testable without sleeping. Restart wins over
    unload (it reclaims strictly more — the stuck pool unload can't free), but
    only when a real pool is held (``held_mps_mb``) so a fresh idle process isn't
    churned. ``held_mps_mb`` of -1 (probe failed) never triggers a restart.
    """
    if inflight > 0:
        return "none"
    if _IDLE_RESTART_SEC > 0 and idle_s >= _IDLE_RESTART_SEC and held_mps_mb >= _IDLE_RESTART_MIN_MPS_MB:
        return "restart"
    if _IDLE_UNLOAD_SEC > 0 and idle_s >= _IDLE_UNLOAD_SEC and models_loaded:
        return "unload"
    return "none"


def _idle_restart(idle_s: float, held_mps_mb: int, footprint_mb: int = -1) -> bool:
    """Exit the process so launchd (KeepAlive) respawns a fresh low-RSS daemon.

    Returns False (does not exit) if a request slipped in — the caller keeps
    watching and retries next tick. On success it never returns (``os._exit``).

    Conflict-free handshake with the request handler: we set ``_shutting_down``
    FIRST, then re-read inflight. The handler increments inflight FIRST, then
    re-checks the gate. This ordering makes "a request runs" and "we exit"
    mutually exclusive — so we never ``os._exit`` on top of an in-flight tool
    call. If a request did arrive in the window, we clear the gate and abort the
    restart (the stuck pool waits for the next idle window — correctness over
    eager reclaim).
    """
    _shutting_down.set()
    if _inflight_requests.get() > 0:
        _shutting_down.clear()
        log.info("Idle watchdog: restart deferred — a request arrived during the idle window; will retry")
        return False
    log.info(
        f"Idle watchdog: full restart after {idle_s:.0f}s idle — returning "
        f"{held_mps_mb}MB driver-reserved MPS (footprint {footprint_mb}MB) that "
        f"unload/empty_cache can't free; launchd KeepAlive will respawn a fresh process"
    )
    with contextlib.suppress(Exception):
        from src.index.builders._memguard import journal

        journal("idle_restart", idle_s=round(idle_s), mps_drv_mb=held_mps_mb, footprint_mb=footprint_mb)
    os._exit(0)


def _idle_watchdog() -> None:
    intervals = [t for t in (_IDLE_UNLOAD_SEC, _IDLE_RESTART_SEC) if t > 0]
    if not intervals:
        return
    check_every = max(30, min(intervals) // 4)
    while not _shutting_down.is_set():
        time.sleep(check_every)
        if _shutting_down.is_set():
            return
        with _activity_lock:
            idle = time.time() - _last_activity_ts
        snap = _mem_snapshot()
        action = _idle_decision(
            idle,
            _inflight_requests.get(),
            bool(snap["models"] or snap["rerank"]),
            snap["mps_drv_mb"],
        )
        if action == "restart":
            # Returns only if deferred (a request raced in); os._exit otherwise.
            # Keep looping so a deferred restart fires on a later quiet tick.
            _idle_restart(idle, snap["mps_drv_mb"], snap["footprint_mb"])
            continue
        if action != "unload":
            continue
        try:
            from src.embedding_provider import reset_providers

            reset_providers()
            with contextlib.suppress(Exception):
                import torch

                if torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            after = _mem_snapshot()
            log.info(
                f"Idle watchdog: unloaded resident models after {idle:.0f}s of inactivity "
                f"(rss {snap['rss_mb']}→{after['rss_mb']}MB, "
                f"mps_drv {snap['mps_drv_mb']}→{after['mps_drv_mb']}MB)"
            )
            _journal_unload("idle_unload", snap, after, idle_s=round(idle))
        except Exception as e:
            log.warning(f"Idle watchdog: unload failed: {e}")


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
    mem_before: dict | None = None,
) -> None:
    """Append tool call record to JSONL log. Never raises.

    Writes preview (300 / 3000 chars) to tool_calls.jsonl (always).
    Writes FULL result to tool_calls_full.jsonl when CODE_RAG_FULL_TOOL_LOG=1
    (opt-in, used for blind-test audit to catch leaks past the preview cutoff).

    Memory: each record carries ``mem_before``/``mem_after`` snapshots (RSS +
    MPS driver-pool + MPS live) so a future "why does this process reserve N GB"
    question is answerable from the call log — which tool grew the footprint, by
    how much, and whether it was reserved cache vs live tensors. A compact line
    also goes to ``logs/mem_journal.log`` for ``tail``-friendly triage alongside
    the embed/unload events already journalled there.

    Concurrency: each line is emitted under a thread lock + fcntl.LOCK_EX so
    multi-threaded ThreadingHTTPServer handlers can't interleave on the same
    buffer. Size-triggered rotation prevents runaway bench runs from filling
    the disk.
    """
    mem_after = _mem_snapshot()
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
            "mem_before": mem_before,
            "mem_after": mem_after,
        }
        _append_jsonl_locked(_CALLS_LOG, {**base, "result_preview": result[:preview_limit].replace("\n", " ")})

        if _FULL_LOG_ENABLED:
            _append_jsonl_locked(_FULL_CALLS_LOG, {**base, "result": result})
    except Exception:
        pass

    # Compact, greppable resource line next to the embed/unload journal so the
    # next memory investigation is one `tail mem_journal.log` away.
    with contextlib.suppress(Exception):
        from src.index.builders._memguard import journal

        journal(
            "tool",
            name=tool_name,
            dur_ms=round(duration_ms),
            rss_mb=mem_after.get("rss_mb"),
            footprint_mb=mem_after.get("footprint_mb"),
            mps_drv_mb=mem_after.get("mps_drv_mb"),
            mps_cur_mb=mem_after.get("mps_cur_mb"),
            models=int(bool(mem_after.get("models"))),
            err=1 if error else 0,
        )


def _resolve_bind(spec: str) -> str:
    """Resolve a bind spec to a concrete IP. CIDR → local interface IP in that subnet."""
    if "/" not in spec:
        return spec
    import ipaddress
    import re
    import subprocess

    ifconfig = "/sbin/ifconfig" if Path("/sbin/ifconfig").exists() else "ifconfig"
    try:
        network = ipaddress.ip_network(spec, strict=False)
        out = subprocess.run([ifconfig], capture_output=True, text=True, timeout=5).stdout
        for ip_str in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out):
            ip = ipaddress.ip_address(ip_str)
            if ip in network and not ip.is_loopback:
                return ip_str
    except (ValueError, OSError, subprocess.TimeoutExpired) as e:
        log.warning(f"Bind spec '{spec}' resolution failed: {e}")
    log.warning(f"No local interface in {spec} (VPN down?) — falling back to loopback")
    return "127.0.0.1"


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

    if _IDLE_UNLOAD_SEC > 0 or _IDLE_RESTART_SEC > 0:
        threading.Thread(target=_idle_watchdog, daemon=True, name="idle-watchdog").start()
        restart_note = (
            f" + full restart after {_IDLE_RESTART_SEC}s (>{_IDLE_RESTART_MIN_MPS_MB}MB MPS held)"
            if _IDLE_RESTART_SEC > 0
            else ""
        )
        log.info(f"Idle watchdog started: unload after {_IDLE_UNLOAD_SEC}s of inactivity{restart_note}")

    bind_ip = _resolve_bind(BIND)
    if bind_ip not in ("127.0.0.1", "localhost") and not AUTH_TOKEN:
        log.warning(f"Binding to {bind_ip} WITHOUT CODE_RAG_TOKEN — anyone on the network can call tools/admin")

    server = ThreadingHTTPServer((bind_ip, PORT), DaemonHandler)
    if bind_ip not in ("127.0.0.1", "localhost"):
        # Keep a loopback listener alongside the VPN one for the owner's local proxy.
        loopback = ThreadingHTTPServer(("127.0.0.1", PORT), DaemonHandler)
        threading.Thread(target=loopback.serve_forever, daemon=True, name="loopback-listener").start()
        log.info(f"Loopback listener ready at http://127.0.0.1:{PORT}")
    try:
        log.info(f"Daemon ready at http://{bind_ip}:{PORT}")
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        server.server_close()
        cleanup_pid()


if __name__ == "__main__":
    main()
