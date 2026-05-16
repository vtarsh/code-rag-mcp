"""Shared fixtures for the test suite."""

import os
import sys
from pathlib import Path

import pytest

# Set CODE_RAG_HOME to the project directory so config.py finds the right data.
# This must happen before any src imports (config.py reads env at import time).
_project_root = str(Path(__file__).resolve().parent.parent)
os.environ.setdefault("CODE_RAG_HOME", _project_root)

# Ensure src imports work
sys.path.insert(0, _project_root)


@pytest.fixture(autouse=True)
def _mock_wiring():
    """Suppress code_facts/env_vars wiring in hybrid tests by default.

    Without this fixture, every test that touches hybrid_search would hit the
    live knowledge.db and pull extra candidates into the pool, breaking
    existing assertions.  Individual tests can override these patches to assert
    the wiring behaviour.
    """
    from unittest.mock import patch

    with (
        patch("src.search.hybrid.code_facts_search", return_value=[]),
        patch("src.search.hybrid.env_var_search", return_value=[]),
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_gh_cache_between_tests():
    """Clear GitHub API cache before each test to prevent cross-test pollution."""
    from src.cache import _query_cache
    from src.tools.analyze.github_helpers import clear_gh_cache

    clear_gh_cache()
    _query_cache.clear()
    yield
    clear_gh_cache()
    _query_cache.clear()
