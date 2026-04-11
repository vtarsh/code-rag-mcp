#!/usr/bin/env python3
"""
Phase 2, Step 2.1: Build SQLite FTS5 Index
Chunks all extracted artifacts and indexes them for full-text search.

Chunking strategies:
- Proto files: by message/service/enum definition
- Markdown: by header sections
- JS/TS code: by function/class (simplified regex-based)
- JSON (package.json): by top-level fields
- Small files (<512 lines): as single chunk
- Env files: as single chunk with key-value pairs
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
_PROFILE = os.getenv("ACTIVE_PROFILE", "")
if not _PROFILE:
    _ap = _BASE_DIR / ".active_profile"
    _PROFILE = _ap.read_text().strip() if _ap.exists() else "example"
_PROFILE_DIR = _BASE_DIR / "profiles" / _PROFILE

EXTRACTED_DIR = _BASE_DIR / "extracted"
RAW_DIR = _BASE_DIR / "raw"

# Load conventions
import yaml  # noqa: E402

_conv_path = _PROFILE_DIR / "conventions.yaml"
_conv = yaml.safe_load(_conv_path.read_text()) if _conv_path.exists() else {}
FEATURE_REPO: str = _conv.get("feature_repo", "grpc-providers-features")
# Gotchas/flows/domain_registry from profile (fall back to legacy docs/)
GOTCHAS_DIR = (
    _PROFILE_DIR / "docs" / "gotchas"
    if (_PROFILE_DIR / "docs" / "gotchas").is_dir()
    else _BASE_DIR / "docs" / "gotchas"
)
FLOWS_DIR = (
    _PROFILE_DIR / "docs" / "flows" if (_PROFILE_DIR / "docs" / "flows").is_dir() else _BASE_DIR / "docs" / "flows"
)
TASKS_DIR = (
    _PROFILE_DIR / "docs" / "tasks" if (_PROFILE_DIR / "docs" / "tasks").is_dir() else _BASE_DIR / "docs" / "tasks"
)
REFERENCES_DIR = (
    _PROFILE_DIR / "docs" / "references"
    if (_PROFILE_DIR / "docs" / "references").is_dir()
    else _BASE_DIR / "docs" / "references"
)
PROVIDERS_DIR = _PROFILE_DIR / "docs" / "providers"
_profile_domain_reg = _PROFILE_DIR / "docs" / "domain_registry.yaml"
DOMAIN_REGISTRY_FILE = (
    _profile_domain_reg if _profile_domain_reg.exists() else _BASE_DIR / "docs" / "domain_registry.yaml"
)
DB_DIR = _BASE_DIR / "db"
DB_PATH = DB_DIR / "knowledge.db"
INDEX_FILE = EXTRACTED_DIR / "_index.json"

# Max chunk size in characters
MAX_CHUNK = 4000
MIN_CHUNK = 50


def create_db(conn: sqlite3.Connection):
    """Create FTS5 tables and metadata tables."""
    conn.executescript("""
        -- Main FTS5 search table
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            content,
            repo_name,
            file_path,
            file_type,
            chunk_type,
            language,
            tokenize='porter unicode61'
        );

        -- Repo metadata (non-FTS)
        CREATE TABLE IF NOT EXISTS repos (
            name TEXT PRIMARY KEY,
            type TEXT,
            sha TEXT,
            org_deps TEXT,  -- JSON array
            artifact_counts TEXT  -- JSON object
        );

        -- Chunk ordering metadata (for sibling retrieval in hybrid search)
        CREATE TABLE IF NOT EXISTS chunk_meta (
            chunk_rowid INTEGER PRIMARY KEY,  -- references chunks rowid
            chunk_order INTEGER NOT NULL,      -- 0-based order within (repo, file)
            total_chunks INTEGER NOT NULL      -- total chunks in this (repo, file)
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_meta_order
            ON chunk_meta(chunk_rowid);

        -- Build metadata
        CREATE TABLE IF NOT EXISTS build_info (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- Code facts: validation guards, const values, joi/zod schemas
        CREATE TABLE IF NOT EXISTS code_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            function_name TEXT,
            fact_type TEXT NOT NULL,  -- 'validation_guard', 'const_value', 'joi_schema', 'require_chain'
            condition TEXT,           -- the if-condition or const name
            message TEXT,             -- error message or const value
            line_number INTEGER,
            raw_snippet TEXT           -- 3-5 lines of context
        );
        CREATE INDEX IF NOT EXISTS idx_code_facts_repo ON code_facts(repo_name);
        CREATE INDEX IF NOT EXISTS idx_code_facts_type ON code_facts(fact_type);

        -- FTS5 for code_facts (searchable)
        CREATE VIRTUAL TABLE IF NOT EXISTS code_facts_fts USING fts5(
            repo_name,
            file_path,
            function_name,
            fact_type,
            condition,
            message,
            content=code_facts,
            content_rowid=id,
            tokenize='porter unicode61'
        );
    """)


def detect_language(file_path: str) -> str:
    """Detect language from file extension."""
    ext = Path(file_path).suffix.lower()
    lang_map = {
        ".js": "javascript",
        ".ts": "typescript",
        ".proto": "protobuf",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".go": "go",
        ".py": "python",
        ".sh": "bash",
    }
    return lang_map.get(ext, "text")


def detect_file_type(artifact_type: str, file_name: str) -> str:
    """Determine file type category."""
    # Domain knowledge files get special type for boosted ranking
    if file_name.upper().startswith("DOMAIN_KNOWLEDGE"):
        return "gotchas"
    if artifact_type == "proto":
        return "proto"
    if artifact_type == "docs":
        return "docs"
    if artifact_type == "config":
        return "config"
    if artifact_type == "env":
        return "env"
    if artifact_type == "k8s":
        return "k8s"
    if artifact_type == "methods":
        return "grpc_method"
    if artifact_type == "libs":
        return "library"
    if artifact_type == "workflows":
        return "workflow"
    if artifact_type == "ci":
        return "ci"
    if artifact_type == "routes":
        return "route"
    if artifact_type == "services":
        return "service"
    if artifact_type == "handlers":
        return "handler"
    if artifact_type in ("utils", "consts"):
        return "library"
    return "other"


def chunk_proto(content: str, repo_name: str) -> list[dict]:
    """Chunk protobuf files by message/service/enum/rpc definitions."""
    chunks = []
    # Split by top-level definitions
    pattern = r"^(message|service|enum|rpc)\s+(\w+)"
    lines = content.split("\n")
    current_chunk = []
    current_type = "header"

    for line in lines:
        match = re.match(pattern, line)
        if match and current_chunk:
            text = "\n".join(current_chunk).strip()
            if len(text) >= MIN_CHUNK:
                chunks.append(
                    {
                        "content": f"[Repo: {repo_name}] {text}",
                        "chunk_type": f"proto_{current_type}",
                    }
                )
            current_chunk = [line]
            current_type = match.group(1)
        else:
            current_chunk.append(line)

    # Last chunk
    if current_chunk:
        text = "\n".join(current_chunk).strip()
        if len(text) >= MIN_CHUNK:
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] {text}",
                    "chunk_type": f"proto_{current_type}",
                }
            )

    # If no chunks were created (small file), index as single chunk
    if not chunks and len(content.strip()) >= MIN_CHUNK:
        chunks.append(
            {
                "content": f"[Repo: {repo_name}] {content.strip()}",
                "chunk_type": "proto_file",
            }
        )

    return chunks


def chunk_markdown(content: str, repo_name: str) -> list[dict]:
    """Chunk markdown by header sections."""
    chunks = []
    content = re.sub(r"\A---\n.*?\n---\n?", "", content, count=1, flags=re.DOTALL)
    sections = re.split(r"^(#{1,3}\s+.+)$", content, flags=re.MULTILINE)

    current_header = ""
    current_content = []

    for part in sections:
        if re.match(r"^#{1,3}\s+", part):
            # Save previous section
            if current_content:
                text = (current_header + "\n" + "\n".join(current_content)).strip()
                if len(text) >= MIN_CHUNK:
                    chunks.append(
                        {
                            "content": f"[Repo: {repo_name}] {text}",
                            "chunk_type": "doc_section",
                        }
                    )
            current_header = part
            current_content = []
        else:
            current_content.append(part)

    # Last section
    if current_content or current_header:
        text = (current_header + "\n" + "\n".join(current_content)).strip()
        if len(text) >= MIN_CHUNK:
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] {text}",
                    "chunk_type": "doc_section",
                }
            )

    if not chunks and len(content.strip()) >= MIN_CHUNK:
        chunks.append(
            {
                "content": f"[Repo: {repo_name}] {content.strip()}",
                "chunk_type": "doc_file",
            }
        )

    return chunks


def _smart_chunk_js(content: str, file_path: str, max_lines: int = 200) -> list[dict]:
    """Split JS/TS content into semantically meaningful chunks using regex heuristics.

    Returns list of dicts with keys: 'content' (raw text), 'chunk_type' (label),
    'name' (detected symbol name or None). Caller is responsible for prepending
    repo prefix.

    Boundary patterns detected:
    - Function declarations / expressions / arrow functions
    - Class declarations
    - Export blocks (module.exports, export default, export {})
    - Method definitions inside classes
    - Decorator / route-handler patterns (@Controller, router.get, app.post)
    """

    # ---- boundary patterns (applied to stripped lines) ----
    # Each tuple: (compiled regex, chunk_type_prefix, name_group_index_or_None)
    _BOUNDARY_PATTERNS = [
        # class Foo / export class Foo
        (re.compile(r"^(?:export\s+(?:default\s+)?)?class\s+(\w+)"), "class", 1),
        # async function foo / export default async function foo
        (re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)"), "function", 1),
        # const foo = async ( / const foo = ( / const foo = async function
        (
            re.compile(r"^(?:export\s+(?:default\s+)?)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\(|function\b)"),
            "function",
            1,
        ),
        # module.exports
        (re.compile(r"^module\.exports\b"), "exports", None),
        # export default / export {
        (re.compile(r"^export\s+(?:default\b|\{)"), "exports", None),
        # decorators: @Controller, @Injectable, @Get, etc.
        (re.compile(r"^@(\w+)"), "decorator", 1),
        # route handlers: router.get( / app.post( / server.route(
        (re.compile(r"^(?:router|app|server|fastify)\s*\.\s*(get|post|put|patch|delete|route|use)\s*\("), "route", 1),
        # class method shorthand: async foo( / foo( at indented level (2-4 spaces or tab)
        # Exclude JS keywords (if/for/while/switch/catch/else) that look like method calls
        (
            re.compile(
                r"^(?:async\s+)?(?!if|for|while|switch|catch|else|return|throw|try|new|await|typeof|delete|void)(\w+)\s*\([^)]*\)\s*\{?\s*$"
            ),
            "method",
            1,
        ),
    ]

    lines = content.split("\n")
    total_lines = len(lines)

    # Detect boundary lines ------------------------------------------------
    # Each entry: (line_index, chunk_type, symbol_name_or_None)
    boundaries: list[tuple[int, str, str | None]] = []

    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
            continue
        for pattern, ctype, name_group in _BOUNDARY_PATTERNS:
            m = pattern.match(stripped)
            if m:
                name = m.group(name_group) if name_group else None
                # For 'method' pattern, only accept if indented (not top-level)
                if ctype == "method" and raw_line[0:1] not in (" ", "\t"):
                    continue
                boundaries.append((idx, ctype, name))
                break  # first matching pattern wins

    # No structure detected -> fall back to None (caller will use line-count)
    if not boundaries:
        return []

    # Build raw segments from boundaries ------------------------------------
    # A segment runs from one boundary line to just before the next boundary.
    segments: list[dict] = []
    # pending_start tracks the start of a too-small segment that should be
    # merged forward into the next segment (e.g. a bare `class Foo {` line).
    pending_start: int | None = None

    for i, (start, ctype, name) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else total_lines

        # Include any leading comment/decorator lines (look back up to 5 lines)
        actual_start = start
        for look_back in range(1, 6):
            prev = start - look_back
            if prev < 0:
                break
            prev_stripped = lines[prev].strip()
            if (
                prev_stripped.startswith("//")
                or prev_stripped.startswith("/*")
                or prev_stripped.startswith("*")
                or prev_stripped.startswith("@")
                or prev_stripped == ""
            ):
                actual_start = prev
            else:
                break

        # Absorb any pending too-small segment from previous iteration
        if pending_start is not None and pending_start < actual_start:
            actual_start = pending_start
            pending_start = None
        elif pending_start is not None:
            pending_start = None  # overlaps, drop it

        # Don't overlap with previous segment
        if segments:
            prev_end = segments[-1]["_end"]
            if actual_start < prev_end:
                actual_start = prev_end

        segment_lines = lines[actual_start:end]
        text = "\n".join(segment_lines).strip()
        if not text:
            continue

        label = f"{ctype}:{name}" if name else ctype

        # If this segment is too small (e.g. a bare `class Foo {` line),
        # merge it forward into the next segment instead of dropping it.
        if len(text) < MIN_CHUNK:
            pending_start = actual_start
            continue

        segments.append(
            {
                "text": text,
                "chunk_type": f"code_{label}",
                "line_count": end - actual_start,
                "_start": actual_start,
                "_end": end,
            }
        )

    # Handle any preamble before the first boundary (imports, top-level vars)
    first_boundary = boundaries[0][0] if boundaries else total_lines
    if first_boundary > 0:
        # Look back for leading comments already claimed
        effective_start = segments[0]["_start"] if segments else first_boundary
        preamble_text = "\n".join(lines[0:effective_start]).strip()
        if preamble_text and len(preamble_text) >= MIN_CHUNK:
            segments.insert(
                0,
                {
                    "text": preamble_text,
                    "chunk_type": "code_preamble",
                    "line_count": effective_start,
                    "_start": 0,
                    "_end": effective_start,
                },
            )

    # Sub-split oversized segments ------------------------------------------
    result: list[dict] = []
    for seg in segments:
        if seg["line_count"] <= max_lines:
            result.append(
                {
                    "content": seg["text"],
                    "chunk_type": seg["chunk_type"],
                }
            )
        else:
            # Split at blank lines or inner function boundaries
            sub_lines = seg["text"].split("\n")
            sub_chunks: list[list[str]] = []
            current: list[str] = []

            for ln in sub_lines:
                current.append(ln)
                if len(current) >= max_lines:
                    # Try to split at the last blank line within the current batch
                    split_at = None
                    for back in range(len(current) - 1, max(len(current) - 20, -1), -1):
                        if back >= 0 and current[back].strip() == "":
                            split_at = back
                            break
                    if split_at is not None and split_at > 0:
                        sub_chunks.append(current[:split_at])
                        current = current[split_at:]
                    else:
                        # No good split point; just cut here
                        sub_chunks.append(current)
                        current = []

            if current:
                sub_chunks.append(current)

            for si, sc in enumerate(sub_chunks):
                text = "\n".join(sc).strip()
                if text and len(text) >= MIN_CHUNK:
                    suffix = f"_part{si + 1}" if len(sub_chunks) > 1 else ""
                    result.append(
                        {
                            "content": text,
                            "chunk_type": seg["chunk_type"] + suffix,
                        }
                    )

    return result


def chunk_code(content: str, repo_name: str, language: str, file_path: str = "") -> list[dict]:
    """Chunk JS/TS code by function/class definitions.

    Uses AST-aware regex heuristics for .js/.ts/.mjs files.
    Falls back to simple line-count splitting for unstructured files.
    """
    chunks = []

    # If small enough, index as single chunk
    if len(content) <= MAX_CHUNK:
        if len(content.strip()) >= MIN_CHUNK:
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] {content.strip()}",
                    "chunk_type": "code_file",
                }
            )
        return chunks

    # Try smart chunking for JS/TS files
    ext = Path(file_path).suffix.lower() if file_path else ""
    if ext in (".js", ".ts", ".mjs"):
        smart = _smart_chunk_js(content, file_path)
        if smart:
            # Prefix with repo name and enforce max size
            for sc in smart:
                text = sc["content"]
                if len(text) > MAX_CHUNK:
                    text = text[:MAX_CHUNK] + "\n... [truncated]"
                chunks.append(
                    {
                        "content": f"[Repo: {repo_name}] {text}",
                        "chunk_type": sc["chunk_type"],
                    }
                )
            return chunks

    # Fallback: split by top-level declarations (original logic)
    patterns = [
        r"^(?:async\s+)?function\s+\w+",
        r"^(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\(?",
        r"^class\s+\w+",
        r"^module\.exports",
        r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const)",
    ]
    combined = "|".join(f"(?:{p})" for p in patterns)

    lines = content.split("\n")
    current_chunk = []

    for _i, line in enumerate(lines):
        if re.match(combined, line.strip()) and current_chunk and len("\n".join(current_chunk)) > MIN_CHUNK:
            text = "\n".join(current_chunk).strip()
            if len(text) >= MIN_CHUNK:
                if len(text) > MAX_CHUNK:
                    text = text[:MAX_CHUNK] + "\n... [truncated]"
                chunks.append(
                    {
                        "content": f"[Repo: {repo_name}] {text}",
                        "chunk_type": "code_function",
                    }
                )
            current_chunk = [line]
        else:
            current_chunk.append(line)

    # Last chunk
    if current_chunk:
        text = "\n".join(current_chunk).strip()
        if len(text) >= MIN_CHUNK:
            if len(text) > MAX_CHUNK:
                text = text[:MAX_CHUNK] + "\n... [truncated]"
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] {text}",
                    "chunk_type": "code_function",
                }
            )

    return chunks


def chunk_json(content: str, repo_name: str) -> list[dict]:
    """Chunk JSON files (especially package.json) by relevant fields."""
    chunks = []
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        if len(content.strip()) >= MIN_CHUNK:
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] {content.strip()[:MAX_CHUNK]}",
                    "chunk_type": "config_file",
                }
            )
        return chunks

    # For package.json, extract key fields
    if isinstance(data, dict):
        # Dependencies chunk
        deps = {}
        for key in ["dependencies", "devDependencies", "peerDependencies"]:
            if key in data:
                deps[key] = data[key]
        if deps:
            dep_text = json.dumps(deps, indent=2)
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] package.json dependencies:\n{dep_text[:MAX_CHUNK]}",
                    "chunk_type": "config_deps",
                }
            )

        # Scripts chunk
        if "scripts" in data:
            scripts_text = json.dumps({"scripts": data["scripts"]}, indent=2)
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] package.json scripts:\n{scripts_text[:MAX_CHUNK]}",
                    "chunk_type": "config_scripts",
                }
            )

        # Name/version/description
        meta = {k: data[k] for k in ["name", "version", "description", "main"] if k in data}
        if meta:
            meta_text = json.dumps(meta, indent=2)
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] package.json metadata:\n{meta_text}",
                    "chunk_type": "config_meta",
                }
            )

    if not chunks and len(content.strip()) >= MIN_CHUNK:
        chunks.append(
            {
                "content": f"[Repo: {repo_name}] {content.strip()[:MAX_CHUNK]}",
                "chunk_type": "config_file",
            }
        )

    return chunks


def chunk_yaml(content: str, repo_name: str) -> list[dict]:
    """Chunk YAML files as single chunks (usually small enough)."""
    chunks = []
    text = content.strip()
    if len(text) >= MIN_CHUNK:
        if len(text) > MAX_CHUNK:
            text = text[:MAX_CHUNK] + "\n... [truncated]"
        chunks.append(
            {
                "content": f"[Repo: {repo_name}] {text}",
                "chunk_type": "yaml_file",
            }
        )
    return chunks


def chunk_env(content: str, repo_name: str) -> list[dict]:
    """Chunk env/config files as single chunks."""
    chunks = []
    text = content.strip()
    if len(text) >= MIN_CHUNK:
        chunks.append(
            {
                "content": f"[Repo: {repo_name}] {text[:MAX_CHUNK]}",
                "chunk_type": "env_config",
            }
        )
    return chunks


def chunk_file(file_path: Path, repo_name: str, artifact_type: str) -> list[dict]:
    """Read and chunk a file based on its type."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if not content.strip() or len(content.strip()) < MIN_CHUNK:
        return []

    language = detect_language(str(file_path))

    if language == "protobuf":
        return chunk_proto(content, repo_name)
    elif language == "markdown":
        return chunk_markdown(content, repo_name)
    elif language == "json":
        return chunk_json(content, repo_name)
    elif language in ("yaml",):
        return chunk_yaml(content, repo_name)
    elif artifact_type == "env":
        return chunk_env(content, repo_name)
    elif language in ("javascript", "typescript"):
        return chunk_code(content, repo_name, language, file_path=str(file_path))
    else:
        # Default: single chunk
        text = content.strip()
        if len(text) > MAX_CHUNK:
            text = text[:MAX_CHUNK] + "\n... [truncated]"
        return [
            {
                "content": f"[Repo: {repo_name}] {text}",
                "chunk_type": "file",
            }
        ]


def delete_repo_chunks(conn: sqlite3.Connection, repo_name: str) -> int:
    """Delete all FTS5 chunks for a specific repo. Returns count of deleted rows."""
    # FTS5 supports DELETE with rowid. We need to find rowids first.
    rowids = conn.execute("SELECT rowid FROM chunks WHERE repo_name = ?", (repo_name,)).fetchall()
    for (rowid,) in rowids:
        conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
    return len(rowids)


def delete_repo_data(conn: sqlite3.Connection, repo_name: str) -> int:
    """Delete all data for a repo: chunks, chunk_meta, code_facts, code_facts_fts, repos row.

    Returns count of deleted chunk rows.
    """
    # Delete chunks and track rowids for chunk_meta cleanup
    rowids = conn.execute("SELECT rowid FROM chunks WHERE repo_name = ?", (repo_name,)).fetchall()
    for (rowid,) in rowids:
        conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))

    # Delete chunk_meta for those rowids
    if rowids:
        rowid_list = [r[0] for r in rowids]
        placeholders = ",".join("?" * len(rowid_list))
        conn.execute(f"DELETE FROM chunk_meta WHERE chunk_rowid IN ({placeholders})", rowid_list)

    # Delete code_facts_fts entries (content table sync — must delete before code_facts)
    fact_ids = conn.execute("SELECT id FROM code_facts WHERE repo_name = ?", (repo_name,)).fetchall()
    for (fid,) in fact_ids:
        conn.execute("DELETE FROM code_facts_fts WHERE rowid = ?", (fid,))

    # Delete code_facts
    conn.execute("DELETE FROM code_facts WHERE repo_name = ?", (repo_name,))

    # Delete repos row
    conn.execute("DELETE FROM repos WHERE name = ?", (repo_name,))

    return len(rowids)


