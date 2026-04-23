#!/usr/bin/env python3
"""Prepare CrossEncoder fine-tune data from Jira ground-truth tasks.

Input:
    - db/tasks.db / task_history  -> PI tasks with files_changed + repos_changed
    - db/knowledge.db / chunks FTS5 -> code chunks to use as positives / mined negatives

Output (under --out, default profiles/pay-com/finetune_data/):
    - train.jsonl    positives + mined negatives + random negatives (label 0/1)
    - test.jsonl     positives only (no negatives -- eval set)
    - manifest.json  run metadata + dataset statistics

Positives:
    For every file in task.files_changed we take up to 5 chunks (longest first)
    and emit (task.summary, chunk.content[:1000], label=1).

Negatives (train only):
    - mined:  FTS5 top-50 for sanitize_fts_query(task.summary), filtered to drop
              any chunk whose file is in task.files_changed.
    - random: chunks from repos NOT in task.repos_changed.

Anti-leakage:
    - train/test ticket sets are disjoint.
    - no chunk from any file in any test task's files_changed appears in train.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.search.fts import sanitize_fts_query

# FTS5 MATCH treats many punctuation chars as reserved syntax; strip them
# so Jira summaries/descriptions like "[APM] - Nuvei", "Alias:", "payment!",
# URLs or "file/path.js" don't cause sqlite3.OperationalError in eval/train.
# Keep: letters, digits, whitespace, _   (FTS5 token chars)
# Keep: . -                              (handled specially by sanitize_fts_query)
# Strip everything else (including / which FTS5 treats as a token separator
# but often trips up when combined with ambiguous terms).
_FTS_PRECLEAN = re.compile(r"[^\w\s\.\-]")


def preclean_for_fts(text: str) -> str:
    return _FTS_PRECLEAN.sub(" ", text)


_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
TASKS_DB = _BASE / "db" / "tasks.db"
KNOWLEDGE_DB = _BASE / "db" / "knowledge.db"

MAX_CHUNKS_PER_FILE = 5
CONTENT_CHAR_LIMIT = 1000
FTS_TOP_FOR_MINING = 50

# Query enrichment params (defaults align with --use-description True).
DESC_CHAR_LIMIT = 800
COMMENT_CHAR_LIMIT = 400
QUERY_HARD_CAP = 1500
QUERY_SHORT_THRESHOLD = 200  # below this, fall back to comments
DIFF_SNIPPET_DEFAULT = 1500

# Match @@ ... @@ unified-diff hunk headers (so we keep the +/- lines but drop the noise).
_DIFF_HUNK_RE = re.compile(r"^@@[^@]*@@.*$", re.MULTILINE)
_WS_RE = re.compile(r"\s+")


# ---------- date helpers --------------------------------------------------- #


def _parse_changelog_dates(raw: str | None) -> list[datetime]:
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    out: list[datetime] = []
    for e in entries or []:
        d = e.get("date") if isinstance(e, dict) else None
        if not d:
            continue
        try:
            out.append(datetime.fromisoformat(d))
        except ValueError:
            continue
    return out


def _resolution_date(raw: str | None) -> datetime | None:
    """Latest (max) status transition = closest approximation of 'resolution'."""
    dates = _parse_changelog_dates(raw)
    return max(dates) if dates else None


# ---------- query enrichment ---------------------------------------------- #


def _flatten(text: str) -> str:
    """Newlines/tabs -> single spaces, collapse runs of whitespace."""
    return _WS_RE.sub(" ", text or "").strip()


def build_query_text(task: dict, use_description: bool = True) -> str:
    """Build a richer query string than just the Jira title.

    Order:
        1. summary (always)
        2. + first DESC_CHAR_LIMIT chars of description (if --use-description)
        3. + first 1-2 jira_comments bodies if total < QUERY_SHORT_THRESHOLD
           (terse-ticket fallback so we don't ship 80-char queries)

    Hard cap at QUERY_HARD_CAP characters so the cross-encoder tokenizer
    doesn't waste budget on the query side.
    """
    parts: list[str] = []
    summary = _flatten(task.get("summary", ""))
    if summary:
        parts.append(summary)

    if use_description:
        desc = _flatten(task.get("description", "") or "")
        if desc:
            parts.append(desc[:DESC_CHAR_LIMIT])

    candidate = " ".join(parts).strip()

    if len(candidate) < QUERY_SHORT_THRESHOLD:
        comments = task.get("jira_comments") or []
        added = 0
        for c in comments:
            if added >= 2:
                break
            body = _flatten((c or {}).get("body", "")) if isinstance(c, dict) else ""
            if not body:
                continue
            parts.append(body[:COMMENT_CHAR_LIMIT])
            added += 1
        candidate = " ".join(parts).strip()

    return candidate[:QUERY_HARD_CAP]


def clean_diff_snippet(patch: str, max_chars: int = DIFF_SNIPPET_DEFAULT) -> str:
    """Strip @@ hunk markers but keep the +/- body, then truncate."""
    if not patch:
        return ""
    body = _DIFF_HUNK_RE.sub("", patch).strip()
    # Squash blank-line runs introduced by header removal.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body[:max_chars]


# ---------- task loading --------------------------------------------------- #


def load_gt_tasks(
    db_path: Path,
    projects: list[str] | None = None,
    min_files: int = 1,
) -> list[dict]:
    """Load tickets with non-empty files_changed.

    projects: list of Jira prefixes ("PI", "BO", ...) or None = all projects.
    min_files: drop tickets with fewer than N files in files_changed. Default 1
               (any non-empty); v2 uses 3 to filter low-signal tickets.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if projects:
        placeholders = " OR ".join(f"ticket_id LIKE '{p}-%'" for p in projects)
        where = f"({placeholders}) AND files_changed IS NOT NULL AND files_changed != '[]'"
    else:
        where = "files_changed IS NOT NULL AND files_changed != '[]'"
    rows = conn.execute(
        "SELECT ticket_id, summary, description, repos_changed, files_changed, "
        "files_changed_diff, jira_comments, status_changelog "
        f"FROM task_history WHERE {where}"
    ).fetchall()
    conn.close()

    tasks: list[dict] = []
    for r in rows:
        try:
            files = json.loads(r["files_changed"] or "[]")
            repos = json.loads(r["repos_changed"] or "[]")
        except json.JSONDecodeError:
            continue
        if len(files) < min_files:
            continue
        # files_changed_diff and jira_comments may be NULL on older rows.
        try:
            patches = json.loads(r["files_changed_diff"] or "[]")
            if not isinstance(patches, list):
                patches = []
        except (json.JSONDecodeError, TypeError):
            patches = []
        try:
            comments = json.loads(r["jira_comments"] or "[]")
            if not isinstance(comments, list):
                comments = []
        except (json.JSONDecodeError, TypeError):
            comments = []
        tasks.append(
            {
                "ticket_id": r["ticket_id"],
                "summary": (r["summary"] or "").strip(),
                "description": (r["description"] or "").strip(),
                "jira_comments": comments,
                "repos_changed": list(repos),
                "files_changed": list(files),
                "patches": patches,
                "resolution": _resolution_date(r["status_changelog"]),
            }
        )
    return tasks


# ---------- test-task selection ------------------------------------------- #


def pick_test_tasks(
    tasks: list[dict],
    requested_ids: list[str] | None,
    *,
    test_ratio: float = 0.0,
    seed: int = 42,
) -> tuple[list[dict], list[dict]]:
    by_id = {t["ticket_id"]: t for t in tasks}

    if requested_ids:
        missing = [tid for tid in requested_ids if tid not in by_id]
        if missing:
            sys.exit(f"ERROR: test tasks not in DB (or empty files_changed): {missing}")
        test = [by_id[tid] for tid in requested_ids]
    elif test_ratio > 0:
        # Real holdout fold. Stratified per-project so every prefix gets
        # roughly the same ratio into test. Models trained on the resulting
        # train set will see this test set for the first time — fixes the
        # 904/5 in-training-distribution gate bug found 2026-04-20.
        if not (0 < test_ratio < 0.5):
            sys.exit(f"ERROR: test_ratio must be in (0, 0.5); got {test_ratio}")
        rng = random.Random(seed)
        by_project: dict[str, list[dict]] = {}
        for t in tasks:
            prefix = t["ticket_id"].split("-", 1)[0]
            by_project.setdefault(prefix, []).append(t)
        test = []
        for prefix in sorted(by_project):
            pool = list(by_project[prefix])
            rng.shuffle(pool)
            n_test = max(1, round(len(pool) * test_ratio))
            test.extend(pool[:n_test])
    else:
        # Legacy auto: PI-54 pinned if present (for v1 comparability) + 1 most-recent
        # per other project. Gives cross-project test coverage but only 5 tickets —
        # too small for a reliable held-out signal. Prefer --test-ratio for new runs.
        test = []
        pinned = by_id.get("PI-54")
        if pinned is not None:
            test.append(pinned)
        by_project: dict[str, list[dict]] = {}
        for t in tasks:
            if t["ticket_id"] == "PI-54" or t["resolution"] is None:
                continue
            prefix = t["ticket_id"].split("-", 1)[0]
            by_project.setdefault(prefix, []).append(t)
        for prefix in sorted(by_project):
            by_project[prefix].sort(key=lambda t: t["resolution"], reverse=True)
            test.append(by_project[prefix][0])
        if not test:
            sys.exit("ERROR: no test tasks selected (empty DB or no resolution dates)")

    if len(tasks) <= len(test):
        sys.exit(f"ERROR: only {len(tasks)} GT tasks vs {len(test)} test -- can't split")

    test_ids = {t["ticket_id"] for t in test}
    train = [t for t in tasks if t["ticket_id"] not in test_ids]
    return train, test


# ---------- chunk lookup --------------------------------------------------- #


def fetch_chunks_for_file(
    conn: sqlite3.Connection,
    repo_name: str,
    file_path: str,
    limit: int = MAX_CHUNKS_PER_FILE,
) -> list[dict]:
    """Return up to `limit` chunks for (repo_name, file_path), longest first."""
    rows = conn.execute(
        "SELECT rowid, content, repo_name, file_path "
        "FROM chunks WHERE repo_name = ? AND file_path = ? "
        "ORDER BY LENGTH(content) DESC LIMIT ?",
        (repo_name, file_path, limit),
    ).fetchall()
    return [
        {
            "rowid": r["rowid"],
            "content": r["content"] or "",
            "repo_name": r["repo_name"] or "",
            "file_path": r["file_path"] or "",
        }
        for r in rows
    ]


def split_qualified_path(qualified: str) -> tuple[str, str] | None:
    """'grpc-apm-trustly/methods/verify.js' -> ('grpc-apm-trustly', 'methods/verify.js')."""
    if "/" not in qualified:
        return None
    repo, rest = qualified.split("/", 1)
    return repo, rest


# ---------- v5 noise filters ----------------------------------------------- #

# Files whose chunks / diffs are almost always unrelated to the ticket semantics.
# Confirmed against real gte_v4 regressions via 9-shard Wave 3 audit (2026-04-19).
_NOISY_BASENAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "npm-shrinkwrap.json",
    "readme.md",
    "readme.rst",
    "readme.txt",
    ".env.example",
    ".env.sample",
    "openapi.yaml",
    "openapi.yml",
    "openapi.json",
    "seeds.cql",
    "schema.sql",
    "pg.sql",
    ".drone.yml",
    "tsconfig.json",
    "tsconfig.base.json",
    "consts.js",
    "consts.ts",
    "types.ts",
    "custom-types.ts",
}
_BARREL_STEMS = ("index.js", "index.ts", "index.tsx", "index.jsx")

