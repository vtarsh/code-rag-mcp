"""Shared sections that run for ALL task types (provider and non-provider)."""

from __future__ import annotations

import fnmatch
import json
import re
import sys
from pathlib import Path

from src.config import GATEWAY_REPO, PROTO_REPOS, SHARED_FILES
from src.formatting import strip_repo_tag

from .base import _KEYWORD_STOP_WORDS, AnalysisContext, Finding, extract_task_id, fts_queries
from .github_helpers import find_task_branches, find_task_prs
from .method_helpers import check_method_exists


def _load_task_files_changed(ctx: AnalysisContext, task_id: str) -> list[str]:
    """Read files_changed JSON array for a task from task_history."""
    if not task_id:
        return []
    try:
        row = ctx.conn.execute(
            "SELECT files_changed FROM task_history WHERE ticket_id = ? COLLATE NOCASE",
            (task_id.upper(),),
        ).fetchone()
    except Exception:
        return []
    if not row or not row[0]:
        return []
    try:
        files = json.loads(row[0])
    except Exception:
        return []
    return files if isinstance(files, list) else []


def _match_shared_files(files: list[str]) -> list[tuple[str, dict]]:
    """Return (matched_file, shared_file_entry) pairs for each file that matches a pattern."""
    if not files or not SHARED_FILES:
        return []
    matches: list[tuple[str, dict]] = []
    seen_patterns: set[str] = set()
    for f in files:
        for entry in SHARED_FILES:
            pattern = entry.get("path_pattern", "")
            if not pattern:
                continue
            if fnmatch.fnmatch(f, pattern):
                key = f"{f}::{pattern}"
                if key in seen_patterns:
                    continue
                seen_patterns.add(key)
                matches.append((f, entry))
                break  # one match per file is enough
    return matches


# Keywords that suggest a task will touch specific shared files.
# Used by _match_shared_files_by_keywords when no explicit file list
# is available (e.g. clean prompt, blind LOO, feature-description query).
# Each keyword → substrings that a shared_files.path_pattern must contain
# for it to be flagged as relevant.
_KEYWORD_FILE_TRIGGERS: dict[str, list[str]] = {
    # Sale / charge family
    "payout": ["payout", "payouts"],
    "sale": ["sale", "sales"],
    "charge": ["sale"],
    "purchase": ["sale"],
    "capture": ["sale"],
    "refund": ["refund"],
    "reversal": ["refund"],
    # Payout family (synonyms)
    "disburse": ["payout"],
    "disbursement": ["payout"],
    "transfer to customer": ["payout"],
    "send money": ["payout"],
    "send money back": ["payout"],
    "send funds": ["payout"],
    "withdraw": ["payout"],
    "withdrawal": ["payout"],
    "credit customer": ["payout"],
    "pay out": ["payout"],
    # Verification family
    "verification": ["verification"],
    "verify": ["verification"],
    "kyc": ["verification"],
    "identity confirmation": ["verification"],
    "identity check": ["verification"],
    "authentication": ["verification"],
    # Webhook / callback family
    "webhook": ["webhook", "handle-activities", "parse-payload"],
    "callback": ["webhook", "handle-activities"],
    "notification": ["webhook", "handle-activities"],
    "notifications": ["webhook", "handle-activities"],
    "async notification": ["webhook", "handle-activities"],
    "status update": ["webhook", "handle-activities"],
    "state change": ["webhook", "handle-activities"],
    "push event": ["webhook", "handle-activities"],
    # Scope / initialize
    "initialize": ["initialize"],
    "s2s": ["initialize"],
    "server-to-server": ["initialize"],
    # Reusable payout / token
    "reusablepayout": ["map-response", "payout"],
    "reusable": ["map-response", "payout"],
    "reusable token": ["map-response", "payout"],
    "payout token": ["map-response", "payout"],
    "repeat customer": ["map-response", "payout"],
    "returning customer": ["map-response", "payout"],
    "storable token": ["map-response", "payout"],
    "stored token": ["map-response", "payout"],
    # Schema / seeds / proto
    "seeds": ["seeds.cql"],
    "seed file": ["seeds.cql"],
    "proto": [".proto"],
    "protobuf": [".proto"],
    "message definition": [".proto"],
    # Methods / payment method composition
    "payment method": ["methods/", "payment-methods"],
    # APM-specific (gated by _has_apm_context)
    "apm": ["initialize", "methods/"],
    "grpc-apm": ["initialize", "methods/"],
    "integrate": ["seeds.cql", "methods/", "initialize"],
    "integration": ["seeds.cql", "methods/", "initialize"],
    "onboard": ["seeds.cql", "methods/", "initialize"],
    "onboarding": ["seeds.cql", "methods/", "initialize"],
    "launch": ["seeds.cql", "methods/", "initialize"],
    "new provider": ["seeds.cql", "methods/", "initialize"],
    "add provider": ["seeds.cql", "methods/", "initialize"],
    "alternative payment method": ["seeds.cql", "methods/", "initialize"],
    "bank transfer": ["methods/", "initialize"],
    "bank-transfer": ["methods/", "initialize"],
}

# Words that confirm the task is about an APM / provider integration,
# used to gate the keyword-triggered branch. Generic words like "provider"
# or "sale" alone are not sufficient — there must be at least one of
# these discriminators AND one operation keyword.
_APM_CONTEXT_MARKERS = (
    "apm", "grpc-apm", "grpc-providers", "payper", "paysafe", "trustly",
    "nuvei", "volt", "ppro", "aeropay", "paynearme", "fonix", "epx",
    "worldpay", "checkout", "braintree", "credorax",
    "alternative payment method",
    "new provider", "add a new provider", "add a provider",
    "provider integration", "integrate a provider", "integrate a new provider",
    "new apm", "apm integration",
    "onboard a new", "launch a new", "bank-transfer", "bank transfer",
    "payment method provider", "new payment method",
)