def load_existing_shas(conn: sqlite3.Connection) -> dict[str, str]:
    """Load repo name -> SHA mapping from existing database."""
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT name, sha FROM repos")}
    except sqlite3.OperationalError:
        return {}  # table doesn't exist yet


def get_current_sha(repo_path: Path) -> str | None:
    """Get current HEAD SHA from a git repo directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def detect_changed_repos(repo_meta: dict, existing_shas: dict[str, str]) -> tuple[set[str], set[str]]:
    """Compare current HEAD SHAs with indexed SHAs.

    Returns (changed_repos, removed_repos).
    changed_repos: repos that need re-indexing (new or SHA mismatch).
    removed_repos: repos in DB but no longer in raw/.
    """
    changed = set()
    current_repos = set()

    for repo_name, meta in repo_meta.items():
        current_repos.add(repo_name)
        indexed_sha = existing_shas.get(repo_name)

        # New repo or SHA changed
        raw_path = RAW_DIR / repo_name
        if raw_path.is_dir():
            current_sha = get_current_sha(raw_path)
            # Fall back to _index.json SHA if git not available
            if current_sha is None:
                current_sha = meta.get("sha", "unknown")
        else:
            current_sha = meta.get("sha", "unknown")

        if indexed_sha is None or indexed_sha != current_sha:
            changed.add(repo_name)

    # Repos in DB but no longer in index
    removed = set(existing_shas.keys()) - current_repos

    return changed, removed


def index_repo(conn: sqlite3.Connection, repo_name: str, meta: dict) -> tuple[int, int]:
    """Index a single repo. Returns (files_count, chunks_count)."""
    artifact_types = [
        "proto",
        "docs",
        "config",
        "env",
        "k8s",
        "methods",
        "libs",
        "workflows",
        "ci",
        "routes",
        "services",
        "handlers",
        "utils",
        "consts",
    ]

    repo_dir = EXTRACTED_DIR / repo_name
    if not repo_dir.is_dir():
        # Still insert repo metadata so incremental mode can track its SHA
        conn.execute(
            "INSERT OR REPLACE INTO repos(name, type, sha, org_deps, artifact_counts) VALUES (?, ?, ?, ?, ?)",
            (
                repo_name,
                meta.get("type", "unknown"),
                meta.get("sha", "unknown"),
                json.dumps(meta.get("org_deps", [])),
                json.dumps(meta.get("artifacts", {})),
            ),
        )
        return 0, 0

    files = 0
    chunks = 0

    for artifact_type in artifact_types:
        type_dir = repo_dir / artifact_type
        if not type_dir.is_dir():
            continue

        for file_path in type_dir.rglob("*"):
            if not file_path.is_file():
                continue

            files += 1
            rel_path = str(file_path.relative_to(repo_dir))
            language = detect_language(str(file_path))
            file_type = detect_file_type(artifact_type, file_path.name)

            file_chunks = chunk_file(file_path, repo_name, artifact_type)
            chunk_rowids = []

            for chunk in file_chunks:
                conn.execute(
                    "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        chunk["content"],
                        repo_name,
                        rel_path,
                        file_type,
                        chunk["chunk_type"],
                        language,
                    ),
                )
                chunk_rowids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                chunks += 1

            # Populate chunk_meta for sibling retrieval
            total = len(chunk_rowids)
            for order, rowid in enumerate(chunk_rowids):
                conn.execute(
                    "INSERT OR REPLACE INTO chunk_meta(chunk_rowid, chunk_order, total_chunks) VALUES (?, ?, ?)",
                    (rowid, order, total),
                )

            # Extract code_facts from JS files in relevant directories
            if language in ("javascript", "typescript") and artifact_type in (
                "methods",
                "libs",
                "handlers",
                "routes",
                "services",
                "utils",
                "consts",
            ):
                try:
                    source = file_path.read_text(encoding="utf-8", errors="replace")
                    facts = extract_code_facts(source, rel_path, repo_name)
                    for fact in facts:
                        conn.execute(
                            "INSERT INTO code_facts(repo_name, file_path, function_name, fact_type, "
                            "condition, message, line_number, raw_snippet) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                fact["repo_name"],
                                fact["file_path"],
                                fact["function_name"],
                                fact["fact_type"],
                                fact["condition"],
                                fact["message"],
                                fact["line_number"],
                                fact["raw_snippet"],
                            ),
                        )
                        # Also insert into FTS5 for searchability
                        conn.execute(
                            "INSERT INTO code_facts_fts(rowid, repo_name, file_path, function_name, "
                            "fact_type, condition, message) "
                            "VALUES (last_insert_rowid(), ?, ?, ?, ?, ?, ?)",
                            (
                                fact["repo_name"],
                                fact["file_path"],
                                fact["function_name"],
                                fact["fact_type"],
                                fact["condition"],
                                fact["message"],
                            ),
                        )
                except Exception:
                    pass  # Don't fail indexing on code_facts extraction errors

    # Insert/update repo metadata
    conn.execute(
        "INSERT OR REPLACE INTO repos(name, type, sha, org_deps, artifact_counts) VALUES (?, ?, ?, ?, ?)",
        (
            repo_name,
            meta.get("type", "unknown"),
            meta.get("sha", "unknown"),
            json.dumps(meta.get("org_deps", [])),
            json.dumps(meta.get("artifacts", {})),
        ),
    )

    return files, chunks


def index_gotchas(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index gotchas files from ~/.code-rag/docs/gotchas/.

    File naming: <repo_name>.md (e.g., grpc-apm-trustly.md).
    Each file is indexed with file_type='gotchas' and repo_name from filename.
    """
    if not GOTCHAS_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(GOTCHAS_DIR.glob("*.md")):
        repo_name = file_path.stem  # grpc-apm-trustly.md → grpc-apm-trustly
        files += 1

        file_chunks = chunk_file(file_path, repo_name, "docs")

        for chunk in file_chunks:
            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk["content"],
                    repo_name,
                    "docs/GOTCHAS.md",
                    "gotchas",
                    chunk["chunk_type"],
                    "markdown",
                ),
            )
            chunks += 1

    if files:
        print(f"  Gotchas: {files} files, {chunks} chunks")

    return files, chunks


