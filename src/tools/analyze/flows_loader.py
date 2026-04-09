"""Flows archetype pattern loader — reads pre-computed frequency YAMLs.

flows/archetypes/{archetype}.yaml holds per-archetype statistics computed
from flows/tasks/{TASK_ID}.yaml corpus. For each archetype we know which
repos changed always/common/sometimes/rarely, and which edge patterns
recur across tasks.

The final_ranker uses this to bias the LLM toward historically-frequent
repos ("always_changed" repos should almost never be dropped for this
archetype).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import yaml

from src.config import PROFILE_DIR

# TTL-based cache: re-reads YAML files after 300s so daemon picks up
# changes from nightly builds without a restart.
_TTL_SECONDS = 300
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _ttl_cache_get(key: str) -> object | None:
    """Return cached value if within TTL, else None."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.monotonic() - ts > _TTL_SECONDS:
            del _cache[key]
            return None
        return val


def _ttl_cache_set(key: str, val: object) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), val)

# Known archetypes emitted by the generator/auditor pipeline.
KNOWN_ARCHETYPES = frozenset({
    "new_apm_provider",
    "add_apm_method",
    "webhook_event",
    "schema_change",
    "new_card_provider",
    "other",
})


def load_archetype_pattern(archetype: str) -> dict | None:
    """Return parsed YAML dict for archetype, or None if missing/unknown."""
    if archetype not in KNOWN_ARCHETYPES:
        return None
    key = f"archetype:{archetype}"
    cached = _ttl_cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    path: Path = PROFILE_DIR / "flows" / "archetypes" / f"{archetype}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    result = data if isinstance(data, dict) else None
    if result is not None:
        _ttl_cache_set(key, result)
    return result


def extract_frequency_buckets(pattern: dict) -> dict[str, list[str]]:
    """Extract repo names grouped by frequency from a pattern YAML.

    Returns {"always": [...], "common": [...], "sometimes": [...], "rarely": [...]}.
    Each list contains bare repo names (strips any " (repo missing locally...)" suffix).
    """
    buckets: dict[str, list[str]] = {"always": [], "common": [], "sometimes": [], "rarely": []}
    changed = pattern.get("changed_repos_by_frequency") or {}
    for tier in ("always", "common", "sometimes", "rarely"):
        entries = changed.get(tier) or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            repo = str(entry.get("repo", "")).strip()
            # Strip auditor's appended annotations.
            if " (" in repo:
                repo = repo.split(" (", 1)[0].strip()
            if repo:
                buckets[tier].append(repo)
    return buckets


def extract_top_edges(pattern: dict, limit: int = 5) -> list[dict]:
    """Return top-N edge patterns with {pattern, count, pct}."""
    edges = pattern.get("top_edge_patterns") or []
    out: list[dict] = []
    for edge in edges[:limit]:
        if isinstance(edge, dict) and edge.get("pattern"):
            out.append({
                "pattern": str(edge["pattern"]),
                "count": int(edge.get("count", 0)),
                "pct": float(edge.get("pct", 0.0)),
            })
    return out


def load_provider_pattern(provider: str) -> dict | None:
    """Return parsed YAML dict for provider, or None if missing."""
    if not provider:
        return None
    key = f"provider:{provider}"
    cached = _ttl_cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    path: Path = PROFILE_DIR / "flows" / "providers" / f"{provider}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    result = data if isinstance(data, dict) else None
    if result is not None:
        _ttl_cache_set(key, result)
    return result


def provider_summary_for_prompt(provider: str) -> dict | None:
    """Return compact provider summary for the LLM prompt, or None if unavailable.

    Output shape:
      {
        "provider": <name>,
        "task_count": N,
        "tasks": [task_ids],
        "changed_repos": [...],       # union across all tasks
        "checklist_repos": [...],     # repos flagged to verify
        "features_supported": [...],
      }
    """
    pattern = load_provider_pattern(provider)
    if not pattern:
        return None
    return {
        "provider": provider,
        "task_count": int(pattern.get("task_count", 0)),
        "tasks": list(pattern.get("tasks", []) or []),
        "changed_repos": list(pattern.get("changed_repos", []) or []),
        "checklist_repos": list(pattern.get("checklist_repos", []) or []),
        "features_supported": list(pattern.get("features_supported", []) or []),
    }


def load_provider_trace_flow(provider: str) -> dict | None:
    """Load runtime trace flow for a provider from trace_flows/providers/{p}.yaml."""
    if not provider:
        return None
    key = f"trace_flow:{provider}"
    cached = _ttl_cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    path: Path = PROFILE_DIR / "trace_flows" / "providers" / f"{provider}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    result = data if isinstance(data, dict) else None
    if result is not None:
        _ttl_cache_set(key, result)
    return result


def _load_canonical_template() -> dict | None:
    """Load canonical APM flow template (fallback for unknown providers)."""
    key = "canonical_apm_flow"
    cached = _ttl_cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    path: Path = PROFILE_DIR / "trace_flows" / "canonical_apm_flow.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    result = data if isinstance(data, dict) else None
    if result is not None:
        _ttl_cache_set(key, result)
    return result


