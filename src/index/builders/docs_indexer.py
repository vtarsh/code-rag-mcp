"""Indexers for profile documentation: gotchas, domain registry, flows,
tasks, references, dictionary, and provider docs.
"""
# ruff: noqa: B023 — _flush closures intentionally capture loop vars; called only within same iter

from __future__ import annotations

import sqlite3

from ._common import (
    DICTIONARY_DIR,
    DOMAIN_REGISTRY_FILE,
    FLOWS_DIR,
    GOTCHAS_DIR,
    MAX_CHUNK,
    MIN_CHUNK,
    PROVIDERS_DIR,
    REFERENCES_DIR,
    TASKS_DIR,
)
from .detect import detect_language
from .dispatcher import chunk_file
from .docs_chunks import chunk_markdown, chunk_task_markdown


def index_gotchas(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index gotchas files from ~/.code-rag/docs/gotchas/.

    File naming: <repo_name>.md (e.g., grpc-apm-trustly.md).
    Each file is indexed with file_type='gotchas' and repo_name from filename.
    """
    if not GOTCHAS_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(GOTCHAS_DIR.glob("*.md")):
        repo_name = file_path.stem  # grpc-apm-trustly.md → grpc-apm-trustly
        files += 1

        file_chunks = chunk_file(file_path, repo_name, "docs")

        for chunk in file_chunks:
            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk["content"],
                    repo_name,
                    "docs/GOTCHAS.md",
                    "gotchas",
                    chunk["chunk_type"],
                    "markdown",
                ),
            )
            chunks += 1

    if files:
        print(f"  Gotchas: {files} files, {chunks} chunks")

    return files, chunks


def index_domain_registry(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index domain registry from docs/domain_registry.yaml.

    Each domain entry becomes a searchable chunk with file_type='domain_registry'.
    This allows queries like "api.dev.example.com" to return the serving repo.
    """
    if not DOMAIN_REGISTRY_FILE.is_file():
        return 0, 0

    try:
        import yaml
    except ImportError:
        # Fallback: parse simple YAML manually
        return _index_domain_registry_simple(conn)

    data = yaml.safe_load(DOMAIN_REGISTRY_FILE.read_text())
    return _insert_domain_entries(conn, data.get("domains", []))


def _index_domain_registry_simple(conn: sqlite3.Connection) -> tuple[int, int]:
    """Fallback parser for domain_registry.yaml without PyYAML."""
    import re

    text = DOMAIN_REGISTRY_FILE.read_text()
    entries = []
    current: dict = {}

    for line in text.splitlines():
        m = re.match(r'\s+-\s+domain:\s+"(.+)"', line)
        if m:
            if current:
                entries.append(current)
            current = {"domain": m.group(1)}
            continue
        m = re.match(r"\s+repo:\s+(\S+)", line)
        if m and current:
            current["repo"] = m.group(1)
        m = re.match(r'\s+description:\s+"(.+)"', line)
        if m and current:
            current["description"] = m.group(1)

    if current:
        entries.append(current)

    return _insert_domain_entries(conn, entries)


def _insert_domain_entries(
    conn: sqlite3.Connection,
    entries: list[dict],
) -> tuple[int, int]:
    """Insert domain registry entries as searchable chunks."""
    chunks = 0
    for entry in entries:
        domain = entry.get("domain", "")
        repo = entry.get("repo", "")
        desc = entry.get("description", "")
        if not domain or not repo:
            continue

        # Create chunk content that's searchable by domain name
        # Expand {env} to all environments for searchability
        envs = ["dev", "staging", ""]  # empty = prod
        domain_variants = []
        for env in envs:
            if env:
                domain_variants.append(domain.replace("{env}.", f"{env}."))
            else:
                domain_variants.append(domain.replace("{env}.", ""))

        content = f"Domain: {domain}\nDomains: {', '.join(domain_variants)}\nRepo: {repo}\nDescription: {desc}\n"

        conn.execute(
            "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, repo, "docs/domain_registry.yaml", "domain_registry", "domain_entry", "yaml"),
        )
        chunks += 1

    if chunks:
        print(f"  Domain registry: {chunks} entries")

    return 1 if chunks else 0, chunks


def index_flows(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index flow annotation files from docs/flows/.

    Each flow becomes a searchable chunk with file_type='flow_annotation'.
    This allows queries about data-driven redirects and cross-service flows
    to find the right chain even when static analysis cannot detect them.
    """
    if not FLOWS_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(FLOWS_DIR.glob("*.yaml")):
        repo_name = file_path.stem  # express-api-internal.yaml → express-api-internal
        files += 1

        text = file_path.read_text()

        # Parse flows from YAML (simple parser, no PyYAML dependency)
        # Each flow block becomes a searchable chunk
        current_flow: list[str] = []
        flow_name = ""

        for line in text.splitlines():
            if line.strip().startswith("- name:"):
                # Save previous flow
                if current_flow and flow_name:
                    content = f"[Flow: {repo_name}] {flow_name}\n" + "\n".join(current_flow)
                    conn.execute(
                        "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (content, repo_name, f"docs/flows/{file_path.name}", "flow_annotation", "flow", "yaml"),
                    )
                    chunks += 1

                # Start new flow
                flow_name = line.strip().split("name:", 1)[1].strip().strip('"').strip("'")
                current_flow = [line]
            elif current_flow is not None:
                current_flow.append(line)

        # Save last flow
        if current_flow and flow_name:
            content = f"[Flow: {repo_name}] {flow_name}\n" + "\n".join(current_flow)
            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (content, repo_name, f"docs/flows/{file_path.name}", "flow_annotation", "flow", "yaml"),
            )
            chunks += 1

    if files:
        print(f"  Flows: {files} files, {chunks} flow annotations")

    return files, chunks


def index_tasks(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index task files from profiles/{profile}/docs/tasks/.

    File naming: <task-slug>.md (e.g., payper-interac-etransfer.md).
    Each file is indexed with file_type='task' and repo_name=task slug.
    """
    if not TASKS_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(TASKS_DIR.glob("*.md")):
        task_name = file_path.stem
        files += 1

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if not content.strip() or len(content.strip()) < MIN_CHUNK:
            continue

        file_chunks = chunk_task_markdown(content, task_name)
        chunk_rowids = []

        for chunk in file_chunks:
            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk["content"],
                    task_name,
                    f"docs/tasks/{file_path.name}",
                    "task",
                    chunk["chunk_type"],
                    "markdown",
                ),
            )
            chunk_rowids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            chunks += 1

        # Populate chunk_meta for sibling retrieval
        total = len(chunk_rowids)
        for order, rowid in enumerate(chunk_rowids):
            conn.execute(
                "INSERT OR REPLACE INTO chunk_meta(chunk_rowid, chunk_order, total_chunks) VALUES (?, ?, ?)",
                (rowid, order, total),
            )

    if files:
        print(f"  Tasks: {files} files, {chunks} chunks")

    return files, chunks


