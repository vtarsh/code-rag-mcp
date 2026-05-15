"""PI (Provider Integration) analyzer — sections specific to provider tasks."""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

from src.config import GATEWAY_REPO, INFRA_REPOS, INFRA_SUFFIXES, PROVIDER_PREFIXES, WEBHOOK_REPOS
from src.formatting import strip_repo_tag

from .base import _KEYWORD_STOP_WORDS, AnalysisContext, Finding

# Short/ambiguous words that match provider names but are common in non-provider contexts
_AMBIGUOUS_PROVIDER_NAMES = frozenset({"ach", "iris", "volt", "checkout", "plaid"})


def _get_provider_names(conn: sqlite3.Connection) -> set[str]:
    """Return set of known provider names from repos table, excluding infra suffixes."""
    if not PROVIDER_PREFIXES:
        return set()
    placeholders = " OR ".join("name LIKE ?" for _ in PROVIDER_PREFIXES)
    params = [f"{p}%" for p in PROVIDER_PREFIXES]
    provider_repos = conn.execute(f"SELECT name FROM repos WHERE {placeholders}", params).fetchall()
    provider_names: set[str] = set()
    for r in provider_repos:
        name = r["name"]
        for prefix in PROVIDER_PREFIXES:
            if name.startswith(prefix):
                suffix = name[len(prefix) :]
                # Skip infra repos (credentials, features, etc.)
                if suffix not in INFRA_SUFFIXES:
                    provider_names.add(suffix)
                break
    return provider_names


def count_matching_providers(conn: sqlite3.Connection, words: set[str]) -> int:
    """Count how many known provider names appear in the task words."""
    provider_names = _get_provider_names(conn)
    return sum(1 for p in provider_names if p in words)


def detect_provider(conn: sqlite3.Connection, words: set[str]) -> str:
    """Auto-detect provider name from task description words."""
    provider_names = _get_provider_names(conn)
    matches = sorted(p for p in provider_names if p in words)
    if not matches:
        return ""
    # Prefer non-ambiguous names over ambiguous ones (e.g. "paymend" > "checkout")
    non_ambiguous = [p for p in matches if p not in _AMBIGUOUS_PROVIDER_NAMES]
    return non_ambiguous[0] if non_ambiguous else matches[0]


_BULK_PATTERNS = re.compile(
    r"\b(?:all|every|each|all live|across all)\s+(?:providers?|integrations?|apm)\b",
    re.IGNORECASE,
)


def _is_bulk_provider_task(description: str) -> bool:
    """Detect if task targets all providers rather than a specific one."""
    return bool(_BULK_PATTERNS.search(description))


def section_bulk_providers(ctx: AnalysisContext) -> str:
    """When task targets all providers, list them all via gateway routing."""
    if ctx.provider or not _is_bulk_provider_task(ctx.description):
        return ""
    if not GATEWAY_REPO:
        return ""

    routed = ctx.conn.execute(
        """SELECT DISTINCT target FROM graph_edges
           WHERE source = ? AND edge_type = 'runtime_routing'
           ORDER BY target""",
        (GATEWAY_REPO,),
    ).fetchall()
    if not routed:
        return ""

    output = f"## Bulk Provider Change ({len(routed)} providers)\n\n"
    if not ctx.brief:
        output += "_Task targets all providers — listing all routed repos:_\n\n"
    for r in routed:
        ctx.findings.append(Finding("provider", r["target"], "high"))
        output += f"  - **{r['target']}**\n"
    output += "\n"
    return output


