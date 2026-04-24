"""Tests for src/index/builders/_memguard.py.

Covers:
- get_limits reads env vars
- pause_daemon handles connection-refused, real-200, error responses
- memory_pressure classifies ok/soft/hard via mocked psutil
- check_and_maybe_exit calls compact_cb on soft, sys.exit(0) on hard
"""

from __future__ import annotations

import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from src.index.builders import _memguard

class TestGetLimits:
    def test_defaults(self, monkeypatch):
        for k in (
            "CODE_RAG_EMBED_RSS_SOFT_GB",
            "CODE_RAG_EMBED_RSS_HARD_GB",
            "CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB",
            "CODE_RAG_EMBED_SYS_AVAIL_HARD_GB",
            "CODE_RAG_DAEMON_PORT",
        ):
            monkeypatch.delenv(k, raising=False)
        limits = _memguard.get_limits()
        assert limits.rss_soft_bytes == 8 * 1024**3
        assert limits.rss_hard_bytes == 10 * 1024**3
        assert limits.sys_avail_soft_bytes == 2 * 1024**3
        assert limits.sys_avail_hard_bytes == int(0.8 * 1024**3)
        assert limits.daemon_port == 8742

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("CODE_RAG_EMBED_RSS_SOFT_GB", "12")
        monkeypatch.setenv("CODE_RAG_EMBED_RSS_HARD_GB", "14")
        monkeypatch.setenv("CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB", "1.5")
        monkeypatch.setenv("CODE_RAG_EMBED_SYS_AVAIL_HARD_GB", "0.5")
        monkeypatch.setenv("CODE_RAG_DAEMON_PORT", "9999")
        limits = _memguard.get_limits()
        assert limits.rss_soft_bytes == 12 * 1024**3
        assert limits.rss_hard_bytes == 14 * 1024**3
        assert limits.sys_avail_soft_bytes == int(1.5 * 1024**3)
        assert limits.sys_avail_hard_bytes == int(0.5 * 1024**3)
        assert limits.daemon_port == 9999

class TestPauseDaemon:
    def test_returns_false_on_econnrefused(self):
        """No daemon listening → False, no print noise about errors."""
        err = OSError(61, "Connection refused")
        url_err = urllib.error.URLError(err)
        with patch("urllib.request.urlopen", side_effect=url_err):
            assert _memguard.pause_daemon(port=8742) is False

    def test_returns_true_on_200(self):
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"status":"shutting_down"}'
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            assert _memguard.pause_daemon(port=8742) is True

    def test_returns_false_on_other_url_error(self, capsys):
        url_err = urllib.error.URLError("timed out")
        with patch("urllib.request.urlopen", side_effect=url_err):
            assert _memguard.pause_daemon(port=8742) is False
        out = capsys.readouterr().out
        assert "shutdown failed" in out

    def test_returns_false_on_generic_exception(self, capsys):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
            assert _memguard.pause_daemon(port=8742) is False
        out = capsys.readouterr().out
        assert "shutdown" in out  # "shutdown error" or "shutdown failed"

class TestMemoryPressure:
    """Mock psutil so we can drive RSS / available memory deterministically."""

    def _patch_psutil(self, rss_bytes, avail_bytes):
        fake_proc = MagicMock()
        fake_proc.memory_info.return_value = MagicMock(rss=rss_bytes)
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = fake_proc
        fake_psutil.virtual_memory.return_value = MagicMock(available=avail_bytes)
        return patch.dict(sys.modules, {"psutil": fake_psutil})

    def test_ok_when_well_below_thresholds(self):
        with self._patch_psutil(rss_bytes=2 * 1024**3, avail_bytes=8 * 1024**3):
            level, _, _ = _memguard.memory_pressure()
        assert level == "ok"

    def test_soft_when_rss_at_soft(self):
        with self._patch_psutil(rss_bytes=9 * 1024**3, avail_bytes=8 * 1024**3):
            level, rss, _ = _memguard.memory_pressure()
        assert level == "soft"
        assert rss == 9 * 1024**3

    def test_soft_when_avail_low(self):
        with self._patch_psutil(rss_bytes=2 * 1024**3, avail_bytes=int(1.5 * 1024**3)):
            level, _, avail = _memguard.memory_pressure()
        assert level == "soft"
        assert avail == int(1.5 * 1024**3)

    def test_hard_when_rss_at_hard(self):
        with self._patch_psutil(rss_bytes=11 * 1024**3, avail_bytes=8 * 1024**3):
            level, _, _ = _memguard.memory_pressure()
        assert level == "hard"

    def test_hard_when_avail_critical(self):
        with self._patch_psutil(rss_bytes=2 * 1024**3, avail_bytes=int(0.5 * 1024**3)):
            level, _, _ = _memguard.memory_pressure()
        assert level == "hard"

class TestCheckAndMaybeExit:
    """Top-level state machine: ok → no-op, soft → compact + maybe sleep,
    hard → sys.exit(0)."""

    def _psutil_returning(self, rss, avail):
        fake_proc = MagicMock()
        fake_proc.memory_info.return_value = MagicMock(rss=rss)
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = fake_proc
        fake_psutil.virtual_memory.return_value = MagicMock(available=avail)
        return fake_psutil

    def test_ok_returns_ok_no_compact(self):
        compact = MagicMock()
        with patch.dict(sys.modules, {"psutil": self._psutil_returning(2 * 1024**3, 8 * 1024**3)}):
            level = _memguard.check_and_maybe_exit(compact_cb=compact)
        assert level == "ok"
        compact.assert_not_called()

    def test_soft_calls_compact_then_sleeps_when_still_soft(self):
        compact = MagicMock()
        # Both checks (initial + post-compact) report soft
        ps = self._psutil_returning(9 * 1024**3, 8 * 1024**3)
        sleeps = []
        with patch.dict(sys.modules, {"psutil": ps}), patch("time.sleep", side_effect=sleeps.append):
            level = _memguard.check_and_maybe_exit(compact_cb=compact)
        assert level == "soft"
        compact.assert_called_once()
        assert sleeps == [30]

    def test_hard_exits_via_sys_exit_0(self):
        compact = MagicMock()
        with (
            patch.dict(sys.modules, {"psutil": self._psutil_returning(11 * 1024**3, 8 * 1024**3)}),
            pytest.raises(SystemExit) as exc,
        ):
            _memguard.check_and_maybe_exit(compact_cb=compact, done=42, total=100)
        # hard path goes straight to sys.exit(0); compact_cb is intentionally
        # not called there — checkpoint is what saves us, not another compact.
        assert exc.value.code == 0

class TestFreeMemory:
    def test_runs_gc_collect(self):
        with patch("gc.collect") as gc_mock:
            _memguard.free_memory()
        gc_mock.assert_called_once()

    def test_safe_when_torch_missing(self):
        # Make `import torch` raise; free_memory should swallow it.
        original_torch = sys.modules.pop("torch", None)
        try:
            sys.modules["torch"] = None  # type: ignore[assignment]
            try:
                _memguard.free_memory()  # should not raise
            finally:
                del sys.modules["torch"]
        finally:
            if original_torch is not None:
                sys.modules["torch"] = original_torch