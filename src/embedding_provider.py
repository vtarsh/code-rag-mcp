"""Embedding and reranking provider abstraction.

API-first with lazy local fallback. Providers:
- GeminiEmbeddingProvider: Gemini embedding-001 via API
- LocalEmbeddingProvider: SentenceTransformer (CodeRankEmbed or MiniLM)
- GeminiRerankerProvider: Gemini LLM listwise reranking via generateContent
- LocalRerankerProvider: CrossEncoder (MiniLM-L-6-v2)

Usage:
    provider = get_embedding_provider()
    vectors = provider.embed(["some code snippet"])

    reranker = get_reranker_provider()
    scores = reranker.rerank("query", ["doc1", "doc2", ...])
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Protocol

import numpy as np

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


# --- Gemini Embedding Provider ---


class GeminiEmbeddingProvider:
    """Gemini embedding-001 via Google AI Studio API."""

    def __init__(self, api_key: str, model: str = "gemini-embedding-001", dim: int = 768):
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._dim = dim

    @property
    def provider_name(self) -> str:
        return f"gemini:{self._model}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str], task_type: str = "query") -> list[list[float]]:
        from src.api_costs import ApiCostRecord, estimate_cost, log_api_cost

        # RETRIEVAL_QUERY for NL→doc search (not CODE_RETRIEVAL_QUERY which is code→code)
        gemini_task = "RETRIEVAL_QUERY" if task_type == "query" else "RETRIEVAL_DOCUMENT"

        t0 = time.time()
        all_vectors: list[list[float]] = []

        # Batch in groups of 50 (API limit), with retry and rate limiting
        for i in range(0, len(texts), 50):
            batch = texts[i : i + 50]
            # Truncate each text to ~2048 tokens (~8000 chars) for embedding-001 limit
            batch = [t[:8000] for t in batch]

            # Rate limit: max ~1200 RPM (well under 3000 RPM limit)
            if i > 0:
                time.sleep(0.05)

            for attempt in range(3):
                try:
                    result = self._client.models.embed_content(
                        model=self._model,
                        contents=batch,
                        config={"task_type": gemini_task, "output_dimensionality": self._dim},
                    )
                    for emb in result.embeddings:
                        vec = list(emb.values)
                        # Normalize for truncated dimensions (MRL requires normalization)
                        norm = np.linalg.norm(vec)
                        if norm > 0:
                            vec = (np.array(vec) / norm).tolist()
                        all_vectors.append(vec)
                    notify_api_success()
                    break
                except Exception as e:
                    notify_api_error()
                    if attempt < 2:
                        # Longer wait for rate limits (429)
                        is_rate_limit = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
                        wait = 15 if is_rate_limit else (attempt + 1) * 2
                        log.warning(f"Gemini embed batch failed (attempt {attempt + 1}): {e}, retrying in {wait}s")
                        time.sleep(wait)
                    else:
                        # All retries exhausted — fallback to local
                        log.error(f"Gemini embedding failed after 3 attempts: {e}. Falling back to local.")
                        local = LocalEmbeddingProvider()
                        return local.embed(texts, task_type)

        duration_ms = (time.time() - t0) * 1000
        input_tokens = sum(len(t) // 4 for t in texts)  # rough estimate
        cost = estimate_cost(self._model, input_tokens)
        log_api_cost(ApiCostRecord(
            provider="gemini-embedding",
            model=self._model,
            operation=f"embed_{task_type}",
            input_tokens=input_tokens,
            estimated_cost_usd=cost,
            duration_ms=duration_ms,
        ))

        return all_vectors


# --- Gemini LLM Reranker ---


class GeminiRerankerProvider:
    """Gemini LLM listwise reranking via generateContent."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    @property
    def provider_name(self) -> str:
        return f"gemini-rerank:{self._model}"

    def rerank(self, query: str, documents: list[str], limit: int = 10) -> list[float]:
        from src.api_costs import ApiCostRecord, estimate_cost, log_api_cost

        if not documents:
            return []

        # Build listwise prompt
        doc_list = ""
        for i, doc in enumerate(documents):
            # Truncate each doc to ~500 chars to fit context
            truncated = doc[:500].replace("\n", " ")
            doc_list += f"[{i}] {truncated}\n"

        prompt = f"""Rate the relevance of each document to the query. Return ONLY a JSON array of scores (0.0 to 1.0) in the same order as the documents.

Query: {query}

Documents:
{doc_list}

Return JSON array of {len(documents)} float scores, e.g. [0.95, 0.2, 0.8, ...]. Nothing else."""

        t0 = time.time()
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                config={"temperature": 0.0},
            )
            text = response.text.strip()
            # Parse JSON array from response
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            # Try direct parse, then regex extraction
            try:
                scores = json.loads(text)
            except json.JSONDecodeError:
                match = re.search(r"\[[\d.,\s]+\]", text)
                if match:
                    scores = json.loads(match.group())
                else:
                    log.warning(f"Gemini reranker returned unparseable response: {text[:200]}")
                    return [0.5] * len(documents)

            # Ensure we have the right number of scores
            if len(scores) != len(documents):
                log.warning(f"Gemini reranker returned {len(scores)} scores for {len(documents)} docs")
                # Pad or truncate
                scores = (scores + [0.0] * len(documents))[:len(documents)]

            scores = [float(s) for s in scores]
            notify_api_success()

        except Exception as e:
            notify_api_error()
            log.warning(f"Gemini reranker failed: {e}, falling back to local")
            local = LocalRerankerProvider()
            return local.rerank(query, documents, limit)

        duration_ms = (time.time() - t0) * 1000
        input_tokens = len(prompt) // 4
        output_tokens = len(documents) * 3  # ~3 tokens per score
        cost = estimate_cost(self._model, input_tokens, output_tokens)
        log_api_cost(ApiCostRecord(
            provider="gemini-rerank",
            model=self._model,
            operation="rerank",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            duration_ms=duration_ms,
        ))

        return scores