def section_provider(ctx: AnalysisContext) -> str:
    """Section 1: Find provider service repos and keyword matches."""
    if not ctx.provider:
        return ""

    output = f"## 1. Provider: {ctx.provider}\n\n"
    for prefix in PROVIDER_PREFIXES:
        repo_name = f"{prefix}{ctx.provider}"
        repo = ctx.conn.execute("SELECT * FROM repos WHERE name = ?", (repo_name,)).fetchone()
        if not repo:
            continue

        methods = ctx.conn.execute(
            "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method'", (repo_name,)
        ).fetchall()
        method_names = [Path(m["file_path"]).stem for m in methods]
        output += f"**{repo_name}** ({repo['type']})\n  Methods: {', '.join(method_names)}\n\n"
        ctx.findings.append(Finding("provider", repo_name, "high"))

        # In brief mode, skip the per-keyword FTS snippet dump — the method
        # list above is the main signal; snippets are secondary grep-fodder
        # that bloat the response.
        if ctx.brief:
            continue

        for keyword in ctx.words:
            if len(keyword) > 4 and keyword not in _KEYWORD_STOP_WORDS:
                matches = ctx.conn.execute(
                    "SELECT snippet(chunks, 0, '>>>', '<<<', '...', 20) as snippet FROM chunks WHERE chunks MATCH ? AND repo_name = ? LIMIT 2",
                    (f'"{keyword}"', repo_name),
                ).fetchall()
                if matches:
                    for m in matches:
                        snip = strip_repo_tag(m["snippet"])
                        output += f"  Found `{keyword}`: {snip[:150]}\n"
                    output += "\n"

    # Cross-provider content search: find other provider repos that reference
    # this provider name in their code (e.g., neteller references in grpc-providers-paysafe)
    if ctx.provider:
        already = {f.repo for f in ctx.findings}
        try:
            cross_hits = ctx.conn.execute(
                "SELECT DISTINCT repo_name FROM chunks WHERE chunks MATCH ? AND repo_name NOT IN ({}) LIMIT 20".format(
                    ",".join("?" for _ in already)
                ),
                (f'"{ctx.provider}"', *already),
            ).fetchall()
            cross_provider = [
                r["repo_name"] for r in cross_hits if any(r["repo_name"].startswith(p) for p in PROVIDER_PREFIXES)
            ]
            if cross_provider:
                output += f"### Cross-provider references to `{ctx.provider}`\n\n"
                for repo in cross_provider:
                    ctx.findings.append(Finding("provider", repo, "high"))
                    output += f"  - **{repo}** (mentions `{ctx.provider}` in code)\n"
                output += "\n"
        except Exception as e:
            print(f"[section_provider] cross-provider search failed: {e}", file=sys.stderr)

    return output


def section_webhooks(ctx: AnalysisContext) -> str:
    """Section 3: Find webhook handling for the provider."""
    if not ctx.provider:
        return ""

    output = "## 3. Webhook Handling\n\n"
    # In brief mode, list files without FTS snippets — repo + path is
    # enough for sub-agents to locate the file; the 100+ char snippet per
    # line is mostly noise for this section.
    if ctx.brief:
        webhook_chunks = ctx.conn.execute(
            "SELECT DISTINCT repo_name, file_path "
            "FROM chunks WHERE chunks MATCH ? AND repo_name LIKE '%webhook%' ORDER BY rank LIMIT 10",
            (f'"{ctx.provider}"',),
        ).fetchall()
        if not webhook_chunks:
            return output + "No webhook handling found for this provider.\n\n"

        repos_seen: set[str] = set()
        for row in webhook_chunks:
            rname = row["repo_name"]
            if rname not in repos_seen:
                repos_seen.add(rname)
                output += f"**{rname}**\n"
                ctx.findings.append(Finding("webhook", rname, "high"))
            output += f"  `{row['file_path']}`\n"
        output += "\n"
        return output

    webhook_chunks = ctx.conn.execute(
        "SELECT repo_name, file_path, snippet(chunks, 0, '>>>', '<<<', '...', 25) as snippet "
        "FROM chunks WHERE chunks MATCH ? AND repo_name LIKE '%webhook%' ORDER BY rank LIMIT 10",
        (f'"{ctx.provider}"',),
    ).fetchall()
    if not webhook_chunks:
        return output + "No webhook handling found for this provider.\n\n"

    repos_seen: set[str] = set()
    for row in webhook_chunks:
        rname = row["repo_name"]
        if rname not in repos_seen:
            repos_seen.add(rname)
            output += f"**{rname}**\n"
            ctx.findings.append(Finding("webhook", rname, "high"))
        snip = strip_repo_tag(row["snippet"])
        output += f"  `{row['file_path']}`: {snip[:150]}\n"
    output += "\n"
    return output


