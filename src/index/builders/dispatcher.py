"""Top-level chunk_file dispatcher — routes a file to the right chunker by language."""

from __future__ import annotations

from pathlib import Path

from ._common import MAX_CHUNK, MIN_CHUNK
from .code_chunks import chunk_code
from .config_chunks import chunk_env, chunk_json, chunk_yaml
from .detect import detect_language
from .docs_chunks import chunk_markdown
from .proto_chunks import chunk_proto


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
