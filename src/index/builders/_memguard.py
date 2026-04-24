"""Shared memory-guard helpers for embedding batch loops.

The full build pipeline (``scripts/build_vectors.py``,
``scripts/build_docs_vectors.py`` → ``docs_vector_indexer.py``) and the nightly
missing-vector sync (``scripts/embed_missing_vectors.py``) all embed chunks in
batches. Without mitigation each run:

- accumulates records in an in-memory list (``all_data``) until end of loop,
- keeps PyTorch MPS buffers from ``model.encode`` alive until the process exits,
- competes with the resident MCP daemon for ~1 GB of CodeRankEmbed RAM.

On a 16 GB M-series Mac this pushes RSS over 14 GB and Jetsam SIGKILLs the
embed process. The fix from commit 74c0732 (2026-04-18) landed in
``embed_missing_vectors.py`` only; the full-build scripts still leak. This
module extracts that pattern so every embed loop can apply it consistently:

- :func:`get_limits` — read env-overridable soft/hard thresholds (RSS + sys avail).
- :func:`pause_daemon` — POST /admin/shutdown so launchd restarts a fresh low-RSS
  daemon; without this the build loads a second copy of CodeRankEmbed.
- :func:`free_memory` — ``gc.collect`` + ``torch.mps.empty_cache`` (best effort).
- :func:`memory_pressure` — classify current pressure (`ok` / `soft` / `hard`).
- :func:`check_and_maybe_exit` — soft→compact+sleep, hard→``sys.exit(0)`` so the
  next run picks up from the on-disk checkpoint / rowid delta.
"""

from __future__ import annotations

import contextlib
import gc
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

_GIB = 1024**3


@dataclass(frozen=True)
class Limits:
    rss_soft_bytes: int
    rss_hard_bytes: int
    sys_avail_soft_bytes: int
    sys_avail_hard_bytes: int
    daemon_port: int


def get_limits() -> Limits:
    """Read env vars with defaults matching the 2026-04-18 baseline."""
    return Limits(
        rss_soft_bytes=int(float(os.getenv("CODE_RAG_EMBED_RSS_SOFT_GB", "8")) * _GIB),
        rss_hard_bytes=int(float(os.getenv("CODE_RAG_EMBED_RSS_HARD_GB", "10")) * _GIB),
        sys_avail_soft_bytes=int(float(os.getenv("CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB", "2")) * _GIB),
        sys_avail_hard_bytes=int(float(os.getenv("CODE_RAG_EMBED_SYS_AVAIL_HARD_GB", "0.8")) * _GIB),
        daemon_port=int(os.getenv("CODE_RAG_DAEMON_PORT", "8742")),
    )


def pause_daemon(port: int | None = None, timeout: float = 5.0) -> bool:
    """Request ``/admin/shutdown`` on the MCP daemon so launchd restarts it fresh.

    Returns True if the daemon acknowledged, False if not reachable (so the
    caller can log "no daemon to pause" and proceed).

    /admin/unload alone is NOT enough — Python's pymalloc keeps freed pages in
    arenas and RSS barely drops. Only process restart returns pages to the OS.
    """
    port = port if port is not None else get_limits().daemon_port
    url = f"http://127.0.0.1:{port}/admin/shutdown"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        print(f"  [daemon on :{port} shutdown requested; launchd will restart fresh]", flush=True)
        return True
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        if isinstance(reason, OSError) and reason.errno in {61, 111}:  # ECONNREFUSED
            return False
        print(f"  [daemon shutdown failed: {reason}; continuing without pause]", flush=True)
        return False
    except Exception as e:
        print(f"  [daemon shutdown error: {e}; continuing without pause]", flush=True)
        return False


def free_memory() -> None:
    """Best-effort release of Python + MPS buffers. Never raises."""
    gc.collect()
    with contextlib.suppress(Exception):
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()


def memory_pressure(limits: Limits | None = None) -> tuple[str, int, int]:
    """Classify current pressure. Returns (level, rss_bytes, avail_bytes).

    - ``"ok"``   — neither threshold tripped.
    - ``"soft"`` — RSS at soft OR sys avail at soft. Caller should compact +
      ``free_memory`` + re-check, and maybe sleep.
    - ``"hard"`` — RSS at hard OR sys avail at hard. Caller should ``sys.exit(0)``
      after writing any pending checkpoint.
    """
    try:
        import psutil  # deferred so test env without psutil still imports
    except ImportError:
        return "ok", 0, 0
    limits = limits or get_limits()
    rss = psutil.Process().memory_info().rss
    avail = psutil.virtual_memory().available
    if rss >= limits.rss_hard_bytes or avail <= limits.sys_avail_hard_bytes:
        return "hard", rss, avail
    if rss >= limits.rss_soft_bytes or avail <= limits.sys_avail_soft_bytes:
        return "soft", rss, avail
    return "ok", rss, avail


def check_and_maybe_exit(
    limits: Limits | None = None,
    *,
    done: int = 0,
    total: int = 0,
    compact_cb=None,
) -> str:
    """Inspect pressure; take action if needed. Returns the pressure level seen.

    - ``soft``: call ``compact_cb()`` if provided (e.g. ``table.optimize``), then
      :func:`free_memory`, re-check, and if still ``soft`` sleep 30 s so the
      kernel has a chance to release pages.
    - ``hard``: print a clean exit banner + ``sys.exit(0)`` so the caller's
      checkpoint/rowid-delta lets the next run resume.
    """
    limits = limits or get_limits()
    level, rss, avail = memory_pressure(limits)
    if level == "ok":
        return "ok"

    reason = f"rss={rss / _GIB:.1f}G avail={avail / _GIB:.1f}G"
    if level == "soft":
        if compact_cb is not None:
            compact_start = time.time()
            try:
                compact_cb()
                print(f"  [compact ok in {time.time() - compact_start:.1f}s ({reason})]", flush=True)
            except Exception as e:
                print(f"  [compact failed ({reason}): {e}]", flush=True)
        free_memory()
        level2, rss2, avail2 = memory_pressure(limits)
        reason2 = f"rss={rss2 / _GIB:.1f}G avail={avail2 / _GIB:.1f}G"
        if level2 == "hard":
            print(
                f"  [hard memory pressure after compact: {reason2}; "
                f"exiting cleanly at {done}/{total} — next run resumes from delta]",
                flush=True,
            )
            sys.exit(0)
        if level2 == "soft":
            print(f"  [{reason2} still tight after compact; sleeping 30s]", flush=True)
            time.sleep(30)
        return "soft"

    # hard
    free_memory()
    print(
        f"  [hard memory pressure: {reason}; exiting cleanly at {done}/{total} — next run resumes from delta]",
        flush=True,
    )
    sys.exit(0)
