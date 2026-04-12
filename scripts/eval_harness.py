"""Blind evaluation harness for analyze_task.

For each evaluated task:
  1. Extracts raw Jira description from task_history (no hint injection)
  2. Runs analyze_task with exclude_task_id=<task> (prevents data leakage)
  3. Scores predicted repos against ground truth from implementation_traces/
  4. Reports recall, precision, top-K accuracy, churn warning coverage

Usage:
  python3 scripts/eval_harness.py
  python3 scripts/eval_harness.py --tasks PI-5,PI-60 --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path.home() / ".code-rag-mcp"
os.environ.setdefault("CODE_RAG_HOME", str(BASE))
os.environ.setdefault("ACTIVE_PROFILE", "pay-com")
PROFILE = BASE / "profiles" / "pay-com"
DB = BASE / "db" / "knowledge.db"
TRACES_DIR = PROFILE / "data" / "implementation_traces"

# Default eval set: tasks with collected ground truth
DEFAULT_TASKS = ["PI-5", "PI-14", "PI-39", "PI-40", "PI-60"]


def load_task_description(conn: sqlite3.Connection, task_id: str) -> tuple[str, str]:
    """Return (summary, description) from task_history."""
    row = conn.execute(
        "SELECT summary, description FROM task_history WHERE ticket_id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return "", ""
    return row[0] or "", row[1] or ""


def load_ground_truth(task_id: str) -> tuple[set[str], set[str], set[str]]:
    """Load (repos_changed, high_churn_files, all_files) from implementation_traces.

    Rules:
      - If top-level `repos_changed` list is non-empty, use it as AUTHORITATIVE
        repo set (ignore repo names from `repos` dict/list — they may contain
        noise from issue linking or cross-repo scanning).
      - Otherwise, extract repos from `repos` dict/list, skipping entries with
        no actual data (empty prs + commits + files_changed + branches).
      - File-level data (for churn) is always extracted from `repos` regardless.

    Handles multiple schemas:
      - v1: {repos_changed: [...], repos: {name: {...}}}
      - v2: {repos: [{repo: name, files_changed: [...]}]}
      - v3: {repos: [name1, name2, ...]}
    """
    path = TRACES_DIR / f"{task_id.lower()}.json"
    if not path.exists():
        return set(), set(), set()
    with open(path) as f:
        data = json.load(f)

    repos_changed_list = data.get("repos_changed") or []
    use_authoritative = bool(repos_changed_list)
    repos: set[str] = set(repos_changed_list) if use_authoritative else set()
    all_files: set[str] = set()
    file_touch_count: dict[str, int] = {}

    def _has_data(entry: dict) -> bool:
        return bool(
            entry.get("prs")
            or entry.get("commits")
            or entry.get("files_changed")
            or entry.get("branches")
            or entry.get("branch")
        )

    repos_data = data.get("repos", {})
    if isinstance(repos_data, dict):
        for name, rd in repos_data.items():
            if not use_authoritative and _has_data(rd):
                repos.add(name)
            for pr in (rd.get("prs", []) or []):
                for f in (pr.get("files", []) or []):
                    p = f.get("path") if isinstance(f, dict) else f
                    if not p or p in ("package.json", "package-lock.json", ".gitignore"):
                        continue
                    all_files.add(p)
                    file_touch_count[p] = file_touch_count.get(p, 0) + 1
    elif isinstance(repos_data, list):
        for item in repos_data:
            if isinstance(item, str):
                if not use_authoritative:
                    repos.add(item)
            elif isinstance(item, dict):
                if not use_authoritative and "repo" in item and _has_data(item):
                    repos.add(item["repo"])
                # Aggregate files from either files_changed or prs[].files
                file_sources = []
                for pr in (item.get("prs", []) or []):
                    file_sources.extend(pr.get("files", []) or [])
                file_sources.extend(item.get("files_changed", []) or [])
                for f in file_sources:
                    if isinstance(f, str):
                        p = f
                    elif isinstance(f, dict):
                        p = f.get("path") or f.get("filename") or ""
                    else:
                        continue
                    if not p or p in ("package.json", "package-lock.json", ".gitignore"):
                        continue
                    all_files.add(p)
                    file_touch_count[p] = file_touch_count.get(p, 0) + 1

    # Prefer top-level file_churn if available (cleaner signal)
    file_churn = data.get("file_churn") or {}
    if isinstance(file_churn, dict) and file_churn:
        for repo_name, repo_churn in file_churn.items():
            if not isinstance(repo_churn, dict):
                continue
            for path, info in repo_churn.items():
                if path in ("package.json", "package-lock.json", ".gitignore"):
                    continue
                all_files.add(path)
                commits = info.get("commits_touched", 0) if isinstance(info, dict) else 0
                file_touch_count[path] = max(file_touch_count.get(path, 0), commits)

    high_churn = {p for p, c in file_touch_count.items() if c >= 3}
    return repos, high_churn, all_files


def strip_hints(description: str) -> str:
    """Remove PR URLs, credentials, and obvious internal refs that would leak info."""
    if not description:
        return ""
    # Strip PR URLs
    text = re.sub(r"https?://github\.com/[^\s)]+", "", description)
    # Strip task IDs
    text = re.sub(r"\b(PI|CORE|BO|HS)-\d+\b", "", text)
    # Strip JWT-like tokens (long base64 strings)
    text = re.sub(r"ey[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}", "[token]", text)
    # Keep first 800 chars (Jira descriptions can be huge with creds/docs)
    return text[:800].strip()


def extract_predicted_repos(output: str) -> tuple[list[str], list[str], list[str]]:
    """Parse analyze_task output to extract predicted repos by tier.

    Returns (core_repos, common_repos, conditional_repos) from Recipe section.
    """
    core: list[str] = []
    common: list[str] = []
    conditional: list[str] = []
    current_tier: list[str] | None = None

    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("**Core:**"):
            current_tier = core
        elif line.startswith("**Common:**"):
            current_tier = common
        elif line.startswith("**Conditional:**"):
            current_tier = conditional
        elif line.startswith("###") or line.startswith("##"):
            current_tier = None
        elif current_tier is not None and line.startswith("- **"):
            # Format: "- **repo-name** (3/4) — description"
            m = re.match(r"-\s+\*\*([^*]+)\*\*", line)
            if m:
                repo = m.group(1).strip()
                # Handle "repoA OR repoB" by including both
                for r in repo.split(" OR "):
                    current_tier.append(r.strip())
    return core, common, conditional


def extract_high_repos(output: str) -> list[str]:
    """Extract repos listed under the 'High' or similar confidence section of analyze output."""
    repos: list[str] = []
    in_section = False
    for line in output.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### High") or stripped.startswith("## High"):
            in_section = True
            continue
        if in_section and (stripped.startswith("## ") or stripped.startswith("### ") or stripped.startswith("---")):
            in_section = False
        elif in_section:
            m = re.match(r"[-*]\s+`?([a-z0-9][a-z0-9-]+)`?", stripped)
            if m:
                repos.append(m.group(1))
    return repos


def extract_churn_warnings(output: str) -> set[str]:
    """Extract files flagged as HIGH/MEDIUM churn risk. Returns basenames for matching."""
    files: set[str] = set()
    in_churn = False
    for line in output.split("\n"):
        line_s = line.strip()
        if "Churn Warning" in line_s:
            in_churn = True
            continue
        if in_churn and line_s.startswith("##"):
            in_churn = False
        elif in_churn:
            m = re.search(r"`([^`]+\.\w+)`", line_s)
            if m:
                path = m.group(1)
                # Store both full path and basename for flexible matching
                files.add(path)
                files.add(path.split("/")[-1])
    return files


def _normalize_file_set(files: set[str]) -> set[str]:
    """Return set of basenames (last path segment) for loose matching."""
    return {f.split("/")[-1] for f in files}


def run_analyze(description: str, provider: str, exclude_task_id: str, final_rank: bool = False) -> str:
    """Run analyze_task and return the output markdown."""
    sys.path.insert(0, str(BASE))
    from src.tools.analyze import analyze_task_tool
    return analyze_task_tool(
        description, provider=provider, exclude_task_id=exclude_task_id, final_rank=final_rank,
    )


def extract_final_ranked_repos(output: str) -> tuple[list[str], list[str]]:
    """Parse the 'Final LLM Ranking' section. Returns (core, related)."""
    core: list[str] = []
    related: list[str] = []
    in_section = False
    current: list[str] | None = None

    for raw in output.split("\n"):
        line = raw.strip()
        if line.startswith("## Final LLM Ranking"):
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("## ") or line.startswith("---"):
            break
        if line.startswith("**Core"):
            current = core
            continue
        if line.startswith("**Related"):
            current = related
            continue
        if line.startswith("**Dropped"):
            current = None
            continue
        if current is not None:
            m = re.match(r"-\s+\*\*([a-z0-9][a-z0-9-]+)\*\*", line)
            if m:
                current.append(m.group(1))
                continue
            m = re.match(r"-\s+([a-z0-9][a-z0-9-]+)\s", line)
            if m:
                current.append(m.group(1))
    return core, related


def extract_dropped_repos(output: str) -> list[str]:
    """Parse repos listed in the 'Dropped' section of the Final LLM Ranking."""
    dropped: list[str] = []
    in_dropped = False
    for raw in output.split("\n"):
        line = raw.strip()
        if line.startswith("**Dropped"):
            in_dropped = True
            continue
        if not in_dropped:
            continue
        if line.startswith("## ") or line.startswith("---") or line.startswith("**"):
            break
        # Format: "- ~~repo-name~~ [TIER] — reason"
        m = re.match(r"-\s+~~([a-z0-9][a-z0-9-]+)~~", line)
        if m:
            dropped.append(m.group(1))
    return dropped


def detect_provider(task_id: str, ground_truth_repos: set[str]) -> str:
    """Detect provider from ground truth repos (for PI tasks)."""
    for repo in ground_truth_repos:
        if repo.startswith("grpc-apm-"):
            return repo.replace("grpc-apm-", "")
    for repo in ground_truth_repos:
        if repo.startswith("grpc-providers-") and repo not in (
            "grpc-providers-credentials", "grpc-providers-features", "grpc-providers-proto"
        ):
            return repo.replace("grpc-providers-", "")
    return ""


def evaluate_task(conn: sqlite3.Connection, task_id: str, *, verbose: bool = False, final_rank: bool = False) -> dict:
    """Evaluate one task. Returns metrics dict."""
    summary, description = load_task_description(conn, task_id)
    if not description and not summary:
        return {"task_id": task_id, "error": "no summary/description in task_history"}

    gt_repos, gt_high_churn, gt_all_files = load_ground_truth(task_id)
    if not gt_repos:
        return {"task_id": task_id, "error": "no ground truth in implementation_traces/"}

    provider = detect_provider(task_id, gt_repos)

    # Strip hints from description to simulate raw Jira input
    clean_desc = strip_hints(f"{summary}\n{description}")

    # Run analyze with task excluded from task_history
    output = run_analyze(clean_desc, provider, exclude_task_id=task_id, final_rank=final_rank)

    # Extract predictions
    churn_files = extract_churn_warnings(output)

    # Template expansion for recipe repos
    def expand(repos):
        result = []
        for r in repos:
            if "{" in r and "}" in r:
                continue  # skip unexpanded
            if provider and "{provider}" in r:
                r = r.replace("{provider}", provider)
            result.append(r)
        return result

    if final_rank:
        # Prefer the LLM ranker's output (core=top 5, related=next).
        ranked_core, ranked_related = extract_final_ranked_repos(output)
        if not ranked_core and not ranked_related:
            # Ranker failed (API error, parse failure). Fall back to recipe tiers
            # so we don't report spurious zero-recall for a Gemini outage.
            print(f"  (final-rank fallback to recipe tiers)")
            core, common, conditional = extract_predicted_repos(output)
            core_e = set(expand(core))
            common_e = set(expand(common))
            cond_e = set(expand(conditional))
            all_predicted = core_e | common_e | cond_e
            ordered_source = expand(core) + expand(common) + expand(conditional)
        else:
            core_e = set(expand(ranked_core))
            common_e = set(expand(ranked_related))
            cond_e = set()
            all_predicted = core_e | common_e
            # Ordering: core first, then related, preserving ranker order.
            ordered_source = expand(ranked_core) + expand(ranked_related)
    else:
        core, common, conditional = extract_predicted_repos(output)
        core_e = set(expand(core))
        common_e = set(expand(common))
        cond_e = set(expand(conditional))
        all_predicted = core_e | common_e | cond_e
        ordered_source = expand(core) + expand(common) + expand(conditional)

    # Metrics
    def pct(a, b):
        return round(100 * len(a) / len(b), 1) if b else 0.0

    recall_all = pct(gt_repos & all_predicted, gt_repos)
    recall_core_common = pct(gt_repos & (core_e | common_e), gt_repos)
    precision = pct(gt_repos & all_predicted, all_predicted) if all_predicted else 0.0

    # Top-K accuracy: ordered predictions (core → common → conditional, recipe order preserved)
    ordered_predictions: list[str] = []
    seen: set[str] = set()
    for r in ordered_source:
        if r not in seen:
            ordered_predictions.append(r)
            seen.add(r)
    top5 = set(ordered_predictions[:5])
    top10 = set(ordered_predictions[:10])
    top5_recall = pct(gt_repos & top5, gt_repos)
    top10_recall = pct(gt_repos & top10, gt_repos)

    # Churn warning coverage — match by basename (paths differ between recipe and traces)
    churn_warned_basenames = _normalize_file_set(churn_files)
    gt_churn_basenames = _normalize_file_set(gt_high_churn)
    churn_hit = len(churn_warned_basenames & gt_churn_basenames)
    churn_total_gt = len(gt_churn_basenames)

    # Dropped-list quality (only meaningful with final_rank).
    dropped_repos: list[str] = []
    false_drops: list[str] = []
    if final_rank:
        dropped_repos = extract_dropped_repos(output)
        false_drops = sorted(gt_repos & set(dropped_repos))

    metrics = {
        "task_id": task_id,
        "provider": provider,
        "gt_repos": len(gt_repos),
        "gt_high_churn_files": len(gt_high_churn),
        "predicted_core": len(core_e),
        "predicted_common": len(common_e),
        "predicted_conditional": len(cond_e),
        "predicted_total": len(all_predicted),
        "recall_all": recall_all,
        "recall_core_common": recall_core_common,
        "precision": precision,
        "top5_recall": top5_recall,
        "top10_recall": top10_recall,
        "churn_warned": len(churn_files),
        "churn_hit": churn_hit,
        "churn_total_gt": churn_total_gt,
        "missed_repos": sorted(gt_repos - all_predicted),
        "extra_repos": sorted(all_predicted - gt_repos),
        "dropped_repos": dropped_repos,
        "false_drops": false_drops,
    }

    if verbose:
        print(f"\n--- {task_id} ({provider}, {len(gt_repos)} ground truth repos) ---")
        print(f"  Predicted: {len(all_predicted)} (core={len(core_e)}, common={len(common_e)}, cond={len(cond_e)})")
        print(f"  Recall (all):          {recall_all}%")
        print(f"  Recall (core+common):  {recall_core_common}%")
        print(f"  Precision:             {precision}%")
        print(f"  Top-5 recall (high conf): {top5_recall}%")
        print(f"  Top-10 recall:         {top10_recall}%")
        print(f"  Churn hits:            {churn_hit}/{churn_total_gt} (warned {len(churn_files)} files)")
        if metrics["missed_repos"]:
            print(f"  MISSED: {', '.join(metrics['missed_repos'][:5])}")
        if metrics["extra_repos"]:
            print(f"  Extra:  {', '.join(metrics['extra_repos'][:5])}")
        if final_rank and metrics["false_drops"]:
            print(f"  FALSE DROPS (GT in dropped list): {', '.join(metrics['false_drops'])}")

    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default=",".join(DEFAULT_TASKS))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--final-rank", action="store_true",
                        help="Use the final LLM ranker's output (precision mode)")
    args = parser.parse_args()

    tasks = [t.strip().upper() for t in args.tasks.split(",")]

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    _tasks_db = DB.parent / "tasks.db"
    if _tasks_db.exists():
        conn.execute(f"ATTACH DATABASE '{_tasks_db}' AS tasks")

    mode = "FINAL-RANK" if args.final_rank else "RECIPE"
    print(f"=== Eval mode: {mode} ===")

    results = []
    for tid in tasks:
        m = evaluate_task(conn, tid, verbose=args.verbose, final_rank=args.final_rank)
        results.append(m)
        if "error" in m:
            print(f"{tid}: ERROR {m['error']}")

    conn.close()

    # Aggregate
    valid = [r for r in results if "error" not in r]
    if valid:
        print("\n=== AGGREGATE ===")
        def avg(k):
            return round(sum(r[k] for r in valid) / len(valid), 1)
        print(f"  Tasks evaluated:       {len(valid)}")
        print(f"  Avg recall (all):      {avg('recall_all')}%")
        print(f"  Avg recall (core+com): {avg('recall_core_common')}%")
        print(f"  Avg precision:         {avg('precision')}%")
        print(f"  Avg top-5 recall:      {avg('top5_recall')}%")
        print(f"  Avg top-10 recall:     {avg('top10_recall')}%")
        avg_hit = avg("churn_hit")
        avg_gt = avg("churn_total_gt")
        print(f"  Avg churn hits:        {avg_hit}/{avg_gt}")

        # Per-task summary table
        print("\n=== PER-TASK TABLE ===")
        print(f"  {'Task':11} {'GT':3} {'Pred':4} {'Rec':6} {'Rec++':6} {'Prec':6} {'T5':6} {'T10':6} {'FD':3}")
        for r in valid:
            fd = len(r.get("false_drops", []))
            print(
                f"  {r['task_id']:11} {r['gt_repos']:3d} {r['predicted_total']:4d} "
                f"{r['recall_all']:5.1f}% {r['recall_core_common']:5.1f}% "
                f"{r['precision']:5.1f}% {r['top5_recall']:5.1f}% {r['top10_recall']:5.1f}% {fd:3d}"
            )

        # False-drop / false-keep analysis (only in final-rank mode)
        if args.final_rank:
            all_false_drops: list[str] = []
            all_kept: set[str] = set()
            all_gt: set[str] = set()
            total_gt_count = 0
            total_kept_count = 0
            # Stable-drop histogram: how many tasks dropped each repo
            from collections import Counter
            drop_counter: Counter[str] = Counter()
            for r in valid:
                all_false_drops.extend(r["false_drops"])
                for repo in r["dropped_repos"]:
                    drop_counter[repo] += 1
                total_gt_count += r["gt_repos"]
                total_kept_count += r["predicted_total"]
                # Note: we can reconstruct kept/gt only per-task for rate calc

            # Rates
            total_false_drop = sum(len(r["false_drops"]) for r in valid)
            total_false_keep = sum(len(r["extra_repos"]) for r in valid)
            false_drop_rate = (total_false_drop / total_gt_count * 100) if total_gt_count else 0
            false_keep_rate = (total_false_keep / total_kept_count * 100) if total_kept_count else 0

            print("\n=== DROPPED-LIST QUALITY ===")
            print(f"  False drops (GT in dropped):  {total_false_drop}/{total_gt_count} = {false_drop_rate:.1f}%  (target <5%)")
            print(f"  False keeps (kept not in GT): {total_false_keep}/{total_kept_count} = {false_keep_rate:.1f}%  (target <50%)")

            # Stable drops — repos dropped in >= 50% of tasks
            threshold = max(2, len(valid) // 2)
            stable_drops = [(r, n) for r, n in drop_counter.most_common() if n >= threshold]
            print(f"\n=== STABLE DROPS (≥{threshold}/{len(valid)} tasks) ===")
            for repo, n in stable_drops[:30]:
                print(f"  {n:2d}×  {repo}")
            if len(stable_drops) > 30:
                print(f"  ... {len(stable_drops) - 30} more")


if __name__ == "__main__":
    main()
