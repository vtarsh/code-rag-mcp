"""Tests for scripts/sample_real_queries.py — stratified query sampling.

Covers:
  - Tool filter (only 'search' entries kept).
  - Normalisation (case + whitespace dedup).
  - Session split via idle gap threshold.
  - Per-session cap.
  - Global dedup across sessions.
  - Deterministic sampling via seed.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sample_real_queries.py"
_SPEC = importlib.util.spec_from_file_location("sample_real_queries", _SCRIPT)
assert _SPEC and _SPEC.loader
sample_real_queries = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sample_real_queries)


def _make_entry(ts: str, query: str, tool: str = "search") -> dict:
    return {
        "ts": ts,
        "tool": tool,
        "args": {"query": query},
        "duration_ms": 1,
        "result_len": 0,
        "result_preview": "",
    }


def _write_jsonl(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "tool_calls.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def test_load_filters_non_search_tools(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path,
        [
            _make_entry("2026-04-01T10:00:00+00:00", "webhook trustly", tool="search"),
            _make_entry("2026-04-01T10:01:00+00:00", "never included", tool="analyze_task"),
            _make_entry("2026-04-01T10:02:00+00:00", "something", tool="health_check"),
        ],
    )
    loaded = sample_real_queries.load_search_queries(path)
    assert len(loaded) == 1
    assert loaded[0]["query"] == "webhook trustly"


def test_load_drops_empty_queries(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path,
        [
            _make_entry("2026-04-01T10:00:00+00:00", ""),
            _make_entry("2026-04-01T10:01:00+00:00", "   "),
            _make_entry("2026-04-01T10:02:00+00:00", "kept"),
        ],
    )
    loaded = sample_real_queries.load_search_queries(path)
    assert [e["query"] for e in loaded] == ["kept"]


def test_load_sorts_by_timestamp(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path,
        [
            _make_entry("2026-04-01T10:02:00+00:00", "third"),
            _make_entry("2026-04-01T10:00:00+00:00", "first"),
            _make_entry("2026-04-01T10:01:00+00:00", "second"),
        ],
    )
    loaded = sample_real_queries.load_search_queries(path)
    assert [e["query"] for e in loaded] == ["first", "second", "third"]


def test_normalise_whitespace_and_case() -> None:
    # Visible in dedup — "Foo  Bar" and "foo bar" must map to the same norm.
    assert sample_real_queries._normalize("Foo  Bar") == "foo bar"
    assert sample_real_queries._normalize("  spaced   out  \n\t") == "spaced out"


def test_split_sessions_by_idle_gap() -> None:
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    entries = [
        {"ts": now, "query": "a", "norm": "a"},
        {"ts": now + timedelta(minutes=5), "query": "b", "norm": "b"},
        # 40 min gap starts new session
        {"ts": now + timedelta(minutes=45), "query": "c", "norm": "c"},
        {"ts": now + timedelta(minutes=50), "query": "d", "norm": "d"},
    ]
    sessions = sample_real_queries.split_sessions(entries, idle_gap=timedelta(minutes=30))
    assert len(sessions) == 2
    assert [e["query"] for e in sessions[0]] == ["a", "b"]
    assert [e["query"] for e in sessions[1]] == ["c", "d"]


def test_split_sessions_empty() -> None:
    assert sample_real_queries.split_sessions([], idle_gap=timedelta(minutes=30)) == []


def test_per_session_cap_applied() -> None:
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    # 10 distinct queries within one session — cap must trim.
    entries = [
        {"ts": now + timedelta(seconds=i), "query": f"q{i}", "norm": f"q{i}"}
        for i in range(10)
    ]
    sampled, stats = sample_real_queries.sample_stratified(
        entries, n=100, per_session_cap=3, idle_gap=timedelta(minutes=30), seed=42
    )
    # Only 3 kept per session; N=100 but after_cap=3.
    assert stats["after_per_session_cap"] == 3
    assert stats["after_global_dedup"] == 3
    assert len(sampled) == 3


def test_global_dedup_across_sessions() -> None:
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    entries = [
        {"ts": now, "query": "webhook", "norm": "webhook"},
        {"ts": now + timedelta(minutes=5), "query": "trustly", "norm": "trustly"},
        # Second session — same query appears.
        {"ts": now + timedelta(minutes=60), "query": "Webhook", "norm": "webhook"},
        {"ts": now + timedelta(minutes=65), "query": "nuvei", "norm": "nuvei"},
    ]
    sampled, stats = sample_real_queries.sample_stratified(
        entries, n=100, per_session_cap=10, idle_gap=timedelta(minutes=30), seed=42
    )
    # "webhook" appears twice; global dedup keeps one.
    assert stats["after_per_session_cap"] == 4
    assert stats["after_global_dedup"] == 3
    queries = {e["query"].lower() for e in sampled}
    assert queries == {"webhook", "trustly", "nuvei"}


def test_sample_is_deterministic_with_seed() -> None:
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    entries = [
        {"ts": now + timedelta(minutes=i * 60), "query": f"q{i}", "norm": f"q{i}"}
        for i in range(50)
    ]
    s1, _ = sample_real_queries.sample_stratified(
        entries, n=10, per_session_cap=5, idle_gap=timedelta(minutes=30), seed=42
    )
    s2, _ = sample_real_queries.sample_stratified(
        entries, n=10, per_session_cap=5, idle_gap=timedelta(minutes=30), seed=42
    )
    assert [e["query"] for e in s1] == [e["query"] for e in s2]


def test_sample_changes_with_different_seed() -> None:
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    entries = [
        {"ts": now + timedelta(minutes=i * 60), "query": f"q{i}", "norm": f"q{i}"}
        for i in range(50)
    ]
    s1, _ = sample_real_queries.sample_stratified(
        entries, n=10, per_session_cap=5, idle_gap=timedelta(minutes=30), seed=42
    )
    s2, _ = sample_real_queries.sample_stratified(
        entries, n=10, per_session_cap=5, idle_gap=timedelta(minutes=30), seed=7
    )
    assert [e["query"] for e in s1] != [e["query"] for e in s2]


def test_write_output_roundtrips(tmp_path: Path) -> None:
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    sampled = [{"ts": now, "query": "test query", "norm": "test query"}]
    out = tmp_path / "nested" / "out.jsonl"
    sample_real_queries.write_output(out, sampled)

    assert out.exists()
    with out.open() as f:
        rows = [json.loads(line) for line in f]
    assert rows == [{"query": "test query", "sampled_ts": now.isoformat()}]


def test_malformed_json_skipped(tmp_path: Path) -> None:
    path = tmp_path / "tool_calls.jsonl"
    with path.open("w") as f:
        f.write(json.dumps(_make_entry("2026-04-01T10:00:00+00:00", "good")) + "\n")
        f.write("this is not json\n")
        f.write(json.dumps(_make_entry("2026-04-01T10:01:00+00:00", "also good")) + "\n")
    loaded = sample_real_queries.load_search_queries(path)
    assert [e["query"] for e in loaded] == ["good", "also good"]
