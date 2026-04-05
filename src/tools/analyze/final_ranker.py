"""Final LLM repo ranker — precision-oriented pruning for analyze_task.

analyze_task accumulates candidates from ~20 sections, each with its own
heuristic. Generic sections (keyword_scan, npm_dep_scan, fanout) add many
repos that happen to share a word but are not task-relevant. The result
is ~150+ candidates, most of which are noise.

This module runs a single LLM call at the END of analysis that:
  1. Inspects WHY each candidate was added (the source sections)
  2. Compares the task description against similar historical tasks
     (ground-truth repos_changed from task_history)
  3. Classifies each candidate into high (core) / medium (related) / drop

ctx.findings is rebuilt from the ranker output:
  - tier=high   → confidence="high"   (shows in "core" count)
  - tier=medium → confidence="medium" (shows in "related" count)
  - tier=drop   → removed from findings (not shown to caller)

Design principles:
  - Recall guardrail: ranker is instructed NOT to drop a repo that appears
    in any similar historical task's repos_changed.
  - Evidence-weighted: each finding's ftype is annotated as STRONG/MEDIUM/
    WEAK in the prompt so the LLM has an explicit signal.
  - Deterministic: temperature=0, structured JSON output.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

from .base import AnalysisContext, Finding
from .flows_loader import provider_summary_for_prompt, summary_for_prompt

# ------------------------------------------------------------------
# Evidence classification — tells the LLM how much to trust each source.
#
# Only very specific, task-targeted signals count as STRONG. Broad
# section outputs (gotchas docs, provider fan-out, generic webhook
# handlers) are MEDIUM because they fire for many repos per query.
# ------------------------------------------------------------------
STRONG_SOURCES = frozenset({
    "recipe",              # evidence-based pattern match (evidence >= 4 historical tasks)
    "domain_template",     # historical domain base repos
    "repo_ref",            # literally named in description
    "pr_url_signal",       # PR URL in description
    "co_change_rule",      # curated co-change pair from conventions.yaml
})

MEDIUM_SOURCES = frozenset({
    "provider",            # provider service hit (broader fan-out)
    "gateway",             # payment gateway keyword match
    "proto",               # proto/schema match
    "gotchas",             # any docs/GOTCHAS.md keyword match
    "webhook",             # webhook-handler match
    "similar_task",        # task_history similarity match
    "co-occurrence",       # statistical co-occurrence
    "cascade",             # upstream chain
    "downstream",          # downstream chain
    "function",            # grpc method exists here
    "pattern",             # historical pattern
    "domain",              # domain-level hit
})

WEAK_SOURCES = frozenset({
    "keyword",             # broad keyword_scan match
    "keyword_scan",
    "npm_dep_scan",        # transitive npm dep shares a keyword
    "fanout",              # provider fan-out (every provider repo)
    "universal",           # generic fallback
    "bulk_migration",      # bulk-scope heuristic
    "reverse_cascade",
})

# Minimum findings count before ranker is worth invoking (below this the
# caller's output is already small enough to read).
MIN_CANDIDATES = 15

MODEL = "gemini-2.5-flash"


def _classify_evidence(sources: list[str]) -> tuple[str, list[str]]:
    """Classify the strongest evidence tier present in a repo's sources.

    Returns (strongest_tier, sorted_unique_sources).
    """
    unique = sorted(set(sources))
    if any(s in STRONG_SOURCES for s in unique):
        return "STRONG", unique
    if any(s in MEDIUM_SOURCES for s in unique):
        return "MEDIUM", unique
    return "WEAK", unique


_PROVIDER_REPO_PATTERNS = (
    re.compile(r"^grpc-apm-([a-z0-9-]+)$"),
    re.compile(r"^grpc-providers-([a-z0-9-]+)$"),
    re.compile(r"^express-webhooks-([a-z0-9-]+)$"),
    re.compile(r"^workflow-([a-z0-9-]+)-webhooks$"),
    re.compile(r"^grpc-connections-([a-z0-9-]+)$"),
)


def _load_known_providers(conn: sqlite3.Connection) -> set[str]:
    """Return set of real provider names, derived from grpc-apm-* repo names.

    grpc-apm-* is the canonical source of APM provider names. Anything
    matching a _PROVIDER_REPO_PATTERNS suffix that is NOT in this set is
    treated as a generic utility repo (grpc-apm-configurations, etc.).
    """
    try:
        rows = conn.execute(
            "SELECT name FROM repos WHERE name LIKE 'grpc-apm-%'"
        ).fetchall()
    except Exception:
        return set()
    providers = set()
    for (name,) in rows:
        if name.startswith("grpc-apm-"):
            providers.add(name[len("grpc-apm-"):])
    return providers


def _other_provider_tag(repo: str, known_providers: set[str]) -> str:
    """If repo is scoped to a provider OTHER THAN known_providers members, return "".

    Returns the provider tag only when it is an actual known provider name.
    """
    for pat in _PROVIDER_REPO_PATTERNS:
        m = pat.match(repo)
        if m:
            tag = m.group(1)
            if tag in known_providers:
                return tag
            return ""  # unknown tag → treat as generic (e.g. grpc-apm-configurations)
    return ""


def _prefilter_other_providers(
    sources_by_repo: dict[str, list[str]],
    current_provider: str,
    known_providers: set[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """Remove candidates scoped to a DIFFERENT known provider than the task's.

    Returns (filtered_sources, dropped_repos).
    """
    if not current_provider:
        return sources_by_repo, []
    filtered: dict[str, list[str]] = {}
    dropped: list[str] = []
    for repo, srcs in sources_by_repo.items():
        tag = _other_provider_tag(repo, known_providers)
        if tag and tag != current_provider:
            dropped.append(repo)
        else:
            filtered[repo] = srcs
    return filtered, dropped


def _get_similar_tasks_with_repos(
    conn: sqlite3.Connection,
    description: str,
    exclude_task_id: str,
    task_prefix: str = "",
) -> list[dict]:
    """Return up to 3 similar tasks with their repos_changed for LLM context.

    When task_prefix is given (e.g. "PI"), results are filtered to task IDs
    starting with that prefix so the context stays within the same domain
    (a Payper APM task should not be shown CORE platform tasks as context).
    """
    words = re.findall(r"[a-zA-Z]{4,}", description.lower())
    if not words:
        return []
    stop = {
        "should", "which", "where", "their", "about", "these", "those",
        "would", "could", "check", "start", "needs", "task", "implement",
        "create", "with", "that", "from", "into", "when", "have", "been",
    }
    terms = [w for w in words if w not in stop and len(w) > 4][:5]
    if not terms:
        return []

    try:
        fts_query = " OR ".join(terms)
        # Fetch more than 3 so we can filter by prefix afterward.
        rows = conn.execute(
            """SELECT t.ticket_id, t.summary, t.repos_changed
               FROM task_history_fts fts
               JOIN task_history t ON t.id = fts.rowid
               WHERE task_history_fts MATCH ?
               ORDER BY rank LIMIT 20""",
            (fts_query,),
        ).fetchall()
    except Exception:
        return []

    exclude_upper = (exclude_task_id or "").upper()
    prefix_upper = (task_prefix or "").upper() + "-" if task_prefix else ""
    out: list[dict] = []
    for r in rows:
        tid_upper = r[0].upper()
        if tid_upper == exclude_upper:
            continue
        if prefix_upper and not tid_upper.startswith(prefix_upper):
            continue
        try:
            repos = json.loads(r[2]) if r[2] else []
        except Exception:
            repos = []
        if not repos:
            continue
        out.append({
            "ticket_id": r[0],
            "summary": (r[1] or "")[:100],
            "repos_changed": repos,
        })
        if len(out) >= 3:
            break
    return out


def _distill_recurring_repos(similar_tasks: list[dict]) -> list[dict]:
    """Keep only repos that appear in ≥2 similar tasks (recurring patterns).

    Loose FTS matches bring in tasks that touch many unrelated repos. Showing
    every repo from every similar task as "must-keep ground truth" inflates
    the kept set. Restricting to repos that recur across ≥2 of the top
    matches filters out one-off noise.
    """
    if len(similar_tasks) < 2:
        # With 0 or 1 tasks we have no recurrence signal — pass through as-is.
        return similar_tasks
    from collections import Counter
    counter: Counter = Counter()
    for t in similar_tasks:
        for repo in set(t.get("repos_changed", [])):
            counter[repo] += 1
    recurring = {r for r, n in counter.items() if n >= 2}
    distilled: list[dict] = []
    for t in similar_tasks:
        kept = [r for r in t.get("repos_changed", []) if r in recurring]
        if kept:
            distilled.append({
                "ticket_id": t["ticket_id"],
                "summary": t["summary"],
                "repos_changed": kept,
            })
    return distilled


def _flows_evidence_enabled() -> bool:
    """Env flag for A/B testing archetype evidence in the ranker.

    Set FLOWS_EVIDENCE=0 to disable (baseline). Any other value (or unset)
    keeps it enabled.
    """
    return os.getenv("FLOWS_EVIDENCE", "1") != "0"


# Keyword → archetype hints. Ordered by specificity (first match wins
# within each branch).
_NEW_PROVIDER_HINTS = (
    "new provider", "integrate new", "add new provider", "integration of",
    "integrate ", "new apm", "new payment provider",
)
_CARD_PROVIDER_HINTS = (
    "card provider", "3ds", "3-d secure", "direct-api", "direct api",
    "card brand", "card network", "eci", "acquirer",
)
_ADD_METHOD_HINTS = (
    "add payment method", "new payment method", "add method",
    "pay by bank", "pbba", "bizum", "pix ", "interac", "etransfer",
    "new apm method", "enable method", "add support for",
)
_WEBHOOK_HINTS = (
    "webhook handler", "webhook handling", "webhook event", "timeout webhook",
    "handle webhook", "webhook reason", "dmn ", "notification handling",
)


def _infer_archetype(
    description: str, domain: str, provider: str
) -> str:
    """Heuristic archetype classifier from task description + domain.

    Returns one of: new_apm_provider, add_apm_method, webhook_event,
    schema_change, new_card_provider, other.
    """
    desc_lc = (description or "").lower()
    dom_lc = (domain or "").lower()

    # CORE-prefixed or core-* domain → schema/migration work.
    if dom_lc.startswith("core-") or dom_lc == "core":
        return "schema_change"
    if re.search(r"\bcore-\d+", description or "", re.IGNORECASE):
        return "schema_change"

    # Non-PI domain (BO/HS/unknown) → other.
    if not dom_lc.startswith("pi"):
        return "other"

    has_provider = bool(provider)
    has_new_hint = any(h in desc_lc for h in _NEW_PROVIDER_HINTS)
    has_card_hint = any(h in desc_lc for h in _CARD_PROVIDER_HINTS)
    has_add_method_hint = any(h in desc_lc for h in _ADD_METHOD_HINTS)
    has_webhook_hint = any(h in desc_lc for h in _WEBHOOK_HINTS)

    # Priority chain — more specific archetypes first.
    if has_provider and has_new_hint and has_card_hint:
        return "new_card_provider"
    if has_provider and has_new_hint:
        return "new_apm_provider"
    if has_webhook_hint and has_provider and not has_new_hint and not has_add_method_hint:
        return "webhook_event"
    if has_provider and has_add_method_hint:
        return "add_apm_method"
    if has_provider:
        # Provider is known but no strong specialisation → assume incremental change.
        return "add_apm_method"
    return "other"


def _task_prefix_from_domain(domain: str) -> str:
    """Map classifier domain to task_history ticket_id prefix."""
    d = (domain or "").split("+")[0].lower()
    if d in ("pi",):
        return "PI"
    if d.startswith("core"):
        return "CORE"
    if d == "bo":
        return "BO"
    if d == "hs":
        return "HS"
    return ""


def _format_archetype_section(archetype_summary: dict | None) -> str:
    """Format the archetype frequency block for the prompt, or "" if absent."""
    if not archetype_summary:
        return ""
    sample_size = archetype_summary.get("sample_size", 0)
    if sample_size < 2:
        # Not enough corpus evidence to be useful.
        return ""

    def _fmt_list(label: str, repos: list[str]) -> str:
        if not repos:
            return ""
        return f"  {label} ({len(repos)}): {repos}\n"

    edges_lines = ""
    for edge in archetype_summary.get("top_edges", []):
        pct_str = f"{edge['pct']:.0%}"
        edges_lines += f"    - {edge['pattern']}  [{edge['count']}x, {pct_str}]\n"

    section = (
        f"\n## Historical archetype pattern ({archetype_summary['archetype']})\n"
        f"_Sample: {sample_size} past tasks of this type._\n\n"
        "Repos that changed in past tasks of this archetype (grouped by frequency):\n"
    )
    section += _fmt_list("always_changed [100%]", archetype_summary["always_changed"])
    section += _fmt_list("common_changed [≥67%]", archetype_summary["common_changed"])
    section += _fmt_list("sometimes_changed [33-66%]", archetype_summary["sometimes_changed"])
    # rarely tier intentionally omitted — too noisy to bias the LLM.
    if edges_lines:
        section += "\nTop runtime edges from this archetype:\n" + edges_lines
    section += (
        "\nArchetype guidance (apply AFTER provider-mismatch rule; per-task evidence always wins):\n"
        "  - Repos in `always_changed` are strong signals for tier=high, but\n"
        "    concrete task evidence (description, PR data, reviewer flags) overrides.\n"
        "  - Repos in `common_changed` favour tier=high/medium when the task\n"
        "    touches that layer.\n"
        "  - Repos in `sometimes_changed` are neutral — judge on task specifics.\n"
        "  - Repos NOT in any archetype tier are still VALID if task evidence\n"
        "    (keywords, PR urls, co-change rules, reviewer mentions) supports them.\n"
        "    Do NOT drop them merely for missing from archetype tiers.\n"
        "  - IMPORTANT: repos named `grpc-apm-<provider>`, `grpc-providers-<provider>`,\n"
        "    or `workflow-<provider>-*` are ALWAYS candidates for tier=high when the\n"
        "    task description or evidence mentions that provider, regardless of\n"
        "    archetype tier membership.\n"
    )
    return section


def _format_provider_section(provider_summary: dict | None) -> str:
    """Format the provider historical pattern block, or "" if absent."""
    if not provider_summary:
        return ""
    task_count = provider_summary.get("task_count", 0)
    if task_count < 1:
        return ""

    provider = provider_summary["provider"]
    changed = provider_summary.get("changed_repos", [])
    checklist = provider_summary.get("checklist_repos", [])
    features = provider_summary.get("features_supported", [])

    section = (
        f"\n## Historical provider pattern ({provider})\n"
        f"_From {task_count} past tasks touching this provider._\n\n"
    )
    if changed:
        section += f"Repos historically changed for `{provider}` ({len(changed)}): {changed}\n"
    if checklist:
        section += f"Repos historically flagged for verification ({len(checklist)}): {checklist}\n"
    if features:
        section += f"Features supported: {features}\n"
    section += (
        "\nProvider guidance: if the current task touches this provider, repos\n"
        "in `changed_repos` above are strong candidates for tier=high/medium.\n"
        "Provider-specific repos (grpc-apm-<provider>, grpc-providers-<provider>)\n"
        "should be tier=high when task work touches the provider's domain.\n"
    )
    return section


def _build_prompt(
    description: str,
    classification_info: dict,
    candidates: list[dict],
    similar_tasks: list[dict],
    archetype_summary: dict | None = None,
    provider_summary: dict | None = None,
) -> str:
    """Build the ranker prompt. Returns full text for Gemini."""
    similar_json = json.dumps(similar_tasks, indent=2) if similar_tasks else "(none)"
    cand_json = json.dumps(candidates, indent=2)
    archetype_section = _format_archetype_section(archetype_summary)
    provider_section = _format_provider_section(provider_summary)

    return f"""You are pruning a noisy candidate repo list for a software engineering task on a payment platform. The candidate list was generated by ~20 heuristic analyzers; many of them over-include. Your job is precision: keep repos that actually need code changes, drop the rest.