# --- Local Providers (lazy-loaded fallback) ---


class LocalEmbeddingProvider:
    """SentenceTransformer embedding — lazy loaded on first use."""

    def __init__(self, model_key: str = "coderank"):
        self._model_key = model_key
        self._model = None

    @property
    def provider_name(self) -> str:
        return f"local:{self._model_key}"

    @property
    def dim(self) -> int:
        from src.models import get_model_config
        return get_model_config(self._model_key).dim

    def _ensure_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            from src.models import get_model_config

            log.warning("Loading local embedding model (Gemini API unavailable)")
            cfg = get_model_config(self._model_key)
            self._model = SentenceTransformer(cfg.name, trust_remote_code=cfg.trust_remote_code)
            self._cfg = cfg

    def embed(self, texts: list[str], task_type: str = "query") -> list[list[float]]:
        self._ensure_model()
        if task_type == "query" and self._cfg.query_prefix:
            texts = [f"{self._cfg.query_prefix}{t}" for t in texts]
        vectors = self._model.encode(texts)
        return [v.tolist() for v in vectors]


class LocalRerankerProvider:
    """CrossEncoder MiniLM — lazy loaded on first use."""

    def __init__(self):
        self._model = None

    @property
    def provider_name(self) -> str:
        return "local:ms-marco-MiniLM-L-6-v2"

    def _ensure_model(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            log.warning("Loading local reranker model (Gemini API unavailable)")
            self._model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

    def rerank(self, query: str, documents: list[str], limit: int = 10) -> list[float]:
        self._ensure_model()
        pairs = [(query, doc) for doc in documents]
        scores = self._model.predict(pairs)
        return [float(s) for s in scores]


# --- Provider Factory ---

_embedding_provider: EmbeddingProvider | None = None
_reranker_provider: RerankerProvider | None = None
_fallback_warning: str | None = None
_api_error_count: int = 0
_API_ERROR_THRESHOLD = 3  # Reset provider after this many consecutive API errors
_fallback_since: float = 0  # timestamp when we fell back to local
_RETRY_API_AFTER = 300  # try Gemini again after 5 minutes on local fallback


def notify_api_error() -> None:
    """Called when an API provider fails. After threshold, resets providers to re-evaluate."""
    global _api_error_count
    _api_error_count += 1
    if _api_error_count >= _API_ERROR_THRESHOLD:
        log.warning(f"Gemini API failed {_api_error_count} times, resetting providers")
        reset_providers()


def notify_api_success() -> None:
    """Called on successful API call. Resets error counter."""
    global _api_error_count
    _api_error_count = 0


def get_embedding_provider() -> tuple[EmbeddingProvider, str | None]:
    """Get the active embedding provider. Returns (provider, warning_or_None).

    Tries Gemini API first, falls back to local if unavailable.
    """
    global _embedding_provider, _fallback_warning

    if _embedding_provider is not None:
        # If on local fallback, periodically retry API
        if (
            _fallback_warning
            and _fallback_since > 0
            and time.time() - _fallback_since > _RETRY_API_AFTER
        ):
            log.info("Retrying Gemini API after fallback period...")
            reset_providers()
            return get_embedding_provider()
        return _embedding_provider, _fallback_warning

    from src.config import EMBEDDING_PROVIDER, GEMINI_API_KEY

    if EMBEDDING_PROVIDER == "local":
        _embedding_provider = LocalEmbeddingProvider()
        return _embedding_provider, None

    if EMBEDDING_PROVIDER in ("gemini", "auto") and GEMINI_API_KEY:
        try:
            provider = GeminiEmbeddingProvider(GEMINI_API_KEY)
            # Quick test to verify API works
            provider.embed(["test"], task_type="query")
            _embedding_provider = provider
            log.info(f"Embedding provider: {provider.provider_name}")
            return _embedding_provider, None
        except Exception as e:
            if EMBEDDING_PROVIDER == "gemini":
                # Explicitly requested gemini but it failed
                log.error(f"Gemini embedding failed: {e}")
            else:
                log.warning(f"Gemini embedding unavailable ({e}), falling back to local")

    # Fallback to local
    _embedding_provider = LocalEmbeddingProvider()
    _fallback_warning = "Gemini API unavailable, using local models"
    _fallback_since = time.time()
    log.warning(_fallback_warning)
    return _embedding_provider, _fallback_warning


def get_reranker_provider() -> tuple[RerankerProvider, str | None]:
    """Get the active reranker provider. Returns (provider, warning_or_None)."""
    global _reranker_provider

    if _reranker_provider is not None:
        return _reranker_provider, _fallback_warning

    from src.config import EMBEDDING_PROVIDER, GEMINI_API_KEY, RERANKER_MODEL

    if EMBEDDING_PROVIDER == "local":
        _reranker_provider = LocalRerankerProvider()
        return _reranker_provider, None

    if EMBEDDING_PROVIDER in ("gemini", "auto") and GEMINI_API_KEY:
        try:
            provider = GeminiRerankerProvider(GEMINI_API_KEY, model=RERANKER_MODEL)
            _reranker_provider = provider
            log.info(f"Reranker provider: {provider.provider_name}")
            return _reranker_provider, None
        except Exception as e:
            log.warning(f"Gemini reranker unavailable ({e}), falling back to local")

    _reranker_provider = LocalRerankerProvider()
    return _reranker_provider, _fallback_warning


def reset_providers() -> None:
    """Reset cached providers. Unloads local models to free RAM."""
    global _embedding_provider, _reranker_provider, _fallback_warning, _api_error_count
    # Unload local models if they were loaded as fallback
    if isinstance(_embedding_provider, LocalEmbeddingProvider) and _embedding_provider._model is not None:
        log.info("Unloading local embedding model to free RAM")
        del _embedding_provider._model
        _embedding_provider._model = None
    if isinstance(_reranker_provider, LocalRerankerProvider) and _reranker_provider._model is not None:
        log.info("Unloading local reranker model to free RAM")
        del _reranker_provider._model
        _reranker_provider._model = None
    _embedding_provider = None
    _reranker_provider = None
    _api_error_count = 0
    _fallback_warning = None
