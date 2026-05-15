"""Field tracing tool — trace fields through the service chain.

Core principle: "Trace EVERY field through the full service chain to the final consumer."

Data sources (priority):
1. field-contracts.yaml — contract specs (cached)
2. trace-chains.yaml — hop chains (cached)
3. reference-snapshots.yaml — provider line references (cached)
4. grep on raw/ — fallback for unknown fields/providers
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading

from src.config import BASE_DIR, PROFILE_DIR

_logger = logging.getLogger(__name__)

# Warn early if PROFILE_DIR looks wrong (common when CODE_RAG_HOME is not set)
if not PROFILE_DIR.exists():
    _logger.warning(
        "PROFILE_DIR %s does not exist. trace_field will return empty results. "
        "Set CODE_RAG_HOME to the project root (e.g. ~/.code-rag-mcp).",
        PROFILE_DIR,
    )

# Shared provider-scoped repos that are NOT tied to any single provider.
_SHARED_PROVIDER_REPOS = frozenset({"credentials", "features", "proto"})


def _hop_provider_tag(hop: dict) -> str:
    """Return provider name if hop is scoped to a single provider, else "".

    Detects provider scope from:
      - service name: "grpc-apm-<provider>", "grpc-providers-<provider>"
      - service parens: "workflow-provider-webhooks (<provider>)"
      - file path: "activities/<provider>/..."
    """
    svc = hop.get("service", "") or ""
    file = hop.get("file", "") or ""
    blob = f"{svc} {file}"
    m = re.search(r"grpc-apm-([a-z0-9-]+?)(?:/|\s|$)", blob, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    m = re.search(r"grpc-providers-([a-z0-9-]+?)(?:/|\s|$)", blob, re.IGNORECASE)
    if m and m.group(1).lower() not in _SHARED_PROVIDER_REPOS:
        return m.group(1).lower()
    m = re.search(r"\(([a-z0-9-]+)\)", svc)
    if m:
        return m.group(1).lower()
    # Provider-scoped subfolder under shared repos:
    # activities/<provider>/... (workflow-provider-webhooks)
    # libs/<provider>/...       (grpc-providers-credentials, etc.)
    m = re.search(r"(?:activities|libs)/([a-z0-9-]+)/", file)
    if m and m.group(1).lower() not in _SHARED_PROVIDER_REPOS:
        return m.group(1).lower()
    return ""


def _filter_hops_by_provider(hops: list, provider: str) -> list:
    """Keep hops that are provider-neutral OR tagged to the requested provider."""
    if not provider:
        return hops
    result = []
    for hop in hops:
        if not isinstance(hop, dict):
            result.append(hop)
            continue
        tag = _hop_provider_tag(hop)
        if tag == "" or tag == provider:
            result.append(hop)
    return result


# --- Lazy-loaded YAML cache ---
_cache: dict[str, dict | list | None] = {}
_fields_lock = threading.Lock()

_REFS_DIR = PROFILE_DIR / "docs" / "references"
_RAW_DIR = BASE_DIR / "raw"

# Repos to grep when field not in YAML (provider integration scope)
_GREP_REPO_PREFIXES = (
    "grpc-apm-",
    "grpc-providers-",
    "grpc-payment-gateway",
    "workflow-provider-webhooks",
    "providers-proto",
)


def _load_ref_yaml(filename: str) -> dict:
    """Load a reference YAML file, cached after first call."""
    with _fields_lock:
        if filename not in _cache:
            path = _REFS_DIR / filename
            if path.exists():
                import yaml

                _cache[filename] = yaml.safe_load(path.read_text()) or {}
            else:
                _cache[filename] = {}
        return _cache[filename]  # type: ignore[return-value]


def _get_contracts() -> dict:
    return _load_ref_yaml("field-contracts.yaml")


def _get_chains() -> dict:
    return _load_ref_yaml("trace-chains.yaml")


def _get_snapshots() -> dict:
    return _load_ref_yaml("reference-snapshots.yaml")


def _find_field_in_contracts(field: str) -> tuple[str, dict] | None:
    """Find a field definition across all contracts. Returns (contract_name, field_spec)."""
    contracts = _get_contracts()
    # Top-level keys are contract names, values are dicts of fields
    for contract_name, contract_data in contracts.items():
        if not isinstance(contract_data, dict):
            continue
        # Fields can be directly under contract or under a "fields" key
        fields = contract_data.get("fields", contract_data)
        if field in fields and isinstance(fields[field], dict):
            return contract_name, fields[field]
        # Check subfields (e.g., "finalize.issuerResponseCode")
        parts = field.split(".")
        if len(parts) == 2 and parts[0] in fields and isinstance(fields[parts[0]], dict):
            parent = fields[parts[0]]
            subfields = parent.get("subfields", {})
            if parts[1] in subfields:
                return contract_name, subfields[parts[1]]
    return None


def _find_chain(field: str) -> dict | None:
    """Find a trace chain for a field."""
    chains = _get_chains()
    # Structure: {"fields": {"processorTransactionId": {...}, ...}}
    fields = chains.get("fields", chains)
    return fields.get(field)


def _find_snapshots(field: str, provider: str = "") -> list[dict]:
    """Find reference snapshots for a field, optionally filtered by provider."""
    snapshots = _get_snapshots()
    results = []
    # Structure: {"map-response": {"volt": {"file": ..., "fields": {"processorTransactionId": ...}}}}
    for section_name, section in snapshots.items():
        if not isinstance(section, dict):
            continue
        for prov_name, prov_data in section.items():
            if provider and provider != prov_name:
                continue
            if not isinstance(prov_data, dict):
                continue
            # Fields may be directly in prov_data or nested under "fields"
            fields = prov_data.get("fields", prov_data)
            if field in fields:
                results.append(
                    {
                        "section": section_name,
                        "provider": prov_name,
                        "file": prov_data.get("file", ""),
                        "detail": fields[field],
                    }
                )
    return results


def _grep_field(field: str, provider: str = "") -> str:
    """Grep for a field in raw/ repos as fallback."""
    if provider:
        # Grep specific provider repos
        targets = []
        for prefix in ("grpc-apm-", "grpc-providers-"):
            path = _RAW_DIR / f"{prefix}{provider}"
            if path.is_dir():
                targets.append(str(path))
        # Also check webhook handler
        wh_path = _RAW_DIR / "workflow-provider-webhooks" / "activities" / provider
        if wh_path.is_dir():
            targets.append(str(wh_path))
    else:
        # Grep all relevant repos
        targets = []
        if _RAW_DIR.is_dir():
            for d in _RAW_DIR.iterdir():
                if d.is_dir() and any(d.name.startswith(p) for p in _GREP_REPO_PREFIXES):
                    targets.append(str(d))

    if not targets:
        return ""

    try:
        result = subprocess.run(
            ["rg", "-n", "--glob", "*.js", "--glob", "*.proto", "-m", "3", field, *targets],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _mode_contract(field: str, provider: str) -> str:
    """Return contract specification for a field."""
    found = _find_field_in_contracts(field)
    if not found:
        grep = _grep_field(field, provider)
        if grep:
            return f"# Field Contract: {field}\n\nNo curated contract found. Grep results:\n\n```\n{grep}\n```\n"
        return f"Field `{field}` not found in contracts or code.\n"

    contract_name, spec = found
    lines = [f"# Field Contract: {field}\n"]
    lines.append(f"**Contract**: {contract_name}\n")
    for key in ("required", "type", "rule", "consumer_reads", "consumer_file", "if_missing", "if_wrong"):
        if key in spec:
            lines.append(f"- **{key}**: {spec[key]}")
    if "subfields" in spec:
        lines.append("\n**Subfields:**")
        for sf_name, sf_spec in spec["subfields"].items():
            req = sf_spec.get("required", "?")
            lines.append(f"- `{sf_name}` (required: {req})")
    lines.append("")
    return "\n".join(lines)


def _mode_trace(field: str, provider: str) -> str:
    """Return full trace chain for a field."""
    lines = [f"# Field Trace: {field}\n"]

    # Contract summary
    found = _find_field_in_contracts(field)
    if found:
        _, spec = found
        lines.append("## Contract")
        for key in ("required", "type", "if_missing", "if_wrong"):
            if key in spec:
                lines.append(f"- **{key.replace('_', ' ').title()}**: {spec[key]}")
        lines.append("")

    # Chain
    chain = _find_chain(field)
    if chain:
        hops = chain.get("hops", chain.get("links", chain.get("chain", [])))
        hops = _filter_hops_by_provider(hops, provider)
        header = f"## Chain ({len(hops)} hops"
        header += f", filtered to `{provider}`" if provider else ""
        header += ")\n"
        lines.append(header)
        for i, hop in enumerate(hops, 1):
            if isinstance(hop, dict):
                service = hop.get("service", hop.get("target", "?"))
                role = hop.get("role", "?")
                detail = hop.get("how", hop.get("detail", hop.get("reads", "")))
                line_num = hop.get("line", "")
                loc = f":{line_num}" if line_num else ""
                lines.append(f"{i}. **{service}{loc}** ({role})")
                if detail:
                    lines.append(f"   {detail}")
            elif isinstance(hop, str):
                lines.append(f"{i}. {hop}")
        if_missing = chain.get("if_missing", "")
        if_wrong = chain.get("if_wrong", "")
        if if_missing:
            lines.append(f"\n**If missing**: {if_missing}")
        if if_wrong:
            lines.append(f"**If wrong**: {if_wrong}")
        lines.append("")
    else:
        # Fallback: grep
        grep = _grep_field(field, provider)
        if grep:
            lines.append("## References (grep)\n")
            lines.append(f"```\n{grep}\n```\n")
        else:
            lines.append("No trace chain found.\n")

    return "\n".join(lines)


def _mode_consumers(field: str, provider: str) -> str:
    """Return who reads this field."""
    lines = [f"# Consumers: {field}\n"]

    found = _find_field_in_contracts(field)
    if found:
        contract_name, spec = found
        lines.append(f"**Contract**: {contract_name}\n")
        if "consumer_reads" in spec:
            lines.append(f"- **Reads as**: `{spec['consumer_reads']}`")
        if "consumer_file" in spec:
            lines.append(f"- **In file**: {spec['consumer_file']}")
        if "consumed_by" in spec:
            if isinstance(spec["consumed_by"], list):
                lines.append("\n**Consumed by:**")
                for c in spec["consumed_by"]:
                    lines.append(f"- {c}")
            else:
                lines.append(f"- **Consumed by**: {spec['consumed_by']}")
        if "if_missing" in spec:
            lines.append(f"- **If missing**: {spec['if_missing']}")
        lines.append("")

    # Also show consumer hops from chain
    chain = _find_chain(field)
    if chain:
        hops = chain.get("hops", chain.get("links", chain.get("chain", [])))
        hops = _filter_hops_by_provider(hops, provider)
        consumer_hops = [
            h for h in hops if isinstance(h, dict) and h.get("role") in ("consumer", "transformer", "stores")
        ]
        if consumer_hops:
            lines.append("## Consumer Hops\n")
            for hop in consumer_hops:
                service = hop.get("service", "?")
                reads = hop.get("reads", hop.get("how", ""))
                line_num = hop.get("line", "")
                loc = f":{line_num}" if line_num else ""
                lines.append(f"- **{service}{loc}** — {reads}")
            lines.append("")

    if len(lines) <= 2:
        grep = _grep_field(field, provider)
        if grep:
            lines.append(f"## Grep Results\n\n```\n{grep}\n```\n")

    return "\n".join(lines)


def _mode_compare(field: str, provider: str) -> str:
    """Compare a field across providers."""
    lines = [f"# Field Compare: {field}\n"]

    snapshots = _find_snapshots(field)
    if snapshots:
        lines.append("| Provider | Section | File | Detail |")
        lines.append("|----------|---------|------|--------|")
        for snap in snapshots:
            lines.append(f"| {snap['provider']} | {snap['section']} | {snap['file']} | {snap['detail']} |")
        lines.append("")

    # Add grep for specific provider if requested
    if provider:
        grep = _grep_field(field, provider)
        if grep:
            lines.append(f"## {provider} (grep)\n\n```\n{grep}\n```\n")

    if len(lines) <= 2:
        lines.append("No snapshots found for this field.\n")

    return "\n".join(lines)


def trace_field_tool(field: str, provider: str = "", mode: str = "trace") -> str:
    """Trace a field through the service chain — from producer to final consumer.

    Core principle: every field change must be traced through ALL services.

    Args:
        field: Field name to trace (e.g., "processorTransactionId", "finalize.issuerResponseCode")
        provider: Optional provider name to filter results (e.g., "payper", "volt")
        mode: Query type — "trace" (full chain), "consumers" (who reads it),
              "compare" (cross-provider), "contract" (field spec)
    """
    field = field.strip()
    provider = provider.strip().lower()
    mode = mode.strip().lower()

    if not field:
        return "Error: field parameter is required."

    if mode == "contract":
        return _mode_contract(field, provider)
    elif mode == "trace":
        return _mode_trace(field, provider)
    elif mode == "consumers":
        return _mode_consumers(field, provider)
    elif mode == "compare":
        return _mode_compare(field, provider)
    else:
        return f"Error: unknown mode '{mode}'. Use: trace, consumers, compare, contract."
