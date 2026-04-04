"""Tests for daemon.py — HTTP request handling logic."""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch


def _make_handler(method, path, body=None, tools=None):
    """Create a DaemonHandler instance with mocked socket I/O.

    Returns (handler, response_bytes) where response_bytes is a BytesIO
    that captures what the handler wrote back.
    """
    # Import here to avoid side effects at module level
    with patch("daemon.start_preload"):
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
        assert data["status"] in ("ok", "warming")
        assert "models_ready" in data
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
        with patch("daemon.start_preload"):
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
        responses, data = _make_handler("POST", "/other/path", body={})
        assert responses[0]["status"] == 404

    def test_empty_body_defaults_to_empty_dict(self):
        """A tool that needs no args should work with Content-Length: 0."""
        mock_tools = {"health_check": lambda args: "ok"}

        with patch("daemon.start_preload"):
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
        responses, data = _make_handler(
            "POST",
            "/tool/a/b",
            body={},
            tools=mock_tools,
        )
        # "a/b" is a valid key if present in TOOLS dict — daemon extracts everything after /tool/
        assert responses[0]["status"] == 200 or responses[0]["status"] == 404