# Operation-family keywords: if a task mentions 2+ of these, it is very
# likely a provider-integration task even without an explicit APM marker.
# Used as a second gate condition in _has_apm_context.
_OPERATION_FAMILIES: list[tuple[str, ...]] = [
    ("sale", "charge", "purchase", "capture", "direct charge"),
    ("payout", "disburse", "disbursement", "send money", "send funds",
     "withdraw", "withdrawal", "credit customer", "transfer to customer", "pay out"),
    ("refund", "reversal"),
    ("verification", "verify", "kyc", "identity confirmation", "identity check", "authentication"),
    ("webhook", "callback", "notification", "notifications", "async notification",
     "status update", "state change", "push event", "payment state"),
]


def _count_operation_families(description_lower: str) -> int:
    """Count how many distinct operation families the description mentions."""
    hit = 0
    for family in _OPERATION_FAMILIES:
        for kw in family:
            pat = r"\b" + re.escape(kw).replace(r"\ ", r"[\s-]") + r"s?\b"
            if re.search(pat, description_lower):
                hit += 1
                break
    return hit


def _detect_trigger_keywords(description: str | None) -> list[str]:
    """Find operation/feature keywords in the task description."""
    if not description:
        return []
    d = description.lower()
    found: list[str] = []
    for kw in _KEYWORD_FILE_TRIGGERS:
        # Match whole words (allow hyphens and spaces for multi-word triggers)
        pat = r"\b" + re.escape(kw).replace(r"\ ", r"[\s-]") + r"s?\b"
        if re.search(pat, d):
            found.append(kw)
    return found


# If the description is clearly about reporting/analytics/BI, the gate
# stays closed even with multi-family keyword hits — those tasks coincidentally
# mention "sales" / "refunds" as metrics, not as coding operations.
_NON_INTEGRATION_MARKERS = (
    "backoffice", "back office", "back-office",
    "dashboard", "dashboards",
    "metrics", "metric",
    "report", "reports", "reporting",
    "analytics", "bi ", " bi",
    "chart", "charts", "graph", "graphs",
    "export csv", "export excel", "csv export",
    "admin panel", "admin page",
    "monitoring dashboard", "kpi",
)


def _has_apm_context(description: str | None, provider: str = "") -> bool:
    """Gate the keyword-triggered branch: only fire when we are confident
    the task is about an APM / provider integration, to avoid spraying
    SHARED FILE IMPACT warnings on backoffice or infra tasks that merely
    contain words like 'sale' or 'provider'.

    Gate opens if:
      1. ctx.provider is set (classifier is confident), OR
      2. Description contains an explicit APM marker phrase, OR
      3. Description touches ≥ 2 distinct operation families AND
         contains no reporting/analytics markers.
    """
    if provider:
        return True
    if not description:
        return False
    d = description.lower()
    if any(marker in d for marker in _APM_CONTEXT_MARKERS):
        return True
    if _count_operation_families(d) >= 2 and not any(m in d for m in _NON_INTEGRATION_MARKERS):
        return True
    return False


def _match_shared_files_by_keywords(
    description: str | None,
    provider: str = "",
) -> list[tuple[str, dict]]:
    """Match shared_files entries whose path_pattern relates to keywords
    detected in the task description. Returns
    ``(path_pattern, shared_file_entry)`` pairs so the existing renderer
    produces sensible output (path, not synthetic marker).

    Gated by `_has_apm_context` to avoid false positives on non-APM tasks.
    When the APM context is confirmed, the effective keyword set also
    includes a synthetic "apm" trigger so that scope-check / initialize /
    method-threading warnings fire even if the task description uses
    paraphrased verbs that don't directly match individual keywords.
    """
    if not SHARED_FILES or not _has_apm_context(description, provider):
        return []
    keywords = _detect_trigger_keywords(description)
    # APM context is confirmed — always include the synthetic "apm"
    # trigger so standing integration checks (initialize, methods/, seeds)
    # fire regardless of paraphrase. Without this, wording like "hook up a
    # new gateway" would never surface the scope check.
    if "apm" not in keywords:
        keywords.append("apm")
    matches: list[tuple[str, dict]] = []
    seen_patterns: set[str] = set()
    for entry in SHARED_FILES:
        pattern = entry.get("path_pattern", "")
        if not pattern or pattern in seen_patterns:
            continue
        pat_lower = pattern.lower()
        for kw in keywords:
            triggers = _KEYWORD_FILE_TRIGGERS.get(kw, [])
            if any(t in pat_lower for t in triggers):
                seen_patterns.add(pattern)
                matches.append((pattern, entry))
                break
    return matches


# Fallback list of real APM provider names — used when shared_files entries
# have semantic markers (e.g., "all_apm_providers_payout_method") instead of
# real provider names. Order matters — paysafe first because it has broad coverage.
_FALLBACK_SIBLINGS = ["paysafe", "trustly", "nuvei", "volt", "ppro", "aeropay", "paynearme", "fonix"]