def index_references(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index reference files from profiles/{profile}/docs/references/.

    Supports .yaml and .md files. Each file indexed with file_type='reference'.
    """
    if not REFERENCES_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(REFERENCES_DIR.glob("*")):
        if file_path.suffix.lower() not in (".yaml", ".yml", ".md"):
            continue

        ref_name = file_path.stem
        files += 1

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if not content.strip() or len(content.strip()) < MIN_CHUNK:
            continue

        language = detect_language(str(file_path))

        if language == "markdown":
            file_chunks = chunk_markdown(content, ref_name)
        else:
            # YAML: index as single chunk or split by top-level entries
            text = content.strip()
            if len(text) > MAX_CHUNK:
                text = text[:MAX_CHUNK] + "\n... [truncated]"
            file_chunks = [{"content": f"[Reference: {ref_name}] {text}", "chunk_type": "reference_entry"}]

        for chunk in file_chunks:
            # Ensure reference prefix
            chunk_content = chunk["content"]
            if not chunk_content.startswith("[Reference:"):
                chunk_content = f"[Reference: {ref_name}] {chunk_content}"

            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk_content,
                    ref_name,
                    f"docs/references/{file_path.name}",
                    "reference",
                    chunk.get("chunk_type", "reference_entry"),
                    language,
                ),
            )
            chunks += 1

    if files:
        print(f"  References: {files} files, {chunks} chunks")

    return files, chunks


def index_dictionary(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index domain dictionary YAMLs from profiles/{profile}/docs/dictionary/.

    Files: fields.yaml, entities.yaml, concepts.yaml (schema in dictionary/README.md).
    Each top-level list entry (`- name: ...`) becomes its own chunk so search can
    cite a single entry and stop. Mirrors index_flows() splitter.

    file_type='dictionary', chunk_type='dictionary_entry'.
    Content prefix: '[Dictionary: <file_stem>] [<entry.name>] aliases: ...'.
    """
    if not DICTIONARY_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    for file_path in sorted(DICTIONARY_DIR.glob("*.yaml")):
        file_stem = file_path.stem
        files += 1

        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        current_block: list[str] = []
        entry_name = ""
        entry_aliases = ""

        def _flush() -> int:
            nonlocal entry_name, entry_aliases, current_block
            if not current_block or not entry_name:
                return 0
            body = "\n".join(current_block)
            header = f"[Dictionary: {file_stem}] [{entry_name}]"
            if entry_aliases:
                header += f" aliases: {entry_aliases}"
            content = f"{header}\n{body}"
            if len(content) > MAX_CHUNK:
                content = content[:MAX_CHUNK] + "\n... [truncated]"
            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    content,
                    file_stem,
                    f"docs/dictionary/{file_path.name}",
                    "dictionary",
                    "dictionary_entry",
                    "yaml",
                ),
            )
            return 1

        for line in text.splitlines():
            stripped = line.lstrip()
            if line.startswith("- name:"):
                chunks += _flush()
                entry_name = line.split("name:", 1)[1].strip().strip('"').strip("'")
                entry_aliases = ""
                current_block = [line]
            elif stripped.startswith("aliases:") and current_block:
                raw = stripped.split("aliases:", 1)[1].strip()
                entry_aliases = raw.strip("[]").strip()
                current_block.append(line)
            elif current_block:
                current_block.append(line)

        chunks += _flush()

    if files:
        print(f"  Dictionary: {files} files, {chunks} entries")

    return files, chunks


