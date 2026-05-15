"""Tests for scripts/_common.py — setup_paths() + daemon_post()."""

from __future__ import annotations

import json
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from scripts import _common
from scripts._common import DaemonError, daemon_post, setup_paths


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear setup_paths cache + remove its sys.path insertion before each test."""
    _common._setup_done = False
    _common._root_cache = None
    yield
    _common._setup_done = False
    _common._root_cache = None


def test_setup_paths_returns_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("CODE_RAG_HOME", raising=False)
    monkeypatch.delenv("ACTIVE_PROFILE", raising=False)
    root = setup_paths()
    assert root == _common._DEFAULT_HOME


def test_setup_paths_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_RAG_HOME", str(tmp_path))
    root = setup_paths()
    assert root == tmp_path


def test_setup_paths_sets_active_profile_default(monkeypatch):
    monkeypatch.delenv("ACTIVE_PROFILE", raising=False)
    setup_paths()
    import os

    assert os.environ.get("ACTIVE_PROFILE") == "pay-com"


def test_setup_paths_does_not_override_active_profile(monkeypatch):
    monkeypatch.setenv("ACTIVE_PROFILE", "custom")
    setup_paths()
    import os

    assert os.environ["ACTIVE_PROFILE"] == "custom"


def test_setup_paths_inserts_into_sys_path(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_RAG_HOME", str(tmp_path))
    sys_path_before = list(sys.path)
    try:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        setup_paths()
        assert str(tmp_path) in sys.path
    finally:
        sys.path[:] = sys_path_before


def test_setup_paths_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_RAG_HOME", str(tmp_path))
    sys_path_before = list(sys.path)
    try:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        setup_paths()
        setup_paths()
        setup_paths()
        assert sys.path.count(str(tmp_path)) == 1
    finally:
        sys.path[:] = sys_path_before


def _mock_urlopen_response(body: bytes):
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_daemon_post_returns_parsed_json():
    body = json.dumps({"result": "ok", "n": 42}).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(body)):
        out = daemon_post("/tool/x", {"a": 1})
    assert out == {"result": "ok", "n": 42}


def test_daemon_post_raises_on_connection_refused():
    err = urllib.error.URLError("Connection refused")
    with patch("urllib.request.urlopen", side_effect=err), pytest.raises(DaemonError, match="Cannot reach daemon"):
        daemon_post("/tool/x", {})


def test_daemon_post_raises_on_http_error():
    err = urllib.error.HTTPError(
        url="http://localhost:8742/tool/x",
        code=500,
        msg="Server Error",
        hdrs={},
        fp=None,
    )
    err.read = MagicMock(return_value=b"boom")
    with patch("urllib.request.urlopen", side_effect=err), pytest.raises(DaemonError, match="HTTP 500"):
        daemon_post("/tool/x", {})


def test_daemon_post_raises_on_invalid_json():
    with (
        patch(
            "urllib.request.urlopen",
            return_value=_mock_urlopen_response(b"not json"),
        ),
        pytest.raises(DaemonError, match="Invalid JSON"),
    ):
        daemon_post("/tool/x", {})


def test_daemon_post_uses_custom_url_and_timeout():
    body = json.dumps({"ok": True}).encode()
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_urlopen_response(body),
    ) as mock:
        daemon_post("/x", {}, daemon_url="http://example:9000", timeout=5)
    req = mock.call_args[0][0]
    assert req.full_url == "http://example:9000/x"
    assert mock.call_args[1]["timeout"] == 5