def _pick_siblings(used_by: list, provider: str) -> list[str]:
    """Pick concrete sibling provider names from used_by, or fall back to known APMs."""
    real = [p for p in used_by if p and p != provider and p in _FALLBACK_SIBLINGS]
    if real:
        return real
    # used_by had only semantic markers or was empty — use hardcoded fallback
    return [p for p in _FALLBACK_SIBLINGS if p != provider]


def _render_shared_file_warning(matches: list[tuple[str, dict]], provider: str) -> str:
    """Render the SHARED FILE IMPACT warning block."""
    out = "## ⚠️ SHARED FILE IMPACT — cross-provider check required\n\n"
    out += "The following changed files are consumed by multiple providers or follow a shared convention. "
    out += "Before approving the review or editing further, verify your changes do not break other consumers.\n\n"
    for fpath, entry in matches:
        out += f"### `{fpath}`\n"
        used_by = entry.get("used_by", [])
        change_risk = entry.get("change_risk", "")
        convention = entry.get("convention", "")
        check = entry.get("check", "")
        if used_by:
            # Show only real provider names (not semantic markers) in the "used by" list
            real_used = [p for p in used_by if p in _FALLBACK_SIBLINGS and p != provider]
            if real_used:
                out += f"- **Also used by**: {', '.join(f'**{p}**' for p in real_used)}\n"
        if change_risk:
            out += f"- **Risk**: {change_risk}\n"
        if convention:
            out += f"- **Convention**: {convention}\n"
        if check:
            out += f"- **Check before edit**: {check}\n"

        # Infer method from file name
        method = ""
        for m in ("payout", "sale", "refund", "verification", "capture"):
            if m in fpath.lower():
                method = m
                break

        # Emit concrete tool calls for siblings (always — use fallback list if needed)
        siblings = _pick_siblings(used_by, provider)
        if siblings:
            primary = siblings[0]
            secondary = siblings[1] if len(siblings) > 1 else ""
            if method:
                out += f"- **Run now**: `provider_type_map(\"{primary}\", \"{method}\", \"fields\")` — compare contract for `{method}` method\n"
                if secondary:
                    out += f"- **Also run**: `provider_type_map(\"{secondary}\", \"{method}\", \"fields\")`\n"
                out += f"- **Then search**: `search(\"{primary} {method} {fpath.split('/')[-1]}\")` — see sibling implementation\n"
            else:
                out += f"- **Run now**: `provider_type_map(\"{primary}\", \"\", \"overview\")` — see sibling methods\n"
                out += f"- **Then search**: `search(\"{primary} {fpath.split('/')[-1]}\")` — see sibling file\n"
        out += "\n"
    out += "**Do NOT ship until each sibling has been compared.** Generic review conventions like \"run linter\" do not catch cross-provider regressions — only explicit sibling comparison does.\n\n"
    return out


_REVIEW_KEYWORDS = (
    "review", "check", "audit", "investigate",
    "глянь", "перевір", "зламали", "досліди", "аудит", "ревʼю",
    "did we break", "did i break", "чи нічого не",
)


def _is_review_mode(description: str) -> bool:
    d = description.lower()
    return any(k in d for k in _REVIEW_KEYWORDS)


def _render_review_mode_reminder(provider: str) -> str:
    """Top-of-output reminder for review/audit tasks — directs to git diff and sibling comparison."""
    out = "## ⚠️ REVIEW MODE — cross-provider check required\n\n"
    out += "This task is a review/audit. The #1 cause of review regressions is **cross-provider impact on shared files**. "
    out += "Before trusting any analysis below, run these steps FIRST:\n\n"
    out += "1. **List actually changed files**: `git -C <repo> diff main...HEAD --stat` for every repo on the task branch.\n"
    out += "2. **For each changed file**: check if another provider uses the same file/route/convention.\n"
    out += "3. **For each shared file found**: compare against 1 sibling provider via `provider_type_map` or `search`.\n\n"
    out += "Shared file patterns to watch for (from conventions.yaml `shared_files`):\n"
    for entry in SHARED_FILES[:8]:
        pattern = entry.get("path_pattern", "")
        risk = entry.get("change_risk") or entry.get("convention", "")
        if pattern:
            risk_short = risk[:120] + ("..." if len(risk) > 120 else "")
            out += f"- `{pattern}` — {risk_short}\n"
    out += "\n**Example first calls** (replace with your actual target):\n"
    out += f"- `provider_type_map(\"paysafe\", \"payout\", \"fields\")`\n"
    out += f"- `search(\"paysafe interac payout validation\")`\n"
    out += f"- `search(\"other APM providers methods/payout.js paymentMethod\")`\n\n"
    out += "**Do not rely on task_history alone** — for in-progress reviews, files_changed may be stale (historical merged PR data, not current open PR). Always verify with live `git diff`.\n\n"
    return out


