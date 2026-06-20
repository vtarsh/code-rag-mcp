"""Shadow filesystem tools — grep/read/list over the extracted repo snapshots.

The extract step materialises a filtered source tree for every indexed repo
under EXTRACTED_DIR (default branch, last build). These tools let MCP clients
verify logic in full source files after a search() hit — without cloning the
repos locally. Everything is strictly read-only and jailed to EXTRACTED_DIR.

Snapshot semantics: content is as fresh as the last index build (see
health_check "Last build"). Feature branches and unpushed changes are not here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.config import EXTRACTED_DIR

_GREP_TIMEOUT_SEC = 30
_MAX_GREP_RESULTS = 500
_MAX_READ_LINES = 1000
_MAX_LINE_CHARS = 500
_MAX_DIR_ENTRIES = 500

_repo_sha_cache: dict[str, str] | None = None


def _repo_shas() -> dict[str, str]:
    """Lazy-load repo → short sha map from the extract index (best effort)."""
    global _repo_sha_cache
    if _repo_sha_cache is None:
        _repo_sha_cache = {}
        index_path = EXTRACTED_DIR / "_index.json"
        try:
            data = json.loads(index_path.read_text())
            _repo_sha_cache = {name: str(meta.get("sha", ""))[:9] for name, meta in data.items()}
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return _repo_sha_cache


def _resolve_jailed(rel_path: str) -> Path | None:
    """Resolve a user-supplied path inside EXTRACTED_DIR. None if it escapes."""
    candidate = (EXTRACTED_DIR / rel_path.lstrip("/")).resolve()
    try:
        candidate.relative_to(EXTRACTED_DIR.resolve())
    except ValueError:
        return None
    return candidate


def _suggest_repos(name: str) -> str:
    """Suggestion line for an unknown repo name (matches repo_overview UX)."""
    if not EXTRACTED_DIR.is_dir():
        return f"Shadow dir not found: {EXTRACTED_DIR}. Run the extract step first."
    matches = [d.name for d in EXTRACTED_DIR.iterdir() if d.is_dir() and name.lower() in d.name.lower()][:10]
    if matches:
        return f"Repo '{name}' not found in shadow. Did you mean: {', '.join(sorted(matches))}"
    return f"Repo '{name}' not found in shadow. Use list_repos or list_shadow_dir('') to see available repos."


def grep_shadow_files(
    pattern: str,
    repo: str = "",
    fixed_string: bool = True,
    case_insensitive: bool = False,
    max_lines: int = 120,
) -> list[dict]:
    """Structured literal/regex grep over the shadow — parsed rows, never raises.

    Powers search()'s recall fallback: when the semantic index whiffs on an
    exact symbol/filename, search() greps the full source here and folds the
    real file locations into its answer. Returns [{repo, path, line, text}, ...]
    (path is repo-relative, e.g. "grpc-apm-volt/methods/initialize.js"). Empty
    list on any error, missing shadow, or no match — callers must tolerate [].
    """
    if not pattern or not EXTRACTED_DIR.is_dir():
        return []

    scope = "."
    if repo:
        repo_dir = _resolve_jailed(repo)
        if repo_dir is None or not repo_dir.is_dir():
            return []
        scope = str(repo_dir.relative_to(EXTRACTED_DIR.resolve()))

    cmd = ["grep", "-rnI", "--exclude=_index.json"]
    cmd.append("-F" if fixed_string else "-E")
    if case_insensitive:
        cmd.append("-i")
    cmd.extend(["--", pattern, scope])

    try:
        proc = subprocess.run(cmd, cwd=EXTRACTED_DIR, capture_output=True, text=True, timeout=_GREP_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode > 1 or not proc.stdout:
        return []

    rows: list[dict] = []
    for ln in proc.stdout.splitlines()[: max(1, int(max_lines))]:
        ln = ln.removeprefix("./")
        parts = ln.split(":", 2)  # <repo>/<path>:<line>:<text>
        if len(parts) < 3:
            continue
        relpath, lineno, text = parts
        rows.append(
            {
                "repo": relpath.split("/", 1)[0],
                "path": relpath,
                "line": lineno,
                "text": text.strip()[:_MAX_LINE_CHARS],
            }
        )
    return rows


def grep_shadow_tool(
    pattern: str,
    repo: str = "",
    glob: str = "",
    max_results: int = 100,
    context: int = 0,
    case_insensitive: bool = False,
    fixed_string: bool = False,
) -> str:
    """Grep the shadow clones. Returns `repo/path:line:text` matches.

    Args:
        pattern: Regex (ERE) or literal string when fixed_string=True
        repo: Optional repo name to scope the search (exact dir under extracted/)
        glob: Optional filename filter, e.g. "*.js" or "initialize*"
        max_results: Cap on emitted match lines (default 100, max 500)
        context: Lines of context around each match (grep -C), 0-5
        case_insensitive: grep -i
        fixed_string: Treat pattern as a literal (grep -F)
    """
    if not pattern:
        return "Error: empty pattern."
    if not EXTRACTED_DIR.is_dir():
        return f"Shadow dir not found: {EXTRACTED_DIR}. Run the extract step first."

    scope = "."
    if repo:
        repo_dir = _resolve_jailed(repo)
        if repo_dir is None or not repo_dir.is_dir():
            return _suggest_repos(repo)
        scope = str(repo_dir.relative_to(EXTRACTED_DIR.resolve()))

    max_results = max(1, min(int(max_results), _MAX_GREP_RESULTS))
    context = max(0, min(int(context), 5))

    cmd = ["grep", "-rnI", "--exclude=_index.json"]
    cmd.append("-F" if fixed_string else "-E")
    if case_insensitive:
        cmd.append("-i")
    if context:
        cmd.append(f"-C{context}")
    if glob:
        cmd.append(f"--include={glob}")
    cmd.extend(["--", pattern, scope])

    try:
        proc = subprocess.run(
            cmd,
            cwd=EXTRACTED_DIR,
            capture_output=True,
            text=True,
            timeout=_GREP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return f"grep timed out after {_GREP_TIMEOUT_SEC}s — narrow the scope with repo= or glob=."

    if proc.returncode == 1 and not proc.stdout:
        where = repo or "all repos"
        return f"No matches for '{pattern}' in {where} (shadow snapshot)."
    if proc.returncode > 1:
        return f"grep error: {proc.stderr.strip() or f'exit {proc.returncode}'}"

    lines = proc.stdout.splitlines()
    total = len(lines)
    shown = [ln if len(ln) <= _MAX_LINE_CHARS else ln[:_MAX_LINE_CHARS] + "…" for ln in lines[:max_results]]
    cleaned = [ln.removeprefix("./") for ln in shown]

    header = f"{total} match line(s) for '{pattern}' in {repo or 'all repos'} (shadow snapshot)"
    if total > max_results:
        header += f" — showing first {max_results}, raise max_results (≤{_MAX_GREP_RESULTS}) or narrow scope"
    return header + "\n\n" + "\n".join(cleaned)


def read_shadow_file_tool(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a file from the shadow clone with line numbers.

    Args:
        path: Repo-relative path, e.g. "grpc-apm-volt/methods/initialize.js"
        offset: 1-based first line to return (default 1)
        limit: Max lines to return (default 200, max 1000)
    """
    if not path:
        return "Error: empty path. Expected '<repo>/<file>' relative to the shadow root."

    target = _resolve_jailed(path)
    if target is None:
        return "Error: path escapes the shadow root."
    if target.is_dir():
        return f"'{path}' is a directory — use list_shadow_dir instead."
    if not target.is_file():
        repo = path.split("/", 1)[0]
        repo_dir = _resolve_jailed(repo)
        if repo_dir is None or not repo_dir.is_dir():
            return _suggest_repos(repo)
        return f"File not found in shadow: {path}. Check the path with list_shadow_dir or grep_shadow."

    offset = max(1, int(offset))
    limit = max(1, min(int(limit), _MAX_READ_LINES))

    try:
        text = target.read_text(errors="replace")
    except OSError as e:
        return f"Error reading {path}: {e}"

    lines = text.splitlines()
    total = len(lines)
    if offset > total:
        return f"{path} has only {total} line(s), offset {offset} is past EOF."

    window = lines[offset - 1 : offset - 1 + limit]
    numbered = [
        f"{offset + i:>6}\t" + (ln if len(ln) <= _MAX_LINE_CHARS else ln[:_MAX_LINE_CHARS] + "…")
        for i, ln in enumerate(window)
    ]

    repo = path.split("/", 1)[0]
    sha = _repo_shas().get(repo, "")
    header = (
        f"{path} (shadow snapshot{f' @ {sha}' if sha else ''}, lines {offset}-{offset + len(window) - 1} of {total})"
    )
    return header + "\n" + "\n".join(numbered)


def list_shadow_dir_tool(path: str = "") -> str:
    """List a directory inside the shadow clone (dirs first, then files with sizes).

    Args:
        path: Repo-relative dir, e.g. "grpc-apm-volt/methods". Empty = repo list.
    """
    target = _resolve_jailed(path) if path else EXTRACTED_DIR
    if target is None:
        return "Error: path escapes the shadow root."
    if target.is_file():
        return f"'{path}' is a file — use read_shadow_file instead."
    if not target.is_dir():
        repo = path.split("/", 1)[0]
        return _suggest_repos(repo) if repo else f"Shadow dir not found: {EXTRACTED_DIR}."

    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    shown = entries[:_MAX_DIR_ENTRIES]
    out = []
    for entry in shown:
        if entry.name == "_index.json":
            continue
        if entry.is_dir():
            out.append(f"{entry.name}/")
        else:
            out.append(f"{entry.name}  {entry.stat().st_size}")
    header = f"{path or '<shadow root>'} — {len(entries)} entr(ies)"
    if len(entries) > _MAX_DIR_ENTRIES:
        header += f", showing first {_MAX_DIR_ENTRIES}"
    return header + "\n" + "\n".join(out)
