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

- :func:`configure_mps_limits` — cap the PyTorch MPS allocator *before* torch
  initialises it, so the embed loop can't balloon to ~12 GB of graphics memory.
- :func:`get_limits` — read env-overridable soft/hard thresholds (RSS + sys avail + MPS).
- :func:`pause_daemon` — POST /admin/shutdown so launchd restarts a fresh low-RSS
  daemon; without this the build loads a second copy of CodeRankEmbed.
- :func:`free_memory` — ``gc.collect`` + ``torch.mps.empty_cache`` (best effort).
- :func:`mps_allocated_bytes` — MPS/Metal memory the RSS checks are blind to.
- :func:`memory_pressure` — classify current pressure (`ok` / `soft` / `hard`).
- :func:`check_and_maybe_exit` — soft→compact+sleep, hard→``sys.exit(0)`` so the
  next run picks up from the on-disk checkpoint / rowid delta.

Why MPS matters: on Apple Silicon the embedding runs on MPS, whose buffers live
in *unified* memory but show up as the process's **graphics** physical footprint,
NOT its RSS. With PyTorch's default high-water-mark ratio (1.7) the allocator is
allowed to grow to ~1.7× the device recommendedMax (~20 GB on a 16 GB Mac); a run
routinely sat at ~12 GB of graphics memory while RSS read ~8 MB, so every prior
RSS-based guard stayed silent and the Mac swapped. :func:`configure_mps_limits`
bounds the ceiling and :func:`memory_pressure` now actually sees MPS.
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
    mps_soft_bytes: int
    mps_hard_bytes: int
    daemon_port: int


def get_limits() -> Limits:
    """Read env vars with defaults matching the 2026-04-18 baseline.

    MPS defaults (7/9 GB) sit just above the ~5.9 GB ceiling that
    :func:`configure_mps_limits` imposes, so they never false-trip a capped run
    but still catch a balloon if the cap is ever raised or removed.
    """
    return Limits(
        rss_soft_bytes=int(float(os.getenv("CODE_RAG_EMBED_RSS_SOFT_GB", "8")) * _GIB),
        rss_hard_bytes=int(float(os.getenv("CODE_RAG_EMBED_RSS_HARD_GB", "10")) * _GIB),
        sys_avail_soft_bytes=int(float(os.getenv("CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB", "2")) * _GIB),
        sys_avail_hard_bytes=int(float(os.getenv("CODE_RAG_EMBED_SYS_AVAIL_HARD_GB", "0.8")) * _GIB),
        mps_soft_bytes=int(float(os.getenv("CODE_RAG_EMBED_MPS_SOFT_GB", "7")) * _GIB),
        mps_hard_bytes=int(float(os.getenv("CODE_RAG_EMBED_MPS_HARD_GB", "9")) * _GIB),
        daemon_port=int(os.getenv("CODE_RAG_DAEMON_PORT", "8742")),
    )


def configure_mps_limits() -> None:
    """Cap the PyTorch MPS allocator BEFORE the first ``import torch``.

    PyTorch's default ``PYTORCH_MPS_HIGH_WATERMARK_RATIO`` of 1.7 lets the MPS
    allocator grow to ~1.7× the device's recommendedMaxWorkingSetSize — ~20 GB on
    a 16 GB Mac. The embed loop then balloons to ~12 GB of *graphics* memory that
    never appears in RSS, so the RSS-based guard stays silent while the machine
    swaps. Capping the ratio bounds that ceiling (~5.9 GB at 0.5 on a 16 GB Mac).

    Idempotent and safe to call repeatedly: an env value already set (e.g. by the
    launchd plist or ``full_update.sh``) always wins via ``setdefault``. Override
    the cap with ``CODE_RAG_MPS_HIGH_WATERMARK`` / ``CODE_RAG_MPS_LOW_WATERMARK``.

    MUST run before any ``import torch`` that touches MPS — torch reads these env
    vars once, at allocator init. All embed entry points call it first thing.
    """
    os.environ.setdefault(
        "PYTORCH_MPS_HIGH_WATERMARK_RATIO", os.getenv("CODE_RAG_MPS_HIGH_WATERMARK", "0.5")
    )
    # Low watermark deliberately aggressive (0.2 ≈ 2.4 GB on a 16 GB Mac) so the
    # allocator releases cached buffers between batches instead of squatting on
    # ~5 GB, leaving headroom for the next encode spike under the high cap.
    os.environ.setdefault(
        "PYTORCH_MPS_LOW_WATERMARK_RATIO", os.getenv("CODE_RAG_MPS_LOW_WATERMARK", "0.2")
    )


