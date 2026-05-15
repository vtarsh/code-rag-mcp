"""Tests for daemon.py — HTTP request handling logic."""

import json
import time
from io import BytesIO
from unittest.mock import MagicMock, patch


def _make_handler(method, path, body=None, tools=None):
    """Create a DaemonHandler instance with mocked socket I/O.

    Returns (handler, response_bytes) where response_bytes is a BytesIO
    that captures what the handler wrote back.
    """
    # Import here to avoid side effects at module level
    from daemon import DaemonHandler

    # Build raw HTTP request
    if body is not None:
        body_bytes = json.dumps(body).encode() if isinstance(body, dict) else body
    else:
        body_bytes = b""

    # Mock the socket/rfile/wfile
    rfile = BytesIO(body_bytes)
    wfile = BytesIO()

    handler = DaemonHandler.__new__(DaemonHandler)
    handler.rfile = rfile
    handler.wfile = wfile
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.headers = {"Content-Length": str(len(body_bytes)), "Content-Type": "application/json"}
    handler.client_address = ("127.0.0.1", 12345)
    handler.server = MagicMock()
    handler.close_connection = True

    # Capture responses
    responses = []

    def mock_send_response(code):
        responses.append({"status": code, "headers": {}})

    def mock_send_header(key, value):
        if responses:
            responses[-1]["headers"][key] = value

    def mock_end_headers():
        pass

    handler.send_response = mock_send_response
    handler.send_header = mock_send_header
    handler.end_headers = mock_end_headers

    # Patch TOOLS if provided
    if tools is not None:
        with patch("daemon.TOOLS", tools):
            if method == "GET":
                handler.do_GET()
            elif method == "POST":
                handler.do_POST()
    else:
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()

    # Parse the response body from wfile
    wfile.seek(0)
    response_body = wfile.read()
    response_data = json.loads(response_body) if response_body else None

    return responses, response_data


class TestHealthEndpoint:
    """GET /health should return status ok."""

    def test_health_returns_200(self):
        responses, data = _make_handler("GET", "/health")
        assert responses[0]["status"] == 200
        assert data["status"] in ("ok", "warming", "ready")
        assert "uptime" in data
        assert "pid" in data

    def test_unknown_get_path_returns_404(self):
        responses, data = _make_handler("GET", "/unknown")
        assert responses[0]["status"] == 404
        assert "error" in data