## Task
{description}

## Classification
{json.dumps(classification_info, indent=2)}

## Source strength
Each candidate has a `sources` list (which analyzer sections added it) and an `evidence` tier:

  STRONG — recipe, domain_template, repo_ref, pr_url_signal, co_change_rule
    (these fire for 1-5 repos per query, task-targeted)
  MEDIUM — provider, gateway, proto, gotchas, webhook, similar_task, co-occurrence, cascade, downstream, function, pattern, domain
    (these often fire for many repos, need per-repo judgement)
  WEAK   — keyword, keyword_scan, npm_dep_scan, fanout, universal, bulk_migration, reverse_cascade
    (broad pattern matches, usually noise)

Decision rules (PRIORITY ORDERED — top rule wins on conflict):

  1. DROP OTHER-PROVIDER REPOS regardless of evidence strength.
     If a repo name is clearly scoped to a DIFFERENT provider than the
     current task's provider (e.g. grpc-apm-volt, grpc-providers-silverflow
     for a Payper task) → drop. Provider-fanout analyzers add these as
     noise; they cannot possibly contain code changes for another provider.

  2. KEEP STRONG evidence (after rule 1). Repos with ≥1 STRONG source and
     not provider-mismatched → tier=high or tier=medium.

  3. KEEP repos in similar_tasks.repos_changed (after rule 1). Cross-task
     ground truth is authoritative → tier=medium.

  4. MEDIUM evidence not in similar_tasks → keep ONLY IF the repo name/purpose
     plausibly fits THIS specific task. When uncertain → drop.

  5. WEAK-only evidence not in similar_tasks → drop.

