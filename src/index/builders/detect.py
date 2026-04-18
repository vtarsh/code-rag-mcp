"""File-type and language detection helpers."""

from __future__ import annotations

from pathlib import Path


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
