#!/usr/bin/env python3
"""validate_doc_size.py: enforce per-tree line-count limits on docs.

Limits:
  gotchas/    ≤ 200 lines
  references/ ≤ 200 lines (test-credentials/, contract-patterns/, scraped/ exempt)
  notes/_moc/ ≤ 100 lines

Skips auto-generated `_*_report.md`. Exits non-zero on violations.
"""

import sys
from pathlib import Path

ROOT = Path.home() / ".code-rag-mcp/profiles/pay-com/docs"

LIMITS = [
    ("gotchas", 200, []),
    ("references", 200, ["test-credentials", "contract-patterns", "scraped"]),
    ("notes/_moc", 100, []),
]


def is_exempt(path: Path) -> bool:
    name = path.name
    if name.startswith("_") and name.endswith("_report.md"):
        return True
    return False


def main() -> int:
    violations = []
    for subdir, limit, exclude in LIMITS:
        base = ROOT / subdir
        if not base.exists():
            continue
        for md in base.rglob("*.md"):
            if any(part in exclude for part in md.relative_to(base).parts):
                continue
            if is_exempt(md):
                continue
            lines = sum(1 for _ in md.open(encoding="utf-8"))
            if lines > limit:
                violations.append((md, lines, limit))
    if not violations:
        print("OK: all docs within size limits.")
        return 0
    print(f"FAIL: {len(violations)} file(s) exceed size limit.")
    for path, lines, limit in violations:
        print(f"  {path.relative_to(ROOT)} — {lines} lines (limit {limit})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