# Frontend-cluster repos. Used by --fe-hard-negatives to inject hard negs
# for BO/CORE tickets whose GT has NO FE repo, breaking BO->FE vocabulary
# leakage (75.8% of BO positives live in FE repos -> reranker learns
# "payments/gateway -> boost FE repos", hurting CORE tickets).
_FE_REPO_CLUSTER = ("graphql", "space-web", "backoffice-web", "hosted-fields")

# Generated / codegen artifacts — schema-emitted boilerplate with near-zero
# semantic signal for reranker learning. Confirmed against v6.1 train.jsonl:
# ~1,700 rows dropped, 0 false-positives (Apollo / kysely / proto outputs).
# NOTE: pure `.d.ts` deliberately excluded — hand-written public type
# declarations exist (e.g. backoffice-web types.d.ts).
_GENERATED_BASENAME_SUFFIXES = (
    ".generated.ts",
    ".generated.js",
    ".generated.tsx",
    ".schema.json",
    ".proto",
)
_GENERATED_PATH_SUBSTRINGS = (
    "/__generated__/",  # Apollo / Relay convention
)

# Pay-com-specific path substrings. These files showed up as positives in
# 40-80% of regressed tickets across audit shards 0/1/4/5/6/7.
# Keep as substrings so they catch slight path variations.
_NOISY_PATH_SUBSTRINGS = (
    "/authorization/consts.",
    "/libs/operation-authorization.",
    "/components/specialcomponents/specialcomponents.",
    "/src/types/tasks.",
    "/src/types/common/",
    "/src/types/risk/",
    "/src/types/custom-types.",
    "/protos/",  # auto-generated .proto files
    "/.github/workflows/",
    "/generated/",
)


