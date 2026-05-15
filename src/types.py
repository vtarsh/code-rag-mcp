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


# ---------------------------------------------------------------------------
# Shadow Type Layer — proto schema + JS field extraction types
# ---------------------------------------------------------------------------


class ProtoField(BaseModel):
    """A single field inside a protobuf message."""

    name: str
    type: str
    number: int
    optional: bool = False
    repeated: bool = False


class ProtoMessage(BaseModel):
    """A parsed protobuf message definition."""

    name: str
    fields: list[ProtoField] = Field(default_factory=list)
    source_file: str = ""
    source_repo: str = ""


class ProtoRPC(BaseModel):
    """A single RPC method inside a protobuf service."""

    name: str
    request_type: str
    response_type: str


class ProtoService(BaseModel):
    """A parsed protobuf service definition."""

    name: str
    rpcs: list[ProtoRPC] = Field(default_factory=list)


class ProtoEnum(BaseModel):
    """A parsed protobuf enum definition."""

    name: str
    values: list[str] = Field(default_factory=list)


class ProtoSchema(BaseModel):
    """Complete parsed schema from one or more .proto files."""

    messages: dict[str, ProtoMessage] = Field(default_factory=dict)
    services: dict[str, ProtoService] = Field(default_factory=dict)
    enums: dict[str, ProtoEnum] = Field(default_factory=dict)


class FieldUsage(BaseModel):
    """A field usage extracted from a JS source file."""

    field_name: str
    file_path: str
    usage_type: str  # "destructure", "payload_build", "response_map", "conditional"
    source_field: str | None = None
    target_field: str | None = None
    is_optional: bool = False


class FieldMapping(BaseModel):
    """Mapping of a field between two hops (e.g. proto request -> API payload)."""

    proto_field: str
    js_field: str
    direction: str = "request"  # "request" or "response"
    transform: str = ""  # e.g. "parseFloat", "sanitizeAndCutInput"


class MethodTypeMap(BaseModel):
    """Shadow type map for a single provider method (e.g. initialize, sale)."""

    method: str
    proto_request: str = ""
    proto_response: str = ""
    request_fields: list[FieldMapping] = Field(default_factory=list)
    response_fields: list[FieldMapping] = Field(default_factory=list)
    api_endpoint: str = ""
    api_method: str = "POST"
    type_gaps: list[str] = Field(default_factory=list)


class ProviderTypeMap(BaseModel):
    """Complete shadow type map for a provider."""

    provider: str
    proto_service: str = ""
    methods: dict[str, MethodTypeMap] = Field(default_factory=dict)
    field_usages: list[FieldUsage] = Field(default_factory=list)
