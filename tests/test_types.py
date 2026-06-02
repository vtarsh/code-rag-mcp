"""Tests for types.py — Pydantic models."""

from src.types import (
    GraphEdge,
    RuntimeStats,
    SearchResult,
    ToolCallStat,
)


class TestSearchResult:
    def test_minimal(self):
        r = SearchResult(repo_name="repo", file_path="f.ts", file_type="grpc_method", chunk_type="function")
        assert r.repo_name == "repo"
        assert r.rowid == 0
        assert r.snippet == ""
        assert r.sources == []

    def test_full(self):
        r = SearchResult(
            rowid=42,
            repo_name="grpc-apm-trustly",
            file_path="src/handler.ts",
            file_type="grpc_method",
            chunk_type="function",
            snippet=">>>match<<<",
            score=0.85,
            sources=["keyword", "vector"],
            rerank_score=0.9,
            combined_score=0.88,
        )
        assert r.rowid == 42
        assert r.combined_score == 0.88
        assert "keyword" in r.sources


class TestGraphEdge:
    def test_creation(self):
        e = GraphEdge(source="a", target="b", edge_type="grpc_call")
        assert e.detail == ""

    def test_with_detail(self):
        e = GraphEdge(source="a", target="b", edge_type="npm_dep", detail="@pay/types")
        assert e.detail == "@pay/types"


class TestToolCallStat:
    def test_creation(self):
        s = ToolCallStat(name="search", call_count=10, avg_ms=42.5, total_ms=425.0)
        assert s.avg_ms == 42.5


class TestRuntimeStats:
    def test_defaults(self):
        rs = RuntimeStats()
        assert rs.uptime_min == 0.0
        assert rs.tool_stats == []
        assert rs.cache_hit_rate is None
