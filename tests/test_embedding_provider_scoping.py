"""Regression tests for src/embedding_provider.py global-scoping bugs.

Context: logs/tool_calls.jsonl observed 6x failures with

    cannot access local variable '_fallback_since' where it is not associated with a value

on /tool/search between 2026-04-05 09:47 and 13:48. Root cause: the
`global` declaration inside get_embedding_provider() listed only
`_embedding_provider, _fallback_warning` — not `_fallback_since` — so the
assignment at the bottom of the function caused Python to infer
`_fallback_since` as a local, and the earlier read inside the retry
block raised UnboundLocalError.

Fixed in commit 26fbc43 by adding `_fallback_since` to the global
declaration. These tests lock in the fix so it can't regress silently.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch


def _reset_module_state():
    """Clear cached globals so each test starts from a clean slate."""
    import src.embedding_provider as ep

    ep._embedding_provider = None
    ep._reranker_provider = None
    ep._fallback_warning = None
    ep._fallback_since = 0
    ep._api_error_count = 0


class TestFallbackSinceScoping:
    def test_global_declaration_includes_fallback_since(self):
        """Statically verify get_embedding_provider() declares _fallback_since global.

        If someone removes it again, the assignment at the bottom of the
        function will make `_fallback_since` a local, and the retry block's
        read will UnboundLocalError on the next call.
        """
        import inspect

        from src.embedding_provider import get_embedding_provider

        src = inspect.getsource(get_embedding_provider)
        # Find the global line
        global_lines = [ln for ln in src.splitlines() if ln.strip().startswith("global ")]
        assert global_lines, "get_embedding_provider must have a `global` declaration"
        joined = " ".join(global_lines)
        assert "_fallback_since" in joined, (
            "get_embedding_provider must declare `_fallback_since` global; "
            "otherwise the inner assignment makes it a local and triggers "
            "UnboundLocalError on the retry-check read."
        )
        assert "_fallback_warning" in joined
        assert "_embedding_provider" in joined

    def test_retry_path_does_not_raise_unbound_local(self):
        """Exercise the exact code path that produced the production failures.

        Setup: cached provider exists, fallback warning is set, fallback_since
        was set long enough ago that the retry-check fires. We stub the Gemini
        probe to FAIL so the inner `except` branch runs — that branch assigns
        `_fallback_since = time.time()` which is what made Python infer the
        name as local. The function must return normally.
        """
        _reset_module_state()
        import src.embedding_provider as ep

        fake_provider = MagicMock()
        ep._embedding_provider = fake_provider
        ep._fallback_warning = "Gemini API unavailable, using local models"
        ep._fallback_since = time.time() - (ep._RETRY_API_AFTER + 10)

        # Simulate: API key present but probe fails → inner except → _fallback_since reassigned.
        with patch("src.config.GEMINI_API_KEY", "fake-key"):
            # Patch google.genai at import time — the function does a late import.
            fake_genai = MagicMock()
            fake_client = MagicMock()
            fake_client.models.embed_content.side_effect = RuntimeError("probe failed")
            fake_genai.Client.return_value = fake_client
            with patch.dict("sys.modules", {"google": MagicMock(genai=fake_genai), "google.genai": fake_genai}):
                provider, warning = ep.get_embedding_provider()

        assert provider is fake_provider
        assert warning == "Gemini API unavailable, using local models"
        # The except branch should have updated _fallback_since to ~now.
        assert ep._fallback_since > time.time() - 5

    def test_cached_provider_fast_path_no_error(self):
        """Hot path: cached provider, no fallback — must not touch retry block."""
        _reset_module_state()
        import src.embedding_provider as ep

        fake_provider = MagicMock()
        ep._embedding_provider = fake_provider
        ep._fallback_warning = None
        ep._fallback_since = 0

        provider, warning = ep.get_embedding_provider()
        assert provider is fake_provider
        assert warning is None