def _basename(file_path: str) -> str:
    return file_path.rsplit("/", 1)[-1].lower()


def is_noisy_file(file_path: str, basenames_only: bool = False, drop_generated: bool = False) -> bool:
    """True if positives on this file are likely noise (version bumps, barrel re-exports, hotspot scaffolding).

    When ``basenames_only=True``, only the generic basename/barrel blocklist is
    consulted — pay-com-specific path substrings are skipped. Use this for v6+
    runs that want generic noise filtering without over-aggressive path drops.

    When ``drop_generated=True``, also drops codegen / schema artifacts
    (``*.generated.ts``, ``*.proto``, ``*.schema.json``, ``/__generated__/``).
    Safe combined with either basenames_only mode.
    """
    base = _basename(file_path)
    if base in _NOISY_BASENAMES:
        return True
    if base in _BARREL_STEMS:
        return True
    if drop_generated:
        for suf in _GENERATED_BASENAME_SUFFIXES:
            if base.endswith(suf):
                return True
        lower_g = file_path.lower()
        for sub in _GENERATED_PATH_SUBSTRINGS:
            if sub in lower_g:
                return True
    if basenames_only:
        return False
    lower = file_path.lower()
    return any(sub in lower for sub in _NOISY_PATH_SUBSTRINGS)


def is_trivial_chunk(content: str) -> bool:
    """True if chunk content carries no semantic signal for reranking."""
    if not content:
        return True
    stripped = content.strip()
    if len(stripped) < 50:
        return True
    # Count non-blank lines whose first real token is import/require/export-from.
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return True
    imports = sum(
        1
        for ln in lines
        if ln.startswith(("import ", "require(", "from ", "export * from", "export { ", "export {"))
        or ("require(" in ln and len(ln) < 120)
    )
    if imports >= max(3, int(0.8 * len(lines))):
        return True
    # Dep-bump detection: mostly `"key": "version"` lines
    version_lines = sum(1 for ln in lines if ln.startswith('"') and ":" in ln and len(ln) < 80 and '"' in ln[-3:])
    return version_lines >= max(3, int(0.7 * len(lines)))


# ---------- positives ------------------------------------------------------ #


def _dedupe_by_file(pairs: list[dict]) -> list[dict]:
    """Keep one positive per (repo, file_path) per ticket.

    Preference order:
      1. diff-positive (hunk — more specific) over chunk-positive.
      2. longest document content among candidates.

    This combats label inflation: in v4, a file with 5 chunks + 1 diff gave
    6 copies of essentially the same (query, document)-ish pair, inflating
    loss weight on that one file.
    """
    best: dict[tuple[str, str], dict] = {}
    for p in pairs:
        key = (p.get("repo_name") or "", p.get("file_path") or "")
        if not key[0]:
            continue
        cur = best.get(key)
        if cur is None:
            best[key] = p
            continue
        # Prefer diff over chunk
        if p.get("positive_source") == "diff" and cur.get("positive_source") != "diff":
            best[key] = p
            continue
        if p.get("positive_source") != "diff" and cur.get("positive_source") == "diff":
            continue
        # Same source — prefer longer document
        if len(p.get("document") or "") > len(cur.get("document") or ""):
            best[key] = p
    return list(best.values())


def _cap_positives(pairs: list[dict], cap: int) -> list[dict]:
    """Cap total positives at N per ticket, keeping longest-doc first."""
    if cap <= 0 or len(pairs) <= cap:
        return pairs
    # Sort: diff first (stronger signal), then by content length desc.
    sorted_pairs = sorted(
        pairs,
        key=lambda p: (p.get("positive_source") != "diff", -len(p.get("document") or "")),
    )
    return sorted_pairs[:cap]


