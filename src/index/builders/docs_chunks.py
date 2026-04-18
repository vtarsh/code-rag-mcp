"""Markdown chunkers — generic doc sections + task-aware chunking."""

from __future__ import annotations

import re

from ._common import MAX_CHUNK, MIN_CHUNK


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
