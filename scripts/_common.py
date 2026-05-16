"""Shared boilerplate helpers for scripts in this directory.

Helpers cover ~80% of the duplicated setup across `scripts/`:

- :func:`setup_paths` — bootstrap CODE_RAG_HOME, ACTIVE_PROFILE, and sys.path
  so that ``from src.X import Y`` works. Idempotent.
- :func:`daemon_post` — POST a JSON payload to the running daemon
  (``http://localhost:8742`` by default) and parse the response. Raises
  :class:`DaemonError` on any failure.
- :func:`pause_daemon` — POST /admin/shutdown to free daemon RAM before
  heavy local work (training, embedding).
- :func:`classify_file` — categorize a file path into doc / code / test /
  ci-yml / config-yaml (used by candidate generation).
- :func:`preclean_for_fts` — strip FTS5-reserved punctuation from query text
  so MATCH doesn't raise sqlite3.OperationalError.

Stdlib-only by design. Do NOT import from ``src.*`` here — callers may rely
on this module loading before their src imports.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_HOME = Path.home() / ".code-rag-mcp"
_DEFAULT_PROFILE = "pay-com"
_DEFAULT_DAEMON = "http://localhost:8742"

_setup_done = False
_root_cache: Path | None = None


class DaemonError(RuntimeError):
    """Raised when a daemon HTTP call fails (connection, HTTP status, JSON)."""


def setup_paths() -> Path:
    """Initialize CODE_RAG_HOME / ACTIVE_PROFILE / sys.path.

    - CODE_RAG_HOME is read from env if set, else falls back to
      ``~/.code-rag-mcp``. The env var is set (via setdefault) so downstream
      code that re-reads it sees the same value.
    - ACTIVE_PROFILE defaults to ``pay-com`` (setdefault — does not override
      a pre-set value).
    - The repo root is inserted at ``sys.path[0]`` exactly once across all
      calls within a process.

    Returns the resolved Path.
    """
    global _setup_done, _root_cache

    if _setup_done and _root_cache is not None:
        return _root_cache

    env_home = os.environ.get("CODE_RAG_HOME")
    root = Path(env_home).expanduser() if env_home else _DEFAULT_HOME

    os.environ.setdefault("CODE_RAG_HOME", str(root))
    os.environ.setdefault("ACTIVE_PROFILE", _DEFAULT_PROFILE)

    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    _setup_done = True
    _root_cache = root
    return root


def daemon_post(
    endpoint: str,
    payload: dict,
    *,
    daemon_url: str = _DEFAULT_DAEMON,
    timeout: int = 120,
) -> dict:
    """POST a JSON payload to the daemon and return the parsed response.

    Raises :class:`DaemonError` for any failure (connection, HTTP status,
    invalid JSON). Use this for scripts that talk to the running MCP daemon.
    """
    url = daemon_url.rstrip("/") + endpoint
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise DaemonError(f"HTTP {e.code} from {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise DaemonError(f"Cannot reach daemon at {url}: {e}") from e
    except TimeoutError as e:
        raise DaemonError(f"Daemon timed out after {timeout}s: {url}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise DaemonError(f"Invalid JSON from {url}: {e}") from e


# ---------------------------------------------------------------------------
# FTS5 query sanitisation
# ---------------------------------------------------------------------------
_FTS_PRECLEAN_RE = re.compile(r"[^\w\s\.\-]")


def preclean_for_fts(text: str) -> str:
    """Strip FTS5-reserved punctuation so MATCH doesn't raise.

    Keeps: word chars, whitespace, ``.``, ``-`` (handled by
    :func:`src.search.fts.sanitize_fts_query`).  Strips everything else,
    including ``/`` which FTS5 treats as a token separator but often trips
    up when combined with ambiguous terms.
    """
    return _FTS_PRECLEAN_RE.sub(" ", text)


# ---------------------------------------------------------------------------
# File classification (candidate generation)
# ---------------------------------------------------------------------------
_CI_PATH_RE = re.compile(r"(?:^|/)(?:ci/deploy\.ya?ml|k8s/\.github/workflows/)", re.IGNORECASE)
_TEST_PATH_RE = re.compile(r"(?:\.spec\.(?:js|ts|tsx|jsx)$|\.test\.(?:js|ts|tsx|jsx|py)$|_test\.py$|/tests?/)")


def classify_file(file_path: str, file_type: str) -> str:
    """Categorise a file path into coarse types used by candidate builders.

    Returns one of: ``ci-yml``, ``test``, ``doc``, ``config-yaml``, ``code``.
    """
    if _CI_PATH_RE.search(file_path or ""):
        return "ci-yml"
    if _TEST_PATH_RE.search(file_path or ""):
        return "test"
    if (file_path or "").endswith(".md") or file_type in {
        "doc",
        "docs",
        "reference",
        "dictionary",
        "gotchas",
        "task",
        "provider_doc",
    }:
        return "doc"
    if (file_path or "").endswith((".yml", ".yaml")):
        return "config-yaml"
    return "code"


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------
def pause_daemon(port: int = 8742, timeout: float = 5.0) -> bool:
    """POST /admin/shutdown to free daemon RAM before heavy local work.

    Drains in-flight /tool/ requests for up to 1 s, returns 503 on new
    requests, then ``os._exit(0)``.  When launched via launchd with
    KeepAlive, the daemon restarts fresh within ~10 s.

    Returns ``True`` if the daemon acknowledged shutdown, ``False`` if it
    was already unreachable.
    """
    url = f"http://127.0.0.1:{port}/admin/shutdown"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        print(f"  [daemon on :{port} shutdown requested; launchd will restart fresh]", flush=True)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 503:
            print(f"  [daemon on :{port} already shutting down]", flush=True)
            return True
        print(f"  [daemon on :{port} HTTP {e.code} on shutdown]", flush=True)
        return False
    except Exception:
        print(f"  [daemon on :{port} not reachable — assuming down]", flush=True)
        return False
