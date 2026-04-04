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
    """Find the best matching recipe for the current task."""
    domain = getattr(classification, "domain", "").split("+")[0]
    provider = ctx.provider

    for name, recipe in recipes.items():
        trigger = recipe.get("trigger", {})
        trigger_domains = trigger.get("domains", [])

        if domain not in trigger_domains:
            continue

        condition = trigger.get("condition", "")
        if not _evaluate_condition(ctx, condition, provider):
            continue

        # Check keyword match (optional — if keywords specified, at least one must match)
        trigger_keywords = trigger.get("keywords", [])
        if trigger_keywords:
            desc_lower = ctx.description.lower()
            if not any(kw in desc_lower for kw in trigger_keywords):
                # No keyword match, but domain + condition matched —
                # still apply if provider is detected (common for PI tasks)
                if not provider:
                    continue

        return name, recipe

    return "", None


def _evaluate_condition(ctx: AnalysisContext, condition: str, provider: str) -> bool:
    """Evaluate a simple condition string against current context."""
    if not condition:
        return True

    # Simple condition evaluation
    if "provider_detected" in condition and not provider:
        return False

    if "NOT provider_repo_exists" in condition:
        # Check if grpc-apm-{provider} already exists in repos table
        if provider:
            repo_name = f"grpc-apm-{provider}"
            exists = ctx.conn.execute(
                "SELECT 1 FROM repos WHERE name = ?", (repo_name,)
            ).fetchone()
            # For new provider recipe, we want repo to NOT exist yet
            # But if it exists, the provider was recently created — still apply recipe
            # (provider may have just been scaffolded from boilerplate)
            # So we always return True when provider is detected
            return True

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