def build_positives(
    task: dict,
    conn: sqlite3.Connection,
    use_description: bool = True,
    use_diff_positives: bool = True,
    diff_snippet_max_chars: int = DIFF_SNIPPET_DEFAULT,
    drop_noisy_files: bool = False,
    drop_noisy_basenames_only: bool = False,
    drop_generated: bool = False,
    drop_trivial_positives: bool = False,
    popular_files: set | None = None,
    dedupe_same_file: bool = False,
    max_positives: int = 0,
) -> list[dict]:
    """Emit (query, document, label=1) pairs for one task.

    Two sources of positives per file:
        1. chunk:  longest N chunks from the file in knowledge.db (always).
        2. diff:   one extra positive per patch whose path matches a GT file
                   (only if use_diff_positives and the task has patches).
    """
    query_text = build_query_text(task, use_description=use_description)
    pairs: list[dict] = []

    # Index patches by qualified path for O(1) lookup. patch["path"] is already
    # qualified ("backoffice-web/src/Pages/...") so it lines up with files_changed.
    patches_by_path: dict[str, dict] = {}
    if use_diff_positives:
        for p in task.get("patches") or []:
            if not isinstance(p, dict):
                continue
            path = p.get("path")
            if path:
                # Prefer the patch with more changed lines if duplicates appear.
                cur = patches_by_path.get(path)
                if cur is None or (p.get("additions", 0) + p.get("deletions", 0)) > (
                    cur.get("additions", 0) + cur.get("deletions", 0)
                ):
                    patches_by_path[path] = p

    gt_files = set(task["files_changed"])

    noise_active = drop_noisy_files or drop_noisy_basenames_only or drop_generated
    # drop_noisy_files (legacy) wins over drop_noisy_basenames_only — full rule.
    noise_basenames_only = drop_noisy_basenames_only and not drop_noisy_files

    for qualified in task["files_changed"]:
        split = split_qualified_path(qualified)
        if not split:
            continue
        repo, rel_path = split
        if noise_active and is_noisy_file(rel_path, basenames_only=noise_basenames_only, drop_generated=drop_generated):
            continue
        if popular_files is not None and (repo, rel_path) in popular_files:
            continue
        chunks = fetch_chunks_for_file(conn, repo, rel_path)
        for c in chunks:
            content = c["content"][:CONTENT_CHAR_LIMIT]
            if drop_trivial_positives and is_trivial_chunk(content):
                continue
            pairs.append(
                {
                    "ticket_id": task["ticket_id"],
                    "query": query_text,
                    "document": content,
                    "label": 1,
                    "repo_name": c["repo_name"],
                    "file_path": c["file_path"],
                    "chunk_rowid": c["rowid"],
                    "chunk_file": f"{c['repo_name']}/{c['file_path']}",
                    "negative_type": None,
                    "positive_source": "chunk",
                }
            )

    if use_diff_positives:
        for qualified, patch in patches_by_path.items():
            if qualified not in gt_files:
                continue  # only patches matching a GT file
            split = split_qualified_path(qualified)
            if not split:
                continue
            repo, rel_path = split
            if noise_active and is_noisy_file(
                rel_path, basenames_only=noise_basenames_only, drop_generated=drop_generated
            ):
                continue
            if popular_files is not None and (repo, rel_path) in popular_files:
                continue
            snippet = clean_diff_snippet(patch.get("patch", ""), diff_snippet_max_chars)
            if not snippet:
                continue
            if drop_trivial_positives and is_trivial_chunk(snippet):
                continue
            pairs.append(
                {
                    "ticket_id": task["ticket_id"],
                    "query": query_text,
                    "document": snippet,
                    "label": 1,
                    "repo_name": repo,
                    "file_path": rel_path,
                    "chunk_rowid": None,
                    "chunk_file": f"{qualified}#diff",
                    "negative_type": None,
                    "positive_source": "diff",
                }
            )

    if dedupe_same_file:
        pairs = _dedupe_by_file(pairs)
    if max_positives > 0:
        pairs = _cap_positives(pairs, max_positives)
    return pairs


# ---------- negatives ------------------------------------------------------ #


def fts_top_chunks(
    conn: sqlite3.Connection,
    query_text: str,
    limit: int = FTS_TOP_FOR_MINING,
) -> list[dict]:
    sanitized = sanitize_fts_query(preclean_for_fts(query_text))
    if not sanitized.strip():
        return []
    try:
        rows = conn.execute(
            "SELECT rowid, content, repo_name, file_path FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
            (sanitized, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "rowid": r["rowid"],
            "content": r["content"] or "",
            "repo_name": r["repo_name"] or "",
            "file_path": r["file_path"] or "",
        }
        for r in rows
    ]


def mine_negatives(
    task: dict,
    conn: sqlite3.Connection,
    n_needed: int,
    use_description: bool = True,
) -> list[dict]:
    if n_needed <= 0:
        return []
    query_text = build_query_text(task, use_description=use_description)
    gt_files = set(task["files_changed"])
    chunks = fts_top_chunks(conn, query_text, limit=FTS_TOP_FOR_MINING)
    out: list[dict] = []
    for c in chunks:
        qualified = f"{c['repo_name']}/{c['file_path']}"
        if qualified in gt_files:
            continue
        out.append(
            {
                "ticket_id": task["ticket_id"],
                "query": query_text,
                "document": c["content"][:CONTENT_CHAR_LIMIT],
                "label": 0,
                "repo_name": c["repo_name"],
                "file_path": c["file_path"],
                "chunk_rowid": c["rowid"],
                "chunk_file": qualified,
                "negative_type": "mined",
            }
        )
        if len(out) >= n_needed:
            break
    return out


def random_negatives(
    task: dict,
    conn: sqlite3.Connection,
    n_needed: int,
    rng: random.Random,
    use_description: bool = True,
) -> list[dict]:
    if n_needed <= 0:
        return []
    query_text = build_query_text(task, use_description=use_description)
    repos = task["repos_changed"]
    if repos:
        placeholders = ",".join("?" * len(repos))
        sql = (
            f"SELECT rowid, content, repo_name, file_path FROM chunks "
            f"WHERE repo_name NOT IN ({placeholders}) "
            f"ORDER BY RANDOM() LIMIT ?"
        )
        # random seed for SQLite RANDOM() cannot be set; we additionally shuffle
        # the over-fetched pool with our seeded rng for determinism.
        rows = conn.execute(sql, (*repos, n_needed * 4)).fetchall()
    else:
        rows = conn.execute(
            "SELECT rowid, content, repo_name, file_path FROM chunks ORDER BY RANDOM() LIMIT ?",
            (n_needed * 4,),
        ).fetchall()
    pool = [dict(r) for r in rows]
    rng.shuffle(pool)
    out: list[dict] = []
    for c in pool[:n_needed]:
        out.append(
            {
                "ticket_id": task["ticket_id"],
                "query": query_text,
                "document": (c["content"] or "")[:CONTENT_CHAR_LIMIT],
                "label": 0,
                "repo_name": c["repo_name"] or "",
                "file_path": c["file_path"] or "",
                "chunk_rowid": c["rowid"],
                "chunk_file": f"{c['repo_name'] or ''}/{c['file_path'] or ''}",
                "negative_type": "random",
            }
        )
    return out


