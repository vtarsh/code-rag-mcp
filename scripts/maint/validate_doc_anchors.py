#!/usr/bin/env python3
"""validate_doc_anchors.py: validator for pay-com docs.
Exits non-zero on violations. Designed for pre-commit / CI integration.

Catches H3: [text](file.md#anchor) markdown links where the heading slug
no longer exists in target file. Uses GitHub-style slug rules.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

DOCS_ROOT = Path.home() / ".code-rag-mcp" / "profiles" / "pay-com" / "docs"
SKIP_DIR_PARTS = {"scraped", "providers"}

LINK_RE = re.compile(r"\[([^\]\n]+)\]\((?!https?://|mailto:|#)([^)\s]+\.md)#([^)\s]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SLUG_DROP_RE = re.compile(r"[^a-z0-9\s\-_]")


def slugify(heading: str) -> str:
    s = heading.lower().strip()
    s = SLUG_DROP_RE.sub("", s)
    # Per spec: collapse multi-`-`, but GitHub's actual algorithm preserves them.
    # Spec wins — keep collapse behaviour.
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def iter_md_files(root: Path):
    for p in root.rglob("*.md"):
        if any(part in SKIP_DIR_PARTS for part in p.parts):
            continue
        yield p


def collect_anchors(target: Path) -> set[str]:
    anchors: set[str] = set()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return anchors
    in_fence = False
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if not m:
            continue
        slug = slugify(m.group(2))
        if not slug:
            continue
        n = counts.get(slug, 0)
        anchors.add(slug if n == 0 else f"{slug}-{n}")
        counts[slug] = n + 1
    return anchors


def main() -> int:
    total = ok = broken_file = broken_anchor = 0
    broken: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    for md in iter_md_files(DOCS_ROOT):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in LINK_RE.finditer(line):
                rel, anchor = m.group(2), m.group(3)
                total += 1
                target = (md.parent / rel).resolve()
                if not target.is_file():
                    broken_file += 1
                    if len(broken) < 30:
                        broken.append(f"{md}:{lineno} -> {rel}#{anchor} -> BROKEN-FILE")
                    continue
                if target not in anchor_cache:
                    anchor_cache[target] = collect_anchors(target)
                if anchor not in anchor_cache[target]:
                    broken_anchor += 1
                    if len(broken) < 30:
                        broken.append(f"{md}:{lineno} -> {rel}#{anchor} -> BROKEN-ANCHOR")
                else:
                    ok += 1
    print(f"md anchor links checked: total={total} ok={ok} broken_file={broken_file} broken_anchor={broken_anchor}")
    if broken:
        print("\nFirst broken refs:")
        for b in broken:
            print(f"  {b}")
    return 1 if (broken_file + broken_anchor) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