def section_impact(ctx: AnalysisContext) -> str:
    """Section 5: Trace dependency impact for provider repos."""
    output = "## 5. Impact Analysis\n\n"
    if not ctx.provider:
        return output

    for prefix in PROVIDER_PREFIXES:
        repo_name = f"{prefix}{ctx.provider}"
        deps = ctx.conn.execute(
            "SELECT target, edge_type FROM graph_edges WHERE source = ? AND target NOT LIKE 'pkg:%'", (repo_name,)
        ).fetchall()
        if deps:
            # In brief mode, filter out tooling/infra proto-message entries
            # (msg:..., envoy.*, opencensus.*) that are noise for change impact.
            if ctx.brief:
                filtered = [
                    d
                    for d in deps
                    if not d["target"].startswith("msg:")
                    and d["edge_type"] not in ("npm_dep_tooling", "proto_message_usage")
                ]
                if not filtered:
                    continue
                output += f"**{repo_name}** depends on:\n"
                for d in filtered:
                    output += f"  - {d['target']} ({d['edge_type']})\n"
                output += "\n"
            else:
                output += f"**{repo_name}** depends on:\n"
                for d in deps:
                    output += f"  - {d['target']} ({d['edge_type']})\n"
                output += "\n"
    return output


def section_change_impact(ctx: AnalysisContext) -> str:
    """Section 9: Method-level change impact — who calls provider methods via gRPC."""
    if not ctx.provider:
        return ""

    output = "## 9. Change Impact (Method Consumers)\n\n"
    provider_repos = [f.repo for f in ctx.findings if f.ftype == "provider"]

    # The gw_callers list depends on GATEWAY_REPO, not on the provider repo —
    # so in non-brief mode it's emitted identically under every provider. In
    # brief mode, emit it once (after all direct consumers) and list which
    # providers are routed via the gateway.
    brief_gateway_providers: list[str] = []

    for repo in provider_repos:
        consumers = ctx.conn.execute(
            """SELECT source, detail FROM graph_edges
               WHERE target = ? AND edge_type = 'grpc_method_call'
               ORDER BY source""",
            (repo,),
        ).fetchall()

        if consumers:
            output += f"**{repo}** is called by:\n"
            by_caller: dict[str, list[str]] = {}
            for c in consumers:
                caller = c["source"]
                method = c["detail"] or "unknown"
                by_caller.setdefault(caller, []).append(method)
            for caller, methods in sorted(by_caller.items()):
                output += f"  - **{caller}**: {', '.join(methods)}\n"
            output += "\n"

        if GATEWAY_REPO:
            gateway_routes = ctx.conn.execute(
                """SELECT detail FROM graph_edges
                   WHERE source = ? AND target = ? AND edge_type = 'runtime_routing'""",
                (GATEWAY_REPO, repo),
            ).fetchall()
            if gateway_routes:
                if ctx.brief:
                    brief_gateway_providers.append(repo)
                else:
                    gw_callers = ctx.conn.execute(
                        """SELECT DISTINCT source, detail FROM graph_edges
                           WHERE target = ? AND edge_type = 'grpc_method_call'""",
                        (GATEWAY_REPO,),
                    ).fetchall()
                    if gw_callers:
                        output += f"**{repo}** via gateway ({GATEWAY_REPO}):\n"
                        for gc in gw_callers[:10]:
                            output += f"  - {gc['source']}: {gc['detail']}\n"
                        output += "\n"

    # Brief mode: emit gateway consumers once, with the list of providers
    # routed through the gateway. Same information, ~75% less output for
    # multi-provider bulk tasks.
    if ctx.brief and brief_gateway_providers and GATEWAY_REPO:
        gw_callers = ctx.conn.execute(
            """SELECT DISTINCT source, detail FROM graph_edges
               WHERE target = ? AND edge_type = 'grpc_method_call'""",
            (GATEWAY_REPO,),
        ).fetchall()
        if gw_callers:
            providers_str = ", ".join(f"**{p}**" for p in brief_gateway_providers)
            output += f"**Via gateway ({GATEWAY_REPO})** for {providers_str}:\n"
            for gc in gw_callers[:10]:
                output += f"  - {gc['source']}: {gc['detail']}\n"
            output += "\n"

    if WEBHOOK_REPOS:
        dispatch_repo = WEBHOOK_REPOS.get("dispatch", "")
        handler_repo = WEBHOOK_REPOS.get("handler", "")
        if dispatch_repo and handler_repo:
            wh_edges = ctx.conn.execute(
                """SELECT source, target, edge_type FROM graph_edges
                   WHERE detail = ? AND edge_type IN ('webhook_dispatch', 'webhook_handler')
                   ORDER BY edge_type""",
                (ctx.provider,),
            ).fetchall()
            if wh_edges:
                output += f"**Webhook chain** for `{ctx.provider}`:\n"
                for e in wh_edges:
                    arrow = "->" if e["edge_type"] == "webhook_dispatch" else "<-"
                    output += f"  {e['source']} {arrow} {e['target']}\n"
                output += "\n"

    return output


