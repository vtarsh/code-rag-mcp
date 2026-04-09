"""Shadow type map tool — show proto-to-JS field mappings for providers.

Exposes the shadow type layer via MCP, letting Claude see the full type chain
from gateway proto messages through provider JS code to external API payloads.

Modes:
  overview — summary of all methods for a provider
  fields   — detailed field mapping for a specific method
  gaps     — type gap report (where fields lose typing)
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.config import PROFILE_DIR

log = logging.getLogger(__name__)

_PROVIDER_TYPES_DIR = PROFILE_DIR / "provider_types"

# Keys that are NOT the provider-side field name in a mapping dict
_META_KEYS = {"proto", "transform", "value", "note", "purpose"}


def _resolve_target(fm: dict) -> str:
    """Resolve the provider-side field name from a mapping dict.

    YAML files use different keys for the provider field:
    - 'js' (payper, trustly, ppro, volt, paysafe, paynearme)
    - 'provider' (aircash, neosurf)
    - '<provider_name>' e.g. 'nuvei' (nuvei)

    This finds the first key that isn't a known meta key.
    """
    if "js" in fm:
        return fm["js"]
    for key in fm:
        if key not in _META_KEYS:
            return fm[key]
    return "?"


def _load_provider_yaml(provider: str) -> dict | None:
    """Load a pre-built provider type map YAML."""
    yaml_path = _PROVIDER_TYPES_DIR / f"{provider}.yaml"
    if not yaml_path.exists():
        return None
    try:
        return yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to load %s: %s", yaml_path, e)
        return None


def _format_overview(data: dict) -> str:
    """Format overview mode output."""
    lines: list[str] = []
    provider = data.get("provider", "?")
    lines.append(f"# Shadow Type Map: {provider}")
    lines.append(f"Proto service: {data.get('proto_service', '?')}")
    lines.append("")

    summary = data.get("summary", {})
    lines.append(f"Total field usages extracted: {summary.get('total_field_usages', 0)}")
    lines.append(f"Total type gaps: {summary.get('total_type_gaps', 0)}")
    lines.append("")

    methods = data.get("methods", {})
    for method_name, method_data in methods.items():
        req_collected = _collect_flow_mappings(method_data, "request")
        resp_collected = _collect_flow_mappings(method_data, "response")
        # Count: flat list = len directly; sectioned = sum of all _mappings
        if req_collected and isinstance(req_collected[0], dict) and "_section" in req_collected[0]:
            req_count = sum(len(s["_mappings"]) for s in req_collected)
        else:
            req_count = len(req_collected)
        if resp_collected and isinstance(resp_collected[0], dict) and "_section" in resp_collected[0]:
            resp_count = sum(len(s["_mappings"]) for s in resp_collected)
        else:
            resp_count = len(resp_collected)
        gap_count = len(method_data.get("type_gaps", []))
        lines.append(f"## {method_name}")
        lines.append(f"  Proto: {method_data.get('proto_request', '?')} -> {method_data.get('proto_response', '?')}")
        lines.append(f"  API:   {method_data.get('api_method', '?')} {method_data.get('api_endpoint', '?')}")
        lines.append(f"  Mappings: {req_count} request, {resp_count} response")
        lines.append(f"  Type gaps: {gap_count}")
        lines.append("")

    return "\n".join(lines)


def _collect_flow_mappings(md: dict, direction: str) -> list[dict]:
    """Collect field mappings from top-level or nested flows/steps.

    Handles three YAML structures:
    1. Top-level: methods.<m>.request_field_mappings (standard)
    2. Flow-based: methods.<m>.flows.<flow>.request_field_mappings (e.g. fonix)
    3. Step-based: methods.<m>.step1_*.request_field_mappings (e.g. paynearme)
    """
    key = f"{direction}_field_mappings"

    # 1. Top-level mappings — return directly if present (must be a list)
    top = md.get(key, [])
    if isinstance(top, list) and top:
        return top

    all_mappings: list[dict] = []

    # 2. Flow-based: flows.<flow_name>.(request|response)_field_mappings
    flows = md.get("flows", {})
    if isinstance(flows, dict):
        for flow_name, flow_data in flows.items():
            if not isinstance(flow_data, dict):
                continue
            flow_maps = flow_data.get(key, [])
            if flow_maps:
                all_mappings.append({"_section": f"flow: {flow_name}", "_mappings": flow_maps})
            # Also check nested steps inside a flow
            for step in flow_data.get("steps", []):
                if isinstance(step, dict):
                    step_maps = step.get(key, [])
                    if step_maps:
                        step_name = step.get("name", "?")
                        all_mappings.append(
                            {"_section": f"flow: {flow_name} / step: {step_name}", "_mappings": step_maps}
                        )

    # 3. Step-based keys at method level: step1_*, step2_*, etc.
    for k, v in md.items():
        if k.startswith("step") and isinstance(v, dict):
            step_maps = v.get(key, [])
            if step_maps:
                all_mappings.append({"_section": k, "_mappings": step_maps})

    return all_mappings


def _format_fields(data: dict, method: str) -> str:
    """Format fields mode output for a specific method."""
    methods = data.get("methods", {})
    if method not in methods:
        available = list(methods.keys())
        return f"Method '{method}' not found. Available: {available}"

    md = methods[method]
    lines: list[str] = []
    lines.append(f"# {data.get('provider', '?')}.{method} — Field Mappings")
    lines.append(f"Proto: {md.get('proto_request', '?')} -> {md.get('proto_response', '?')}")
    lines.append(f"API:   {md.get('api_method', '?')} {md.get('api_endpoint', '?')}")
    lines.append("")

    # --- Request mappings ---
    req_mappings = _collect_flow_mappings(md, "request")
    lines.append("## Request (proto -> API payload)")
    if req_mappings and isinstance(req_mappings[0], dict) and "_section" in req_mappings[0]:
        # Sectioned output (from flows/steps)
        for section in req_mappings:
            lines.append(f"  ### {section['_section']}")
            for fm in section["_mappings"]:
                proto = fm.get("proto", "(constant)")
                target = _resolve_target(fm)
                transform = f" [{fm['transform']}]" if fm.get("transform") else ""
                lines.append(f"    {proto:48s} -> {target}{transform}")
            lines.append("")
    else:
        # Flat list (top-level)
        for fm in req_mappings:
            proto = fm.get("proto", "(constant)")
            target = _resolve_target(fm)
            transform = f" [{fm['transform']}]" if fm.get("transform") else ""
            lines.append(f"  {proto:50s} -> {target}{transform}")

    lines.append("")

    # --- Response mappings ---
    resp_mappings = _collect_flow_mappings(md, "response")
    lines.append("## Response (API response -> proto)")
    if resp_mappings and isinstance(resp_mappings[0], dict) and "_section" in resp_mappings[0]:
        for section in resp_mappings:
            lines.append(f"  ### {section['_section']}")
            for fm in section["_mappings"]:
                proto = fm.get("proto", "(constant)")
                target = _resolve_target(fm)
                transform = f" [{fm['transform']}]" if fm.get("transform") else ""
                lines.append(f"    {proto:48s} <- {target}{transform}")
            lines.append("")
    else:
        for fm in resp_mappings:
            proto = fm.get("proto", "(constant)")
            target = _resolve_target(fm)
            transform = f" [{fm['transform']}]" if fm.get("transform") else ""
            lines.append(f"  {proto:50s} <- {target}{transform}")

    return "\n".join(lines)


def _format_gaps(data: dict, method: str = "") -> str:
    """Format gaps mode output."""
    lines: list[str] = []
    lines.append(f"# Type Gaps: {data.get('provider', '?')}")
    lines.append("")

    methods = data.get("methods", {})
    target_methods = {method: methods[method]} if method and method in methods else methods

    total = 0
    for method_name, md in target_methods.items():
        gaps = md.get("type_gaps", [])
        if not gaps:
            continue
        lines.append(f"## {method_name} ({len(gaps)} gaps)")
        for g in gaps:
            lines.append(f"  - {g}")
        lines.append("")
        total += len(gaps)

    lines.append(f"Total type gaps: {total}")
    return "\n".join(lines)


def provider_type_map_tool(provider: str, method: str = "", mode: str = "overview") -> str:
    """Show shadow type map for a provider.

    Args:
        provider: Provider name (e.g. "payper")
        method: Specific method name (required for "fields" mode)
        mode: "overview" | "fields" | "gaps"

    Returns:
        Formatted type map information
    """
    data = _load_provider_yaml(provider)
    if data is None:
        available = []
        if _PROVIDER_TYPES_DIR.exists():
            available = [p.stem for p in _PROVIDER_TYPES_DIR.glob("*.yaml")]
        if available:
            return (
                f"No type map found for '{provider}'. "
                f"Available: {available}. "
                f"Run: python scripts/build_shadow_types.py --provider={provider}"
            )
        return (
            f"No type maps built yet. "
            f"Run: python scripts/build_shadow_types.py --provider={provider}"
        )

    if mode == "fields":
        if not method:
            return "Mode 'fields' requires a method name. " + _format_overview(data)
        return _format_fields(data, method)
    elif mode == "gaps":
        return _format_gaps(data, method)
    else:
        return _format_overview(data)
