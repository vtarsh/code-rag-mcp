#!/usr/bin/env python3
"""Parse raw Jaeger trace JSON into a compact trace summary.

Usage:
    python scripts/parse_jaeger_trace.py <trace.json> [--output traces/raw/provider.summary.json]
    python scripts/parse_jaeger_trace.py <trace.json> --text   # human-readable table

Input:  Raw Jaeger export JSON (data[0].spans[] + processes{})
Output: Compact summary with services, call tree, timings, and inter-service edges.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _tag_map(span: dict) -> dict[str, str | int | float]:
    return {t["key"]: t["value"] for t in span.get("tags", [])}


def _clean_service_name(raw: str) -> str:
    """dev-api-checkout-1-0 -> api-checkout, dev-apm-payper-1-29 -> apm-payper"""
    name = re.sub(r"^dev-", "", raw)
    name = re.sub(r"-\d+-\d+$", "", name)
    return name


def _operation_label(span: dict, tags: dict) -> str:
    """Build human-readable operation label from span."""
    rpc_method = tags.get("rpc.method")
    rpc_service = tags.get("rpc.service", "")
    if rpc_method:
        # grpc.transactions.TransactionsService/getByPrimaryKey -> grpc.transactions.getByPrimaryKey
        svc_short = rpc_service.split(".")[-1] if rpc_service else ""
        # Remove 'Service' suffix: TransactionsService -> Transactions
        svc_short = re.sub(r"Service$", "", svc_short)
        prefix = rpc_service.split(".")[0] if rpc_service else "rpc"
        return f"{prefix}.{svc_short.lower()}.{rpc_method}" if svc_short else f"rpc.{rpc_method}"

    http_method = tags.get("http.method", "")
    http_url = tags.get("http.url", "")
    http_target = tags.get("http.target", "")
    if http_url and ("sandbox" in http_url or "external" in http_url or "api" in http_url):
        return f"{http_method} {http_url}"
    if http_target:
        return f"{http_method} {http_target}"

    db_system = tags.get("db.system")
    if db_system:
        return f"{db_system}.execute"

    return span.get("operationName", "unknown")


def parse_trace(data: dict) -> dict:
    """Parse Jaeger JSON into structured summary."""
    trace = data["data"][0]
    spans = trace["spans"]
    processes = trace["processes"]
    trace_id = trace["traceID"]

    # Build process -> clean service name map
    svc_map: dict[str, str] = {}
    for pid, proc in processes.items():
        svc_map[pid] = _clean_service_name(proc["serviceName"])

    # Index spans by ID
    span_by_id: dict[str, dict] = {s["spanID"]: s for s in spans}

    # Build call tree
    root_span = None
    children: dict[str, list[str]] = {}
    for s in spans:
        refs = s.get("references", [])
        if not refs:
            root_span = s
        else:
            parent_id = refs[0]["spanID"]
            children.setdefault(parent_id, []).append(s["spanID"])

    # Extract spans info
    span_infos = []
    for s in spans:
        tags = _tag_map(s)
        svc = svc_map.get(s["processID"], s["processID"])
        kind = tags.get("span.kind", "")
        duration_ms = round(s["duration"] / 1000, 1)
        error = tags.get("error", False) or tags.get("otel.status_code") == "ERROR"
        error_msg = tags.get("grpc.error_message", tags.get("otel.status_description", ""))

        span_infos.append({
            "span_id": s["spanID"],
            "parent_id": s["references"][0]["spanID"] if s.get("references") else None,
            "service": svc,
            "operation": _operation_label(s, tags),
            "kind": kind,
            "duration_ms": duration_ms,
            "start_time": s["startTime"],
            "error": bool(error),
            "error_message": error_msg if error_msg else None,
        })

    # Sort by start time
    span_infos.sort(key=lambda x: x["start_time"])

    # Unique services
    services = sorted(set(si["service"] for si in span_infos))

    # Inter-service edges (client -> server calls)
    edges: list[dict] = []
    seen_edges: set[tuple] = set()
    for si in span_infos:
        if si["kind"] != "client":
            continue
        # Find the server span this client calls
        child_ids = children.get(si["span_id"], [])
        for cid in child_ids:
            child = span_by_id.get(cid)
            if not child:
                continue
            child_tags = _tag_map(child)
            child_svc = svc_map.get(child["processID"], child["processID"])
            if child_svc != si["service"]:
                edge_key = (si["service"], child_svc, si["operation"])
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append({
                        "from": si["service"],
                        "to": child_svc,
                        "operation": si["operation"],
                        "duration_ms": si["duration_ms"],
                    })

    # External calls (HTTP to outside services)
    external_calls = []
    for si in span_infos:
        if "http" not in si["operation"].lower():
            continue
        op = si["operation"]
        if "sandbox" in op or "payper.ca" in op or "trustly" in op or "nuvei" in op:
            external_calls.append({
                "from": si["service"],
                "url": op,
                "duration_ms": si["duration_ms"],
            })

    # Detect provider from spans
    # Patterns: apm-payper, providers-nuvei (not credentials/features/mapping), card-nuvei
    PROVIDER_INFRA = {"providers-credentials", "providers-features", "providers-mapping", "providers-sources"}
    provider = None
    for svc in services:
        if svc.startswith("apm-"):
            provider = svc.replace("apm-", "")
            break
        if svc.startswith("card-"):
            provider = svc.replace("card-", "")
            break
    if not provider:
        for svc in services:
            if svc.startswith("providers-") and svc not in PROVIDER_INFRA:
                provider = svc.replace("providers-", "")
                break

    # Root span info
    root_info = None
    if root_span:
        root_tags = _tag_map(root_span)
        root_info = {
            "service": svc_map.get(root_span["processID"], ""),
            "operation": root_tags.get("http.target", root_span["operationName"]),
            "total_duration_ms": round(root_span["duration"] / 1000, 1),
        }

    return {
        "trace_id": trace_id,
        "provider": provider,
        "root": root_info,
        "total_spans": len(spans),
        "services": services,
        "service_count": len(services),
        "inter_service_edges": edges,
        "external_calls": external_calls,
        "spans": span_infos,
    }


def format_text(summary: dict) -> str:
    """Format summary as human-readable table (like trace_charge_creation_sdk.txt)."""
    lines = []
    root = summary.get("root", {})
    provider = summary.get("provider", "unknown")

    lines.append(f"Provider: {provider}")
    lines.append(f"Trace: {summary['trace_id'][:12]}...")
    lines.append(f"Duration: {root.get('total_duration_ms', 0):.1f}ms | "
                 f"Services: {summary['service_count']} | Spans: {summary['total_spans']}")
    lines.append(f"Entry: {root.get('service', '?')} {root.get('operation', '?')}")
    lines.append("")

    # Service table (only inter-service, skip internal db/logging noise)
    lines.append(f"{'Service':<40} {'Operation':<55} {'Duration':>10}  Error")
    lines.append("-" * 110)

    for si in summary["spans"]:
        if si["kind"] == "internal":
            continue
        error_mark = f"  [ERROR] {si['error_message'] or ''}" if si["error"] else ""
        op = si["operation"][:54]
        lines.append(f"{si['service']:<40} {op:<55} {si['duration_ms']:>8.1f}ms{error_mark}")

    lines.append("")
    lines.append("Inter-service edges:")
    for e in summary["inter_service_edges"]:
        op_short = e["operation"].split(".")[-1] if "." in e["operation"] else e["operation"]
        lines.append(f"  {e['from']} -> {e['to']} ({op_short}) {e['duration_ms']:.1f}ms")

    if summary["external_calls"]:
        lines.append("")
        lines.append("External calls:")
        for ec in summary["external_calls"]:
            lines.append(f"  {ec['from']} -> {ec['url']} {ec['duration_ms']:.1f}ms")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/parse_jaeger_trace.py <trace.json> [--text] [--output <path>]")
        sys.exit(1)

    trace_path = Path(sys.argv[1])
    text_mode = "--text" in sys.argv
    output_path = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = Path(sys.argv[idx + 1])

    with open(trace_path) as f:
        data = json.load(f)

    if "data" not in data:
        print(f"Skipping {trace_path.name}: not a Jaeger trace export (keys: {list(data.keys())[:3]})")
        sys.exit(0)

    summary = parse_trace(data)

    if text_mode:
        print(format_text(summary))
    elif output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"Written to {output_path}")
        print(f"  Provider: {summary['provider']}")
        print(f"  Services: {summary['service_count']} | Spans: {summary['total_spans']}")
        print(f"  Inter-service edges: {len(summary['inter_service_edges'])}")
    else:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