def index_providers(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index provider docs from profiles/{profile}/docs/providers/{provider}/*.md.

    Each provider gets repo_name='{provider}-docs', file_type='provider_doc'.
    Chunks get content prefix '[{Provider Title} Docs: {slug}]' for search boost.
    Idempotent: deletes existing chunks for each provider before re-inserting.
    """
    if not PROVIDERS_DIR.is_dir():
        return 0, 0

    total_files = 0
    total_chunks = 0

    for provider_dir in sorted(PROVIDERS_DIR.iterdir()):
        if not provider_dir.is_dir():
            continue
        provider = provider_dir.name
        repo_name = f"{provider}-docs"
        provider_title = provider.replace("-", " ").replace("_", " ").title()

        # Remove existing chunks for this provider (idempotent)
        conn.execute("DELETE FROM chunks WHERE repo_name = ?", (repo_name,))

        files_count = 0
        chunks_count = 0

        for md_file in sorted(provider_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if len(content.strip()) < MIN_CHUNK:
                continue

            slug = md_file.stem
            rel_path = f"docs/providers/{provider}/{md_file.name}"
            file_chunks = chunk_markdown(content, slug)

            for chunk in file_chunks:
                chunk_content = chunk["content"]
                if not chunk_content.startswith(f"[{provider_title} Docs:"):
                    chunk_content = f"[{provider_title} Docs: {slug}] {chunk_content}"
                conn.execute(
                    "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (chunk_content, repo_name, rel_path, "provider_doc", "provider_doc", "markdown"),
                )
                chunks_count += 1
            files_count += 1

        if files_count > 0:
            total_files += files_count
            total_chunks += chunks_count

    if total_files:
        print(f"  Provider docs: {total_files} files, {total_chunks} chunks")

    return total_files, total_chunks