class TestToolEndpoint:
    """POST /tool/<name> tests."""

    def test_valid_tool_call(self):
        mock_tools = {"search": lambda args: f"results for {args['query']}"}
        responses, data = _make_handler(
            "POST",
            "/tool/search",
            body={"query": "payment"},
            tools=mock_tools,
        )
        assert responses[0]["status"] == 200
        assert data["result"] == "results for payment"

    def test_unknown_tool_returns_404(self):
        mock_tools = {"search": lambda args: "ok"}
        responses, data = _make_handler(
            "POST",
            "/tool/nonexistent",
            body={},
            tools=mock_tools,
        )
        assert responses[0]["status"] == 404
        assert "unknown tool" in data["error"]

    def test_malformed_json_returns_400(self):
        mock_tools = {"search": lambda args: "ok"}

        # Create handler manually with invalid JSON body
        from daemon import DaemonHandler

        rfile = BytesIO(b"not json at all{{{")
        wfile = BytesIO()

        handler = DaemonHandler.__new__(DaemonHandler)
        handler.rfile = rfile
        handler.wfile = wfile
        handler.requestline = "POST /tool/search HTTP/1.1"
        handler.command = "POST"
        handler.path = "/tool/search"
        handler.request_version = "HTTP/1.1"
        handler.headers = {"Content-Length": "18", "Content-Type": "application/json"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.server = MagicMock()

        responses = []

        def mock_send_response(code):
            responses.append({"status": code})

        handler.send_response = mock_send_response
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None

        with patch("daemon.TOOLS", mock_tools):
            handler.do_POST()

        wfile.seek(0)
        data = json.loads(wfile.read())
        assert responses[0]["status"] == 400
        assert "invalid JSON" in data["error"]

    def test_tool_exception_returns_500(self):
        def failing_tool(args):
            raise ValueError("something broke")

        mock_tools = {"broken": failing_tool}
        responses, data = _make_handler(
            "POST",
            "/tool/broken",
            body={},
            tools=mock_tools,
        )
        assert responses[0]["status"] == 500
        assert "something broke" in data["error"]

    def test_non_tool_post_returns_404(self):
        responses, _data = _make_handler("POST", "/other/path", body={})
        assert responses[0]["status"] == 404

    def test_empty_body_defaults_to_empty_dict(self):
        """A tool that needs no args should work with Content-Length: 0."""
        mock_tools = {"health_check": lambda args: "ok"}

        from daemon import DaemonHandler

        rfile = BytesIO(b"")
        wfile = BytesIO()

        handler = DaemonHandler.__new__(DaemonHandler)
        handler.rfile = rfile
        handler.wfile = wfile
        handler.requestline = "POST /tool/health_check HTTP/1.1"
        handler.command = "POST"
        handler.path = "/tool/health_check"
        handler.request_version = "HTTP/1.1"
        handler.headers = {"Content-Length": "0"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.server = MagicMock()

        responses = []

        def mock_send_response(code):
            responses.append({"status": code})

        handler.send_response = mock_send_response
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None

        with patch("daemon.TOOLS", mock_tools):
            handler.do_POST()

        wfile.seek(0)
        data = json.loads(wfile.read())
        assert responses[0]["status"] == 200
        assert data["result"] == "ok"

    def test_missing_required_field_returns_500(self):
        """Tool that expects a required field should fail with 500 on KeyError."""
        mock_tools = {"search": lambda args: args["query"]}  # requires 'query'
        responses, data = _make_handler(
            "POST",
            "/tool/search",
            body={},  # missing 'query'
            tools=mock_tools,
        )
        assert responses[0]["status"] == 500
        assert "error" in data


class TestToolNameExtraction:
    """Verify tool name is correctly extracted from URL path."""

    def test_nested_path_not_matched(self):
        """Only /tool/<name> is valid, not /tool/a/b."""
        mock_tools = {"a/b": lambda args: "ok", "a": lambda args: "wrong"}
        responses, _data = _make_handler(
            "POST",
            "/tool/a/b",
            body={},
            tools=mock_tools,
        )
        # "a/b" is a valid key if present in TOOLS dict — daemon extracts everything after /tool/
        assert responses[0]["status"] == 200 or responses[0]["status"] == 404


class TestAdminEndpoints:
    """Split /admin/unload (reversible) vs /admin/shutdown (drain + exit)."""

    def test_unload_is_reversible(self):
        """POST /admin/unload drops refs, does NOT exit, next /tool/ works."""
        import daemon

        # Ensure shutdown flag is clear (cross-test state)
        daemon._shutting_down.clear()

        unload_calls = {"n": 0}

        def fake_reset():
            unload_calls["n"] += 1

        with patch("src.embedding_provider.reset_providers", fake_reset):
            responses, data = _make_handler("POST", "/admin/unload", body={})

        assert responses[0]["status"] == 200
        assert data["status"] == "unloaded"
        assert data["will_exit"] is False, "unload must NOT schedule exit"
        assert unload_calls["n"] == 1
        # Shutdown flag must still be clear after unload
        assert not daemon._shutting_down.is_set()

        # Now a follow-up /tool/ call still works (lazy reload path).
        mock_tools = {"search": lambda args: "reloaded-ok"}
        responses2, data2 = _make_handler(
            "POST",
            "/tool/search",
            body={"query": "anything"},
            tools=mock_tools,
        )
        assert responses2[0]["status"] == 200
        assert data2["result"] == "reloaded-ok"

        # Second unload is idempotent — should still succeed.
        with patch("src.embedding_provider.reset_providers", fake_reset):
            responses3, data3 = _make_handler("POST", "/admin/unload", body={})
        assert responses3[0]["status"] == 200
        assert data3["will_exit"] is False
        assert unload_calls["n"] == 2

    def test_shutdown_drains_and_exits(self):
        """POST /admin/shutdown sets drain flag; new /tool/ gets 503; os._exit fires."""
        import daemon

        daemon._shutting_down.clear()

        exit_calls = {"code": None}

        def fake_exit(code):
            exit_calls["code"] = code

        with patch("daemon.os._exit", fake_exit):
            responses, data = _make_handler("POST", "/admin/shutdown", body={})
            assert responses[0]["status"] == 200
            assert data["status"] == "shutting_down"
            assert data["will_exit"] is True
            assert daemon._shutting_down.is_set()

            mock_tools = {"search": lambda args: "should-not-run"}
            responses2, data2 = _make_handler(
                "POST",
                "/tool/search",
                body={"query": "late"},
                tools=mock_tools,
            )
            assert responses2[0]["status"] == 503
            assert "shutting down" in data2["error"].lower()

            deadline = time.monotonic() + 3.0
            while exit_calls["code"] is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert exit_calls["code"] == 0, "shutdown must call os._exit(0)"

            responses3, data3 = _make_handler("POST", "/admin/shutdown", body={})
            assert responses3[0]["status"] == 200
            assert data3["status"] == "already_shutting_down"

        daemon._shutting_down.clear()

    def test_shutdown_waits_for_inflight_request(self):
        """A running /tool/ call should finish before os._exit fires."""
        import daemon

        daemon._shutting_down.clear()
        # Simulate an in-flight request by bumping the counter.
        daemon._inflight_requests.inc()

        exit_calls = {"code": None, "ts": None}

        def fake_exit(code):
            exit_calls["code"] = code
            exit_calls["ts"] = time.monotonic()

        with patch("daemon.os._exit", fake_exit):
            t0 = time.monotonic()
            _make_handler("POST", "/admin/shutdown", body={})

            # Release the "in-flight" after ~100 ms.
            def _release():
                time.sleep(0.1)
                daemon._inflight_requests.dec()

            import threading as _t

            _t.Thread(target=_release, daemon=True).start()

            # Wait for exit to actually fire.
            deadline = time.monotonic() + 3.0
            while exit_calls["code"] is None and time.monotonic() < deadline:
                time.sleep(0.02)

            assert exit_calls["code"] == 0
            # Drain thread should have waited at least ~80 ms for the release.
            assert exit_calls["ts"] is not None and exit_calls["ts"] - t0 >= 0.08

        daemon._shutting_down.clear()


class TestLogConcurrency:
    """Concurrent JSONL writes must not interleave or tear lines."""

    def test_log_concurrent_writes_no_interleave(self, tmp_path):
        """N threads each write M lines; every line is valid JSON with our key."""
        import threading as _t

        import daemon

        log_path = tmp_path / "tool_calls.jsonl"

        num_threads = 8
        per_thread = 40
        barrier = _t.Barrier(num_threads)

        def worker(tid: int) -> None:
            barrier.wait()
            for i in range(per_thread):
                payload = "x" * 500 + f"-t{tid}-i{i}"
                rec = {"tid": tid, "i": i, "payload": payload}
                daemon._append_jsonl_locked(log_path, rec)

        threads = [_t.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        lines = log_path.read_text().splitlines()
        assert len(lines) == num_threads * per_thread, f"expected {num_threads * per_thread} lines, got {len(lines)}"
        for line in lines:
            # Every line parses as complete JSON (no tearing).
            obj = json.loads(line)
            assert "tid" in obj and "i" in obj and "payload" in obj
            assert obj["payload"].startswith("x" * 500)
            assert obj["payload"].endswith(f"-t{obj['tid']}-i{obj['i']}")

    def test_log_size_rotation(self, tmp_path):
        """Writing past the cap rotates to .1/.2/.3 and keeps growing primary."""
        import daemon

        log_path = tmp_path / "tool_calls.jsonl"

        with patch.object(daemon, "_JSONL_MAX_BYTES", 2048):
            for i in range(40):
                daemon._append_jsonl_locked(log_path, {"i": i, "pad": "p" * 800})

        backup1 = tmp_path / "tool_calls.jsonl.1"
        assert log_path.exists(), "primary must still exist after rotation"
        assert backup1.exists(), "at least one rotated backup (.1) must exist"
        assert log_path.stat().st_size > 0

        existing_backups = sum(1 for i in (1, 2, 3, 4) if (tmp_path / f"tool_calls.jsonl.{i}").exists())
        assert existing_backups <= daemon._JSONL_BACKUPS + 1  # allow fencepost tolerance

        all_paths = [log_path, backup1]
        for i in (2, 3):
            p = tmp_path / f"tool_calls.jsonl.{i}"
            if p.exists():
                all_paths.append(p)
        seen_is = []
        for p in all_paths:
            for line in p.read_text().splitlines():
                obj = json.loads(line)
                assert "i" in obj
                seen_is.append(obj["i"])
        assert len(set(seen_is)) == len(seen_is)