def section_shared_files_warning(ctx: AnalysisContext) -> str:
    """Emit a SHARED FILE IMPACT warning when changed files match shared-file patterns.

    Three modes (both can fire):
    1. Review mode (keywords detected) → emit generic reminder with git diff hint.
    2. Task-history mode → if task_id found AND files_changed match shared_files patterns,
       emit specific file warnings with sibling providers + tool calls.

    Priority: review reminder is always first if triggered. Specific matches follow.
    Returns empty string if neither mode fires.
    """
    if not SHARED_FILES:
        return ""

    out = ""
    review_mode = _is_review_mode(ctx.description)

    # Always show review reminder first when in review mode
    if review_mode:
        out += _render_review_mode_reminder(ctx.provider)

    # Also try task_history lookup for specific file matches
    task_id = extract_task_id(ctx.description)
    # Skip own task when in eval mode (blind LOO)
    if task_id and ctx.exclude_task_id and task_id.upper() == ctx.exclude_task_id.upper():
        return out

    files = _load_task_files_changed(ctx, task_id)
    if files:
        matches = _match_shared_files(files)
        if matches:
            if review_mode:
                out += "### Historical matches from task_history (may be stale for in-progress work)\n\n"
            out += _render_shared_file_warning(matches, ctx.provider)
    else:
        # No known files (blind LOO, clean prompt, feature description).
        # Use keyword triggers to predict which shared files this task
        # will likely touch and emit preemptive warnings. This is the
        # "proactivity" branch — surfaces scope/convention checks before
        # any file has been committed.
        kw_matches = _match_shared_files_by_keywords(ctx.description, ctx.provider)
        if kw_matches:
            out += "### Predicted shared-file impact (from task description keywords)\n\n"
            out += _render_shared_file_warning(kw_matches, ctx.provider)

    return out


def section_gotchas(ctx: AnalysisContext) -> str:
    """Section 0: Surface curated domain knowledge."""
    queries = fts_queries(ctx.provider, ctx.words)
    if not queries:
        return ""

    seen_snippets: set[str] = set()
    results = []
    for q in queries:
        try:
            rows = ctx.conn.execute(
                "SELECT repo_name, file_path, snippet(chunks, 0, '>>>', '<<<', '...', 40) as snippet "
                "FROM chunks WHERE chunks MATCH ? AND file_type = 'gotchas' ORDER BY rank LIMIT 5",
                (q,),
            ).fetchall()
            for row in rows:
                snip = row["snippet"][:300]
                if snip not in seen_snippets:
                    seen_snippets.add(snip)
                    results.append(row)
        except Exception as e:
            print(f"[section_gotchas] FTS query failed for '{q}': {e}", file=sys.stderr)
            continue

    if not results:
        return ""

    output = "## ⚠️ Known Gotchas (from past reviews & production bugs)\n\n"
    output += "_These traps are NOT visible from code — read before coding._\n\n"
    for row in results[:8]:
        snip = strip_repo_tag(row["snippet"])
        output += f"**{row['repo_name']}** (`{row['file_path']}`):\n{snip}\n\n"
    return output


def section_existing_tasks(ctx: AnalysisContext) -> str:
    """Section 0.5: Surface existing task documents."""
    queries = fts_queries(ctx.provider, ctx.words)
    if not queries:
        return ""

    seen_snippets: set[str] = set()
    results = []

    # Build exclude variants for data-leakage prevention (blind eval)
    _excl_variants: list[str] = []
    if ctx.exclude_task_id:
        _eid = extract_task_id(ctx.exclude_task_id) or ctx.exclude_task_id.lower()
        _excl_variants.append(_eid)                    # e.g. "pi-60"
        _excl_variants.append(_eid.replace("-", "_"))   # e.g. "pi_60"

    for q in queries:
        try:
            rows = ctx.conn.execute(
                "SELECT repo_name, file_path, chunk_type, snippet(chunks, 0, '>>>', '<<<', '...', 30) as snippet "
                "FROM chunks WHERE chunks MATCH ? AND file_type = 'task' "
                "AND chunk_type != 'task_progress' ORDER BY rank LIMIT 5",
                (q,),
            ).fetchall()
            for row in rows:
                if ctx.provider and ctx.provider not in row["repo_name"]:
                    continue
                # Skip chunks belonging to the excluded task (data-leakage guard)
                if _excl_variants:
                    haystack = (row["repo_name"] + " " + (row["file_path"] or "")).lower()
                    if any(v in haystack for v in _excl_variants):
                        continue
                snip = row["snippet"][:300]
                if snip not in seen_snippets:
                    seen_snippets.add(snip)
                    results.append(row)
        except Exception as e:
            print(f"[section_existing_tasks] FTS query failed for '{q}': {e}", file=sys.stderr)
            continue

    if not results:
        return ""

    output = "## 📋 Existing Task Documents\n\n"
    output += "_Found related task context from previous work._\n\n"
    for row in results[:6]:
        snip = strip_repo_tag(row["snippet"])
        output += f"**{row['repo_name']}** ({row['chunk_type']}): {snip}\n\n"
    return output


