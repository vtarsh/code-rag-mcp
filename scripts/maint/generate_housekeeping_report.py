#!/usr/bin/env python3
"""generate_housekeeping_report.py — implements H10 ruling.

Produces TWO outputs:
1. `profiles/pay-com/docs/_housekeeping_report.md` — top-5 per section + recall-delta-weighted columns + per-PR batching.
2. `/tmp/docs-housekeeping-tasks.md` — auto-task list for Section 3 only (cross-link reciprocity).
   Format: action-binary `#TODO` rows. ≤5/topic, ≤10/week total. Namespaced `housekeeping-*`.
   Close-on-validator-pass: a task is "done" when the action is mirrored in the source files.

Sections:
  1. Duplicate blocks (top-5 by Jaccard) — REPORT-ONLY (format upgrade)
  2. Term divergences (top-5 by impact: reciprocal × age) — REPORT-ONLY
  3. Cross-link reciprocity (≤5 topics, ≤10 tasks total) — AUTO-TASK
  4. Stale files >14d (top-5 by age × cross-link inbound) — REPORT-ONLY

Run: python3.12 scripts/generate_housekeeping_report.py
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path.home() / ".code-rag-mcp/profiles/pay-com/docs"
REPORT = ROOT / "_housekeeping_report.md"
TASKS_OUT = Path("/tmp/docs-housekeeping-tasks.md")

SCAN_DIRS = ["gotchas", "references", "notes/_moc", "dictionary", "flows"]
EXCLUDE_PARTS = {"scraped", "test-credentials", "contract-patterns"}

LINK_RE = re.compile(r"\[([^\]\n]+)\]\((?!https?://|mailto:|#)([^)\s]+\.md)(?:#[^)\s]*)?\)")

WEEKLY_TASK_CAP = 10
PER_TOPIC_CAP = 5


def is_in_scope(p: Path) -> bool:
    if any(part in EXCLUDE_PARTS for part in p.parts):
        return False
    if p.name.startswith("_") and p.name.endswith("_report.md"):
        return False
    if p.name == "_index.md":
        return True
    return p.suffix == ".md"


def iter_docs():
    for sub in SCAN_DIRS:
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*.md"):
            if is_in_scope(p):
                yield p


def parse_outbound_links(md: Path) -> set[str]:
    """Return set of relative target .md paths this file links to."""
    out = set()
    text = md.read_text(encoding="utf-8", errors="replace")
    for m in LINK_RE.finditer(text):
        rel = m.group(2)
        try:
            target = (md.parent / rel).resolve()
            target_rel = str(target.relative_to(ROOT))
            out.add(target_rel)
        except (ValueError, OSError):
            continue
    return out


def compute_reciprocity():
    """For every (A → B) link, check whether (B → A) exists. Returns asymmetric pairs."""
    docs = list(iter_docs())
    rels = {str(d.relative_to(ROOT)): d for d in docs}
    outbound = {rel: parse_outbound_links(p) for rel, p in rels.items()}

    asymmetric = []  # (source, target, topic_hint)
    for src, links in outbound.items():
        for tgt in links:
            if tgt not in outbound:
                continue
            if src not in outbound[tgt]:
                # extract topic from src name (strip suffix, replace dashes)
                topic = Path(src).stem.replace("-MOC", "").replace("grpc-apm-", "").replace("_", "-")
                asymmetric.append((src, tgt, topic))
    return asymmetric


def stale_files(days: int = 14):
    """Return list of (relpath, age_days, inbound_count) for files mtime > days."""
    docs = list(iter_docs())
    rels = {str(d.relative_to(ROOT)): d for d in docs}
    outbound = {rel: parse_outbound_links(p) for rel, p in rels.items()}
    inbound = defaultdict(int)
    for src, links in outbound.items():
        for tgt in links:
            inbound[tgt] += 1
    cutoff = dt.datetime.now().timestamp() - days * 86400
    out = []
    for rel, p in rels.items():
        mtime = p.stat().st_mtime
        if mtime < cutoff:
            age = int((dt.datetime.now().timestamp() - mtime) / 86400)
            out.append((rel, age, inbound.get(rel, 0)))
    return sorted(out, key=lambda x: -(x[1] * (x[2] + 1)))


def render_report(asym: list, stales: list) -> str:
    today = dt.date.today().isoformat()
    lines = []
    lines.append(f"# Weekly Docs Housekeeping Report — {today}")
    lines.append("")
    lines.append("Format: H10 ruling (top-5 per section, per-PR batched, recall-delta-weighted).")
    lines.append("")
    lines.append("## 1. Duplicate blocks (top-5 by Jaccard)")
    lines.append("")
    lines.append("_Detector not yet implemented in this generator. Stub for future analyzer._")
    lines.append("")
    lines.append("## 2. Term divergences (top-5 by impact-weight)")
    lines.append("")
    lines.append("_Detector not yet implemented in this generator. Stub for future analyzer._")
    lines.append("")
    lines.append(f"## 3. Cross-link reciprocity (asymmetric pairs: {len(asym)})")
    lines.append("")
    lines.append("Auto-tasks emitted to `/tmp/docs-housekeeping-tasks.md` (≤5/topic, ≤10/week).")
    lines.append("")
    lines.append("| # | Topic | Source → Target | Action |")
    lines.append("|---|-------|-----------------|--------|")
    for i, (src, tgt, topic) in enumerate(asym[:10], start=1):
        lines.append(f"| {i} | {topic} | `{src}` → `{tgt}` | add `{src}` to `{tgt}#related` |")
    lines.append("")
    lines.append("## 4. Stale files >14d (top-5 by age × inbound-count)")
    lines.append("")
    lines.append("| File | Age (days) | Inbound links | Action |")
    lines.append("|------|------------|---------------|--------|")
    for rel, age, inbound in stales[:5]:
        lines.append(f"| `{rel}` | {age} | {inbound} | review or mark `historical: true` |")
    lines.append("")
    lines.append("## Re-evaluation gate")
    lines.append("")
    lines.append("- 2026-10-30: if Section 3 actioned-rate >50% → recommend-only vindicated for the rest.")
    lines.append("- If still ~0%: schedule 3-way debate (auto-task vs recommend-only vs operator-habit-intervention).")
    return "\n".join(lines) + "\n"


def render_tasks(asym: list) -> str:
    """Emit ≤5 per topic, ≤10 total, namespaced `housekeeping-*`."""
    by_topic = defaultdict(list)
    for src, tgt, topic in asym:
        by_topic[topic].append((src, tgt))
    today = dt.date.today().isoformat()
    lines = [f"# Docs housekeeping auto-tasks — {today}", ""]
    lines.append("Generated by `scripts/generate_housekeeping_report.py`.")
    lines.append("Close-on-validator-pass: when source/target related: lists are mirrored, task auto-resolves.")
    lines.append("")
    total = 0
    for topic, pairs in sorted(by_topic.items()):
        if total >= WEEKLY_TASK_CAP:
            break
        lines.append(f"## {topic}")
        for src, tgt in pairs[:PER_TOPIC_CAP]:
            if total >= WEEKLY_TASK_CAP:
                break
            lines.append(f"- [ ] **housekeeping-reciprocity:{topic}** — add backlink in `{tgt}` to `{src}`")
            total += 1
        lines.append("")
    if total == 0:
        lines.append("_All cross-links reciprocal. Nothing to action this week._")
    lines.append("")
    lines.append(f"Total: {total} task(s). Cap: {WEEKLY_TASK_CAP}/week.")
    return "\n".join(lines) + "\n"


def main() -> int:
    asym = compute_reciprocity()
    stales = stale_files()
    REPORT.write_text(render_report(asym, stales), encoding="utf-8")
    TASKS_OUT.write_text(render_tasks(asym), encoding="utf-8")
    print(f"OK: report -> {REPORT.relative_to(Path.home())}")
    print(f"OK: tasks  -> {TASKS_OUT}")
    print(f"asymmetric pairs: {len(asym)}, stale files: {len(stales)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
