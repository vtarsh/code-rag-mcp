"""Tests for two-tower embedding foundation (2026-04-23).

Covers:
- src/models.py — "docs" key config
- src/embedding_provider.py — per-key singletons, document_prefix, loaded_provider_names()
- src/container.py — per-key LanceDB caching (get_vector_search(model_key))

SentenceTransformer + LanceDB are mocked — we validate wiring, not real inference.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_provider_state():
    """Reset embedding provider + container lance-table caches between tests."""
    from src import container, embedding_provider

    embedding_provider._embedding_providers = {}
    embedding_provider._embedding_provider = None
    embedding_provider._reranker_provider = None
    container._lance_tables = {}
    container._lance_table = None
    yield
    embedding_provider._embedding_providers = {}
    embedding_provider._embedding_provider = None
    embedding_provider._reranker_provider = None
    container._lance_tables = {}
    container._lance_table = None


class TestModelsRegistry:
    def test_docs_key_registered(self):
        from src.models import DOCS_MODEL, EMBEDDING_MODELS

        assert DOCS_MODEL == "docs"
        assert "docs" in EMBEDDING_MODELS

    def test_docs_config_has_expected_fields(self):
        from src.models import get_model_config

        cfg = get_model_config("docs")
        assert cfg.key == "docs"
        assert cfg.name == "nomic-ai/nomic-embed-text-v1.5"
        assert cfg.dim == 768
        assert cfg.query_prefix == "search_query: "
        assert cfg.document_prefix == "search_document: "
        assert cfg.lance_dir == "vectors.lance.docs"
        assert cfg.trust_remote_code is True

    def test_coderank_config_unchanged(self):
        """Foundation must not change the code tower's behaviour."""
        from src.models import get_model_config

        cfg = get_model_config("coderank")
        assert cfg.name == "nomic-ai/CodeRankEmbed"
        assert cfg.dim == 768
        assert cfg.lance_dir == "vectors.lance.coderank"
        assert cfg.document_prefix == ""

    def test_default_fallback(self):
        from src.models import DEFAULT_MODEL, get_model_config

        assert DEFAULT_MODEL == "coderank"
        assert get_model_config(None).key == "coderank"
        assert get_model_config("nonexistent").key == "coderank"


class TestEmbeddingProviderPerKey:
    def test_per_key_singletons_are_distinct(self):
        from src.embedding_provider import get_embedding_provider

        code_provider, _ = get_embedding_provider("coderank")
        docs_provider, _ = get_embedding_provider("docs")

        assert code_provider is not docs_provider
        assert code_provider.provider_name == "local:coderank"
        assert docs_provider.provider_name == "local:docs"

    def test_per_key_caching(self):
        from src.embedding_provider import get_embedding_provider

        first, _ = get_embedding_provider("docs")
        second, _ = get_embedding_provider("docs")
        assert first is second

    def test_default_key_uses_configured_default(self):
        from src.embedding_provider import get_embedding_provider

        default_provider, _ = get_embedding_provider()
        assert default_provider.provider_name == "local:coderank"

    def test_legacy_singleton_points_at_first_loaded(self):
        """daemon.py /health reads the module-level _embedding_provider var."""
        from src import embedding_provider
        from src.embedding_provider import get_embedding_provider

        assert embedding_provider._embedding_provider is None
        docs_provider, _ = get_embedding_provider("docs")
        assert embedding_provider._embedding_provider is docs_provider
        # Later loads do NOT overwrite the legacy alias — first wins.
        code_provider, _ = get_embedding_provider("coderank")
        assert embedding_provider._embedding_provider is docs_provider
        assert code_provider is not docs_provider


class TestDocumentPrefixRouting:
    def test_query_prefix_applied_for_query_task(self):
        from src.embedding_provider import LocalEmbeddingProvider

        p = LocalEmbeddingProvider(model_key="docs")
        fake_model = MagicMock()
        fake_model.encode.return_value = [MagicMock(tolist=lambda: [0.1] * 768)]
        with patch("sentence_transformers.SentenceTransformer", return_value=fake_model):
            p.embed(["payout flow"], task_type="query")
        fake_model.encode.assert_called_once()
        (called_texts,), _ = fake_model.encode.call_args
        assert called_texts == ["search_query: payout flow"]

    def test_document_prefix_applied_for_document_task(self):
        from src.embedding_provider import LocalEmbeddingProvider

        p = LocalEmbeddingProvider(model_key="docs")
        fake_model = MagicMock()
        fake_model.encode.return_value = [MagicMock(tolist=lambda: [0.1] * 768)]
        with patch("sentence_transformers.SentenceTransformer", return_value=fake_model):
            p.embed(["how nuvei processes refunds"], task_type="document")
        (called_texts,), _ = fake_model.encode.call_args
        assert called_texts == ["search_document: how nuvei processes refunds"]

    def test_coderank_does_not_add_document_prefix(self):
        """coderank has empty document_prefix — should pass text through."""
        from src.embedding_provider import LocalEmbeddingProvider

        p = LocalEmbeddingProvider(model_key="coderank")
        fake_model = MagicMock()
        fake_model.encode.return_value = [MagicMock(tolist=lambda: [0.1] * 768)]
        with patch("sentence_transformers.SentenceTransformer", return_value=fake_model):
            p.embed(["def foo(): pass"], task_type="document")
        (called_texts,), _ = fake_model.encode.call_args
        assert called_texts == ["def foo(): pass"]