def section_task_patterns(ctx: AnalysisContext) -> str:
    """Section 0.6: Surface learned patterns from task history."""
    output_parts: list[str] = []
    pattern_repos: list[tuple[str, str, int]] = []

    try:
        tables = {r[0] for r in ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "task_patterns" not in tables:
            return ""
        patterns = ctx.conn.execute(
            "SELECT pattern_type, missed_repo, trigger_repos, occurrences, confidence "
            "FROM task_patterns ORDER BY occurrences DESC"
        ).fetchall()
        if not patterns:
            return ""
    except Exception as e:
        print(f"[section_task_patterns] failed to load patterns: {e}", file=sys.stderr)
        return ""

    # Similar tasks from FTS (exclude self-match to prevent data leakage)
    similar_tasks: list[dict] = []
    current_task_id = extract_task_id(ctx.description)
    try:
        if "task_history_fts" in tables:
            search_terms = []
            if ctx.provider:
                search_terms.append(ctx.provider)
            for w in ctx.words:
                if len(w) > 5 and w not in _KEYWORD_STOP_WORDS:
                    search_terms.append(w)
            if search_terms:
                fts_query = " OR ".join(search_terms[:5])
                rows = ctx.conn.execute(
                    """SELECT t.ticket_id, t.summary, t.repos_changed, t.files_changed, t.pr_urls
                       FROM task_history_fts fts
                       JOIN task_history t ON t.id = fts.rowid
                       WHERE task_history_fts MATCH ?
                       ORDER BY rank LIMIT 6""",
                    (fts_query,),
                ).fetchall()
                for r in rows:
                    # Skip self-match (same ticket ID)
                    if current_task_id and r[0].lower() == current_task_id:
                        continue
                    # Skip explicitly excluded task (for blind eval)
                    if ctx.exclude_task_id and r[0].lower() == ctx.exclude_task_id.lower():
                        continue
                    # Extract repo names from PR URLs
                    pr_repos: list[str] = []
                    if r[4]:
                        try:
                            pr_urls = json.loads(r[4])
                            for url in pr_urls:
                                m = re.search(r"github\.com/[^/]+/([^/]+)/pull", url)
                                if m:
                                    pr_repos.append(m.group(1))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    similar_tasks.append(
                        {
                            "ticket": r[0],
                            "summary": r[1],
                            "repos": json.loads(r[2]) if r[2] else [],
                            "files": json.loads(r[3]) if r[3] else [],
                            "pr_repos": list(dict.fromkeys(pr_repos)),  # dedupe, preserve order
                        }
                    )
                similar_tasks = similar_tasks[:3]
    except Exception as e:
        print(f"[section_task_patterns] similar task search failed: {e}", file=sys.stderr)

    if similar_tasks:
        output_parts.append("## Historical Task Patterns\n")
        output_parts.append("_Based on similar past tasks, these repos/files were involved:_\n")
        existing_finding_repos = {f.repo for f in ctx.findings}
        for t in similar_tasks:
            repos_str = ", ".join(f"**{r}**" for r in t["repos"][:8])
            if len(t["repos"]) > 8:
                repos_str += f" (+{len(t['repos']) - 8} more)"
            output_parts.append(f"**{t['ticket']}** — {t['summary']}\n  Repos: {repos_str}\n")

            # PR URL signal: repos extracted from PR URLs of similar past tasks
            pr_repos = t.get("pr_repos", [])
            if pr_repos:
                pr_repos_str = ", ".join(f"**{r}**" for r in pr_repos[:8])
                output_parts.append(f"  PR repos: {pr_repos_str}\n")
                for repo in pr_repos:
                    if repo not in existing_finding_repos:
                        ctx.findings.append(Finding("pr_url_signal", repo, "high"))
                        existing_finding_repos.add(repo)

            # Similar-task boost: if past task shares ≥3 repos with current findings,
            # inject its other repos as findings (high confidence of same scope)
            overlap = existing_finding_repos & set(t["repos"])
            if len(overlap) >= 3:
                for repo in t["repos"]:
                    if repo not in existing_finding_repos:
                        ctx.findings.append(Finding("similar_task", repo, "medium"))
                        existing_finding_repos.add(repo)

    # Upstream caller patterns
    upstream_patterns = [p for p in patterns if p[0] == "upstream_caller" and p[3] >= 5]
    if upstream_patterns:
        if not output_parts:
            output_parts.append("## Historical Task Patterns\n")
        output_parts.append("\n### ⚡ Main Flow Repos (frequently missed)\n")
        output_parts.append("_These repos are part of the main request flow and were missed in many past tasks:_\n")
        for p in upstream_patterns[:6]:
            output_parts.append(f"- **{p[1]}** — missed in {p[3]} past tasks (avg confidence {p[4]:.0%})\n")
            pattern_repos.append((p[1], "upstream_caller", p[3]))

    # Co-occurrence patterns
    co_patterns = [p for p in patterns if p[0] == "co_occurrence"]
    relevant_co: list[str] = []
    for p in co_patterns:
        trigger_repos = json.loads(p[2]) if isinstance(p[2], str) else p[2]
        missed = p[1]
        trigger_words = set()
        for repo in trigger_repos:
            trigger_words.update(repo.lower().split("-"))
        is_relevant = (ctx.provider and ctx.provider in trigger_words) or bool(ctx.words & trigger_words)
        if is_relevant:
            relevant_co.append(
                f"When changing **{', '.join(trigger_repos[:3])}** → also check **{missed}** ({p[3]} past occurrences)"
            )
            pattern_repos.append((missed, "co_occurrence", p[3]))

    if relevant_co:
        if not output_parts:
            output_parts.append("## Historical Task Patterns\n")
        output_parts.append("\n### 🔗 Co-occurrence Patterns\n")
        output_parts.append("_Repos frequently missed together in past tasks:_\n")
        for p in relevant_co[:5]:
            output_parts.append(f"- {p}\n")

    # Cluster patterns
    cluster_patterns = [p for p in patterns if p[0] == "cluster"]
    relevant_clusters: list[str] = []
    for p in cluster_patterns:
        paired_with = json.loads(p[2]) if isinstance(p[2], str) else p[2]
        known = {r for r, _, _ in pattern_repos}
        if p[1] in known or any(pw in known for pw in paired_with):
            relevant_clusters.append(f"**{p[1]}** + **{', '.join(paired_with)}** — co-missed in {p[3]} tasks")

    if relevant_clusters:
        output_parts.append("\n### 📦 Gap Clusters (repos missed together)\n")
        for c in relevant_clusters[:5]:
            output_parts.append(f"- {c}\n")

    # Inject pattern repos into findings
    existing_finding_repos = {f.repo for f in ctx.findings}
    added = 0
    for repo, _reason, occurrences in pattern_repos:
        if repo not in existing_finding_repos and occurrences >= 5:
            ctx.findings.append(Finding("pattern", repo, "medium"))
            existing_finding_repos.add(repo)
            added += 1

    if added:
        output_parts.append(f"\n_Added {added} pattern-based repos to completeness checklist._\n")

    if output_parts:
        output_parts.append("\n")
    return "".join(output_parts)


def section_file_patterns(ctx: AnalysisContext) -> str:
    """Section 0.7: File-level patterns — hub files, directory pairs, cross-repo files."""
    try:
        tables = {r[0] for r in ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "file_patterns" not in tables:
            return ""
    except Exception as e:
        print(f"[section_file_patterns] failed to check tables: {e}", file=sys.stderr)
        return ""

    output_parts: list[str] = []
    relevant_terms = set(ctx.words)
    if ctx.provider:
        relevant_terms.add(ctx.provider)

    # Hub files
    hub_files = ctx.conn.execute(
        "SELECT source, occurrences FROM file_patterns WHERE pattern_type = 'hub_file' ORDER BY occurrences DESC"
    ).fetchall()
    relevant_hubs = []
    for row in hub_files:
        source = row[0]
        source_lower = source.lower()
        if any(t in source_lower for t in relevant_terms if len(t) > 3):
            relevant_hubs.append((source, row[1]))
    if relevant_hubs:
        output_parts.append("## 📁 File-Level Patterns\n")
        output_parts.append("### Hub Files (changed in many tasks)\n")
        for path, occ in relevant_hubs[:5]:
            output_parts.append(f"- `{path}` — changed in {occ} tasks\n")

    # Directory pairs
    dir_pairs = ctx.conn.execute(
        "SELECT source, target, occurrences, confidence FROM file_patterns "
        "WHERE pattern_type = 'dir_pair' AND occurrences >= 3 ORDER BY occurrences DESC"
    ).fetchall()
    relevant_dirs = []
    for row in dir_pairs:
        src, tgt, occ, conf = row
        src_lower, tgt_lower = src.lower(), tgt.lower()
        if any(t in src_lower or t in tgt_lower for t in relevant_terms if len(t) > 3):
            relevant_dirs.append((src, tgt, occ, conf))
    if relevant_dirs:
        if not output_parts:
            output_parts.append("## 📁 File-Level Patterns\n")
        output_parts.append("\n### Directory Pairs (co-changed)\n")
        for src, tgt, occ, conf in relevant_dirs[:6]:
            output_parts.append(f"- `{src}` ↔ `{tgt}` — {occ} tasks ({conf:.0%} confidence)\n")

    # Cross-repo file pairs
    cross_files = ctx.conn.execute(
        "SELECT source, target, occurrences, confidence FROM file_patterns "
        "WHERE pattern_type = 'cross_repo_file' AND occurrences >= 3 ORDER BY occurrences DESC"
    ).fetchall()
    relevant_cross = []
    for row in cross_files:
        src, tgt, occ, conf = row
        src_lower, tgt_lower = src.lower(), tgt.lower()
        if any(t in src_lower or t in tgt_lower for t in relevant_terms if len(t) > 3):
            relevant_cross.append((src, tgt, occ, conf))
    if relevant_cross:
        if not output_parts:
            output_parts.append("## 📁 File-Level Patterns\n")
        output_parts.append("\n### Cross-Repo File Pairs\n")
        for src, tgt, occ, conf in relevant_cross[:5]:
            output_parts.append(f"- `{src}` → `{tgt}` — {occ} tasks ({conf:.0%} confidence)\n")

    if output_parts:
        output_parts.append("\n")
    return "".join(output_parts)


def section_proto(ctx: AnalysisContext) -> str:
    """Section 2: Check proto contract for available RPC methods."""
    proto_repo = PROTO_REPOS[0] if PROTO_REPOS else ""
    output = f"## 2. Proto Contract ({proto_repo or 'N/A'})\n\n"
    if proto_repo:
        proto_service = ctx.conn.execute(
            "SELECT content FROM chunks WHERE repo_name = ? AND chunk_type = 'proto_service'",
            (proto_repo,),
        ).fetchall()
        if proto_service:
            proto_methods: set[str] = set()
            for row in proto_service:
                for match in re.finditer(r"rpc\s+(\w+)", row["content"]):
                    proto_methods.add(match.group(1))
            output += f"Available RPC methods: {', '.join(sorted(proto_methods))}\n\n"
            for word in ctx.words:
                matching = [m for m in proto_methods if word in m.lower()]
                if matching:
                    output += f"  `{word}` matches proto method: **{', '.join(matching)}**\n"
            output += "\n"
        ctx.findings.append(Finding("proto", proto_repo, "high"))
    return output


def section_gateway(ctx: AnalysisContext) -> str:
    """Section 4: Check payment gateway methods."""
    output = "## 4. Payment Gateway\n\n"
    if not GATEWAY_REPO:
        return output
    gateway_methods = ctx.conn.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method'",
        (GATEWAY_REPO,),
    ).fetchall()
    if gateway_methods:
        method_names = [Path(m["file_path"]).stem for m in gateway_methods]
        output += f"**{GATEWAY_REPO}** methods: {', '.join(method_names)}\n"
        matching_methods = [m for m in method_names if m.lower() in ctx.words]
        if matching_methods:
            output += f"  Task-relevant methods: **{', '.join(matching_methods)}**\n"
        output += "\n"
        ctx.findings.append(Finding("gateway", GATEWAY_REPO, "high"))
    return output


def section_methods(ctx: AnalysisContext) -> tuple[str, set[str], dict[str, dict]]:
    """Section 6: Check if gRPC methods exist in provider/gateway repos."""
    output = "## 6. Code Analysis (method existence)\n\n"
    known_methods_rows = ctx.conn.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE file_type = 'grpc_method'"
    ).fetchall()
    known_method_names = {Path(r["file_path"]).stem.lower() for r in known_methods_rows}
    task_methods: set[str] = ctx.words & known_method_names

    method_status: dict[str, dict] = {}
    for finding in ctx.findings:
        if finding.ftype in ("provider", "gateway"):
            rname = finding.repo
            for method in task_methods:
                result = check_method_exists(rname, method, ctx.conn)
                key = f"{rname}:{method}"
                method_status[key] = result
                status = "EXISTS" if result["exists"] else "MISSING"
                output += f"- `{rname}` → `{method}`: **{status}**"
                if result["exists"]:
                    output += f" ({result.get('file_path', '')})"
                output += "\n"

    if not task_methods:
        output += "No specific method names detected in task description.\n"
    output += "\n"
    return output, task_methods, method_status


