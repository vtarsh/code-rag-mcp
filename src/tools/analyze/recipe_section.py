"""Recipe injection for analyze_task — structured implementation patterns.

When a task matches a known recipe (e.g., new_apm_provider), this section
adds evidence-based repo predictions, implementation order, churn warnings,
and review checklist to the analysis output.

Recipes are loaded from profiles/{profile}/recipes.yaml via config.RECIPES.
"""

from __future__ import annotations

import sys

from .base import AnalysisContext, Finding


def section_recipe(ctx: AnalysisContext, classification: object) -> str:
    """Inject recipe-based findings and guidance if task matches a recipe.

    Returns markdown section with: matched repos, implementation order,
    churn warnings, review checklist, and reference providers.
    """
    from src.config import RECIPES

    if not RECIPES:
        return ""

    recipe_name, recipe = _match_recipe(ctx, classification, RECIPES)
    if not recipe:
        return ""

    output = f"\n## Recipe: {recipe_name}\n\n"
    output += f"_{recipe.get('description', '')}_\n\n"

    evidence = recipe.get("evidence", {})
    if evidence:
        output += (
            f"_Based on {evidence.get('sample_size', '?')} completed tasks, "
            f"{evidence.get('total_review_comments_analyzed', '?')} review comments analyzed._\n\n"
        )

    # Add repos from recipe tiers to ctx.findings
    output += _inject_recipe_repos(ctx, recipe)

    # Implementation order
    output += _format_implementation_order(recipe)

    # Churn warnings
    output += _format_churn_warnings(recipe)

    # Review checklist
    output += _format_review_checklist(recipe)

    # Reference providers
    output += _format_references(recipe)

    return output


def _match_recipe(
    ctx: AnalysisContext,
    classification: object,
    recipes: dict[str, dict],
) -> tuple[str, dict | None]:
    """Find the best matching recipe for the current task using keyword scoring.

    Scoring rules:
      - Domain must match (trigger.domains contains classification domain)
      - Condition must pass (_evaluate_condition)
      - Score = number of trigger.keywords matched in description
      - Ties broken by: longer matched phrases win (specificity) then recipe name length desc
      - At least 1 keyword match is required (no implicit match via provider detection)
    """
    domain = getattr(classification, "domain", "").split("+")[0]
    provider = ctx.provider
    desc_lower = ctx.description.lower()

    candidates: list[tuple[int, int, str, str, dict]] = []  # (score, specificity, name_priority, name, recipe)

    for name, recipe in recipes.items():
        trigger = recipe.get("trigger", {})
        trigger_domains = trigger.get("domains", [])

        if domain not in trigger_domains:
            continue

        condition = trigger.get("condition", "")
        if not _evaluate_condition(ctx, condition, provider, ctx.conn, bool(ctx.exclude_task_id)):
            continue

        trigger_keywords = trigger.get("keywords", [])
        matched = [kw for kw in trigger_keywords if kw.lower() in desc_lower]
        if not matched:
            continue  # must have at least one keyword match

        score = len(matched)
        specificity = sum(len(kw) for kw in matched)  # longer phrases = more specific
        candidates.append((score, specificity, -len(name), name, recipe))

    if not candidates:
        return "", None

    # Highest score wins; ties broken by specificity then name
    candidates.sort(key=lambda x: (-x[0], -x[1], x[2]))
    _, _, _, name, recipe = candidates[0]
    return name, recipe