Quotas (HARD):
  - tier="high": 4-5 repos (repos that WILL contain new source code).
  - tier="medium": EXACTLY 5-7 repos (no more). These must be repos where
    you can name a specific file/function that will be touched. Be strict.
  - tier="drop": everything else.

Target total kept: 9-12. Do not exceed. Do not put "shared infrastructure"
repos in medium just because they could conceivably be involved
(e.g. backoffice-web, express-api-callbacks, e2e-tests, graphql,
kafka-cdc-sink, grpc-graphql-authorization). These are called by MANY
tasks but rarely contain new code. Drop unless there is a concrete
change required.

"Possibly related", "potentially involved", "might need updates" are
NOT sufficient reasons — drop those.

## Similar past tasks (ground-truth repos_changed)
{similar_json}
{archetype_section}{provider_section}
## Candidates ({len(candidates)})
{cand_json}

Return ONLY a JSON array (no markdown fences, no prose) with one entry per candidate:
  [
    {{"repo": "<name>", "tier": "high"|"medium"|"drop", "rank": <int or null>, "reason": "<short>"}}
  ]

Tier assignment:
  - tier="high" with rank 1-5 → 3-5 most central services (DEFINITELY touched)
  - tier="medium" with rank 6+ → supporting repos (rules 1 and 2 mandatory)
  - tier="drop" with rank=null → noise (only WEAK evidence not in similar tasks)