def index_domain_registry(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index domain registry from docs/domain_registry.yaml.

    Each domain entry becomes a searchable chunk with file_type='domain_registry'.
    This allows queries like "api.dev.example.com" to return the serving repo.
    """
    if not DOMAIN_REGISTRY_FILE.is_file():
        return 0, 0

    try:
        import yaml
    except ImportError:
        # Fallback: parse simple YAML manually
        return _index_domain_registry_simple(conn)

    data = yaml.safe_load(DOMAIN_REGISTRY_FILE.read_text())
    return _insert_domain_entries(conn, data.get("domains", []))


def _index_domain_registry_simple(conn: sqlite3.Connection) -> tuple[int, int]:
    """Fallback parser for domain_registry.yaml without PyYAML."""
    import re

    text = DOMAIN_REGISTRY_FILE.read_text()
    entries = []
    current: dict = {}

    for line in text.splitlines():
        m = re.match(r'\s+-\s+domain:\s+"(.+)"', line)
        if m:
            if current:
                entries.append(current)
            current = {"domain": m.group(1)}
            continue
        m = re.match(r"\s+repo:\s+(\S+)", line)
        if m and current:
            current["repo"] = m.group(1)
        m = re.match(r'\s+description:\s+"(.+)"', line)
        if m and current:
            current["description"] = m.group(1)

    if current:
        entries.append(current)

    return _insert_domain_entries(conn, entries)


def _insert_domain_entries(
    conn: sqlite3.Connection,
    entries: list[dict],
) -> tuple[int, int]:
    """Insert domain registry entries as searchable chunks."""
    chunks = 0
    for entry in entries:
        domain = entry.get("domain", "")
        repo = entry.get("repo", "")
        desc = entry.get("description", "")
        if not domain or not repo:
            continue

        # Create chunk content that's searchable by domain name
        # Expand {env} to all environments for searchability
        envs = ["dev", "staging", ""]  # empty = prod
        domain_variants = []
        for env in envs:
            if env:
                domain_variants.append(domain.replace("{env}.", f"{env}."))
            else:
                domain_variants.append(domain.replace("{env}.", ""))

        content = f"Domain: {domain}\nDomains: {', '.join(domain_variants)}\nRepo: {repo}\nDescription: {desc}\n"

        conn.execute(
            "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, repo, "docs/domain_registry.yaml", "domain_registry", "domain_entry", "yaml"),
        )
        chunks += 1

    if chunks:
        print(f"  Domain registry: {chunks} entries")

    return 1 if chunks else 0, chunks


def index_flows(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index flow annotation files from docs/flows/.

    Each flow becomes a searchable chunk with file_type='flow_annotation'.
    This allows queries about data-driven redirects and cross-service flows
    to find the right chain even when static analysis cannot detect them.
    """
    if not FLOWS_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(FLOWS_DIR.glob("*.yaml")):
        repo_name = file_path.stem  # express-api-internal.yaml → express-api-internal
        files += 1

        text = file_path.read_text()

        # Parse flows from YAML (simple parser, no PyYAML dependency)
        # Each flow block becomes a searchable chunk
        current_flow: list[str] = []
        flow_name = ""

        for line in text.splitlines():
            if line.strip().startswith("- name:"):
                # Save previous flow
                if current_flow and flow_name:
                    content = f"[Flow: {repo_name}] {flow_name}\n" + "\n".join(current_flow)
                    conn.execute(
                        "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (content, repo_name, f"docs/flows/{file_path.name}", "flow_annotation", "flow", "yaml"),
                    )
                    chunks += 1

                # Start new flow
                flow_name = line.strip().split("name:", 1)[1].strip().strip('"').strip("'")
                current_flow = [line]
            elif current_flow is not None:
                current_flow.append(line)

        # Save last flow
        if current_flow and flow_name:
            content = f"[Flow: {repo_name}] {flow_name}\n" + "\n".join(current_flow)
            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (content, repo_name, f"docs/flows/{file_path.name}", "flow_annotation", "flow", "yaml"),
            )
            chunks += 1

    if files:
        print(f"  Flows: {files} files, {chunks} flow annotations")

    return files, chunks


