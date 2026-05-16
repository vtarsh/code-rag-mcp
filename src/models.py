"""Embedding model registry — supported models and their configurations.

Add new models here. Select at runtime via profile config or CODE_RAG_MODEL env var.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingModel:
    """Configuration for an embedding model."""

    key: str
    name: str  # HuggingFace model name
    dim: int
    query_prefix: str  # Prepended to queries at search time (empty = no prefix)
    trust_remote_code: bool
    batch_size: int  # For short chunks (<= short_limit chars)
    short_limit: int  # Max chars for batch encoding
    long_limit: int  # Max chars for single-item encoding
    lance_dir: str  # Subdirectory under db/ for this model's vectors
    description: str  # Shown in setup wizard
    document_prefix: str = ""  # Prepended to documents at index time (empty = no prefix)


EMBEDDING_MODELS: dict[str, EmbeddingModel] = {
    "coderank": EmbeddingModel(
        key="coderank",
        name="nomic-ai/CodeRankEmbed",
        dim=768,
        query_prefix="Represent this query for searching relevant code: ",
        trust_remote_code=True,
        batch_size=32,
        short_limit=1500,
        long_limit=8000,
        lance_dir="vectors.lance.coderank",
        description="SOTA code embeddings by Nomic. Best for code search. ~230MB RAM.",
    ),
    "minilm": EmbeddingModel(
        key="minilm",
        name="all-MiniLM-L6-v2",
        dim=384,
        query_prefix="",
        trust_remote_code=False,
        batch_size=64,
        short_limit=1500,
        long_limit=1500,
        lance_dir="vectors.lance",
        description="Lightweight general-purpose embeddings. Faster, ~80MB RAM.",
    ),
    # Two-tower docs tower (2026-04-23): v12a single-tower FT rejected after 12
    # iterations because CodeRankEmbed was trained on code only — docs ended up
    # with near-random embeddings. nomic-embed-text-v1.5 is trained for general
    # text retrieval and matches CodeRankEmbed at 768-dim, so downstream RRF
    # merge doesn't need dimension alignment tricks.
    "docs": EmbeddingModel(
        key="docs",
        name="nomic-ai/nomic-embed-text-v1.5",
        dim=768,
        query_prefix="search_query: ",
        document_prefix="search_document: ",
        trust_remote_code=True,
        batch_size=16,
        short_limit=2000,
        long_limit=8000,
        lance_dir="vectors.lance.docs",
        description="General-text embeddings for docs tower. ~550MB RAM.",
    ),
}

DEFAULT_MODEL = "coderank"
DOCS_MODEL = "docs"


def get_model_config(key: str | None = None) -> EmbeddingModel:
    """Get model config by key. Returns default if key is None or invalid."""
    if key and key in EMBEDDING_MODELS:
        return EMBEDDING_MODELS[key]
    return EMBEDDING_MODELS[DEFAULT_MODEL]
