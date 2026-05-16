"""Build a clean jira eval by dropping mechanical PR-noise + unhittable GT.

DE1 (meta-converged STAGE 1) — drops:
1. Mechanical PR-noise paths (lockfiles, generated, configs, dotfiles, tests).
2. GT (repo, file_path) pairs not in current `db/knowledge.db` chunks table.
3. Queries with <3 GT pairs after cleaning.

Inputs:  profiles/pay-com/eval/jira_eval_n900.jsonl
Outputs: profiles/pay-com/eval/jira_eval_clean.jsonl
         .claude/debug/current/exp1-clean-eval-stats.md (also stdout)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

NOISE_REGEXES: tuple[re.Pattern, ...] = tuple(
    re.compile(p)
    for p in [
        r"(?:^|/)package-lock\.json$",
        r"(?:^|/)package\.json$",
        r"(?:^|/)generated/",
        r"(?:^|/)__generated__/",
        r"(?:^|/)\.drone\.yml$",
        r"(?:^|/)Dockerfile(\.|$)",
        r"\.test\.[a-z]+$",
        r"\.spec\.[a-z]+$",
        r"_spec\.[a-z]+$",
        r"(?:^|/)__tests__/",
        r"(?:^|/)\.eslintrc(\.|$)",
        r"(?:^|/)\.prettierignore$",
        r"(?:^|/)\.dockerignore$",
        r"(?:^|/)\.gitignore$",
        r"(?:^|/)tsconfig(?:\..*)?\.json$",
        r"(?:^|/)\.editorconfig$",
    ]
)


def is_noise(file_path: str) -> bool:
    return any(rx.search(file_path) for rx in NOISE_REGEXES)


def load_indexed_pairs(db_path: Path) -> tuple[set[tuple[str, str]], dict[str, set[str]]]:
    """Return (exact_pairs, by_repo_paths_map).

    `by_repo` is repo → set of all chunk file_paths. Used for suffix-match to
    handle the extractor's category-prefix scheme (`proto/service.proto`,
    `workflows/activities/foo.js`, etc.) per index-gap-detective F-bucket finding.
    """
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute("SELECT DISTINCT repo_name, file_path FROM chunks")
    rows = cur.fetchall()
    con.close()
    pairs = {(r, p) for r, p in rows}
    by_repo: dict[str, set[str]] = {}
    for r, p in rows:
        by_repo.setdefault(r, set()).add(p)
    return pairs, by_repo


def is_indexed(
    repo: str, fp: str, indexed: set[tuple[str, str]], by_repo: dict[str, set[str]]
) -> tuple[bool, str | None]:
    """Check if (repo, fp) is indexed. Returns (found, canonical_path).

    1. Exact match: returns (True, fp).
    2. Suffix-match: chunk path endswith "/{fp}" — returns (True, canonical_chunk_path).
       This handles extractor's category-prefix scheme.
    3. None: returns (False, None).
    """
    if (repo, fp) in indexed:
        return True, fp
    repo_paths = by_repo.get(repo)
    if not repo_paths:
        return False, None
    suffix = "/" + fp
    for chunk_path in repo_paths:
        if chunk_path.endswith(suffix):
            return True, chunk_path
    return False, None


def expected_pairs(row: dict) -> list[tuple[str, str]]:
    out = []
    for p in row.get("expected_paths", []):
        if isinstance(p, dict):
            out.append((p.get("repo_name", ""), p.get("file_path", "")))
        elif isinstance(p, list | tuple) and len(p) >= 2:
            out.append((p[0], p[1]))
    return out


def clean_row(
    row: dict,
    indexed: set[tuple[str, str]],
    by_repo: dict[str, set[str]],
) -> tuple[list[tuple[str, str]], int, int, int]:
    """Return (kept_pairs, dropped_noise, dropped_unhittable, suffix_matched).

    kept_pairs uses CANONICAL chunk path (so downstream bench's hit-detection
    matches exact strings). suffix_matched counts how many GT entries were
    rescued by the suffix-match (F-bucket) — informational only.
    """
    pairs = expected_pairs(row)
    kept: list[tuple[str, str]] = []
    n_noise = 0
    n_unhit = 0
    n_suffix = 0
    for repo, fp in pairs:
        if is_noise(fp):
            n_noise += 1
            continue
        found, canonical = is_indexed(repo, fp, indexed, by_repo)
        if not found:
            n_unhit += 1
            continue
        if canonical != fp:
            n_suffix += 1
        kept.append((repo, canonical or fp))
    return kept, n_noise, n_unhit, n_suffix


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, default=REPO_ROOT / "profiles/pay-com/eval/jira_eval_n900.jsonl")
    p.add_argument("--output", type=Path, default=REPO_ROOT / "profiles/pay-com/eval/jira_eval_clean.jsonl")
    p.add_argument("--db", type=Path, default=REPO_ROOT / "db/knowledge.db")
    p.add_argument("--min-gt", type=int, default=3)
    p.add_argument("--stats", type=Path, default=REPO_ROOT / ".claude/debug/current/exp1-clean-eval-stats.md")
    args = p.parse_args()

    if not args.input.is_file():
        sys.exit(f"input missing: {args.input}")
    if not args.db.is_file():
        sys.exit(f"db missing: {args.db}")

    indexed, by_repo = load_indexed_pairs(args.db)
    print(f"loaded {len(indexed):,} distinct (repo, file_path) pairs from {args.db}")

    rows: list[dict] = []
    with args.input.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    n_orig = len(rows)

    orig_total_gt = sum(len(expected_pairs(r)) for r in rows)
    sum_dropped_noise = 0
    sum_dropped_unhit = 0
    sum_kept_gt = 0
    sum_suffix_matched = 0
    n_dropped_query = 0
    repo_counter: Counter[str] = Counter()
    cleaned_rows: list[dict] = []

    for row in rows:
        kept, n_noise, n_unhit, n_suffix = clean_row(row, indexed, by_repo)
        sum_dropped_noise += n_noise
        sum_dropped_unhit += n_unhit
        sum_suffix_matched += n_suffix
        if len(kept) < args.min_gt:
            n_dropped_query += 1
            continue
        sum_kept_gt += len(kept)
        for repo, _ in kept:
            repo_counter[repo] += 1
        new_row = dict(row)
        new_row["expected_paths"] = [{"repo_name": r, "file_path": fp} for r, fp in kept]
        cleaned_rows.append(new_row)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        for r in cleaned_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_clean = len(cleaned_rows)
    mean_orig = orig_total_gt / n_orig if n_orig else 0.0
    mean_clean = sum_kept_gt / n_clean if n_clean else 0.0
    pct_q_dropped = 100.0 * n_dropped_query / n_orig if n_orig else 0.0
    pct_gt_dropped = 100.0 * (orig_total_gt - sum_kept_gt) / orig_total_gt if orig_total_gt else 0.0

    lines = [
        "# EXP1 — clean jira eval stats",
        "",
        f"- Input: `{args.input.relative_to(REPO_ROOT) if args.input.is_relative_to(REPO_ROOT) else args.input}`",
        f"- Output: `{args.output.relative_to(REPO_ROOT) if args.output.is_relative_to(REPO_ROOT) else args.output}`",
        f"- DB: `{args.db.relative_to(REPO_ROOT) if args.db.is_relative_to(REPO_ROOT) else args.db}` ({len(indexed):,} distinct (repo, file) pairs)",
        f"- min_gt threshold: {args.min_gt}",
        "",
        "## Counts",
        "",
        f"- original n_queries = {n_orig}",
        f"- original total GT pairs = {orig_total_gt:,}",
        f"- dropped noise pairs = {sum_dropped_noise:,}",
        f"- dropped unhittable pairs = {sum_dropped_unhit:,}",
        f"- suffix-matched (F-bucket recovered) = {sum_suffix_matched:,}",
        f"- queries dropped (GT < {args.min_gt}) = {n_dropped_query} ({pct_q_dropped:.2f}%)",
        f"- final n_queries = {n_clean}",
        f"- final total GT pairs = {sum_kept_gt:,}",
        f"- mean GT/query: before = {mean_orig:.2f}, after = {mean_clean:.2f}",
        f"- total GT pairs dropped = {pct_gt_dropped:.2f}% of original",
        "",
        "## Top 20 repos in cleaned set (by GT-pair count)",
        "",
        "| rank | repo | gt_pairs |",
        "|---|---|---|",
    ]
    for i, (repo, count) in enumerate(repo_counter.most_common(20), 1):
        lines.append(f"| {i} | {repo} | {count} |")
    lines.append("")

    text = "\n".join(lines)
    print(text)

    args.stats.parent.mkdir(parents=True, exist_ok=True)
    args.stats.write_text(text)
    print(f"wrote stats to {args.stats}")
    print(f"wrote {n_clean} rows to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
