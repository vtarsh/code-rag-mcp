#!/usr/bin/env python3
"""validate_doc_frontmatter.py: ensure new gotchas/references docs carry `type:` frontmatter.

Required: type: must appear in YAML frontmatter (between leading `---` blocks).
Skips: auto-gen `_*_report.md`, `_index.md`, scraped/ subdirs, providers/.
Exits non-zero on violations.
"""

import sys
from pathlib import Path

ROOT = Path.home() / ".code-rag-mcp/profiles/pay-com/docs"

SCAN = ["gotchas", "references", "notes/_moc", "flows", "dictionary"]
EXCLUDE_PARTS = {"scraped", "test-credentials", "contract-patterns"}


def is_exempt(path: Path) -> bool:
    if path.name.startswith("_") and path.name.endswith("_report.md"):
        return True
    if any(part in EXCLUDE_PARTS for part in path.parts):
        return True
    return False


def has_type_field(md: Path) -> bool:
    text = md.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    fm = text[3:end]
    return any(line.strip().startswith("type:") for line in fm.splitlines())


def main() -> int:
    missing = []
    for subdir in SCAN:
        base = ROOT / subdir
        if not base.exists():
            continue
        for md in base.rglob("*.md"):
            if is_exempt(md):
                continue
            if not has_type_field(md):
                missing.append(md)
    if not missing:
        print("OK: all docs have `type:` frontmatter.")
        return 0
    print(f"FAIL: {len(missing)} file(s) missing `type:` frontmatter.")
    for p in missing:
        print(f"  {p.relative_to(ROOT)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