def mps_allocated_bytes() -> int:
    """MPS/Metal memory allocated by this process (incl. cache), or 0.

    ``driver_allocated_memory`` is what shows up as the process's *graphics*
    physical footprint — the number every RSS-based check is blind to. Never
    raises (no torch / no MPS / old torch → 0).
    """
    try:
        import torch

        if torch.backends.mps.is_available():
            return int(torch.mps.driver_allocated_memory())
    except Exception:
        pass
    return 0


def journal(event: str, **fields) -> None:
    """Append one diagnostic line to ``$CODE_RAG_HOME/logs/mem_journal.log``.

    A durable, append-only resource journal so the next "Mac thrashes during
    rebuild" investigation is one ``tail mem_journal.log`` away instead of a
    30-session rediscovery. Survives the ``| tail -N`` truncation that drops most
    per-batch lines from the cron stdout log. Self-trims; never raises.
    """
    try:
        from datetime import datetime, timezone

        base = os.environ.get("CODE_RAG_HOME") or os.path.expanduser("~/.code-rag")
        log_dir = os.path.join(base, "logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "mem_journal.log")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        kv = " ".join(f"{k}={v}" for k, v in fields.items())
        with contextlib.suppress(Exception):
            if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
                with open(path, encoding="utf-8") as f:
                    tail = f.readlines()[-4000:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(tail)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {event} {kv}\n")
    except Exception:
        pass


def cap_seq_length(model, cap: int | None = None) -> int:
    """Clamp the encoder's ``max_seq_length`` so one long chunk can't blow up the
    O(seq²) attention matrix (and the MPS buffer behind it).

    CodeRankEmbed ships ``max_seq_length=8192``; a single batch of long chunks at
    that length asks the attention matmul for >18 GB — which silently balloons
    MPS (default high-water-mark 1.7 → ~20 GB ceiling) and thrashes the Mac. The
    embed paths truncate text to ≤ ``long_limit`` (~8000 chars ≈ 3200 tokens), so
    the default 4096 cap never truncates real content — it's a pure safety
    ceiling that quarters the worst-case attention vs 8192. Lower it via
    ``CODE_RAG_EMBED_MAX_SEQ`` if you still see pressure. Never raises an existing
    smaller limit. Returns the effective cap.
    """
    if cap is None:
        cap = int(os.getenv("CODE_RAG_EMBED_MAX_SEQ", "4096"))
    current = getattr(model, "max_seq_length", None)
    if isinstance(current, int) and current <= cap:
        return current
    with contextlib.suppress(Exception):
        model.max_seq_length = cap
    for module in getattr(model, "_modules", {}).values():
        if hasattr(module, "max_seq_length"):
            with contextlib.suppress(Exception):
                if getattr(module, "max_seq_length", cap + 1) > cap:
                    module.max_seq_length = cap
    return cap


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
    # MPS lives in unified RAM but is invisible to RSS — fold it in so the guard
    # finally fires on the graphics-memory balloon that caused the swap thrash.
    mps = mps_allocated_bytes()
    if rss >= limits.rss_hard_bytes or avail <= limits.sys_avail_hard_bytes or mps >= limits.mps_hard_bytes:
        return "hard", rss, avail
    if rss >= limits.rss_soft_bytes or avail <= limits.sys_avail_soft_bytes or mps >= limits.mps_soft_bytes:
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

    reason = f"rss={rss / _GIB:.1f}G mps={mps_allocated_bytes() / _GIB:.1f}G avail={avail / _GIB:.1f}G"
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
        reason2 = f"rss={rss2 / _GIB:.1f}G mps={mps_allocated_bytes() / _GIB:.1f}G avail={avail2 / _GIB:.1f}G"
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
