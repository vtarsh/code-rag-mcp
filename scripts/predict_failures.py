#!/usr/bin/env python3
"""Failure Propagation Engine: changed files -> affected tests -> downstream repos.

Usage:
    python3 predict_failures.py --files "repo/path/file.js,repo/path/other.js"
    python3 predict_failures.py --repo grpc-apm-trustly
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag")) / "db" / "knowledge.db"
# Fallback to pay-knowledge if code-rag doesn't exist
if not DB_PATH.exists():
    DB_PATH = Path.home() / ".pay-knowledge" / "db" / "knowledge.db"


@dataclass
class TestHit:
    test_file: str
    verified: bool


@dataclass
class DownstreamRepo:
    repo: str
    edge_type: str
    detail: str | None = None


@dataclass
class CoChangedFile:
    target: str
    pattern_type: str
    occurrences: int
    confidence: float


@dataclass
class CIStatus:
    repo: str
    conclusion: str
    created_at: str
    workflow: str


@dataclass
class FileAnalysis:
    """Analysis results for a single changed file."""

    file_path: str  # repo/path format
    repo: str
    relative_path: str
    tests: list = field(default_factory=list)
    downstream: list = field(default_factory=list)
    co_changed: list = field(default_factory=list)
    ci_statuses: dict = field(default_factory=dict)  # repo -> CIStatus


@dataclass
class RepoRisk:
    """Aggregated risk for a downstream repo."""

    repo: str
    is_downstream: bool = False
    edge_types: list = field(default_factory=list)
    co_changed_hits: list = field(default_factory=list)
    has_tests: bool = False
    task_pattern_hit: bool = False
    ci_status: CIStatus | None = None

    @property
    def level(self) -> str:
        if self.is_downstream and self.co_changed_hits and self.has_tests:
            return "HIGH"
        if self.is_downstream and self.co_changed_hits:
            return "HIGH"
        if self.is_downstream or self.co_changed_hits:
            return "MEDIUM"
        if self.task_pattern_hit:
            return "LOW"
        return "LOW"


def get_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"Error: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def find_tests(conn: sqlite3.Connection, repo: str, relative_path: str) -> list[TestHit]:
    """Find tests for a source file via test_source_map."""
    results = []
    # Exact match on source_file
    rows = conn.execute(
        "SELECT test_file, verified FROM test_source_map WHERE repo_name = ? AND source_file = ?",
        (repo, relative_path),
    ).fetchall()
    for r in rows:
        results.append(TestHit(test_file=r["test_file"], verified=bool(r["verified"])))

    # Also check if this file is referenced in another repo's test map (e.g. webhook handler tests)
    rows = conn.execute(
        "SELECT repo_name, test_file, verified FROM test_source_map WHERE source_file LIKE ?",
        (f"%{relative_path}%",),
    ).fetchall()
    for r in rows:
        if r["repo_name"] != repo:
            results.append(
                TestHit(
                    test_file=f"{r['repo_name']}/{r['test_file']}",
                    verified=bool(r["verified"]),
                )
            )

    return results


def find_downstream(conn: sqlite3.Connection, repo: str) -> list[DownstreamRepo]:
    """Find repos that depend on / are downstream of the changed repo.

    In graph_edges: source=dependent, target=dependency.
    So downstream of repo X = rows where target=X (repos that depend on X).
    """
    rows = conn.execute(
        "SELECT source, edge_type, detail FROM graph_edges WHERE target = ?",
        (repo,),
    ).fetchall()
    return [DownstreamRepo(repo=r["source"], edge_type=r["edge_type"], detail=r["detail"]) for r in rows]


def find_co_changed(conn: sqlite3.Connection, repo: str, relative_path: str) -> list[CoChangedFile]:
    """Find files that historically change together with this file."""
    full_path = f"{repo}/{relative_path}"
    results = []

    # cross_repo_file: exact file match
    rows = conn.execute(
        "SELECT target, pattern_type, occurrences, confidence FROM file_patterns "
        "WHERE source = ? AND pattern_type = 'cross_repo_file'",
        (full_path,),
    ).fetchall()
    for r in rows:
        results.append(
            CoChangedFile(
                target=r["target"],
                pattern_type=r["pattern_type"],
                occurrences=r["occurrences"],
                confidence=r["confidence"],
            )
        )

    # Also check reverse direction
    rows = conn.execute(
        "SELECT source AS target, pattern_type, occurrences, confidence FROM file_patterns "
        "WHERE target = ? AND pattern_type = 'cross_repo_file'",
        (full_path,),
    ).fetchall()
    for r in rows:
        results.append(
            CoChangedFile(
                target=r["target"],
                pattern_type=r["pattern_type"],
                occurrences=r["occurrences"],
                confidence=r["confidence"],
            )
        )

    # dir_pair: match on directory prefix
    dir_path = f"{repo}/{'/'.join(relative_path.split('/')[:-1])}/"
    if dir_path != f"{repo}/":
        rows = conn.execute(
            "SELECT target, pattern_type, occurrences, confidence FROM file_patterns "
            "WHERE source = ? AND pattern_type = 'dir_pair'",
            (dir_path,),
        ).fetchall()
        for r in rows:
            results.append(
                CoChangedFile(
                    target=r["target"],
                    pattern_type=r["pattern_type"],
                    occurrences=r["occurrences"],
                    confidence=r["confidence"],
                )
            )

    return results


def get_ci_status(conn: sqlite3.Connection, repo: str) -> CIStatus | None:
    """Get the most recent CI run for a repo."""
    row = conn.execute(
        "SELECT repo_name, conclusion, created_at, workflow_name FROM ci_runs "
        "WHERE repo_name = ? ORDER BY created_at DESC LIMIT 1",
        (repo,),
    ).fetchone()
    if row:
        return CIStatus(
            repo=row["repo_name"],
            conclusion=row["conclusion"] or "unknown",
            created_at=row["created_at"],
            workflow=row["workflow_name"],
        )
    return None


def find_task_pattern_repos(conn: sqlite3.Connection, repo: str) -> list[tuple[str, int, float]]:
    """Find repos that historically get missed when this repo changes (task_patterns)."""
    rows = conn.execute(
        "SELECT missed_repo, occurrences, confidence FROM task_patterns "
        "WHERE trigger_repos LIKE ? ORDER BY occurrences DESC",
        (f'%"{repo}"%',),
    ).fetchall()
    return [(r["missed_repo"], r["occurrences"], r["confidence"]) for r in rows]


def analyze_file(conn: sqlite3.Connection, file_path: str) -> FileAnalysis:
    """Analyze a single changed file (repo/path format)."""
    parts = file_path.split("/", 1)
    if len(parts) != 2:
        print(f"Warning: invalid file format '{file_path}', expected 'repo/path'", file=sys.stderr)
        return FileAnalysis(file_path=file_path, repo=parts[0], relative_path="")

    repo, relative_path = parts
    analysis = FileAnalysis(file_path=file_path, repo=repo, relative_path=relative_path)
    analysis.tests = find_tests(conn, repo, relative_path)
    analysis.downstream = find_downstream(conn, repo)
    analysis.co_changed = find_co_changed(conn, repo, relative_path)

    # Collect CI statuses for all affected repos
    affected_repos = set()
    for d in analysis.downstream:
        affected_repos.add(d.repo)
    for c in analysis.co_changed:
        target_repo = c.target.split("/", 1)[0]
        affected_repos.add(target_repo)

    for r in affected_repos:
        ci = get_ci_status(conn, r)
        if ci:
            analysis.ci_statuses[r] = ci

    return analysis


def compute_risks(conn: sqlite3.Connection, analyses: list[FileAnalysis]) -> dict[str, RepoRisk]:
    """Aggregate risk across all changed files for each affected repo."""
    risks: dict[str, RepoRisk] = {}
    source_repos = set()

    for a in analyses:
        source_repos.add(a.repo)

        # Downstream repos
        for d in a.downstream:
            if d.repo not in risks:
                risks[d.repo] = RepoRisk(repo=d.repo)
            risks[d.repo].is_downstream = True
            if d.edge_type not in risks[d.repo].edge_types:
                risks[d.repo].edge_types.append(d.edge_type)

        # Co-changed files
        for c in a.co_changed:
            target_repo = c.target.split("/", 1)[0]
            if target_repo in source_repos:
                continue  # Skip self-references
            if target_repo not in risks:
                risks[target_repo] = RepoRisk(repo=target_repo)
            risks[target_repo].co_changed_hits.append(c)

    # Check for test coverage in each risk repo
    for repo_name, risk in risks.items():
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM test_source_map WHERE repo_name = ?",
            (repo_name,),
        ).fetchone()
        risk.has_tests = row["cnt"] > 0

        # CI status
        risk.ci_status = get_ci_status(conn, repo_name)

    # Task pattern hits
    for a in analyses:
        task_hits = find_task_pattern_repos(conn, a.repo)
        for missed_repo, _occ, _conf in task_hits:
            if missed_repo in source_repos:
                continue
            if missed_repo not in risks:
                risks[missed_repo] = RepoRisk(repo=missed_repo)
            risks[missed_repo].task_pattern_hit = True
            # Also get CI for task-pattern-only repos
            if not risks[missed_repo].ci_status:
                risks[missed_repo].ci_status = get_ci_status(conn, missed_repo)

    return risks


def format_ci(ci: CIStatus | None) -> str:
    if not ci:
        return "no CI data"
    icon = "\u2705" if ci.conclusion == "success" else "\u274c"
    date = ci.created_at[:10] if ci.created_at else "unknown"
    return f"{icon} last {ci.conclusion} ({date})"


def print_report(analyses: list[FileAnalysis], risks: dict[str, RepoRisk]):
    """Print the structured failure prediction report."""
    print(f"\n{'=' * 60}")
    print(f"  Failure Prediction for {len(analyses)} changed file(s)")
    print(f"{'=' * 60}\n")

    for a in analyses:
        print(f"\U0001f4c4 {a.file_path}")

        # Tests
        if a.tests:
            for t in a.tests:
                v = "\u2705 (verified)" if t.verified else "\u2753 (unverified)"
                print(f"  Tests: {t.test_file} {v}")
        else:
            print("  Tests: none mapped")

        # Downstream
        if a.downstream:
            downstream_strs = []
            for d in a.downstream:
                downstream_strs.append(f"{d.repo} ({d.edge_type})")
            print(f"  Downstream: {', '.join(downstream_strs)}")
        else:
            print("  Downstream: none found")

        # Co-changed
        if a.co_changed:
            for c in a.co_changed:
                pct = f"{c.confidence * 100:.0f}%"
                print(f"  Co-changed: {c.target} ({c.occurrences} tasks, {pct})")
        else:
            print("  Co-changed: none found")

        # CI status for affected repos
        if a.ci_statuses:
            ci_strs = []
            for repo_name, ci in a.ci_statuses.items():
                ci_strs.append(f"{repo_name}: {format_ci(ci)}")
            print(f"  CI Status: {'; '.join(ci_strs)}")

        print()

    # Risk summary
    if risks:
        sorted_risks = sorted(risks.values(), key=lambda r: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[r.level])
        print("\u26a0\ufe0f  Risk Summary:")

        for risk in sorted_risks:
            reasons = []
            if risk.is_downstream:
                reasons.append("downstream")
            if risk.co_changed_hits:
                reasons.append("co-changed")
            if risk.has_tests:
                reasons.append("has test coverage")
            if risk.task_pattern_hit:
                reasons.append("task pattern")
            if risk.ci_status:
                reasons.append(format_ci(risk.ci_status))

            reason_str = " + ".join(reasons) if reasons else "indirect"
            icon = {"HIGH": "\U0001f534", "MEDIUM": "\U0001f7e1", "LOW": "\u26aa"}[risk.level]
            print(f"  {icon} {risk.level}: {risk.repo} -- {reason_str}")

        print()
    else:
        print("No downstream risks detected.\n")


def run_repo_mode_json(conn: sqlite3.Connection, repo: str):
    """JSON output for repo mode."""
    downstream = find_downstream(conn, repo)
    downstream_by_repo: dict[str, list] = defaultdict(list)
    for d in downstream:
        downstream_by_repo[d.repo].append(d)

    task_hits = find_task_pattern_repos(conn, repo)

    output = {
        "repo": repo,
        "downstream": [
            {
                "repo": dep_repo,
                "edge_types": sorted(set(e.edge_type for e in edges)),
                "ci": (lambda c: {"conclusion": c.conclusion, "date": c.created_at} if c else None)(
                    get_ci_status(conn, dep_repo)
                ),
            }
            for dep_repo, edges in sorted(downstream_by_repo.items())
        ],
        "task_patterns": [{"missed_repo": m, "occurrences": o, "confidence": c} for m, o, c in task_hits],
        "risk_summary": [],
    }

    # Build risk levels
    task_pattern_repos = {m for m, _, _ in task_hits}
    for dep_repo in downstream_by_repo:
        level = "MEDIUM"
        if dep_repo in task_pattern_repos:
            level = "HIGH"
        output["risk_summary"].append({"repo": dep_repo, "level": level})
    for m, _o, _c in task_hits:
        if m not in downstream_by_repo:
            output["risk_summary"].append({"repo": m, "level": "LOW"})

    print(json.dumps(output, indent=2))


def run_repo_mode(conn: sqlite3.Connection, repo: str):
    """Predict failures for all downstream repos of a given repo."""
    print(f"\n{'=' * 60}")
    print(f"  Failure Prediction for repo: {repo}")
    print(f"{'=' * 60}\n")

    # Downstream repos (who depends on this repo)
    downstream = find_downstream(conn, repo)
    if not downstream:
        print(f"No downstream dependencies found for {repo}.\n")

    # Group by repo
    downstream_by_repo: dict[str, list[DownstreamRepo]] = defaultdict(list)
    for d in downstream:
        downstream_by_repo[d.repo].append(d)

    print(f"\U0001f517 Downstream repos ({len(downstream_by_repo)}):")
    for dep_repo, edges in sorted(downstream_by_repo.items()):
        edge_types = ", ".join(sorted(set(e.edge_type for e in edges)))
        ci = get_ci_status(conn, dep_repo)
        print(f"  {dep_repo} ({edge_types}) -- CI: {format_ci(ci)}")

    print()

    # Task patterns: repos historically missed when this repo changes
    task_hits = find_task_pattern_repos(conn, repo)
    if task_hits:
        print("\U0001f50d Task pattern hits (historically missed repos):")
        for missed, occ, conf in task_hits:
            pct = f"{conf * 100:.1f}%"
            print(f"  {missed} ({occ} occurrences, {pct} confidence)")
        print()

    # File patterns: directory-level co-change
    dir_patterns = conn.execute(
        "SELECT source, target, occurrences, confidence FROM file_patterns "
        "WHERE (source LIKE ? OR target LIKE ?) AND pattern_type = 'dir_pair' "
        "ORDER BY occurrences DESC LIMIT 15",
        (f"{repo}/%", f"{repo}/%"),
    ).fetchall()
    if dir_patterns:
        print("\U0001f4c1 Directory co-change patterns:")
        for p in dir_patterns:
            pct = f"{p['confidence'] * 100:.0f}%"
            src = p["source"]
            tgt = p["target"]
            print(f"  {src} <-> {tgt} ({p['occurrences']} tasks, {pct})")
        print()

    # Cross-repo file patterns
    file_patterns = conn.execute(
        "SELECT source, target, occurrences, confidence FROM file_patterns "
        "WHERE (source LIKE ? OR target LIKE ?) AND pattern_type = 'cross_repo_file' "
        "ORDER BY occurrences DESC LIMIT 15",
        (f"{repo}/%", f"{repo}/%"),
    ).fetchall()
    if file_patterns:
        print("\U0001f4c4 File-level co-change patterns:")
        for p in file_patterns:
            pct = f"{p['confidence'] * 100:.0f}%"
            print(f"  {p['source']} <-> {p['target']} ({p['occurrences']} tasks, {pct})")
        print()

    # Risk summary
    # all_affected = set()  # reserved for future
    risk_map: dict[str, RepoRisk] = {}

    for dep_repo, edges in downstream_by_repo.items():
        risk_map[dep_repo] = RepoRisk(repo=dep_repo, is_downstream=True, edge_types=[e.edge_type for e in edges])

    for missed, _occ, _conf in task_hits:
        if missed not in risk_map:
            risk_map[missed] = RepoRisk(repo=missed)
        risk_map[missed].task_pattern_hit = True

    for p in file_patterns:
        src_repo = p["source"].split("/", 1)[0]
        tgt_repo = p["target"].split("/", 1)[0]
        affected = tgt_repo if src_repo == repo else src_repo
        if affected != repo:
            if affected not in risk_map:
                risk_map[affected] = RepoRisk(repo=affected)
            risk_map[affected].co_changed_hits.append(
                CoChangedFile(
                    target=p["target"] if src_repo == repo else p["source"],
                    pattern_type="cross_repo_file",
                    occurrences=p["occurrences"],
                    confidence=p["confidence"],
                )
            )

    # Enrich with test coverage and CI
    for rname, risk in risk_map.items():
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM test_source_map WHERE repo_name = ?",
            (rname,),
        ).fetchone()
        risk.has_tests = row["cnt"] > 0
        risk.ci_status = get_ci_status(conn, rname)

    if risk_map:
        sorted_risks = sorted(risk_map.values(), key=lambda r: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[r.level])
        print("\u26a0\ufe0f  Risk Summary:")
        for risk in sorted_risks:
            reasons = []
            if risk.is_downstream:
                reasons.append(f"downstream ({', '.join(sorted(set(risk.edge_types)))})")
            if risk.co_changed_hits:
                reasons.append("co-changed files")
            if risk.has_tests:
                reasons.append("has test coverage")
            if risk.task_pattern_hit:
                reasons.append("task pattern")
            if risk.ci_status:
                reasons.append(format_ci(risk.ci_status))

            reason_str = " + ".join(reasons) if reasons else "indirect"
            icon = {"HIGH": "\U0001f534", "MEDIUM": "\U0001f7e1", "LOW": "\u26aa"}[risk.level]
            print(f"  {icon} {risk.level}: {risk.repo} -- {reason_str}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Failure Propagation Engine")
    parser.add_argument("--files", help="Comma-separated list of changed files (repo/path format)")
    parser.add_argument("--repo", help="Repo name to analyze all downstream risks")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if not args.files and not args.repo:
        parser.error("Provide --files or --repo")

    conn = get_db()

    if args.repo:
        if args.json:
            run_repo_mode_json(conn, args.repo)
        else:
            run_repo_mode(conn, args.repo)
    else:
        file_list = [f.strip() for f in args.files.split(",") if f.strip()]
        analyses = [analyze_file(conn, f) for f in file_list]
        risks = compute_risks(conn, analyses)

        if args.json:
            output = {
                "files": [
                    {
                        "path": a.file_path,
                        "repo": a.repo,
                        "tests": [{"file": t.test_file, "verified": t.verified} for t in a.tests],
                        "downstream": [{"repo": d.repo, "edge_type": d.edge_type} for d in a.downstream],
                        "co_changed": [
                            {"target": c.target, "occurrences": c.occurrences, "confidence": c.confidence}
                            for c in a.co_changed
                        ],
                    }
                    for a in analyses
                ],
                "risks": [
                    {
                        "repo": r.repo,
                        "level": r.level,
                        "is_downstream": r.is_downstream,
                        "co_changed": bool(r.co_changed_hits),
                        "has_tests": r.has_tests,
                        "task_pattern": r.task_pattern_hit,
                        "ci_conclusion": r.ci_status.conclusion if r.ci_status else None,
                    }
                    for r in sorted(risks.values(), key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x.level])
                ],
            }
            print(json.dumps(output, indent=2))
        else:
            print_report(analyses, risks)

    conn.close()


if __name__ == "__main__":
    main()