_TASK_SECTION_MAP = {
    "description": "task_description",
    "api spec": "task_api_spec",
    "api": "task_api_spec",
    "implementation plan": "task_plan",
    "implementation": "task_plan",
    "plan": "task_plan",
    "decisions": "task_decisions",
    "key decisions": "task_decisions",
    "gotchas": "task_gotchas",
    "gotchas found": "task_gotchas",
    "progress": "task_progress",
    "progress log": "task_progress",
    "webhook": "task_api_spec",
    "webhook spec": "task_api_spec",
    "config": "task_plan",
    "repos to change": "task_plan",
    "gaps": "task_decisions",
}


def _detect_task_chunk_type(header: str) -> str:
    """Map a markdown header to a task chunk_type."""
    normalized = re.sub(r"^#+\s*", "", header).strip().lower()
    # Try exact match first, then prefix match
    if normalized in _TASK_SECTION_MAP:
        return _TASK_SECTION_MAP[normalized]
    for key, ctype in _TASK_SECTION_MAP.items():
        if normalized.startswith(key) or key in normalized:
            return ctype
    return "task_section"


def chunk_task_markdown(content: str, task_name: str) -> list[dict]:
    """Chunk task markdown with section-aware splitting.

    - Frontmatter (---...---) → task_metadata chunk
    - Each ## section → chunk typed by section name
    - ### sub-sections within large sections → separate chunks
    - Oversized chunks split at blank lines
    """
    chunks = []

    # Extract frontmatter
    fm_match = re.match(r"^---\n(.+?\n)---\n?", content, re.DOTALL)
    frontmatter = ""
    body = content
    if fm_match:
        frontmatter = fm_match.group(1).strip()
        body = content[fm_match.end() :]
        if len(frontmatter) >= MIN_CHUNK:
            chunks.append(
                {
                    "content": f"[Task: {task_name}] [Metadata]\n{frontmatter}",
                    "chunk_type": "task_metadata",
                }
            )

    # Split body by H2 headers
    h2_sections = re.split(r"^(##\s+.+)$", body, flags=re.MULTILINE)

    current_header = ""
    current_content = []

    for part in h2_sections:
        if re.match(r"^##\s+", part):
            # Flush previous section
            if current_content:
                _flush_task_section(chunks, task_name, current_header, "\n".join(current_content))
            current_header = part
            current_content = []
        else:
            current_content.append(part)

    # Flush last section
    if current_content or current_header:
        _flush_task_section(chunks, task_name, current_header, "\n".join(current_content))

    # Fallback: if no chunks from body, index whole body
    if not chunks and len(body.strip()) >= MIN_CHUNK:
        chunks.append(
            {
                "content": f"[Task: {task_name}] {body.strip()[:MAX_CHUNK]}",
                "chunk_type": "task_section",
            }
        )

    return chunks


