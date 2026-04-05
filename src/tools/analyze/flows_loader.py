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

from functools import lru_cache
from pathlib import Path

import yaml

from src.config import PROFILE_DIR

# Known archetypes emitted by the generator/auditor pipeline.
KNOWN_ARCHETYPES = frozenset({
    "new_apm_provider",
    "add_apm_method",
    "webhook_event",
    "schema_change",
    "new_card_provider",
    "other",
})


@lru_cache(maxsize=16)
def load_archetype_pattern(archetype: str) -> dict | None:
    """Return parsed YAML dict for archetype, or None if missing/unknown."""
    if archetype not in KNOWN_ARCHETYPES:
        return None
    path: Path = PROFILE_DIR / "flows" / "archetypes" / f"{archetype}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


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


@lru_cache(maxsize=32)
def load_provider_pattern(provider: str) -> dict | None:
    """Return parsed YAML dict for provider, or None if missing."""
    if not provider:
        return None
    path: Path = PROFILE_DIR / "flows" / "providers" / f"{provider}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


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


@lru_cache(maxsize=32)
def load_provider_trace_flow(provider: str) -> dict | None:
    """Load runtime trace flow for a provider from trace_flows/providers/{p}.yaml."""
    if not provider:
        return None
    path: Path = PROFILE_DIR / "trace_flows" / "providers" / f"{provider}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def trace_flow_summary_for_prompt(provider: str) -> dict | None:
    """Return compact trace flow summary for the LLM prompt.

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
        }
      }
    """
    tf = load_provider_trace_flow(provider)
    if not tf:
        return None

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