Ordering: return sorted by rank ascending; put drops at the end."""


def _call_gemini(prompt: str) -> list[dict] | None:
    """Call Gemini 2.5 Flash, return parsed JSON or None on failure.

    Tries each key in GEMINI_API_KEYS in turn on quota (429) errors.
    """
    from src.config import GEMINI_API_KEYS
    if not GEMINI_API_KEYS:
        return None
    try:
        from google import genai
    except ImportError:
        return None

    response = None
    last_err = None
    for idx, api_key in enumerate(GEMINI_API_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=MODEL,
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                config={"temperature": 0.0},
            )
            break
        except Exception as e:
            last_err = e
            err_str = str(e)
            # Rotate key on quota/rate-limit errors; abort on other errors.
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                print(f"[final_ranker] key #{idx+1} quota exceeded, trying next", file=sys.stderr)
                continue
            print(f"[final_ranker] Gemini call failed: {e}", file=sys.stderr)
            return None
    if response is None:
        print(f"[final_ranker] all keys exhausted, last error: {last_err}", file=sys.stderr)
        return None

    text = (response.text or "").strip()
    # Strip markdown fences if any.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Attempt to locate the first JSON array in the output.
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            print(f"[final_ranker] No JSON array in response: {text[:200]!r}", file=sys.stderr)
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            print(f"[final_ranker] Failed to parse JSON: {e}", file=sys.stderr)
            return None

    if isinstance(data, dict):
        # Some models wrap in {"repos": [...]}
        for key in ("repos", "results", "ranked"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return None
    if not isinstance(data, list):
        return None
    return data


def section_final_ranker(ctx: AnalysisContext, classification: object) -> str:
    """Run the final LLM ranker if enough candidates. Rewrites ctx.findings.

    Returns a markdown section describing kept and dropped repos.
    Empty string if skipped (too few candidates, API unavailable, or parse fail).
    """
    # Aggregate sources per repo.
    sources_by_repo: dict[str, list[str]] = {}
    for f in ctx.findings:
        sources_by_repo.setdefault(f.repo, []).append(f.ftype)

    if len(sources_by_repo) < MIN_CANDIDATES:
        return ""

    # Pre-filter repos scoped to a DIFFERENT provider than this task's.
    # Provider fan-out analyzers add these as noise; they can't contain
    # code changes for another provider.
    known_providers = _load_known_providers(ctx.conn)
    sources_by_repo, prefiltered = _prefilter_other_providers(
        sources_by_repo, ctx.provider, known_providers
    )

    candidates: list[dict] = []
    for repo, srcs in sources_by_repo.items():
        evidence, unique = _classify_evidence(srcs)
        candidates.append({
            "repo": repo,
            "sources": unique,
            "evidence": evidence,
        })

    classification_info = {
        "domain": getattr(classification, "domain", ""),
        "confidence": getattr(classification, "confidence", 0),
        "provider": getattr(classification, "provider", "") or ctx.provider,
    }

    task_prefix = _task_prefix_from_domain(classification_info["domain"])
    similar_raw = _get_similar_tasks_with_repos(
        ctx.conn, ctx.description, ctx.exclude_task_id, task_prefix=task_prefix,
    )
    # Keep only repos that appear in ≥2 of the similar tasks (recurring pattern).
    # Single-occurrence repos are one-off noise from loosely-matched tasks.
    similar = _distill_recurring_repos(similar_raw)

    # Archetype evidence from flows corpus (gated by FLOWS_EVIDENCE env var).
    archetype_summary: dict | None = None
    archetype_name = ""
    provider_summary: dict | None = None
    if _flows_evidence_enabled():
        archetype_name = _infer_archetype(
            ctx.description,
            classification_info["domain"],
            classification_info["provider"],
        )
        archetype_summary = summary_for_prompt(archetype_name)
        # Provider-specific hint: if task has a provider, load its snapshot.
        provider = classification_info.get("provider", "")
        if provider:
            provider_summary = provider_summary_for_prompt(provider)

    prompt = _build_prompt(
        ctx.description, classification_info, candidates, similar,
        archetype_summary=archetype_summary,
        provider_summary=provider_summary,
    )
    ranking = _call_gemini(prompt)
    if not ranking:
        return ""

    # Build a lookup of ranker entries by repo name.
    rank_by_repo: dict[str, dict] = {}
    for entry in ranking:
        if not isinstance(entry, dict):
            continue
        repo = entry.get("repo", "")
        if repo:
            rank_by_repo[repo] = entry

    # Rebuild findings: keep only high/medium tiers, reassign confidence.
    new_findings: list[Finding] = []
    dropped: list[tuple[str, str, str]] = []   # (repo, evidence, reason)
    kept_core: list[tuple[str, str]] = []       # (repo, reason)
    kept_related: list[tuple[str, str]] = []
    unknown: list[str] = []                     # repos the ranker didn't classify

    for repo in sources_by_repo:
        entry = rank_by_repo.get(repo)
        if entry is None:
            # Ranker omitted this repo. Keep at low confidence — do NOT drop silently.
            unknown.append(repo)
            new_findings.append(Finding("final_rank_unknown", repo, "low"))
            continue
        tier = (entry.get("tier") or "").lower()
        reason = entry.get("reason", "")
        if tier == "high":
            new_findings.append(Finding("final_rank", repo, "high"))
            kept_core.append((repo, reason))
        elif tier == "medium":
            new_findings.append(Finding("final_rank", repo, "medium"))
            kept_related.append((repo, reason))
        else:
            # tier=drop or unknown
            ev = _classify_evidence(sources_by_repo[repo])[0]
            dropped.append((repo, ev, reason))

    ctx.findings = new_findings

    # Build output section.
    output = "\n## Final LLM Ranking (precision pass)\n\n"
    total = len(sources_by_repo) + len(prefiltered)
    total_dropped = len(dropped) + len(prefiltered)
    output += f"_Pruned {total_dropped}/{total} candidates based on evidence + historical similarity._\n\n"
    if prefiltered:
        output += f"_Pre-filtered {len(prefiltered)} other-provider repo(s) (not scoped to `{ctx.provider or '?'}`)._\n\n"
    if archetype_summary and archetype_summary.get("sample_size", 0) >= 2:
        output += (
            f"_Archetype evidence: `{archetype_name}` "
            f"(n={archetype_summary['sample_size']})._\n\n"
        )

    if kept_core:
        output += "**Core (top 5):**\n"
        for repo, reason in kept_core:
            output += f"  - **{repo}** — {reason}\n"
        output += "\n"

    if kept_related:
        output += f"**Related ({len(kept_related)}):**\n"
        for repo, reason in kept_related:
            output += f"  - {repo} — {reason}\n"
        output += "\n"

    if dropped:
        output += f"**Dropped ({len(dropped)}):**\n"
        for repo, ev, reason in dropped[:15]:
            output += f"  - ~~{repo}~~ [{ev}] — {reason}\n"
        if len(dropped) > 15:
            output += f"  - _…and {len(dropped) - 15} more_\n"
        output += "\n"

    if unknown:
        output += f"_{len(unknown)} repo(s) not classified by ranker — kept at low confidence._\n\n"

    return output