def _canonical_summary_for_prompt(provider: str) -> dict | None:
    """Build trace_flow summary from canonical template for an unknown provider.

    Transforms canonical_apm_flow.yaml structure into the same shape as
    trace_flow_summary_for_prompt() so the ranker can consume it identically.
    """
    canonical = _load_canonical_template()
    if not canonical:
        return None

    phases = canonical.get("phases", {})
    svc_to_repo = canonical.get("service_to_repo", {})

    def _svc_to_repo(svc: str) -> str:
        """Map service name to repo, handling {provider} placeholders."""
        resolved = svc.replace("{provider}", provider)
        # Try exact match first, then resolved match
        repo = svc_to_repo.get(resolved, "")
        if not repo:
            repo = svc_to_repo.get(svc, resolved)
        # Apply repo naming convention for unresolved services
        if repo == resolved and not repo.startswith(("grpc-", "express-", "workflow-", "loggers-")):
            if resolved.startswith("apm-"):
                repo = f"grpc-{resolved}"
            elif resolved.startswith("providers-"):
                repo = f"grpc-{resolved}"
        return repo.replace("{provider}", provider)

    # Collect participants from always + common + provider services
    init_phase = phases.get("initialization", {})
    participants: list[str] = []
    for svc_list_key in ("always_services", "common_services", "provider_services"):
        for svc in init_phase.get(svc_list_key, []):
            repo = _svc_to_repo(svc)
            if repo and repo not in participants:
                participants.append(repo)

    # Webhook phase participants
    webhook_phase = phases.get("webhook_sale", {})
    for svc_list_key in ("additional_services", "webhook_entry"):
        for svc in webhook_phase.get(svc_list_key, []):
            repo = _svc_to_repo(svc)
            if repo and repo not in participants:
                participants.append(repo)

    # Phases present
    phases_present = [name for name in phases if name != "config"]

    # Key edges from always_edges
    key_edges: dict[str, list[str]] = {}
    init_edges = init_phase.get("always_edges", [])
    top_init: list[str] = []
    for edge_str in init_edges[:5]:
        edge_str = edge_str.replace("{provider}", provider)
        top_init.append(edge_str)
    if top_init:
        key_edges["initialization"] = top_init

    return {
        "provider": provider,
        "integration_type": "canonical_apm_template",
        "participants": participants,
        "phases_present": phases_present,
        "key_edges_by_phase": key_edges,
        "is_canonical": True,
    }


def trace_flow_summary_for_prompt(provider: str) -> dict | None:
    """Return compact trace flow summary for the LLM prompt.

    Falls back to canonical APM template when no provider-specific trace flow
    exists. The canonical template is derived from 10 real Jaeger traces across
    10 providers and captures the common infrastructure pattern.

    Output shape:
      {
        "provider": <name>,
        "integration_type": <value>,
        "participants": [<repo names, excluding external:*>],
        "phases_present": [initialization, webhook, ...],
        "key_edges_by_phase": {
          "initialization": [<up to 3 most important edges as "from → to (op)">],
          "webhook": [...],
          ...
        },
        "is_canonical": bool  # True when using canonical fallback
      }
    """
    tf = load_provider_trace_flow(provider)
    if not tf:
        # Fallback: use canonical APM template
        return _canonical_summary_for_prompt(provider)

    participants_raw = tf.get("participants", []) or []
    internal_participants = [
        p.get("name", "") for p in participants_raw
        if isinstance(p, dict) and p.get("name") and not p["name"].startswith("external:")
    ]

    phases_data = tf.get("phases", {}) or {}
    phases_present = [name for name, phase in phases_data.items()
                      if isinstance(phase, dict) and phase.get("steps")]

    key_edges: dict[str, list[str]] = {}
    for phase_name, phase in phases_data.items():
        if not isinstance(phase, dict):
            continue
        steps = phase.get("steps", []) or []
        top_edges: list[str] = []
        for step in steps[:3]:
            if not isinstance(step, dict):
                continue
            frm = step.get("from", "?")
            to = step.get("to", "?")
            op = step.get("operation", "")
            edge = f"{frm} → {to} ({op})" if op else f"{frm} → {to}"
            top_edges.append(edge)
        if top_edges:
            key_edges[phase_name] = top_edges

    return {
        "provider": provider,
        "integration_type": tf.get("integration_type", ""),
        "participants": internal_participants,
        "phases_present": phases_present,
        "key_edges_by_phase": key_edges,
        "is_canonical": False,
    }


def summary_for_prompt(archetype: str) -> dict | None:
    """Return a compact summary for the LLM prompt, or None if unavailable.

    Output shape:
      {
        "archetype": <name>,
        "sample_size": N,
        "always_changed": [...],   # 100% frequency
        "common_changed": [...],   # ≥67%
        "sometimes_changed": [...],# 33-66%
        "rarely_changed": [...],   # <33%
        "top_edges": [{pattern, count, pct}, ...],
      }
    """
    pattern = load_archetype_pattern(archetype)
    if not pattern:
        return None
    buckets = extract_frequency_buckets(pattern)
    return {
        "archetype": archetype,
        "sample_size": int(pattern.get("sample_size", 0)),
        "always_changed": buckets["always"],
        "common_changed": buckets["common"],
        "sometimes_changed": buckets["sometimes"],
        "rarely_changed": buckets["rarely"],
        "top_edges": extract_top_edges(pattern, limit=5),
    }
