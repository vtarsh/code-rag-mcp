"""JS field extractor — scans provider JS files for field usage patterns.

Extracts destructuring, payload building, response mapping, and conditional
field spreading patterns from provider method and lib files.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.types import FieldUsage


def extract_fields_from_file(file_path: str, content: str | None = None) -> list[FieldUsage]:
    """Extract all field usages from a single JS file."""
    if content is None:
        content = Path(file_path).read_text(encoding="utf-8")

    usages: list[FieldUsage] = []
    usages.extend(_extract_destructuring(content, file_path))
    usages.extend(_extract_payload_builds(content, file_path))
    usages.extend(_extract_response_maps(content, file_path))
    usages.extend(_extract_conditional_fields(content, file_path))
    return usages


def extract_fields_from_directory(dir_path: str, glob: str = "**/*.js") -> list[FieldUsage]:
    """Extract field usages from all JS files under a directory."""
    root = Path(dir_path)
    usages: list[FieldUsage] = []
    for p in sorted(root.glob(glob)):
        if "node_modules" in str(p):
            continue
        usages.extend(extract_fields_from_file(str(p)))
    return usages


# ---------------------------------------------------------------------------
# Destructuring: const { field1, field2 } = object
# Also handles nested: { identifiers: { transactionId, companyId } }
# ---------------------------------------------------------------------------

# Nested destructuring within a larger destructure
_NESTED_DESTRUCTURE_RE = re.compile(
    r"(\w+)\s*:\s*\{([^}]+)\}",
)

# Simple field in a destructure list (handles renamed: `status: providerStatus`)
_FIELD_NAME_RE = re.compile(r"(\w+)(?:\s*:\s*\w+)?")

_SKIP_KEYWORDS = frozenset(
    {"const", "let", "var", "async", "await", "function", "class", "new", "return", "if", "else"}
)


def _find_destructure_block(content: str, start: int) -> str:
    """Extract brace-balanced block starting from { at or after start."""
    brace_idx = content.index("{", start)
    depth = 0
    for i in range(brace_idx, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return content[brace_idx + 1 : i]
    return content[brace_idx + 1 :]


# Pattern to find destructuring assignments (start of one)
_DESTRUCTURE_START_RE = re.compile(
    r"(?:const|let|var)\s+\{",
)

# Pattern to find the source object after closing brace: } = source
_DESTRUCTURE_SOURCE_RE = re.compile(r"\}\s*(?:=\s*([\w.]+(?:\s*\?\.\s*\w+)*)|\s*$)")


def _extract_destructuring(content: str, file_path: str) -> list[FieldUsage]:
    usages: list[FieldUsage] = []

    for m in _DESTRUCTURE_START_RE.finditer(content):
        try:
            fields_block = _find_destructure_block(content, m.start())
        except ValueError:
            continue

        # Search for = source after the block in the original content
        remaining = content[m.start() :]
        # Find matching closing brace position
        brace_start = remaining.index("{")
        depth = 0
        close_pos = brace_start
        for i in range(brace_start, len(remaining)):
            if remaining[i] == "{":
                depth += 1
            elif remaining[i] == "}":
                depth -= 1
                if depth == 0:
                    close_pos = i
                    break

        after_brace = remaining[close_pos + 1 : close_pos + 100]
        source_match = re.match(r"\s*(?:=\s*\{?\s*\}?)?\s*=\s*([\w.]+)", after_brace)
        if not source_match:
            # Try simpler pattern
            source_match = re.match(r"\s*=\s*([\w.]+)", after_brace)
        source_obj = source_match.group(1) if source_match else "?"

        # Handle nested destructuring
        for nm in _NESTED_DESTRUCTURE_RE.finditer(fields_block):
            parent_field = nm.group(1)
            nested_fields = nm.group(2)
            for fm in _FIELD_NAME_RE.finditer(nested_fields):
                field = fm.group(1)
                if field in _SKIP_KEYWORDS:
                    continue
                usages.append(
                    FieldUsage(
                        field_name=field,
                        file_path=file_path,
                        usage_type="destructure",
                        source_field=f"{source_obj}.{parent_field}.{field}",
                    )
                )

        # Top-level fields (skip the nested parts)
        cleaned = _NESTED_DESTRUCTURE_RE.sub("", fields_block)
        for fm in _FIELD_NAME_RE.finditer(cleaned):
            field = fm.group(1)
            if field in _SKIP_KEYWORDS or not field:
                continue
            usages.append(
                FieldUsage(
                    field_name=field,
                    file_path=file_path,
                    usage_type="destructure",
                    source_field=f"{source_obj}.{field}",
                )
            )
    return usages


# ---------------------------------------------------------------------------
# Payload building: return { key: value, ... }
# Also matches: const payload = { ... }
# ---------------------------------------------------------------------------

# Matches return { ... }, const x = { ... }, and funcCall({ ... })
_PAYLOAD_BUILD_RE = re.compile(
    r"(?:return|(?:const|let|var)\s+\w+\s*=|(?:await\s+)?\w+\(\s*)\{([^}]{5,})\}",
    re.DOTALL,
)

_KV_RE = re.compile(r"(\w+)\s*:")


def _extract_payload_builds(content: str, file_path: str) -> list[FieldUsage]:
    usages: list[FieldUsage] = []
    for m in _PAYLOAD_BUILD_RE.finditer(content):
        body = m.group(1)
        for kv in _KV_RE.finditer(body):
            key = kv.group(1)
            if key in ("const", "let", "var", "if", "else", "return", "async", "await"):
                continue
            usages.append(
                FieldUsage(
                    field_name=key,
                    file_path=file_path,
                    usage_type="payload_build",
                    target_field=key,
                )
            )
    return usages


# ---------------------------------------------------------------------------
# Response mapping: mapResponse({ response: body, ... })
# Also: module.exports = ({ response, ...}) => { ... return { ... } }
# ---------------------------------------------------------------------------

_RESPONSE_MAP_RE = re.compile(
    r"(?:mapResponse|return)\s*\(\s*\{([^}]+)\}",
    re.DOTALL,
)


def _extract_response_maps(content: str, file_path: str) -> list[FieldUsage]:
    usages: list[FieldUsage] = []
    for m in _RESPONSE_MAP_RE.finditer(content):
        body = m.group(1)
        for kv in _KV_RE.finditer(body):
            key = kv.group(1)
            if key in ("const", "let", "var", "if", "else", "return", "async", "await"):
                continue
            usages.append(
                FieldUsage(
                    field_name=key,
                    file_path=file_path,
                    usage_type="response_map",
                    target_field=key,
                )
            )
    return usages


# ---------------------------------------------------------------------------
# Conditional fields: ...(val && { key: val })
# ---------------------------------------------------------------------------

# Matches both:
#   ...(val && { key: val })    — explicit key
#   ...(val && { key })          — shorthand
_CONDITIONAL_RE = re.compile(
    r"\.\.\.\((\w+)\s*&&\s*\{\s*(\w+)\s*[,:}\s]",
)


def _extract_conditional_fields(content: str, file_path: str) -> list[FieldUsage]:
    usages: list[FieldUsage] = []
    for m in _CONDITIONAL_RE.finditer(content):
        condition = m.group(1)
        key = m.group(2)
        usages.append(
            FieldUsage(
                field_name=key,
                file_path=file_path,
                usage_type="conditional",
                source_field=condition,
                target_field=key,
                is_optional=True,
            )
        )
    return usages