class TestLoadedProviderNames:
    def test_empty_when_no_models_loaded(self):
        from src.embedding_provider import loaded_provider_names

        assert loaded_provider_names() == []

    def test_lists_only_providers_with_resident_models(self):
        from src.embedding_provider import get_embedding_provider, loaded_provider_names

        get_embedding_provider("coderank")
        get_embedding_provider("docs")
        assert loaded_provider_names() == []

        from src import embedding_provider as ep_mod

        ep_mod._embedding_providers["docs"]._model = MagicMock()  # type: ignore[attr-defined]
        names = loaded_provider_names()
        assert names == ["local:docs"]


class TestResetProviders:
    def test_reset_clears_all_keys(self):
        from src import embedding_provider as ep_mod
        from src.embedding_provider import get_embedding_provider, reset_providers

        get_embedding_provider("coderank")
        get_embedding_provider("docs")
        ep_mod._embedding_providers["coderank"]._model = MagicMock()  # type: ignore[attr-defined]
        ep_mod._embedding_providers["docs"]._model = MagicMock()  # type: ignore[attr-defined]

        reset_providers()

        assert ep_mod._embedding_providers == {}
        assert ep_mod._embedding_provider is None
        assert ep_mod._reranker_provider is None


class TestIsModelLoaded:
    def test_false_before_any_provider(self):
        from src.container import is_model_loaded

        assert is_model_loaded() is False

    def test_true_once_a_provider_is_created(self):
        from src.container import is_model_loaded
        from src.embedding_provider import get_embedding_provider

        get_embedding_provider("docs")
        assert is_model_loaded() is True


class TestVectorSearchPerKey:
    def test_docs_key_points_at_docs_lance_dir(self, tmp_path, monkeypatch):
        """get_vector_search('docs') should probe db/vectors.lance.docs/."""
        from src import container

        fake_table = MagicMock()
        fake_db = MagicMock()
        fake_db.open_table.return_value = fake_table
        fake_lancedb = MagicMock()
        fake_lancedb.connect.return_value = fake_db

        docs_lance = tmp_path / "db" / "vectors.lance.docs"
        docs_lance.mkdir(parents=True)
        code_lance = tmp_path / "db" / "vectors.lance.coderank"
        code_lance.mkdir(parents=True)

        monkeypatch.setattr(container, "DB_PATH", tmp_path / "db" / "knowledge.db")

        with patch.dict("sys.modules", {"lancedb": fake_lancedb}):
            provider, table, err = container.get_vector_search("docs")
            assert err is None
            fake_lancedb.connect.assert_called_with(str(docs_lance))
            assert table is fake_table
            assert provider.provider_name == "local:docs"

    def test_missing_lance_dir_returns_error(self, tmp_path, monkeypatch):
        from src import container

        monkeypatch.setattr(container, "DB_PATH", tmp_path / "db" / "knowledge.db")
        # vectors.lance.docs is NOT created — should return informative error
        _, table, err = container.get_vector_search("docs")
        assert table is None
        assert err is not None
        assert "vectors.lance.docs" in err
        assert "--model docs" in err

    def test_cache_returns_same_table_for_repeat_calls(self, tmp_path, monkeypatch):
        from src import container

        fake_table = MagicMock()
        fake_db = MagicMock()
        fake_db.open_table.return_value = fake_table
        fake_lancedb = MagicMock()
        fake_lancedb.connect.return_value = fake_db

        (tmp_path / "db" / "vectors.lance.docs").mkdir(parents=True)
        monkeypatch.setattr(container, "DB_PATH", tmp_path / "db" / "knowledge.db")

        with patch.dict("sys.modules", {"lancedb": fake_lancedb}):
            _, table_a, _ = container.get_vector_search("docs")
            _, table_b, _ = container.get_vector_search("docs")
            assert table_a is table_b
            # Only one connect — second call hit the cache.
            assert fake_lancedb.connect.call_count == 1
