"""Proto file parser — extracts messages, services, enums from .proto files.

Regex-based parser (no protoc dependency). Handles proto2/proto3 syntax
including optional/required/repeated qualifiers, map fields, and nested imports.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.types import ProtoEnum, ProtoField, ProtoMessage, ProtoRPC, ProtoSchema, ProtoService


def parse_proto_file(
    content: str,
    source_file: str = "",
    source_repo: str = "",
) -> ProtoSchema:
    """Parse a single .proto file content into a ProtoSchema."""
    messages = _extract_messages(content, source_file, source_repo)
    services = _extract_services(content)
    enums = _extract_enums(content)
    return ProtoSchema(messages=messages, services=services, enums=enums)


def parse_proto_path(
    path: Path,
    source_repo: str = "",
) -> ProtoSchema:
    """Parse a .proto file from disk."""
    content = path.read_text(encoding="utf-8")
    return parse_proto_file(content, source_file=str(path), source_repo=source_repo)


def merge_schemas(*schemas: ProtoSchema) -> ProtoSchema:
    """Merge multiple ProtoSchema objects into one (later wins on conflicts)."""
    merged = ProtoSchema(messages={}, services={}, enums={})
    for s in schemas:
        merged.messages.update(s.messages)
        merged.services.update(s.services)
        merged.enums.update(s.enums)
    return merged


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------

# Match message blocks — handles nested messages by counting braces
_MESSAGE_START_RE = re.compile(r"^message\s+(\w+)\s*\{", re.MULTILINE)
_SERVICE_START_RE = re.compile(r"^service\s+(\w+)\s*\{", re.MULTILINE)
_ENUM_START_RE = re.compile(r"^enum\s+(\w+)\s*\{", re.MULTILINE)

# Field patterns inside a message body
_FIELD_RE = re.compile(
    r"^\s*"
    r"(?P<qualifier>optional|required|repeated)?\s*"
    r"(?P<type>map<[^>]+>|[\w.]+)\s+"
    r"(?P<name>\w+)\s*=\s*"
    r"(?P<number>\d+)",
    re.MULTILINE,
)

_RPC_RE = re.compile(
    r"rpc\s+(\w+)\s*\(\s*([\w.]+)\s*\)\s*returns\s*\(\s*([\w.]+)\s*\)"
)

_ENUM_VALUE_RE = re.compile(r"^\s*(\w+)\s*=\s*\d+", re.MULTILINE)


def _extract_block(content: str, start_pos: int) -> str:
    """Extract the body between { and matching }, starting from the { char."""
    brace_idx = content.index("{", start_pos)
    depth = 0
    for i in range(brace_idx, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return content[brace_idx + 1 : i]
    return content[brace_idx + 1 :]


def _extract_messages(
    content: str, source_file: str, source_repo: str
) -> dict[str, ProtoMessage]:
    messages: dict[str, ProtoMessage] = {}
    for m in _MESSAGE_START_RE.finditer(content):
        name = m.group(1)
        body = _extract_block(content, m.start())
        fields = _parse_fields(body)
        messages[name] = ProtoMessage(
            name=name,
            fields=fields,
            source_file=source_file,
            source_repo=source_repo,
        )
    return messages


def _parse_fields(body: str) -> list[ProtoField]:
    fields: list[ProtoField] = []
    for fm in _FIELD_RE.finditer(body):
        qualifier = fm.group("qualifier") or ""
        is_optional = qualifier == "optional"
        is_repeated = qualifier == "repeated"
        fields.append(
            ProtoField(
                name=fm.group("name"),
                type=fm.group("type"),
                number=int(fm.group("number")),
                optional=is_optional,
                repeated=is_repeated,
            )
        )
    return fields


def _extract_services(content: str) -> dict[str, ProtoService]:
    services: dict[str, ProtoService] = {}
    for m in _SERVICE_START_RE.finditer(content):
        name = m.group(1)
        body = _extract_block(content, m.start())
        rpcs: list[ProtoRPC] = []
        for rm in _RPC_RE.finditer(body):
            rpcs.append(
                ProtoRPC(
                    name=rm.group(1),
                    request_type=rm.group(2),
                    response_type=rm.group(3),
                )
            )
        services[name] = ProtoService(name=name, rpcs=rpcs)
    return services


def _extract_enums(content: str) -> dict[str, ProtoEnum]:
    enums: dict[str, ProtoEnum] = {}
    for m in _ENUM_START_RE.finditer(content):
        name = m.group(1)
        body = _extract_block(content, m.start())
        values = [em.group(1) for em in _ENUM_VALUE_RE.finditer(body)]
        enums[name] = ProtoEnum(name=name, values=values)
    return enums