def mine_fe_hard_negatives(
    task: dict,
    conn: sqlite3.Connection,
    n_needed: int,
    use_description: bool = True,
) -> list[dict]:
    """Mine hard negatives from the FE repo cluster for tickets whose GT has no FE repo.

    Rationale: 75.8% of BO positives live in FE repos, so the reranker learns
    "payments/gateway -> boost FE repos", hurting CORE tickets. Injecting FE
    chunks as negatives for non-FE tickets breaks that vocabulary leakage.

    Returns [] when:
      - n_needed <= 0 (flag disabled), OR
      - ticket already touches an FE repo (injection would be spurious).
    """
    if n_needed <= 0:
        return []
    repos_changed = task.get("repos_changed") or []
    if any(r in _FE_REPO_CLUSTER for r in repos_changed):
        return []
    query_text = build_query_text(task, use_description=use_description)
    sanitized = sanitize_fts_query(preclean_for_fts(query_text))
    if not sanitized.strip():
        return []
    gt_files = set(task["files_changed"])
    out: list[dict] = []
    for fe_repo in _FE_REPO_CLUSTER:
        if len(out) >= n_needed:
            break
        try:
            rows = conn.execute(
                "SELECT rowid, content, repo_name, file_path FROM chunks "
                "WHERE chunks MATCH ? AND repo_name = ? ORDER BY rank LIMIT 20",
                (sanitized, fe_repo),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            qualified = f"{r['repo_name'] or ''}/{r['file_path'] or ''}"
            if qualified in gt_files:
                continue
            out.append(
                {
                    "ticket_id": task["ticket_id"],
                    "query": query_text,
                    "document": (r["content"] or "")[:CONTENT_CHAR_LIMIT],
                    "label": 0,
                    "repo_name": r["repo_name"] or "",
                    "file_path": r["file_path"] or "",
                    "chunk_rowid": r["rowid"],
                    "chunk_file": qualified,
                    "negative_type": "fe_hard_neg",
                }
            )
            if len(out) >= n_needed:
                break
    return out


# ---------- writing -------------------------------------------------------- #


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def verify_no_leakage(train_records: list[dict], test_tasks: list[dict]) -> None:
    """Assert no train record references a file that any test task touched.

    Checks both positives (would be false labels if model saw them) AND
    negatives (the model still gets trained on these chunks, so they must
    not be test-set files — otherwise the evaluator effectively re-sees
    what the trainer saw).
    """
    test_files = {qf for t in test_tasks for qf in t["files_changed"]}
    for r in train_records:
        cf = r["chunk_file"]
        bare = cf[:-5] if cf.endswith("#diff") else cf
        assert bare not in test_files, (
            f"LEAK: train {('positive' if r['label'] == 1 else r.get('negative_type', 'neg'))} "
            f"uses test file {cf} (ticket {r['ticket_id']})"
        )


# ---------- query-level leakage guard -------------------------------------- #


def _default_query_tokens(text: str) -> set[str]:
    return set(re.findall(r"\w{3,}", (text or "").lower()))


def verify_no_query_leakage(
    train_items,
    holdout_items,
    token_fn=_default_query_tokens,
    threshold: float = 0.5,
) -> None:
    """Raise ValueError if any train/holdout pair has Jaccard ≥ threshold.

    Items may be strings or dicts with ``summary``/``description`` keys
    (concatenated before tokenising). Used to cross-check v12 train vs
    held-out Jira / runtime sets before training.
    """

    def _text(item) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return f"{item.get('summary', '') or ''} {item.get('description', '') or ''}".strip()
        return str(item)

    if not train_items or not holdout_items:
        return

    train_tokens = [token_fn(_text(t)) for t in train_items]
    holdout_tokens = [token_fn(_text(h)) for h in holdout_items]

    for i, t_toks in enumerate(train_tokens):
        if not t_toks:
            continue
        for j, h_toks in enumerate(holdout_tokens):
            if not h_toks:
                continue
            jac = len(t_toks & h_toks) / len(t_toks | h_toks)
            if jac >= threshold:
                raise ValueError(
                    f"query-leakage detected: train[{i}] ∩ holdout[{j}] "
                    f"jaccard={jac:.1f} (threshold={threshold})"
                )


# ---------- main ----------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-tasks", default="", help="Comma-separated ticket IDs for test split. Empty = auto-pick.")
    ap.add_argument(
        "--projects", default="", help="Comma-separated Jira project prefixes (PI,BO,CORE,HS). Empty = all."
    )
    ap.add_argument(
        "--min-files",
        type=int,
        default=1,
        help="Drop tasks with fewer than N files_changed. Default 1 (any). v2 uses 3.",
    )
    ap.add_argument("--out", default="profiles/pay-com/finetune_data/", help="Output directory.")
    ap.add_argument("--neg-mined-ratio", type=float, default=2.0, help="Mined negatives per positive (train only).")
    ap.add_argument("--neg-random-ratio", type=float, default=1.0, help="Random negatives per positive (train only).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--tasks-db", default=str(TASKS_DB))
    ap.add_argument("--knowledge-db", default=str(KNOWLEDGE_DB))
    # Query-enrichment ablation flags. Defaults match the v4 design;
    # pass --no-use-description --no-use-diff-positives to reproduce v2 behavior.
    ap.add_argument(
        "--use-description",
        dest="use_description",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Append description (and comments if terse) to query text.",
    )
    ap.add_argument(
        "--use-diff-positives",
        dest="use_diff_positives",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Emit one extra positive per patch matching a GT file.",
    )
    ap.add_argument(
        "--diff-snippet-max-chars",
        type=int,
        default=DIFF_SNIPPET_DEFAULT,
        help="Max chars retained per diff body (after stripping @@ markers).",
    )
    # v5 noise filters (confirmed via cross-audit against gte_v4 regressions)
    ap.add_argument(
        "--drop-noisy-files",
        action="store_true",
        default=False,
        help="Drop positives on package.json/package-lock.json/pnpm-lock.yaml/"
        "README.md/.env.example/openapi.yaml/seeds.cql and barrel "
        "index.(js|ts|tsx) files PLUS pay-com-specific path substrings "
        "(authorization/consts, /protos/, /.github/workflows/, /generated/, "
        "etc.). Legacy v5 behaviour — aggressive. Prefer "
        "--drop-noisy-basenames for v6+ runs.",
    )
    ap.add_argument(
        "--drop-noisy-basenames",
        action="store_true",
        default=False,
        help="Drop positives on generic noise basenames only "
        "(package*.json, lock files, README, .env*, openapi.*, "
        "barrel index.*) — skips pay-com-specific path substrings. "
        "Safer than --drop-noisy-files. Overridden by --drop-noisy-files "
        "if both passed.",
    )
    ap.add_argument(
        "--drop-generated",
        action="store_true",
        default=False,
        help="Drop codegen / schema artifacts: *.generated.{ts,js,tsx}, "
        "*.proto, *.schema.json, paths containing /__generated__/. "
        "Safe complement to --drop-noisy-basenames. v6.1 audit: "
        "~1,700 rows dropped, 0 false-positives.",
    )
    ap.add_argument(
        "--drop-trivial-positives",
        action="store_true",
        default=False,
        help="Drop positives whose content is only import/require statements, "
        "dep-bump version lines, or test fixtures shorter than 50 chars.",
    )
    ap.add_argument(
        "--min-query-len",
        type=int,
        default=0,
        help="Drop tickets whose build_query_text output is shorter than N chars. "
        "Typical value: 50 (weak regression signal).",
    )
    ap.add_argument(
        "--oversample",
        default="",
        help="Comma-separated PROJECT=MULTIPLIER pairs (e.g. 'PI=3') — "
        "duplicate all records for those projects N times in train.",
    )
    ap.add_argument(
        "--drop-popular-files",
        type=int,
        default=0,
        help="IDF filter: drop positive pairs whose (repo,file_path) "
        "appears as positive in >= N distinct tickets. Typical 20. "
        "0 disables.",
    )
    ap.add_argument(
        "--min-positives",
        type=int,
        default=0,
        help="Drop tickets whose surviving positive count is < N (after all other filters). Typical 10. 0 disables.",
    )
    ap.add_argument(
        "--max-positives",
        type=int,
        default=0,
        help="Cap per-ticket positive count at N (keep longest-doc "
        "chunk-positives first, then diff-positives). Typical 50. "
        "0 disables.",
    )
    ap.add_argument(
        "--dedupe-same-file",
        action="store_true",
        default=False,
        help="Keep only one positive per (ticket, repo, file_path). "
        "Prefers diff-positive (hunk content) over chunk-positive "
        "(whole chunk), and longest content among multiple chunks. "
        "Prevents label inflation found in 87%% of audit tickets.",
    )
    ap.add_argument(
        "--max-rows-per-ticket",
        type=int,
        default=0,
        help="Cap total rows (pos + mined_neg + rand_neg) per ticket at N, "
        "proportionally preserving the pos:mined:rand ratio. Applied "
        "before --oversample multiplier. Typical 120. 0 disables.",
    )
    ap.add_argument(
        "--skip-empty-desc-multi-file",
        action="store_true",
        default=False,
        help="Skip train tickets with empty description AND >= 5 files_changed (audit: 2.5x baseline regression rate).",
    )
    ap.add_argument(
        "--fe-hard-negatives",
        type=int,
        default=0,
        help="Inject N hard negatives from frontend-cluster repos "
        "(graphql/space-web/backoffice-web/hosted-fields) for "
        "tickets whose GT has NO FE repo. Breaks BO->FE vocab "
        "leakage that hurts CORE tickets. Typical 3-5. 0 disables.",
    )
    ap.add_argument(
        "--test-ratio",
        type=float,
        default=0.0,
        help="Fraction of tickets reserved as REAL holdout (stratified "
        "per project). Typical 0.15-0.20. Overrides the legacy "
        "5-ticket auto-selection — use this for any v9+ run so the "
        "verdict gate can detect overfitting. 0.0 = legacy behaviour.",
    )
    args = ap.parse_args()

    rng = random.Random(args.seed)

    requested = [x.strip() for x in args.test_tasks.split(",") if x.strip()] or None
    projects = [x.strip() for x in args.projects.split(",") if x.strip()] or None

    tasks = load_gt_tasks(Path(args.tasks_db), projects=projects, min_files=args.min_files)
    if len(tasks) <= 3:
        sys.exit(
            f"ERROR: only {len(tasks)} GT tasks (projects={projects or 'all'}, min_files={args.min_files}) -- aborting."
        )

    train_tasks, test_tasks = pick_test_tasks(
        tasks,
        requested,
        test_ratio=args.test_ratio,
        seed=args.seed,
    )

    print(f"[info] GT tasks: {len(tasks)}  train: {len(train_tasks)}  test: {len(test_tasks)}")
    print("[test selection]")
    for t in test_tasks:
        res = t["resolution"].isoformat() if t["resolution"] else "unknown"
        print(f"  - {t['ticket_id']:<6} {res}  {t['summary']!r}")

    kconn = sqlite3.connect(args.knowledge_db, timeout=30)
    kconn.row_factory = sqlite3.Row
    kconn.execute("PRAGMA query_only = ON")

    # Test: positives only. Test queries use the same enrichment flags so the
    # eval set is consistent with what the model is trained on.
    # Parse oversample spec: "PI=3,CORE=2" -> {"PI": 3, "CORE": 2}
    oversample_map: dict[str, int] = {}
    for chunk in (args.oversample or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        proj, mult = chunk.split("=", 1)
        try:
            oversample_map[proj.strip().upper()] = max(1, int(mult))
        except ValueError:
            continue
    if oversample_map:
        print(f"[filter] oversample: {oversample_map}", flush=True)

    # Precompute popular-file set across train_tasks (IDF-style filter).
    # A (repo, rel_path) pair that appears in N or more ticket's files_changed
    # is considered "generic" — e.g. shared UI scaffolding, authorization barrels,
    # package.json — and its positives tend to introduce spurious correlations.
    popular_files: set | None = None
    if args.drop_popular_files > 0:
        file_ticket_counts: dict[tuple[str, str], set[str]] = {}
        for t in train_tasks:
            for qualified in t["files_changed"]:
                split = split_qualified_path(qualified)
                if not split:
                    continue
                file_ticket_counts.setdefault(split, set()).add(t["ticket_id"])
        popular_files = {k for k, tids in file_ticket_counts.items() if len(tids) >= args.drop_popular_files}
        print(
            f"[filter] drop_popular_files: {len(popular_files)} files flagged "
            f"(seen in >= {args.drop_popular_files} distinct tickets)",
            flush=True,
        )

    test_records: list[dict] = []
    for t in test_tasks:
        test_records.extend(
            build_positives(
                t,
                kconn,
                use_description=args.use_description,
                use_diff_positives=args.use_diff_positives,
                diff_snippet_max_chars=args.diff_snippet_max_chars,
                drop_noisy_files=args.drop_noisy_files,
                drop_noisy_basenames_only=args.drop_noisy_basenames,
                drop_generated=args.drop_generated,
                drop_trivial_positives=args.drop_trivial_positives,
                popular_files=popular_files,
                dedupe_same_file=args.dedupe_same_file,
                max_positives=args.max_positives,
            )
        )

    # Train: positives + mined + random.
    # Drop any train positive whose chunk comes from a file that appears in
    # some test task's files_changed -- otherwise the model would see test
    # documents during training (anti-leakage).
    test_files_qualified = {qf for t in test_tasks for qf in t["files_changed"]}
    # Diff-augmented positives use a "<qf>#diff" chunk_file marker; their
    # underlying file is still test-leakage-relevant, so derive the bare path
    # for the leakage check.

    def _bare_chunk_file(cf: str) -> str:
        return cf[:-5] if cf.endswith("#diff") else cf

    train_records: list[dict] = []
    n_pos = n_mined = n_rand = 0
    n_fe_neg = 0
    n_pos_chunk = n_pos_diff = 0
    n_pos_dropped = 0
    n_neg_dropped = 0
    n_tasks_with_patches = 0
    n_tasks_without_patches = 0
    _total_train = len(train_tasks)
    n_dropped_short_query = 0
    n_dropped_empty_desc_multi_file = 0
    n_capped_tickets = 0
    n_rows_capped_total = 0
    print(f"[progress] processing {_total_train} train tasks...", flush=True)
    for _idx, t in enumerate(train_tasks, 1):
        if _idx == 1 or _idx % 20 == 0 or _idx == _total_train:
            print(
                f"[progress] {_idx}/{_total_train}  pos={n_pos} "
                f"(chunk={n_pos_chunk} diff={n_pos_diff}) mined={n_mined} rand={n_rand} "
                f"fe_neg={n_fe_neg}",
                flush=True,
            )
        if args.min_query_len > 0:
            qt = build_query_text(t, use_description=args.use_description)
            if len(qt) < args.min_query_len:
                n_dropped_short_query += 1
                continue
        if (
            args.skip_empty_desc_multi_file
            and len(t.get("description") or "") == 0
            and len(t.get("files_changed") or []) >= 5
        ):
            n_dropped_empty_desc_multi_file += 1
            continue
        if t.get("patches"):
            n_tasks_with_patches += 1
        else:
            n_tasks_without_patches += 1
        raw_positives = build_positives(
            t,
            kconn,
            use_description=args.use_description,
            use_diff_positives=args.use_diff_positives,
            diff_snippet_max_chars=args.diff_snippet_max_chars,
            drop_noisy_files=args.drop_noisy_files,
            drop_noisy_basenames_only=args.drop_noisy_basenames,
            drop_generated=args.drop_generated,
            drop_trivial_positives=args.drop_trivial_positives,
            popular_files=popular_files,
            dedupe_same_file=args.dedupe_same_file,
            max_positives=args.max_positives,
        )
        positives = [p for p in raw_positives if _bare_chunk_file(p["chunk_file"]) not in test_files_qualified]
        n_pos_dropped += len(raw_positives) - len(positives)
        if not positives:
            continue
        if args.min_positives > 0 and len(positives) < args.min_positives:
            continue
        # Negative ratios are computed against chunk-positives only so adding
        # diff positives doesn't double the negative budget per task.
        chunk_pos_count = sum(1 for p in positives if p.get("positive_source") == "chunk")
        if chunk_pos_count == 0:
            chunk_pos_count = len(positives)
        raw_mined = mine_negatives(
            t,
            kconn,
            n_needed=round(chunk_pos_count * args.neg_mined_ratio),
            use_description=args.use_description,
        )
        mined = [n for n in raw_mined if n["chunk_file"] not in test_files_qualified]
        raw_rand = random_negatives(
            t,
            kconn,
            n_needed=round(chunk_pos_count * args.neg_random_ratio),
            rng=rng,
            use_description=args.use_description,
        )
        randneg = [n for n in raw_rand if n["chunk_file"] not in test_files_qualified]
        raw_fe = mine_fe_hard_negatives(
            t,
            kconn,
            n_needed=args.fe_hard_negatives,
            use_description=args.use_description,
        )
        fe_neg = [n for n in raw_fe if n["chunk_file"] not in test_files_qualified]
        n_neg_dropped += (len(raw_mined) - len(mined)) + (len(raw_rand) - len(randneg)) + (len(raw_fe) - len(fe_neg))
        # Per-ticket row cap: subsample proportionally preserving pos:mined:rand ratio.
        # Applied BEFORE oversample multiplier so multiplier acts on the capped set.
        if args.max_rows_per_ticket > 0:
            cap = args.max_rows_per_ticket
            total = len(positives) + len(mined) + len(randneg)
            if total > cap:
                ratio = cap / total
                keep_pos = min(len(positives), round(len(positives) * ratio))
                keep_mined = min(len(mined), round(len(mined) * ratio))
                keep_rand = min(len(randneg), round(len(randneg) * ratio))
                # Distribute rounding slack so the total approximates `cap`.
                diff = cap - (keep_pos + keep_mined + keep_rand)
                # Expand when we're short, contract when we overshoot; priority
                # order (pos, mined, rand) is stable for determinism.
                while diff > 0:
                    if keep_pos < len(positives):
                        keep_pos += 1
                        diff -= 1
                        if diff == 0:
                            break
                    if keep_mined < len(mined):
                        keep_mined += 1
                        diff -= 1
                        if diff == 0:
                            break
                    if keep_rand < len(randneg):
                        keep_rand += 1
                        diff -= 1
                        if diff == 0:
                            break
                    if keep_pos == len(positives) and keep_mined == len(mined) and keep_rand == len(randneg):
                        break
                while diff < 0:
                    if keep_rand > 0:
                        keep_rand -= 1
                        diff += 1
                        if diff == 0:
                            break
                    if keep_mined > 0:
                        keep_mined -= 1
                        diff += 1
                        if diff == 0:
                            break
                    if keep_pos > 0:
                        keep_pos -= 1
                        diff += 1
                        if diff == 0:
                            break
                    if keep_pos == 0 and keep_mined == 0 and keep_rand == 0:
                        break

                def _subsample(items: list, k: int) -> list:
                    if k >= len(items):
                        return items
                    if k <= 0:
                        return []
                    idxs = sorted(rng.sample(range(len(items)), k))
                    return [items[i] for i in idxs]

                new_total = keep_pos + keep_mined + keep_rand
                n_rows_capped_total += total - new_total
                n_capped_tickets += 1
                positives = _subsample(positives, keep_pos)
                mined = _subsample(mined, keep_mined)
                randneg = _subsample(randneg, keep_rand)
        # Oversample: duplicate records N times for this project if flag is set.
        proj_prefix = t["ticket_id"].split("-", 1)[0].upper()
        mult = oversample_map.get(proj_prefix, 1)
        for _ in range(mult):
            train_records.extend(positives)
            train_records.extend(mined)
            train_records.extend(randneg)
            train_records.extend(fe_neg)
        n_pos += len(positives) * mult
        n_pos_chunk += sum(1 for p in positives if p.get("positive_source") == "chunk") * mult
        n_pos_diff += sum(1 for p in positives if p.get("positive_source") == "diff") * mult
        n_mined += len(mined) * mult
        n_rand += len(randneg) * mult
        n_fe_neg += len(fe_neg) * mult

    kconn.close()
    if args.min_query_len > 0:
        print(f"[filter] dropped {n_dropped_short_query} tickets with query_len < {args.min_query_len}", flush=True)

    # Anti-leakage checks.
    train_ids = {t["ticket_id"] for t in train_tasks}
    test_ids = {t["ticket_id"] for t in test_tasks}
    assert train_ids.isdisjoint(test_ids), f"LEAK: overlapping ticket_ids {train_ids & test_ids}"
    verify_no_leakage(train_records, test_tasks)

    # Shuffle train (deterministic).
    rng.shuffle(train_records)

    out_dir = Path(args.out)
    train_path = out_dir / "train.jsonl"
    test_path = out_dir / "test.jsonl"
    manifest_path = out_dir / "manifest.json"

    write_jsonl(train_path, train_records)
    write_jsonl(test_path, test_records)

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "seed": args.seed,
        "projects": projects or "all",
        "min_files": args.min_files,
        "neg_mined_ratio": args.neg_mined_ratio,
        "neg_random_ratio": args.neg_random_ratio,
        "tasks_db": str(Path(args.tasks_db).resolve()),
        "knowledge_db": str(Path(args.knowledge_db).resolve()),
        # query / positive enrichment knobs (v4)
        "query_uses_description": bool(args.use_description),
        "query_uses_comments_when_short": bool(args.use_description),  # comments only kick in alongside description
        "positives_include_diff": bool(args.use_diff_positives),
        "diff_snippet_max_chars": args.diff_snippet_max_chars,
        # filter knobs (v5+)
        "drop_noisy_files": bool(args.drop_noisy_files),
        "drop_noisy_basenames": bool(args.drop_noisy_basenames),
        "drop_generated": bool(args.drop_generated),
        "drop_trivial_positives": bool(args.drop_trivial_positives),
        "min_query_len": int(args.min_query_len),
        "oversample": args.oversample or "",
        "drop_popular_files": int(args.drop_popular_files),
        "min_positives": int(args.min_positives),
        "max_positives": int(args.max_positives),
        "dedupe_same_file": bool(args.dedupe_same_file),
        "max_rows_per_ticket": int(args.max_rows_per_ticket),
        "skip_empty_desc_multi_file": bool(args.skip_empty_desc_multi_file),
        "fe_hard_negatives": args.fe_hard_negatives,
        "n_capped_tickets": n_capped_tickets,
        "n_rows_capped_total": n_rows_capped_total,
        "n_dropped_empty_desc_multi_file": n_dropped_empty_desc_multi_file,
        "train_tickets": sorted(train_ids),
        "train_tickets_effective": sorted({r["ticket_id"] for r in train_records}),
        "test_tickets": sorted(test_ids),
        "train_positives": n_pos,
        "train_positives_from_chunks": n_pos_chunk,
        "train_positives_from_diffs": n_pos_diff,
        "train_positives_dropped_due_to_test_overlap": n_pos_dropped,
        "train_negatives_mined": n_mined,
        "train_negatives_random": n_rand,
        "train_negatives_fe_hard": n_fe_neg,
        "train_negatives_dropped_due_to_test_overlap": n_neg_dropped,
        "train_tasks_with_patches": n_tasks_with_patches,
        "train_tasks_without_patches": n_tasks_without_patches,
        "train_total": len(train_records),
        "test_positives": len(test_records),
        "anti_leakage_verified": True,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    print(f"[out] {train_path}  ({len(train_records)} rows)")
    print(f"[out] {test_path}   ({len(test_records)} rows)")
    print(f"[out] {manifest_path}")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