def _evaluate_condition(ctx: AnalysisContext, condition: str, provider: str, conn, blind_eval: bool = False) -> bool:
    """Evaluate a simple condition string against current context.

    Supported predicates:
      - provider_detected / NOT provider_detected
      - provider_repo_exists / NOT provider_repo_exists
      - new_provider / NOT new_provider  (alias for NOT provider_repo_exists)
      - webhook_handler_target_detected / core_schemas_or_libs_types_target (domain-derived, always True here)

    In blind_eval mode: provider_repo_exists checks are skipped (treated as True
    for both sides) so recipes can fire for historical tasks where the repo now
    exists but didn't when the task was implemented.
    """
    if not condition:
        return True

    # Check provider detection
    has_provider = bool(provider)
    repo_exists = False
    if has_provider and not blind_eval:
        repo_name = f"grpc-apm-{provider}"
        row = conn.execute("SELECT 1 FROM repos WHERE name = ?", (repo_name,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT 1 FROM repos WHERE name = ?", (f"grpc-providers-{provider}",)
            ).fetchone()
        repo_exists = bool(row)

    # Parse AND-joined predicates (supports NOT prefix)
    # Split on " AND " (case-insensitive)
    import re
    predicates = [p.strip() for p in re.split(r"\s+AND\s+", condition, flags=re.IGNORECASE)]

    for pred in predicates:
        negated = False
        pred_lower = pred.lower()
        if pred_lower.startswith("not "):
            negated = True
            pred_lower = pred_lower[4:].strip()

        # In blind_eval, skip repo-state predicates entirely (historical tasks: repo
        # may now exist but didn't when task was implemented). Both the predicate and
        # its negation are treated as satisfied so it doesn't block the AND chain.
        if blind_eval and pred_lower in ("provider_repo_exists", "new_provider"):
            continue

        result: bool
        if pred_lower == "provider_detected":
            result = has_provider
        elif pred_lower == "provider_repo_exists":
            result = repo_exists
        elif pred_lower == "new_provider":
            result = has_provider and not repo_exists
        else:
            # Unknown predicate (e.g. domain-derived) — treat as True
            result = True

        if negated:
            result = not result
        if not result:
            return False

    return True


def _inject_recipe_repos(ctx: AnalysisContext, recipe: dict) -> str:
    """Add recipe repos to findings and return formatted output."""
    provider = ctx.provider
    repos_config = recipe.get("repos", {})
    output = "### Repos (from recipe)\n\n"

    existing_repos = {f.repo for f in ctx.findings}

    for tier, confidence in [("core", "high"), ("common", "medium"), ("conditional", "low")]:
        tier_repos = repos_config.get(tier, [])
        added = []
        for entry in tier_repos:
            repo = entry["repo"]
            if provider and "{provider}" in repo:
                repo = repo.replace("{provider}", provider)

            # Verify repo exists in DB (or is the new provider repo)
            is_new_provider = repo.startswith("grpc-apm-") and repo not in existing_repos
            repo_exists = ctx.conn.execute(
                "SELECT 1 FROM repos WHERE name = ?", (repo,)
            ).fetchone()

            if not repo_exists and not is_new_provider:
                continue

            if repo not in existing_repos:
                ctx.findings.append(Finding("recipe", repo, confidence))

            freq = entry.get("frequency", "")
            desc = entry.get("description", "")
            added.append(f"**{repo}** ({freq}) — {desc}")

        if added:
            label = {"core": "Core", "common": "Common", "conditional": "Conditional"}[tier]
            output += f"**{label}:**\n"
            for line in added:
                output += f"  - {line}\n"
            output += "\n"

    return output


def _format_implementation_order(recipe: dict) -> str:
    """Format implementation order section."""
    impl = recipe.get("implementation_order", {})
    if not impl:
        return ""

    output = "### Implementation Order\n\n"
    for phase_key, phase in impl.items():
        if not isinstance(phase, dict):
            continue
        desc = phase.get("description", phase_key)
        output += f"**{desc}**\n"
        for step in phase.get("steps", []):
            output += f"  - {step}\n"
        for gotcha in phase.get("gotchas", []):
            output += f"  - :warning: {gotcha}\n"
        output += "\n"

    return output


def _format_churn_warnings(recipe: dict) -> str:
    """Format churn prediction warnings."""
    churn = recipe.get("churn_prediction", {})
    if not churn:
        return ""

    output = "### Churn Warnings\n\n"
    output += "_Files most likely to be rewritten during review:_\n\n"

    for severity in ["high", "medium"]:
        items = churn.get(severity, [])
        for item in items:
            f = item.get("file", "")
            prob = item.get("probability", 0)
            rec = item.get("recommendation", "")
            label = severity.upper()
            output += f"  - **{label}**: `{f}` ({prob:.0%} rewrite probability) — {rec}\n"

    output += "\n"
    return output


def _format_review_checklist(recipe: dict) -> str:
    """Format review checklist from recipe."""
    checklist = recipe.get("review_checklist", {})
    if not checklist:
        return ""

    output = "### Review Checklist (from PR history)\n\n"

    for severity in ["critical", "important"]:
        items = checklist.get(severity, [])
        if not items:
            continue
        label = severity.capitalize()
        output += f"**{label}:**\n"
        for item in items:
            check = item.get("check", "")
            freq = item.get("frequency", "")
            ref = item.get("reference", "")
            line = f"  - [ ] {check} ({freq})"
            if ref:
                line += f" — ref: `{ref}`"
            output += line + "\n"
        output += "\n"

    return output


def _format_references(recipe: dict) -> str:
    """Format reference providers section."""
    refs = recipe.get("reference_providers", {})
    if not refs:
        return ""

    output = "### Reference Providers\n\n"
    for key, value in refs.items():
        label = key.replace("_", " ").title()
        output += f"  - **{label}**: `{value}`\n"
    output += "\n"
    return output