_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def _description_tokens(description: str) -> set[str]:
    """Split description into lowercase alphanumeric tokens.

    Uses non-alphanumeric separators (whitespace, ``_``, ``-``, punctuation)
    so that ``eu_bank_account`` yields ``{eu, bank, account}``. This lets
    trigger keywords match word-by-word without the substring false positives
    of ``"check" in "checkout"``.
    """
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(description)}


def _matched_triggers(triggers: list[str], desc_tokens: set[str]) -> list[str]:
    """Return the triggers whose tokens are all present in ``desc_tokens``.

    A trigger can be a single word (``sepa``) or a compound
    (``bank_account``, ``name verification``). Both forms are tokenised and
    all parts must be present (order-independent). Returns a sorted,
    de-duplicated list preserving the original trigger strings for display.
    """
    matched: set[str] = set()
    for trigger in triggers:
        parts = [m.group(0).lower() for m in _TOKEN_RE.finditer(trigger)]
        if parts and all(p in desc_tokens for p in parts):
            matched.add(trigger)
    return sorted(matched)


def promote_critical_infra(ctx: AnalysisContext) -> None:
    """Pre-pass: promote infra_repos with matching keyword_triggers to findings.

    Runs BEFORE ``section_completeness`` so the Completeness Report groups
    these repos under Core (high confidence). Without this pre-pass the
    Critical list in §10 is rendered AFTER §8 already tiered the repos as
    Peripheral (via npm_dep_scan), producing a contradictory view where the
    same repo appears both ⚠️ Critical and [low-confidence] Peripheral.

    Runs for ALL domains, not just PI. Non-PI tasks (chargeback, risk,
    BO) often hit infra keywords too (e.g. ``workflow-collaboration-processing``
    triggers on ``chargeback`` / ``representment``); gating on ``ctx.provider``
    hid those signals, leaving the relevant repo in the generic keyword-scan
    section instead of Critical.

    Idempotent — repeated calls add nothing because
    ``Finding("critical_trigger", repo, "high")`` already lives in
    ``ctx.findings``.
    """
    if not INFRA_REPOS:
        return
    desc_tokens = _description_tokens(ctx.description)
    # Track repos we've already promoted in this pass (idempotence within a
    # single call). We do NOT skip when the repo is already in findings with
    # a different ftype — e.g. npm_dep_scan may have added it at low
    # confidence, and the whole point is to upgrade that to high.
    promoted: set[str] = {f.repo for f in ctx.findings if f.ftype == "critical_trigger"}
    for item in INFRA_REPOS:
        repo = item.get("repo", "")
        if not repo or repo in promoted:
            continue
        if not item.get("critical_note"):
            continue
        triggers = item.get("keyword_triggers", []) or []
        if not _matched_triggers(triggers, desc_tokens):
            continue
        has_repo = ctx.conn.execute("SELECT 1 FROM repos WHERE name = ?", (repo,)).fetchone()
        if not has_repo:
            continue
        ctx.findings.append(Finding("critical_trigger", repo, "high"))
        promoted.add(repo)


