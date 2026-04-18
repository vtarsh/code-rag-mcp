"""Embedding and reranking provider abstraction.

Local-only: SentenceTransformer (CodeRankEmbed / MiniLM) + CrossEncoder reranker.

Usage:
    provider, _ = get_embedding_provider()
    vectors = provider.embed(["some code snippet"])

    reranker, _ = get_reranker_provider()
    scores = reranker.rerank("query", ["doc1", "doc2", ...])
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

log = logging.getLogger(__name__)


# --- Protocols ---


class EmbeddingProvider(Protocol):
    """Interface for embedding backends."""

    @property
    def provider_name(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str], task_type: str = "query") -> list[list[float]]:
        """Embed texts. task_type: 'query' or 'document'."""
        ...


class RerankerProvider(Protocol):
    """Interface for reranking backends."""

    @property
    def provider_name(self) -> str: ...

    def rerank(self, query: str, documents: list[str], limit: int = 10) -> list[float]:
        """Score documents against query. Returns relevance scores (higher = better)."""
        ...


# --- Local providers ---


class LocalEmbeddingProvider:
    """SentenceTransformer embedding — lazy loaded on first use."""

    def __init__(self, model_key: str = "coderank"):
        self._model_key = model_key
        self._model = None
        self._cfg = None

    @property
    def provider_name(self) -> str:
        return f"local:{self._model_key}"

    @property
    def dim(self) -> int:
        from src.models import get_model_config

        return get_model_config(self._model_key).dim

    def _ensure_model(self) -> None:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            from src.models import get_model_config

            cfg = get_model_config(self._model_key)
            log.info(f"Loading embedding model: {cfg.name}")
            self._model = SentenceTransformer(cfg.name, trust_remote_code=cfg.trust_remote_code)
            self._cfg = cfg

    def embed(self, texts: list[str], task_type: str = "query") -> list[list[float]]:
        self._ensure_model()
        if task_type == "query" and self._cfg and self._cfg.query_prefix:
            texts = [f"{self._cfg.query_prefix}{t}" for t in texts]
        assert self._model is not None
        vectors = self._model.encode(texts)
        return [v.tolist() for v in vectors]


class LocalRerankerProvider:
    """CrossEncoder reranker — lazy loaded on first use.

    Model resolution: CODERANK_RERANK_MODEL env var > config.reranker_model > default.
    Short names (no "/") auto-prefix "cross-encoder/" for HuggingFace compatibility.
    """

    DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __init__(self, model_name: str | None = None):
        import os

        self._model = None
        self._model_name = model_name or self._resolve_model_name(os.environ.get("CODERANK_RERANK_MODEL"))

    @classmethod
    def _resolve_model_name(cls, env_override: str | None) -> str:
        if env_override:
            env_override = env_override.strip()
            return env_override if "/" in env_override else f"cross-encoder/{env_override}"
        try:
            from src.config import RERANKER_MODEL
        except ImportError:
            RERANKER_MODEL = ""
        cfg = (RERANKER_MODEL or "").strip()
        # Guard: ignore legacy Gemini model IDs that may still sit in stale configs.
        if cfg and not cfg.lower().startswith("gemini"):
            return cfg if "/" in cfg else f"cross-encoder/{cfg}"
        return cls.DEFAULT_MODEL

    @property
    def provider_name(self) -> str:
        return f"local:{self._model_name}"

    def _ensure_model(self) -> None:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            log.info(f"Loading reranker model: {self._model_name}")
            self._model = CrossEncoder(self._model_name)

    def rerank(self, query: str, documents: list[str], limit: int = 10) -> list[float]:
        self._ensure_model()
        assert self._model is not None
        pairs = [(query, doc) for doc in documents]
        scores = self._model.predict(pairs)
        return [float(s) for s in scores]


# --- Singletons ---

_embedding_provider: EmbeddingProvider | None = None
_reranker_provider: RerankerProvider | None = None
_provider_lock = threading.Lock()


def get_embedding_provider() -> tuple[EmbeddingProvider, str | None]:
    """Get the active embedding provider. Returns (provider, None)."""
    global _embedding_provider

    with _provider_lock:
        if _embedding_provider is None:
            _embedding_provider = LocalEmbeddingProvider()
        return _embedding_provider, None


def get_reranker_provider() -> tuple[RerankerProvider, str | None]:
    """Get the active reranker provider. Returns (provider, None)."""
    global _reranker_provider

    with _provider_lock:
        if _reranker_provider is None:
            _reranker_provider = LocalRerankerProvider()
        return _reranker_provider, None


def _reset_providers_locked() -> None:
    """Reset cached providers while _provider_lock is already held. Unloads local models to free RAM."""
    global _embedding_provider, _reranker_provider
    import gc

    need_gc = False
    if isinstance(_embedding_provider, LocalEmbeddingProvider) and _embedding_provider._model is not None:
        log.info("Unloading embedding model to free RAM")
        del _embedding_provider._model
        _embedding_provider._model = None
        need_gc = True
    if isinstance(_reranker_provider, LocalRerankerProvider) and _reranker_provider._model is not None:
        log.info("Unloading reranker model to free RAM")
        del _reranker_provider._model
        _reranker_provider._model = None
        need_gc = True
    if need_gc:
        gc.collect()
    _embedding_provider = None
    _reranker_provider = None


def reset_providers() -> None:
    """Reset cached providers. Unloads local models to free RAM."""
    with _provider_lock:
        _reset_providers_locked()
