"""Markdown chunkers — generic doc sections + task-aware chunking."""

from __future__ import annotations

import hashlib
import re

from ._common import MAX_CHUNK, MIN_CHUNK

# Minimum body length (after stripping [Repo: X] / [Provider Docs: X] prefix)
# required to keep a doc_section chunk. Prevents orphan headings / TODO markers
# from polluting the index. Intentionally > MIN_CHUNK because the prefix
# (~25-40 chars) previously let bodies as small as 15 chars squeak through.
MIN_DOC_BODY = 120

# Regex matching the "[Repo: X]" / "[... Docs: X]" prefix used for doc chunks.
# Shared by chunk_markdown and downstream dedup/filter passes.
_DOC_PREFIX_RE = re.compile(r"^\s*\[(?:Repo|[^]]+?Docs):\s*[^\]]+\]\s*")


def _body_only(content: str) -> str:
    """Strip the '[Repo: X]' / '[Provider Docs: X]' prefix from chunk content."""
    return _DOC_PREFIX_RE.sub("", content)


def content_hash(content: str) -> str:
    """Stable 16-char md5 of the chunk body (prefix-independent).

    Used by the doc indexers to skip boilerplate duplicates where the same body
    appears under ``[Repo: A]`` and ``[Repo: B]`` prefixes. Scope the dedup per
    file_type upstream so a generic body like "### Responses\n\nOK" doesn't
    shadow a gotcha entry that happens to match.
    """
    body = _body_only(content)
    return hashlib.md5(body.encode("utf-8")).hexdigest()[:16]


def _subsplit_oversized(section_text: str, max_chars: int = MAX_CHUNK, overlap: int = 400) -> list[str]:
    """Split an oversized markdown section on paragraph boundaries with overlap.

    - Sections <= max_chars pass through unchanged.
    - Otherwise split on blank lines; accumulate paragraphs up to max_chars.
    - When flushing, keep trailing paragraphs up to ~overlap chars as the head
      of the next chunk so cross-chunk context is preserved.
    - A single paragraph larger than max_chars is hard-char-split with overlap.
    """
    if len(section_text) <= max_chars:
        return [section_text]
    paragraphs = re.split(r"\n\n+", section_text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current_len + len(para) + 2 > max_chars and current:
            chunks.append("\n\n".join(current))
            # overlap: keep last paragraph(s) up to `overlap` chars
            tail: list[str] = []
            tail_len = 0
            for p in reversed(current):
                if tail_len + len(p) > overlap:
                    break
                tail.insert(0, p)
                tail_len += len(p)
            current = tail
            current_len = tail_len
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    # Final safety: hard-split any chunk that still exceeds max_chars
    # (happens when a single paragraph is larger than max_chars).
    out: list[str] = []
    step = max(1, max_chars - overlap)
    for ch in chunks:
        if len(ch) <= max_chars:
            out.append(ch)
        else:
            for i in range(0, len(ch), step):
                out.append(ch[i : i + max_chars])
    return out


def chunk_markdown(content: str, repo_name: str) -> list[dict]:
    """Chunk markdown by header sections.

    Behavior:
    - Header split on ^#{1,3}\\s+ (unchanged).
    - Each emitted section is subsplit with :func:`_subsplit_oversized` so no
      single chunk exceeds MAX_CHUNK. This unlocks provider mega-dumps
      (Plaid, Credorax, Skrill, EVO) for the reranker.
    - Orphan-heading filter: chunks whose *body* (content minus
      ``[Repo: X]``/``[Docs: X]`` prefix) is shorter than :data:`MIN_DOC_BODY`
      are dropped. Protects against headings-with-links-only and tiny TODOs.
    """
    chunks: list[dict] = []
    content = re.sub(r"\A---\n.*?\n---\n?", "", content, count=1, flags=re.DOTALL)
    sections = re.split(r"^(#{1,3}\s+.+)$", content, flags=re.MULTILINE)

    current_header = ""
    current_content: list[str] = []

    def _emit(section_text: str, chunk_type: str) -> None:
        # MIN_CHUNK is a cheap pre-filter; MIN_DOC_BODY is the real gate.
        if len(section_text) < MIN_CHUNK:
            return
        body_len = len(_body_only(section_text).strip())
        if body_len < MIN_DOC_BODY:
            return
        for piece in _subsplit_oversized(section_text):
            # Re-check body length after subsplit (a piece may be mostly overlap).
            piece_body_len = len(_body_only(piece).strip())
            if piece_body_len < MIN_DOC_BODY:
                continue
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] {piece}",
                    "chunk_type": chunk_type,
                }
            )

    for part in sections:
        if re.match(r"^#{1,3}\s+", part):
            # Save previous section
            if current_content:
                text = (current_header + "\n" + "\n".join(current_content)).strip()
                _emit(text, "doc_section")
            current_header = part
            current_content = []
        else:
            current_content.append(part)

    # Last section
    if current_content or current_header:
        text = (current_header + "\n" + "\n".join(current_content)).strip()
        _emit(text, "doc_section")

    if not chunks and len(content.strip()) >= MIN_CHUNK:
        body = content.strip()
        if len(body) >= MIN_DOC_BODY:
            for piece in _subsplit_oversized(body):
                if len(piece.strip()) >= MIN_DOC_BODY:
                    chunks.append(
                        {
                            "content": f"[Repo: {repo_name}] {piece}",
                            "chunk_type": "doc_file",
                        }
                    )

    return chunks


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