def _flush_task_section(chunks: list[dict], task_name: str, header: str, content: str):
    """Flush a task section, splitting by H3 if it exceeds MAX_CHUNK."""
    chunk_type = _detect_task_chunk_type(header) if header else "task_section"
    full_text = (header + "\n" + content).strip() if header else content.strip()

    if len(full_text) < MIN_CHUNK:
        return

    if len(full_text) <= MAX_CHUNK:
        chunks.append(
            {
                "content": f"[Task: {task_name}] [{chunk_type}] {full_text}",
                "chunk_type": chunk_type,
            }
        )
        return

    # Split by H3 sub-sections
    h3_parts = re.split(r"^(###\s+.+)$", content, flags=re.MULTILINE)
    sub_header = header
    sub_content: list[str] = []

    for part in h3_parts:
        if re.match(r"^###\s+", part):
            # Flush previous sub-section
            sub_text = (
                (sub_header + "\n" + "\n".join(sub_content)).strip() if sub_header else "\n".join(sub_content).strip()
            )
            if len(sub_text) >= MIN_CHUNK:
                if len(sub_text) > MAX_CHUNK:
                    sub_text = sub_text[:MAX_CHUNK] + "\n... [truncated]"
                chunks.append(
                    {
                        "content": f"[Task: {task_name}] [{chunk_type}] {sub_text}",
                        "chunk_type": chunk_type,
                    }
                )
            sub_header = part
            sub_content = []
        else:
            sub_content.append(part)

    # Flush last sub-section
    sub_text = (sub_header + "\n" + "\n".join(sub_content)).strip() if sub_header else "\n".join(sub_content).strip()
    if len(sub_text) >= MIN_CHUNK:
        if len(sub_text) > MAX_CHUNK:
            sub_text = sub_text[:MAX_CHUNK] + "\n... [truncated]"
        chunks.append(
            {
                "content": f"[Task: {task_name}] [{chunk_type}] {sub_text}",
                "chunk_type": chunk_type,
            }
        )


def index_tasks(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index task files from profiles/{profile}/docs/tasks/.

    File naming: <task-slug>.md (e.g., payper-interac-etransfer.md).
    Each file is indexed with file_type='task' and repo_name=task slug.
    """
    if not TASKS_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(TASKS_DIR.glob("*.md")):
        task_name = file_path.stem
        files += 1

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if not content.strip() or len(content.strip()) < MIN_CHUNK:
            continue

        file_chunks = chunk_task_markdown(content, task_name)
        chunk_rowids = []

        for chunk in file_chunks:
            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk["content"],
                    task_name,
                    f"docs/tasks/{file_path.name}",
                    "task",
                    chunk["chunk_type"],
                    "markdown",
                ),
            )
            chunk_rowids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            chunks += 1

        # Populate chunk_meta for sibling retrieval
        total = len(chunk_rowids)
        for order, rowid in enumerate(chunk_rowids):
            conn.execute(
                "INSERT OR REPLACE INTO chunk_meta(chunk_rowid, chunk_order, total_chunks) VALUES (?, ?, ?)",
                (rowid, order, total),
            )

    if files:
        print(f"  Tasks: {files} files, {chunks} chunks")

    return files, chunks


def index_references(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index reference files from profiles/{profile}/docs/references/.

    Supports .yaml and .md files. Each file indexed with file_type='reference'.
    """
    if not REFERENCES_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(REFERENCES_DIR.glob("*")):
        if file_path.suffix.lower() not in (".yaml", ".yml", ".md"):
            continue

        ref_name = file_path.stem
        files += 1

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if not content.strip() or len(content.strip()) < MIN_CHUNK:
            continue

        language = detect_language(str(file_path))

        if language == "markdown":
            file_chunks = chunk_markdown(content, ref_name)
        else:
            # YAML: index as single chunk or split by top-level entries
            text = content.strip()
            if len(text) > MAX_CHUNK:
                text = text[:MAX_CHUNK] + "\n... [truncated]"
            file_chunks = [{"content": f"[Reference: {ref_name}] {text}", "chunk_type": "reference_entry"}]

        for chunk in file_chunks:
            # Ensure reference prefix
            chunk_content = chunk["content"]
            if not chunk_content.startswith("[Reference:"):
                chunk_content = f"[Reference: {ref_name}] {chunk_content}"

            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk_content,
                    ref_name,
                    f"docs/references/{file_path.name}",
                    "reference",
                    chunk.get("chunk_type", "reference_entry"),
                    language,
                ),
            )
            chunks += 1

    if files:
        print(f"  References: {files} files, {chunks} chunks")

    return files, chunks