def section_github(ctx: AnalysisContext) -> tuple[str, dict[str, list[dict]], dict[str, list[str]]]:
    """Section 7: Search GitHub for branches/PRs matching task ID."""
    output = "## 7. GitHub Activity\n\n"
    task_id = extract_task_id(ctx.description)

    # Prioritize high-confidence repos for GitHub API calls (capped at 20 by github_helpers)
    _conf_order = {"high": 0, "medium": 1, "low": 2}
    sorted_findings = sorted(ctx.findings, key=lambda f: _conf_order.get(f.confidence, 2))
    all_repos = list(dict.fromkeys(f.repo for f in sorted_findings))  # dedupe, preserve order
    all_repos.append("e2e-tests")  # always check e2e

    pr_data: dict[str, list[dict]] = {}
    branch_data: dict[str, list[str]] = {}

    if not task_id:
        output += "No task ID detected. Add a task ID (e.g., 'PI-54') for PR/branch scanning.\n\n"
        return output, pr_data, branch_data

    # Data leakage guard: skip GitHub API calls when evaluating the excluded task
    if ctx.exclude_task_id and task_id.upper() == ctx.exclude_task_id.upper():
        output += f"**Task ID detected**: `{task_id}` (skipped — excluded for eval)\n\n"
        return output, pr_data, branch_data

    output += f"**Task ID detected**: `{task_id}`\n\n"
    branch_data = find_task_branches(all_repos, task_id)
    pr_data = find_task_prs(all_repos, task_id)

    if not branch_data and not pr_data:
        output += f"No branches or PRs found matching `{task_id}` in any repo.\n\n"
        return output, pr_data, branch_data

    output += "### Found activity:\n\n"
    covered_repos: set[str] = set()
    for repo_name in all_repos:
        branches = branch_data.get(repo_name, [])
        prs = pr_data.get(repo_name, [])
        if not branches and not prs:
            continue
        covered_repos.add(repo_name)
        output += f"**{repo_name}**:\n"
        for b in branches:
            output += f"  - Branch: `{b}`\n"
        for pr in prs:
            status = "MERGED" if pr["merged_at"] else pr["state"].upper()
            output += f"  - PR #{pr['number']} [{status}]: {pr['title']}\n"
            if pr.get("files"):
                for f in pr["files"][:8]:
                    output += f"    - {f}\n"
        output += "\n"

    uncovered = set(all_repos) - covered_repos
    if uncovered:
        output += "### No activity found:\n"
        for repo_name in sorted(uncovered):
            output += f"  - **{repo_name}**\n"
        output += "\n"

    return output, pr_data, branch_data


