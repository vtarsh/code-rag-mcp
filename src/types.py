"""Typed data contracts for search results, graph edges, and tool outputs.

All data flowing between modules uses these models.
Pydantic BaseModel for validation + serialization.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    """Single search result from FTS5 or vector search."""

    rowid: int = 0
    repo_name: str
    file_path: str
    file_type: str
    chunk_type: str
    snippet: str = ""
    score: float = 0.0
    sources: list[str] = Field(default_factory=list)
    rerank_score: float | None = None
    combined_score: float | None = None


class VectorResult(BaseModel):
    """Raw result from LanceDB vector search."""

    rowid: int
    repo_name: str
    file_path: str
    file_type: str
    chunk_type: str
    content_preview: str = ""
    distance: float = 0.0


class GraphEdge(BaseModel):
    """Edge in the dependency graph."""

    source: str
    target: str
    edge_type: str
    detail: str = ""


class ImpactNode(BaseModel):
    """Node discovered during impact analysis BFS."""

    name: str
    level: int
    via_type: str


class FlowPath(BaseModel):
    """A path found by trace_flow between two repos."""

    nodes: list[str]
    edges: list[str]
    score: int = 0


class ToolCallStat(BaseModel):
    """Runtime stats for a single tool."""

    name: str
    call_count: int = 0
    avg_ms: float = 0.0
    total_ms: float = 0.0


class RuntimeStats(BaseModel):
    """Aggregated runtime statistics for health_check output."""

    uptime_min: float = 0.0
    tool_stats: list[ToolCallStat] = Field(default_factory=list)
    total_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hit_rate: float | None = None
