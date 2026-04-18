"""Protobuf chunking — split by message/service/enum/rpc definitions."""

from __future__ import annotations

import re

from ._common import MIN_CHUNK


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