def section_provider_checklist(ctx: AnalysisContext) -> str:
    """Section 10: Infrastructure checklist.

    Split into two subsections when a provider is detected; only the
    Critical subsection renders for non-PI tasks (no provider), because
    the full infra list is only meaningful for provider integration work.

    - ⚠️ Critical for this task — repos whose ``keyword_triggers`` match the
      task description (word-level match, not substring). They carry a
      ``critical_note`` that warns the reader *why* skipping them tends to
      lead to reinvented infrastructure. Promotion to high-confidence
      findings is done earlier by :func:`promote_critical_infra`.
    - Other infrastructure repos — only rendered when ``ctx.provider``
      is set (the non-critical list is provider-checklist specific).
    """
    if not INFRA_REPOS:
        return ""

    title = "## 10. Provider Integration Checklist" if ctx.provider else "## 10. Infrastructure Checklist"
    output = f"{title}\n\n"
    finding_repos = {f.repo for f in ctx.findings}
    desc_tokens = _description_tokens(ctx.description)

    def _render(item: dict, *, show_note: bool, matched_triggers: list[str] | None = None) -> str:
        repo = item.get("repo", "")
        desc = item.get("description", "")
        # Only count provider references when a provider is set — otherwise
        # ``content LIKE '%%'`` matches every row and reports bogus counts.
        if ctx.provider:
            provider_match = ctx.conn.execute(
                "SELECT COUNT(*) as cnt FROM chunks WHERE repo_name = ? AND content LIKE ?",
                (repo, f"%{ctx.provider}%"),
            ).fetchone()["cnt"]
        else:
            provider_match = 0
        in_findings = repo in finding_repos
        status_found = in_findings or provider_match > 0
        marker = "[x]" if status_found else "[ ]"
        line = f"- {marker} **{repo}** — {desc}"
        if provider_match > 0 and not in_findings:
            line += f" ({provider_match} references found)"
        line += "\n"
        if show_note:
            note = item.get("critical_note", "")
            if matched_triggers:
                line += f"    - **Triggered by:** {', '.join(matched_triggers)}\n"
            if note:
                line += f"    - ⚠️ **{note}**\n"
        return line

    critical_items: list[tuple[dict, list[str]]] = []
    regular_items: list[dict] = []

    for item in INFRA_REPOS:
        repo = item.get("repo", "")
        if not repo:
            continue
        has_repo = ctx.conn.execute("SELECT 1 FROM repos WHERE name = ?", (repo,)).fetchone()
        if not has_repo:
            continue

        triggers = item.get("keyword_triggers", []) or []
        matched = _matched_triggers(triggers, desc_tokens)
        if matched and item.get("critical_note"):
            critical_items.append((item, matched))
        else:
            regular_items.append(item)

    # For non-PI tasks (no provider) we skip the full infra list because it's
    # not actionable outside of a provider integration flow. If no triggers
    # fire either, the whole section is empty and we return "" so the
    # analyze_task output doesn't carry a dangling "## 10." header.
    if not ctx.provider and not critical_items:
        return ""

    if critical_items:
        output += "### ⚠️ Critical for this task (keyword-matched)\n\n"
        if not ctx.brief:
            output += (
                "_Keyword triggers for these repos fired against the task description. "
                "Read them BEFORE designing your solution — skipping leads to reinventing "
                "existing infrastructure (token storage, vault flows, verification pipelines)._\n\n"
            )
        for item, matched in critical_items:
            output += _render(item, show_note=True, matched_triggers=matched)
        if ctx.provider:
            output += "\n### Other infrastructure repos\n\n"

    if ctx.provider:
        for item in regular_items:
            output += _render(item, show_note=False)

    output += "\n"
    return output
