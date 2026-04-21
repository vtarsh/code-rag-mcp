"""JS/TS code chunking — semantic regex-based chunker plus fallback splitter."""

from __future__ import annotations

import re
from pathlib import Path

from ._common import MAX_CHUNK, MIN_CHUNK


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

    # Try smart chunking for JS/TS files (incl. JSX/TSX)
    ext = Path(file_path).suffix.lower() if file_path else ""
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
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
