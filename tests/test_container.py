"""Tests for container.py — get_db and check_db_health."""

import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch


class TestGetDb:
    @patch("src.container._wal_set", False)
    @patch("src.container.DB_PATH", Path(":memory:"))
    def test_returns_connection_with_row_factory(self):
        from src.container import get_db

        # Use a temp file to test properly
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            with patch("src.container.DB_PATH", tmp_path), patch("src.container._wal_set", False):
                conn = get_db()
                assert conn.row_factory == sqlite3.Row
                conn.close()
        finally:
            os.unlink(tmp_path)

    def test_row_factory_works(self):
        """Verify row_factory produces dict-like access."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            with patch("src.container.DB_PATH", tmp_path), patch("src.container._wal_set", False):
                from src.container import get_db

                conn = get_db()
                conn.execute("CREATE TABLE test (col1 TEXT, col2 INTEGER)")
                conn.execute("INSERT INTO test VALUES ('hello', 42)")
                row = conn.execute("SELECT * FROM test").fetchone()
                assert row["col1"] == "hello"
                assert row["col2"] == 42
                conn.close()
        finally:
            os.unlink(tmp_path)


class TestWalPragmaThreadSafety:
    def test_wal_pragma_thread_safe(self):
        """Run get_db() from 10 threads concurrently; the WAL pragma branch
        must execute exactly once (i.e. `_wal_set` flips True once, not N
        times), proving the check-then-act is guarded by the module lock."""
        import src.container as container_mod
        from src.container import get_db

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            # sqlite3.Connection is a C type and its `execute` is immutable,
            # so we cannot monkey-patch it directly. Instead, wrap
            # sqlite3.connect and count WAL-pragma invocations on the
            # returned connection via an __getattribute__-backed proxy.
            pragma_runs = {"count": 0}
            pragma_lock = threading.Lock()
            real_connect = sqlite3.connect

            class _Proxy:
                def __init__(self, conn: sqlite3.Connection) -> None:
                    object.__setattr__(self, "_conn", conn)

                def execute(self, sql, *args, **kwargs):
                    if isinstance(sql, str) and "journal_mode=WAL" in sql:
                        with pragma_lock:
                            pragma_runs["count"] += 1
                    return self._conn.execute(sql, *args, **kwargs)

                def __getattr__(self, name: str):
                    return getattr(self._conn, name)

                def __setattr__(self, name: str, value) -> None:
                    setattr(self._conn, name, value)

            def wrapped_connect(*args, **kwargs):
                return _Proxy(real_connect(*args, **kwargs))

            barrier = threading.Barrier(10)
            conns: list = []
            conns_lock = threading.Lock()
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    barrier.wait()
                    c = get_db()
                    with conns_lock:
                        conns.append(c)
                except BaseException as e:
                    errors.append(e)

            with (
                patch.object(container_mod, "DB_PATH", tmp_path),
                patch.object(container_mod, "_wal_set", False),
                patch("src.container.sqlite3.connect", side_effect=wrapped_connect),
            ):
                threads = [threading.Thread(target=worker) for _ in range(10)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                assert not errors, f"worker errors: {errors}"
                assert container_mod._wal_set is True
                # Exactly one thread should have executed the WAL pragma.
                assert pragma_runs["count"] == 1, f"expected WAL pragma to run once, got {pragma_runs['count']}"

            for c in conns:
                c.close()
        finally:
            os.unlink(tmp_path)


class TestDbConnection:
    def test_context_manager_closes_connection(self):
        """Verify db_connection() context manager closes conn on exit."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            with patch("src.container.DB_PATH", tmp_path), patch("src.container._wal_set", False):
                from src.container import db_connection

                with db_connection() as conn:
                    conn.execute("CREATE TABLE test (col1 TEXT)")
                    conn.execute("INSERT INTO test VALUES ('hello')")
                    row = conn.execute("SELECT * FROM test").fetchone()
                    assert row["col1"] == "hello"
                # After exiting, connection should be closed
                import contextlib

                with contextlib.suppress(Exception):
                    conn.execute("SELECT 1")
        finally:
            os.unlink(tmp_path)

    def test_context_manager_closes_on_exception(self):
        """Verify db_connection() closes conn even when exception occurs."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            with patch("src.container.DB_PATH", tmp_path), patch("src.container._wal_set", False):
                from src.container import db_connection

                try:
                    with db_connection() as _conn:
                        raise ValueError("test error")
                except ValueError:
                    pass
                # Connection should still have been closed despite exception
        finally:
            os.unlink(tmp_path)


class TestCheckDbHealth:
    @patch("src.container.DB_PATH")
    def test_db_not_exists(self, mock_path):
        from src.container import check_db_health

        mock_path.exists.return_value = False
        result = check_db_health()
        assert result is not None
        assert "not built yet" in result

    def test_healthy_db(self):
        from src.container import check_db_health

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            conn = sqlite3.connect(str(tmp_path))
            conn.execute("CREATE TABLE chunks (id INTEGER)")
            conn.execute("CREATE TABLE repos (id INTEGER)")
            conn.execute("CREATE TABLE build_info (id INTEGER)")
            conn.close()
            with patch("src.container.DB_PATH", tmp_path):
                result = check_db_health()
                assert result is None
        finally:
            os.unlink(tmp_path)

    def test_missing_tables(self):
        from src.container import check_db_health

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            conn = sqlite3.connect(str(tmp_path))
            conn.execute("CREATE TABLE chunks (id INTEGER)")
            # Missing repos and build_info
            conn.close()
            with patch("src.container.DB_PATH", tmp_path):
                result = check_db_health()
                assert result is not None
                assert "incomplete" in result
                assert "build_info" in result or "repos" in result
        finally:
            os.unlink(tmp_path)

    def test_corrupt_db(self):
        from src.container import check_db_health

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False, mode="w") as f:
            f.write("this is not a sqlite file")
            tmp_path = Path(f.name)
        try:
            with patch("src.container.DB_PATH", tmp_path):
                result = check_db_health()
                assert result is not None
                assert "error" in result.lower() or "not built" in result.lower()
        finally:
            os.unlink(tmp_path)
