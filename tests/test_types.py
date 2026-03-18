"""Tests for types.py — Pydantic models."""

from src.types import (
    FlowPath,
    GraphEdge,
    ImpactNode,
    RuntimeStats,
    SearchResult,
    ToolCallStat,
    VectorResult,
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


class TestVectorResult:
    def test_creation(self):
        r = VectorResult(rowid=1, repo_name="r", file_path="f", file_type="proto", chunk_type="message")
        assert r.distance == 0.0
        assert r.content_preview == ""


class TestGraphEdge:
    def test_creation(self):
        e = GraphEdge(source="a", target="b", edge_type="grpc_call")
        assert e.detail == ""

    def test_with_detail(self):
        e = GraphEdge(source="a", target="b", edge_type="npm_dep", detail="@pay/types")
        assert e.detail == "@pay/types"


class TestImpactNode:
    def test_creation(self):
        n = ImpactNode(name="repo-x", level=2, via_type="grpc_call")
        assert n.level == 2


class TestFlowPath:
    def test_creation(self):
        fp = FlowPath(nodes=["a", "b", "c"], edges=["grpc_call", "npm_dep"], score=5)
        assert len(fp.nodes) == 3
        assert fp.score == 5

    def test_defaults(self):
        fp = FlowPath(nodes=[], edges=[])
        assert fp.score == 0


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
