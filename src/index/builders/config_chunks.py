"""Config-file chunkers — JSON (package.json), YAML, env."""

from __future__ import annotations

import json

from ._common import MAX_CHUNK, MIN_CHUNK


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