_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

_FTYPE_LABELS = {
    "provider": "Implement method handler",
    "proto": "Proto contract",
    "webhook": "Webhook activity",
    "gateway": "Gateway routing",
    "pattern": "Pattern-based (historically missed)",
    "similar_task": "Similar past task",
    "bulk_migration": "Bulk migration",
    "npm_dep_scan": "npm dependency",
    "critical_trigger": "Critical infra (keyword-triggered — see §10)",
    "repo_ref": "Repo name in description",
    "domain_template": "Domain template",
    "co_change_rule": "Co-change rule (always changes together)",
    "pr_url_signal": "PR URL from similar task",
    "domain": "Domain service",
    "cascade": "Cascade dependency",
    "downstream": "Downstream dependency",
    "reverse_cascade": "Reverse cascade (called by found repo)",
    "keyword": "Keyword match",
    "co-occurrence": "Co-occurrence",
    "universal": "Frequently changed",
    "fanout": "Provider fan-out",
    "function": "Function reference",
}


def section_completeness(
    ctx: AnalysisContext,
    task_methods: set[str],
    method_status: dict[str, dict],
    pr_data: dict[str, list[dict]],
    branch_data: dict[str, list[str]],
) -> str:
    """Section 8: Build completeness checklist from all findings, grouped by confidence."""
    output = "## 8. Completeness Report\n\n"

    # Deduplicate by repo name, keeping highest confidence
    best: dict[str, Finding] = {}  # repo → Finding
    for finding in ctx.findings:
        if finding.repo not in best or _CONFIDENCE_RANK.get(finding.confidence, 1) < _CONFIDENCE_RANK.get(
            best[finding.repo].confidence, 1
        ):
            best[finding.repo] = finding

    # Build checklist entries: (repo, label, status, reason, confidence)
    checklist: list[tuple[str, str, str, str, str]] = []

    for rname, bf in best.items():
        ftype, conf = bf.ftype, bf.confidence
        status = "TODO"
        reason = ""

        if ftype == "gateway":
            for method in task_methods:
                key = f"{rname}:{method}"
                ms = method_status.get(key)
                if ms and ms["exists"]:
                    status = "OK"
                    reason = f"`{method}` already implemented"
        elif ftype == "proto":
            for method in task_methods:
                _proto_repo = PROTO_REPOS[0] if PROTO_REPOS else ""
                proto_check = (
                    ctx.conn.execute(
                        "SELECT content FROM chunks WHERE repo_name = ? AND chunk_type = 'proto_service' AND content LIKE ?",
                        (_proto_repo, f"%{method}%"),
                    ).fetchone()
                    if _proto_repo
                    else None
                )
                if proto_check:
                    status = "OK"
                    reason = f"`{method}` RPC already in proto"

        pr_exists = rname in pr_data
        branch_exists = rname in branch_data
        if pr_exists:
            pr = pr_data[rname][0]
            if pr["merged_at"]:
                status = "DONE"
                reason = f"PR #{pr['number']} merged"
            else:
                status = "IN PROGRESS"
                reason = f"PR #{pr['number']} ({pr['state']})"
        elif branch_exists:
            status = "IN PROGRESS"
            reason = f"Branch `{branch_data[rname][0]}` exists"

        label = _FTYPE_LABELS.get(ftype, ftype)
        checklist.append((rname, label, status, reason, conf))

    # e2e-tests
    e2e_status = "TODO"
    e2e_reason = ""
    if "e2e-tests" in pr_data:
        pr = pr_data["e2e-tests"][0]
        e2e_status = "DONE" if pr["merged_at"] else "IN PROGRESS"
        e2e_reason = f"PR #{pr['number']}"
    elif "e2e-tests" in branch_data:
        e2e_status = "IN PROGRESS"
        e2e_reason = f"Branch `{branch_data['e2e-tests'][0]}`"
    checklist.append(("e2e-tests", "E2E tests", e2e_status, e2e_reason, "high"))

    done = sum(1 for _, _, s, _, _ in checklist if s in ("DONE", "OK"))
    in_progress = sum(1 for _, _, s, _, _ in checklist if s == "IN PROGRESS")
    todo = sum(1 for _, _, s, _, _ in checklist if s == "TODO")
    output += f"**Progress**: {done} done, {in_progress} in progress, {todo} todo (out of {len(checklist)})\n\n"

    # Group by confidence tier
    high_items = [(r, lbl, s, d) for r, lbl, s, d, c in checklist if c == "high"]
    medium_items = [(r, lbl, s, d) for r, lbl, s, d, c in checklist if c == "medium"]
    low_items = [(r, lbl, s, d) for r, lbl, s, d, c in checklist if c == "low"]

    def _render_row(rname: str, label: str, status: str, reason: str, marker: str) -> str:
        icon = {"DONE": "[x]", "OK": "[x]", "IN PROGRESS": "[-]"}.get(status, marker)
        return f"| {icon} **{rname}** | {label} | **{status}** | {reason} |\n"

    if high_items:
        output += "### Core repos (high confidence)\n\n"
        output += "| Repo | Area | Status | Detail |\n|------|------|--------|--------|\n"
        for r, lbl, s, d in high_items:
            output += _render_row(r, lbl, s, d, "[x]")
        output += "\n"

    if medium_items:
        output += "### Related repos (medium confidence)\n\n"
        output += "| Repo | Area | Status | Detail |\n|------|------|--------|--------|\n"
        for r, lbl, s, d in medium_items:
            output += _render_row(r, lbl, s, d, "[?]")
        output += "\n"

    if low_items:
        output += f"<details>\n<summary>Peripheral repos (low confidence) — {len(low_items)} repos</summary>\n\n"
        output += "| Repo | Area | Status | Detail |\n|------|------|--------|--------|\n"
        for r, lbl, s, d in low_items:
            output += _render_row(r, lbl, s, d, "[ ]")
        output += "\n</details>\n\n"

    return output