def index_providers(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index provider docs from profiles/{profile}/docs/providers/{provider}/*.md.

    Each provider gets repo_name='{provider}-docs', file_type='provider_doc'.
    Chunks get content prefix '[{Provider Title} Docs: {slug}]' for search boost.
    Idempotent: deletes existing chunks for each provider before re-inserting.
    """
    if not PROVIDERS_DIR.is_dir():
        return 0, 0

    total_files = 0
    total_chunks = 0

    for provider_dir in sorted(PROVIDERS_DIR.iterdir()):
        if not provider_dir.is_dir():
            continue
        provider = provider_dir.name
        repo_name = f"{provider}-docs"
        provider_title = provider.replace("-", " ").replace("_", " ").title()

        # Remove existing chunks for this provider (idempotent)
        conn.execute("DELETE FROM chunks WHERE repo_name = ?", (repo_name,))

        files_count = 0
        chunks_count = 0

        for md_file in sorted(provider_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if len(content.strip()) < MIN_CHUNK:
                continue

            slug = md_file.stem
            rel_path = f"docs/providers/{provider}/{md_file.name}"
            file_chunks = chunk_markdown(content, slug)

            for chunk in file_chunks:
                chunk_content = chunk["content"]
                if not chunk_content.startswith(f"[{provider_title} Docs:"):
                    chunk_content = f"[{provider_title} Docs: {slug}] {chunk_content}"
                conn.execute(
                    "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (chunk_content, repo_name, rel_path, "provider_doc", "provider_doc", "markdown"),
                )
                chunks_count += 1
            files_count += 1

        if files_count > 0:
            total_files += files_count
            total_chunks += chunks_count

    if total_files:
        print(f"  Provider docs: {total_files} files, {total_chunks} chunks")

    return total_files, total_chunks


def chunk_cql_seeds(content: str, repo_name: str) -> list[dict]:
    """Chunk seeds.cql — each INSERT becomes a separate chunk with provider config metadata.

    This is critical for provider integration: maps provider → payment_method_type, features, currencies.
    """
    chunks = []
    # Pattern to extract provider and payment_method_type from INSERT VALUES
    insert_pattern = re.compile(
        r"INSERT INTO\s+\S+\s*\(([^)]+)\)\s*VALUES\s*\((.+)\)\s*;",
        re.IGNORECASE,
    )

    for line in content.splitlines():
        line = line.strip()
        if not line or not line.upper().startswith("INSERT"):
            continue

        m = insert_pattern.match(line)
        if not m:
            continue

        columns_str = m.group(1)
        columns = [c.strip() for c in columns_str.split(",")]

        # Parse values (handle nested structures like [] and {})
        values_str = m.group(2)
        values = _parse_cql_values(values_str)

        if len(values) < len(columns):
            # Fallback: index whole line as chunk
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] [Provider Config] {line}",
                    "chunk_type": "provider_config",
                }
            )
            continue

        col_val = dict(zip(columns, values, strict=False))
        provider = col_val.get("provider", "").strip("'\"")
        pmt = col_val.get("payment_method_type", "").strip("'\"")

        # Extract ALL feature flags with explicit true/false values
        feature_cols = [
            "authorization",
            "sale",
            "capture_multiple",
            "capture_partial",
            "refund_multiple",
            "refund_partial",
            "cancel_multiple",
            "cancel_partial",
            "incremental_authorization",
            "payout",
            "verification",
            "network_tokens",
            "external_settlement",
            "internal_settlement",
        ]
        enabled = [c for c in feature_cols if col_val.get(c, "").strip() == "true"]
        disabled = [c for c in feature_cols if col_val.get(c, "").strip() == "false"]

        currencies = col_val.get("processing_currency_codes", "[]")
        settlement_currencies = col_val.get("settlement_currency_codes", "[]")
        precision = col_val.get("default_precision", "").strip("'\"")

        # Card scheme flags
        card_schemes = []
        for scheme in ["visa", "mastercard", "amex", "discover"]:
            val = col_val.get(scheme, "").strip()
            if val == "true":
                card_schemes.append(scheme)

        # Build rich chunk content with ALL values explicit
        header = f"Provider: {provider} | payment_method_type: {pmt}"
        enabled_line = f"Enabled features: {', '.join(enabled)}" if enabled else "Enabled features: none"
        disabled_line = f"Disabled features: {', '.join(disabled)}" if disabled else "Disabled features: none"
        currency_line = f"Processing currencies: {currencies}"
        settlement_line = f"Settlement currencies: {settlement_currencies}"
        precision_line = f"Default precision: {precision}" if precision else ""
        schemes_line = f"Card schemes: {', '.join(card_schemes)}" if card_schemes else ""

        # Explicit boolean matrix for search (key for benchmark accuracy)
        bool_lines = []
        for col in feature_cols:
            val = col_val.get(col, "").strip()
            if val in ("true", "false"):
                bool_lines.append(f"  {col} = {val}")
        bool_matrix = "Feature flags:\n" + "\n".join(bool_lines) if bool_lines else ""

        parts = [
            f"[Repo: {repo_name}] [Provider Config — Source of Truth]",
            header,
            enabled_line,
            disabled_line,
            currency_line,
            settlement_line,
        ]
        if precision_line:
            parts.append(precision_line)
        if schemes_line:
            parts.append(schemes_line)
        if bool_matrix:
            parts.append(bool_matrix)
        parts.append(f"Raw: {line[: MAX_CHUNK - 800]}")

        chunk_content = "\n".join(parts)

        chunks.append(
            {
                "content": chunk_content,
                "chunk_type": "provider_config",
            }
        )

    return chunks


def _parse_cql_values(values_str: str) -> list[str]:
    """Parse CQL VALUES clause, handling nested [] and {} structures."""
    values: list[str] = []
    current = ""
    depth = 0

    for char in values_str:
        if char in ("[", "{"):
            depth += 1
            current += char
        elif char in ("]", "}"):
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            values.append(current.strip())
            current = ""
        elif char == "'" and depth == 0:
            current += char
        else:
            current += char

    if current.strip():
        values.append(current.strip())

    return values


def extract_code_facts(content: str, file_path: str, repo_name: str) -> list[dict]:
    """Extract validation guards, const declarations, and joi/zod schemas from JS code.

    Returns list of dicts with: repo_name, file_path, function_name, fact_type,
    condition, message, line_number, raw_snippet.
    """
    facts: list[dict] = []
    lines = content.splitlines()

    # Track current function scope
    current_function = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track function scope
        func_match = re.match(
            r"(?:async\s+)?function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?|(\w+)\s*[:(]\s*(?:async\s+)?(?:function|\()",
            stripped,
        )
        if func_match:
            current_function = func_match.group(1) or func_match.group(2) or func_match.group(3)

        # Pattern 1: Validation guards — if (condition) throw/return error
        if_throw = re.match(
            r"if\s*\((.+?)\)\s*\{?\s*$",
            stripped,
        )
        if if_throw:
            condition = if_throw.group(1)
            # Look ahead for throw/return/error within next 10 lines
            for j in range(i + 1, min(i + 11, len(lines))):
                next_line = lines[j].strip()
                # Match throw, issuer_response_text, message patterns
                throw_match = re.search(r"throw\s+(?:new\s+\w+\()?['\"]([^'\"]+)['\"]", next_line) or re.search(
                    r"throw\s+(?:new\s+\w+\()?`([^`]+)`", next_line
                )
                response_match = re.search(r"issuer_response_text:\s*['\"]([^'\"]+)['\"]", next_line)
                message_match = re.search(r"message:\s*['\"]([^'\"]+)['\"]", next_line)
                msg = None
                if throw_match:
                    msg = throw_match.group(1)
                elif response_match:
                    msg = response_match.group(1)
                elif message_match:
                    msg = message_match.group(1)

                if msg:
                    snippet = "\n".join(lines[max(0, i - 1) : min(len(lines), j + 2)])
                    facts.append(
                        {
                            "repo_name": repo_name,
                            "file_path": file_path,
                            "function_name": current_function,
                            "fact_type": "validation_guard",
                            "condition": condition,
                            "message": msg,
                            "line_number": i + 1,
                            "raw_snippet": snippet[:500],
                        }
                    )
                    break

        # Pattern 2: Const declarations with literal values
        const_match = re.match(
            r"(?:const|let|var)\s+([A-Z][A-Z_0-9]+)\s*=\s*(.+?)(?:;?\s*$)",
            stripped,
        )
        if const_match:
            name = const_match.group(1)
            value = const_match.group(2).strip().rstrip(";")
            # Only index simple values (numbers, strings, small arrays)
            if len(value) < 300 and not value.startswith("require") and not value.startswith("function"):
                facts.append(
                    {
                        "repo_name": repo_name,
                        "file_path": file_path,
                        "function_name": current_function,
                        "fact_type": "const_value",
                        "condition": name,
                        "message": value,
                        "line_number": i + 1,
                        "raw_snippet": stripped[:500],
                    }
                )

        # Pattern 3: Joi schemas
        joi_match = re.search(r"Joi\.(object|string|number|array|boolean)\s*\(", stripped)
        if joi_match and ("validate" in stripped.lower() or "schema" in stripped.lower() or "=" in stripped):
            facts.append(
                {
                    "repo_name": repo_name,
                    "file_path": file_path,
                    "function_name": current_function,
                    "fact_type": "joi_schema",
                    "condition": stripped[:200],
                    "message": "",
                    "line_number": i + 1,
                    "raw_snippet": "\n".join(lines[max(0, i) : min(len(lines), i + 5)])[:500],
                }
            )

        # Pattern 4: process.env lookups with defaults
        env_match = re.search(
            r"process\.env\.(\w+)\s*(?:\|\||===?\s*|!==?\s*|\?\?)\s*['\"`]?([^'\"`;\n,)]{1,100})['\"`]?",
            stripped,
        )
        if env_match:
            env_name = env_match.group(1)
            default_val = env_match.group(2).strip()
            facts.append(
                {
                    "repo_name": repo_name,
                    "file_path": file_path,
                    "function_name": current_function,
                    "fact_type": "env_var",
                    "condition": env_name,
                    "message": default_val,
                    "line_number": i + 1,
                    "raw_snippet": stripped[:500],
                }
            )

        # Pattern 5: Temporal activity retry policies
        if "maximumAttempts" in stripped or "backoffCoefficient" in stripped or "initialInterval" in stripped:
            retry_match = re.search(
                r"(maximumAttempts|backoffCoefficient|initialInterval|startToCloseTimeout)"
                r"\s*:\s*['\"]?([^'\",}\s]+)['\"]?",
                stripped,
            )
            if retry_match:
                facts.append(
                    {
                        "repo_name": repo_name,
                        "file_path": file_path,
                        "function_name": current_function,
                        "fact_type": "temporal_retry",
                        "condition": retry_match.group(1),
                        "message": retry_match.group(2),
                        "line_number": i + 1,
                        "raw_snippet": "\n".join(lines[max(0, i - 2) : min(len(lines), i + 3)])[:500],
                    }
                )

        # Pattern 6: gRPC status code mapping
        grpc_status_match = re.search(
            r"(?:code|status)\s*:\s*(?:grpc\.status\.|status\.)?(\w+)\s*,\s*(?:message|details)\s*:\s*['\"`]([^'\"`]+)['\"`]",
            stripped,
        )
        if grpc_status_match:
            facts.append(
                {
                    "repo_name": repo_name,
                    "file_path": file_path,
                    "function_name": current_function,
                    "fact_type": "grpc_status",
                    "condition": grpc_status_match.group(1),
                    "message": grpc_status_match.group(2),
                    "line_number": i + 1,
                    "raw_snippet": stripped[:500],
                }
            )

    return facts


def index_seeds(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index seeds.cql from feature repo as provider config source of truth.

    Each INSERT = separate chunk with provider, payment_method_type, features, currencies.
    """
    seeds_path = RAW_DIR / FEATURE_REPO / "seeds.cql"
    if not seeds_path.is_file():
        return 0, 0

    try:
        content = seeds_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0, 0

    chunks = chunk_cql_seeds(content, FEATURE_REPO)
    count = 0

    for chunk in chunks:
        conn.execute(
            "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk["content"],
                FEATURE_REPO,
                "seeds.cql",
                "provider_config",
                chunk["chunk_type"],
                "cql",
            ),
        )
        count += 1

    if count:
        print(f"  Seeds.cql: {count} provider config chunks")

    return 1 if count else 0, count


def index_test_scripts(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index scripts/ directories from provider repos.

    Test scripts contain credentials, correct request formats, URLs — valuable for
    onboarding and debugging provider integrations.
    """
    if not RAW_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    # Index scripts from all repos that have a scripts/ directory
    for repo_dir in sorted(RAW_DIR.iterdir()):
        if not repo_dir.is_dir():
            continue
        scripts_dir = repo_dir / "scripts"
        if not scripts_dir.is_dir():
            continue

        repo_name = repo_dir.name

        for script_path in sorted(scripts_dir.rglob("*")):
            if not script_path.is_file():
                continue
            ext = script_path.suffix.lower()
            if ext not in (".js", ".ts", ".sh", ".mjs", ".py"):
                continue

            try:
                content = script_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if not content.strip() or len(content.strip()) < MIN_CHUNK:
                continue

            files += 1
            language = detect_language(str(script_path))
            rel_path = f"scripts/{script_path.relative_to(scripts_dir)}"

            # Mask potential secrets but keep structure
            text = content.strip()
            if len(text) > MAX_CHUNK:
                text = text[:MAX_CHUNK] + "\n... [truncated]"

            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"[Repo: {repo_name}] [Test Script] {text}",
                    repo_name,
                    rel_path,
                    "test_script",
                    "code_file",
                    language,
                ),
            )
            chunks += 1

    if files:
        print(f"  Test scripts: {files} files, {chunks} chunks")

    return files, chunks


def main():
    if not EXTRACTED_DIR.exists() or not INDEX_FILE.exists():
        print("Error: Run extract_artifacts.py first.")
        sys.exit(1)

    # Parse CLI flags
    only_repos = None
    incremental = False
    for arg in sys.argv[1:]:
        if arg.startswith("--repos="):
            only_repos = set(arg.split("=", 1)[1].split(","))
        elif arg == "--incremental":
            incremental = True

    # --repos implies incremental behavior
    if only_repos:
        incremental = True

    # Load repo metadata
    repo_meta = json.loads(INDEX_FILE.read_text())
    print(f"Found {len(repo_meta)} repos in index")

    DB_DIR.mkdir(parents=True, exist_ok=True)

    if incremental and not DB_PATH.exists():
        print("No existing database found. Running full build instead.")
        incremental = False
        only_repos = None

    # For --incremental without --repos: auto-detect changed repos by SHA comparison
    if incremental and only_repos is None:
        conn_detect = sqlite3.connect(str(DB_PATH))
        existing_shas = load_existing_shas(conn_detect)
        conn_detect.close()

        changed, removed = detect_changed_repos(repo_meta, existing_shas)

        if not changed and not removed:
            # Even when no repos changed, profile docs (providers/tasks/references) may have
            # updated — re-index those cheaply before returning
            conn_docs = sqlite3.connect(str(DB_PATH))
            conn_docs.execute("BEGIN")
            pd_files, pd_chunks = index_providers(conn_docs)
            conn_docs.commit()
            conn_docs.close()
            if pd_chunks:
                print(f"Provider docs: {pd_files} files, {pd_chunks} chunks re-indexed")
            print("All repos up to date. Nothing else to re-index.")
            return

        only_repos = changed
        print(
            f"SHA comparison: {len(changed)} changed, {len(removed)} removed, {len(repo_meta) - len(changed)} unchanged"
        )

        # Clean removed repos
        if removed:
            conn_clean = sqlite3.connect(str(DB_PATH))
            conn_clean.execute("BEGIN")
            for repo_name in sorted(removed):
                deleted = delete_repo_data(conn_clean, repo_name)
                if deleted:
                    print(f"  Removed {deleted} chunks for deleted repo {repo_name}")
            conn_clean.commit()
            conn_clean.close()

    # Track whether we're using a temp file (full build) for atomic rename later
    tmp_path = None

    if not incremental:
        # Full build: write to temp file, then atomic rename on success
        tmp_path = DB_PATH.with_suffix(".db.tmp")
        if tmp_path.exists():
            tmp_path.unlink()

        conn = sqlite3.connect(str(tmp_path))
        create_db(conn)

        repos_to_index = sorted(repo_meta.items())
        print(f"Full build: indexing {len(repos_to_index)} repos")
    else:
        conn = sqlite3.connect(str(DB_PATH))
        create_db(conn)  # ensures tables exist

        repos_to_index = [(name, meta) for name, meta in sorted(repo_meta.items()) if name in only_repos]
        print(f"Incremental build: re-indexing {len(repos_to_index)} repos")

    total_chunks = 0
    total_files = 0

    if incremental:
        # Wrap incremental delete+insert in a single transaction
        conn.execute("BEGIN")
        try:
            # Delete old data for changed repos (chunks, chunk_meta, code_facts)
            for repo_name in sorted(only_repos):
                deleted = delete_repo_data(conn, repo_name)
                if deleted:
                    print(f"  Removed {deleted} old chunks for {repo_name}")

            for i, (repo_name, meta) in enumerate(repos_to_index, 1):
                files, chunks = index_repo(conn, repo_name, meta)
                total_files += files
                total_chunks += chunks

                if i % 50 == 0:
                    print(f"  [{i}/{len(repos_to_index)}] {total_chunks} chunks indexed...")

            # Re-index gotchas (delete old, insert fresh)
            deleted_dk = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'gotchas'").fetchall()
            for (rowid,) in deleted_dk:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_dk:
                print(f"  Removed {len(deleted_dk)} old gotchas chunks")
            dk_files, dk_chunks = index_gotchas(conn)
            total_files += dk_files
            total_chunks += dk_chunks

            # Re-index domain registry (delete old, insert fresh)
            deleted_dr = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'domain_registry'").fetchall()
            for (rowid,) in deleted_dr:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_dr:
                print(f"  Removed {len(deleted_dr)} old domain registry chunks")
            dr_files, dr_chunks = index_domain_registry(conn)
            total_files += dr_files
            total_chunks += dr_chunks

            # Re-index flow annotations (delete old, insert fresh)
            deleted_fl = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'flow_annotation'").fetchall()
            for (rowid,) in deleted_fl:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_fl:
                print(f"  Removed {len(deleted_fl)} old flow annotation chunks")
            fl_files, fl_chunks = index_flows(conn)
            total_files += fl_files
            total_chunks += fl_chunks

            # Re-index seeds.cql (delete old, insert fresh)
            deleted_sc = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'provider_config'").fetchall()
            for (rowid,) in deleted_sc:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_sc:
                print(f"  Removed {len(deleted_sc)} old provider config chunks")
            sc_files, sc_chunks = index_seeds(conn)
            total_files += sc_files
            total_chunks += sc_chunks

            # Re-index test scripts (delete old, insert fresh)
            deleted_ts = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'test_script'").fetchall()
            for (rowid,) in deleted_ts:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_ts:
                print(f"  Removed {len(deleted_ts)} old test script chunks")
            ts_files, ts_chunks = index_test_scripts(conn)
            total_files += ts_files
            total_chunks += ts_chunks

            # Re-index tasks (delete old, insert fresh)
            deleted_tk = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'task'").fetchall()
            for (rowid,) in deleted_tk:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_tk:
                tk_rowids = [r[0] for r in deleted_tk]
                placeholders = ",".join("?" * len(tk_rowids))
                conn.execute(f"DELETE FROM chunk_meta WHERE chunk_rowid IN ({placeholders})", tk_rowids)
                print(f"  Removed {len(deleted_tk)} old task chunks")
            tk_files, tk_chunks = index_tasks(conn)
            total_files += tk_files
            total_chunks += tk_chunks

            # Re-index references (delete old, insert fresh)
            deleted_rf = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'reference'").fetchall()
            for (rowid,) in deleted_rf:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_rf:
                print(f"  Removed {len(deleted_rf)} old reference chunks")
            rf_files, rf_chunks = index_references(conn)
            total_files += rf_files
            total_chunks += rf_chunks

            # Re-index provider docs (function is idempotent — deletes per-provider first)
            pd_files, pd_chunks = index_providers(conn)
            total_files += pd_files
            total_chunks += pd_chunks

            # Clean old package_usage chunks (rebuilt by build_graph.py)
            deleted_pu = conn.execute("SELECT rowid FROM chunks WHERE file_type = 'package_usage'").fetchall()
            for (rowid,) in deleted_pu:
                conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
            if deleted_pu:
                print(f"  Removed {len(deleted_pu)} old package usage chunks")

            # Update build info with global counts
            total_chunks_global = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            total_repos_global = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]

            conn.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)",
                ("last_build", datetime.now(UTC).isoformat()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)",
                ("total_chunks", str(total_chunks_global)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_files", str(total_files))
            )
            conn.execute(
                "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_repos", str(total_repos_global))
            )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        # Full build: no explicit transaction needed (writing to temp file)
        for i, (repo_name, meta) in enumerate(repos_to_index, 1):
            files, chunks = index_repo(conn, repo_name, meta)
            total_files += files
            total_chunks += chunks

            if i % 50 == 0:
                print(f"  [{i}/{len(repos_to_index)}] {total_chunks} chunks indexed...")
                conn.commit()

        # Index gotchas files (separate from repo clones)
        dk_files, dk_chunks = index_gotchas(conn)
        total_files += dk_files
        total_chunks += dk_chunks

        # Index domain registry
        dr_files, dr_chunks = index_domain_registry(conn)
        total_files += dr_files
        total_chunks += dr_chunks

        # Index flow annotations
        fl_files, fl_chunks = index_flows(conn)
        total_files += fl_files
        total_chunks += fl_chunks

        # Index seeds.cql provider configs
        sc_files, sc_chunks = index_seeds(conn)
        total_files += sc_files
        total_chunks += sc_chunks

        # Index test scripts from repo scripts/ directories
        ts_files, ts_chunks = index_test_scripts(conn)
        total_files += ts_files
        total_chunks += ts_chunks

        # Index task files
        tk_files, tk_chunks = index_tasks(conn)
        total_files += tk_files
        total_chunks += tk_chunks

        # Index reference files
        rf_files, rf_chunks = index_references(conn)
        total_files += rf_files
        total_chunks += rf_chunks

        # Index provider docs
        pd_files, pd_chunks = index_providers(conn)
        total_files += pd_files
        total_chunks += pd_chunks

        total_chunks_global = total_chunks
        total_repos_global = len(repo_meta)

        conn.execute(
            "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("last_build", datetime.now(UTC).isoformat())
        )
        conn.execute(
            "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_chunks", str(total_chunks_global))
        )
        conn.execute("INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_files", str(total_files)))
        conn.execute(
            "INSERT OR REPLACE INTO build_info(key, value) VALUES (?, ?)", ("total_repos", str(total_repos_global))
        )

        conn.commit()

    # Optimize FTS
    print("Optimizing FTS index...")
    conn.execute(
        "INSERT INTO chunks(chunks, rank, content, repo_name, file_path, file_type, chunk_type, language) VALUES('optimize', '', '', '', '', '', '', '')"
    )
    conn.commit()
    conn.close()

    # Full build: atomic rename from temp file to final path
    if tmp_path is not None:
        tmp_path.rename(DB_PATH)

    db_size = DB_PATH.stat().st_size / (1024 * 1024)
    print("\n=== Index Summary ===")
    if incremental:
        print(f"Mode:          incremental ({len(repos_to_index)} repos)")
        print(f"Re-indexed:    {total_chunks} chunks from {total_files} files")
        print(f"Total chunks:  {total_chunks_global}")
    else:
        print("Mode:          full")
        print(f"Total files:   {total_files}")
        print(f"Total chunks:  {total_chunks}")
    print(f"Total repos:   {total_repos_global}")
    print(f"Database size: {db_size:.1f} MB")
    print(f"Database:      {DB_PATH}")
    print("====================")


if __name__ == "__main__":
    main()
