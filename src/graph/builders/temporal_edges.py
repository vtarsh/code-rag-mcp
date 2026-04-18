"""Temporal workflow edge parsers."""

import re
import sqlite3

from ._common import NPM_SCOPE


def parse_temporal_edges(conn: sqlite3.Connection):
    """Parse Temporal workflow patterns: child workflows, signals, cross-repo workflow imports.

    Patterns:
      - executeChild('workflowName', ...) / startChild('workflowName', ...)
      - startChild(variableName, ...) with taskQueue
      - require('@scope/temporal-tools/workflows/...') — cross-repo workflow import
      - defineSignal('signalName') — signal definitions (stored as node metadata)
      - taskQueue: 'queueName' — links workflow to a task queue
      - activateWorkflow({ workflowName: 'name' }) — starts workflow via grpc-core-workflows
    """
    edges = []
    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    # Get all workflow-related chunks
    rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks WHERE file_type = 'workflow' OR "
        "(file_type IN ('grpc_method', 'library', 'code_file', 'code_function') AND (content LIKE '%executeChild%' OR content LIKE '%startChild%' OR content LIKE '%activateWorkflow%'))"
    ).fetchall()

    # Also get chunks that import from temporal-tools workflows
    import_rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks WHERE content LIKE '%temporal-tools/workflows/%'"
    ).fetchall()

    all_rows = list(rows) + list(import_rows)

    # Patterns
    child_workflow_pattern = re.compile(
        r'(?:executeChild|startChild)\s*\(\s*[\'"](\w[\w-]*)[\'"]',
    )
    task_queue_pattern = re.compile(
        r'taskQueue:\s*[\'"]([a-zA-Z][\w-]*)[\'"]',
    )
    signal_def_pattern = re.compile(
        r'defineSignal\s*\(\s*[\'"](\w+)[\'"]',
    )
    # Cross-repo workflow imports: require('{NPM_SCOPE}/temporal-tools/workflows/some-workflow/workflow')
    workflow_import_pattern = re.compile(
        rf"""require\s*\(\s*['"]({re.escape(NPM_SCOPE)}/temporal-tools/workflows/)([\w-]+)""",
    )
    # activateWorkflow({ workflowName: 'someWorkflow' }) — starts a workflow via grpc-core-workflows
    activate_workflow_pattern = re.compile(
        r"""activateWorkflow\s*\(\s*\{[^}]*workflowName:\s*['"](\w+)['"]""",
        re.DOTALL,
    )

    # Build a mapping: taskQueue name → repo name (heuristic: repo name often matches queue)
    task_queue_to_repo = {}
    for name in repo_names:
        # workflow-settlement-worker → settlementWorker or settlement-worker
        short = name.replace("workflow-", "")
        task_queue_to_repo[short] = name
        # camelCase version: settlement-worker → settlementWorker
        camel = re.sub(r"-(\w)", lambda m: m.group(1).upper(), short)
        task_queue_to_repo[camel] = name

    # Also build: workflow function name → repo name
    workflow_name_to_repo = {}
    for name in repo_names:
        if name.startswith("workflow-"):
            short = name.replace("workflow-", "")
            camel = re.sub(r"-(\w)", lambda m: m.group(1).upper(), short)
            workflow_name_to_repo[camel] = name
            workflow_name_to_repo[short] = name

    seen_signals = {}  # repo → [signal_names]

    # Additional patterns for signal sending and cross-repo activities
    signal_send_pattern = re.compile(
        r'(?:temporal\.signal|\.signal)\s*\(\s*(?:\w+,\s*)?[\'"](\w+)[\'"]',
    )
    # Cross-repo activity imports: require('{NPM_SCOPE}/temporal-tools/activities/some-activity')
    activity_import_pattern = re.compile(
        rf"""(?:require|from)\s*\(?['"]({re.escape(NPM_SCOPE)}/temporal-tools/activities/)([\w-]+)""",
    )
    # proxyActivities with cross-repo type import
    proxy_activity_type_pattern = re.compile(
        rf"""proxyActivities\s*<\s*typeof\s+import\s*\(\s*['"]({re.escape(NPM_SCOPE)}/([\w-]+))""",
    )

    # Also get chunks with signal sending or activity patterns
    signal_send_rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks "
        "WHERE content LIKE '%temporal.signal%' OR content LIKE '%.signal(%'"
    ).fetchall()
    activity_rows = conn.execute(
        "SELECT repo_name, file_path, content FROM chunks "
        "WHERE content LIKE '%temporal-tools/activities/%' "
        "OR (content LIKE '%proxyActivities%' AND content LIKE '%import%')"
    ).fetchall()

    all_rows = list(set(all_rows + signal_send_rows + activity_rows))

    for row in all_rows:
        source = row[0]
        content = row[2]

        # 1. Child workflow calls
        for match in child_workflow_pattern.finditer(content):
            child_name = match.group(1)
            # Try to resolve to a repo
            target = workflow_name_to_repo.get(child_name)
            if not target:
                # Try with task queue context
                tq_match = task_queue_pattern.search(content)
                if tq_match:
                    tq = tq_match.group(1)
                    target = task_queue_to_repo.get(tq)

            if target and target != source:
                edges.append((source, target, "child_workflow", child_name))
            elif not target:
                # Record as unresolved workflow reference
                edges.append((source, f"workflow:{child_name}", "child_workflow", child_name))

        # 2. Cross-repo workflow imports
        for match in workflow_import_pattern.finditer(content):
            workflow_path = match.group(2)  # e.g., "update-transactions-bulk-with-s3"
            # Try to find target repo
            target = None
            for candidate in [f"workflow-{workflow_path}", workflow_path]:
                if candidate in repo_names:
                    target = candidate
                    break
            # If not a separate repo, it's from temporal-tools itself
            if not target:
                target = "node-libs-temporal-tools"
                if target not in repo_names:
                    target = f"pkg:{NPM_SCOPE}/temporal-tools"
            if target != source:
                edges.append((source, target, "workflow_import", workflow_path))

        # 3. Signal definitions → signal_handler edges
        for match in signal_def_pattern.finditer(content):
            signal_name = match.group(1)
            seen_signals.setdefault(source, []).append(signal_name)
            # Create edge: workflow handles this signal
            edges.append((source, f"signal:{signal_name}", "signal_handler", signal_name))

        # 4. Signal sending → signal_send edges
        for match in signal_send_pattern.finditer(content):
            signal_name = match.group(1)
            edges.append((source, f"signal:{signal_name}", "signal_send", signal_name))

        # 5. Cross-repo activity imports
        for match in activity_import_pattern.finditer(content):
            activity_path = match.group(2)
            target = "node-libs-temporal-tools"
            if target not in repo_names:
                target = f"pkg:{NPM_SCOPE}/temporal-tools"
            if target != source:
                edges.append((source, target, "activity_import", activity_path))

        # 6. proxyActivities with cross-repo type reference
        for match in proxy_activity_type_pattern.finditer(content):
            pkg_name = match.group(2)
            # Find the actual repo for this package
            target_candidates = [pkg_name, f"node-libs-{pkg_name}"]
            target = None
            for candidate in target_candidates:
                if candidate in repo_names:
                    target = candidate
                    break
            if not target:
                target = f"pkg:{match.group(1)}"
            if target != source:
                edges.append((source, target, "activity_import", pkg_name))

        # 7. activateWorkflow({ workflowName: 'X' }) — resolve to target workflow repo
        for match in activate_workflow_pattern.finditer(content):
            wf_name = match.group(1)  # camelCase, e.g. 'collaborationMaster'
            target = workflow_name_to_repo.get(wf_name)
            if not target:
                # Convert camelCase to kebab-case and try workflow-{kebab} variants
                kebab = re.sub(r"([a-z])([A-Z])", r"\1-\2", wf_name).lower()
                for candidate in [f"workflow-{kebab}", f"workflow-{kebab}-processing"]:
                    if candidate in repo_names:
                        target = candidate
                        break
            if target and target != source:
                edges.append((source, target, "temporal_activate", wf_name))
            elif not target:
                edges.append((source, f"workflow:{wf_name}", "temporal_activate", wf_name))

    # Deduplicate edges
    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)

    # Connect signal senders to signal receivers through shared signal names
    signal_edges = 0
    signal_defs = {}  # signal_name → set of defining repos
    for repo, signals in seen_signals.items():
        for sig in signals:
            signal_defs.setdefault(sig, set()).add(repo)

    # Find senders: repos with signal_send edges to signal:X
    signal_senders = {}
    for e in unique_edges:
        if e[2] == "signal_send":
            signal_senders.setdefault(e[3], set()).add(e[0])

    # Create direct sender → handler edges where we can match signal names
    for sig_name, senders in signal_senders.items():
        handlers = signal_defs.get(sig_name, set())
        for sender in senders:
            for handler in handlers:
                if sender != handler:
                    conn.execute(
                        "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
                        (sender, handler, "temporal_signal", sig_name),
                    )
                    signal_edges += 1

    resolved = len(
        [
            e
            for e in unique_edges
            if not e[1].startswith("workflow:") and not e[1].startswith("pkg:") and not e[1].startswith("signal:")
        ]
    )
    unresolved = len([e for e in unique_edges if e[1].startswith("workflow:") or e[1].startswith("pkg:")])
    signal_count = sum(len(v) for v in seen_signals.values())
    activity_count = len([e for e in unique_edges if e[2] == "activity_import"])
    print(f"  Temporal edges: {len(unique_edges)} ({resolved} resolved, {unresolved} unresolved)")
    print(f"  Signal definitions: {signal_count} across {len(seen_signals)} repos")
    print(f"  Signal sender→handler edges: {signal_edges}")
    print(f"  Activity import edges: {activity_count}")