def section_ci_risk(ctx: AnalysisContext) -> str:
    """Section 11: CI risk — recent failures in affected repos."""
    has_ci = ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ci_runs'").fetchone()
    if not has_ci:
        return ""

    finding_repos = {f.repo for f in ctx.findings}
    if not finding_repos:
        return ""

    # Batch query instead of N+1
    placeholders = ",".join("?" for _ in finding_repos)
    rows = ctx.conn.execute(
        f"""SELECT repo_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN conclusion = 'failure' THEN 1 ELSE 0 END) as fails
            FROM ci_runs WHERE repo_name IN ({placeholders})
            GROUP BY repo_name""",
        list(finding_repos),
    ).fetchall()

    risky: list[tuple[str, int, int]] = []
    for row in rows:
        if row["fails"] and row["fails"] > 0:
            risky.append((row["repo_name"], row["fails"], row["total"]))

    if not risky:
        return ""

    output = "## 11. CI Risk\n\n"
    risky.sort(key=lambda x: x[1], reverse=True)
    for repo, fails, total in risky:
        pct = fails / total * 100 if total > 0 else 0
        level = "HIGH" if pct > 30 else "MEDIUM" if pct > 15 else "LOW"
        output += f"- **{repo}**: {fails}/{total} runs failed ({pct:.0f}%) — {level} risk\n"
    output += "\n"
    return output
