"""Webhook routing edges (dispatch, handler, callback)."""

import re
import sqlite3

from ._common import PROVIDER_PREFIXES, WEBHOOK_REPOS


def parse_webhook_edges(conn: sqlite3.Connection):
    """Parse webhook routing to find provider↔webhook service connections.

    Three webhook layers:
      1. express-webhooks: HTTP ingress, routes via webhook.bind(null, 'provider')
      2. workflow-provider-webhooks: Temporal dispatch, provider[name](params)
      3. express-api-callbacks: APM redirects, ?provider=X

    Creates edges:
      - express-webhooks → workflow-provider-webhooks (webhook_dispatch)
      - workflow-provider-webhooks → grpc-apm-{provider} (webhook_handler)
      - express-api-callbacks → grpc-apm-{provider} (callback_handler)
    """
    edges = []
    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    # 1. Parse webhook dispatch repo routes
    _wh_dispatch = WEBHOOK_REPOS.get("dispatch", "express-webhooks")
    _wh_handler = WEBHOOK_REPOS.get("handler", "workflow-provider-webhooks")
    route_rows = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND (file_path LIKE '%routes%' OR file_path LIKE '%webhook%')",
        (_wh_dispatch,),
    ).fetchall()

    webhook_providers_express = set()
    bind_pattern = re.compile(r"webhook\.bind\s*\(\s*null\s*,\s*['\"](\w+)['\"]")

    for row in route_rows:
        for match in bind_pattern.finditer(row[0]):
            provider = match.group(1)
            webhook_providers_express.add(provider)

    # 2. Parse webhook handler repo: provider handler map
    handler_rows = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND "
        "(file_path LIKE '%run-activities%' OR file_path LIKE '%activities/index%')",
        (_wh_handler,),
    ).fetchall()

    webhook_providers_workflow = set()
    # Patterns: handleTrustly, ...trustly, require('./activities/trustly/...')
    require_pattern = re.compile(r"require\s*\(\s*['\"]\.*/activities/(\w+)")
    handler_map_pattern = re.compile(r"(\w+)\s*:\s*handle\w+")

    for row in handler_rows:
        content = row[0]
        for match in require_pattern.finditer(content):
            webhook_providers_workflow.add(match.group(1))
        for match in handler_map_pattern.finditer(content):
            webhook_providers_workflow.add(match.group(1))

    # Also scan activity directories — each subdirectory is a provider
    activity_dirs = conn.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_path LIKE '%activities/%/%.js'",
        (_wh_handler,),
    ).fetchall()

    for row in activity_dirs:
        # activities/trustly/webhook/handle-activities.js → trustly
        parts = row[0].split("/")
        for i, part in enumerate(parts):
            if part == "activities" and i + 1 < len(parts):
                provider = parts[i + 1]
                if provider not in ("index", "libs", "utils", "helpers", "common"):
                    webhook_providers_workflow.add(provider)

    # 3. Parse express-api-callbacks
    callback_rows = conn.execute("SELECT content FROM chunks WHERE repo_name = 'express-api-callbacks'").fetchall()

    callback_providers = set()
    # Pattern: case 'ideal': or provider === 'volt'
    case_pattern = re.compile(r"case\s+['\"](\w+)['\"]")
    provider_eq_pattern = re.compile(r"provider\s*===?\s*['\"](\w+)['\"]")

    for row in callback_rows:
        content = row[0]
        for match in case_pattern.finditer(content):
            callback_providers.add(match.group(1))
        for match in provider_eq_pattern.finditer(content):
            callback_providers.add(match.group(1))

    # Build edges
    all_providers = webhook_providers_express | webhook_providers_workflow | callback_providers

    # Dispatch repo forwards to handler repo
    if _wh_dispatch in repo_names and _wh_handler in repo_names:
        edges.append((_wh_dispatch, _wh_handler, "webhook_dispatch", "all-providers"))

    for provider in all_providers:
        # Find the provider repo
        target = None
        for prefix in PROVIDER_PREFIXES:
            candidate = f"{prefix}{provider}"
            if candidate in repo_names:
                target = candidate
                break

        # dispatch → handler (per provider)
        if provider in webhook_providers_express and _wh_handler in repo_names:
            edges.append((_wh_dispatch, _wh_handler, "webhook_dispatch", provider))

        # handler → provider repo
        if provider in webhook_providers_workflow and target:
            edges.append((_wh_handler, target, "webhook_handler", provider))

        # express-api-callbacks → provider repo (callback)
        if provider in callback_providers and target:
            edges.append(("express-api-callbacks", target, "callback_handler", provider))

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)

    dispatch_count = len([e for e in unique_edges if e[2] == "webhook_dispatch"])
    handler_count = len([e for e in unique_edges if e[2] == "webhook_handler"])
    callback_count = len([e for e in unique_edges if e[2] == "callback_handler"])
    print(
        f"  Webhook edges: {len(unique_edges)} (dispatch: {dispatch_count}, handler: {handler_count}, callback: {callback_count})"
    )
    print(
        f"  Providers found: {len(all_providers)} ({len(webhook_providers_express)} express, {len(webhook_providers_workflow)} workflow, {len(callback_providers)} callback)"
    )
