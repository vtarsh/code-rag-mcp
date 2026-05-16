#!/usr/bin/env python3
"""validate_doc_file_line_refs.py: validator for pay-com docs.
Exits non-zero on violations. Designed for pre-commit / CI integration.

Catches H13: file:line references in MD docs that no longer exist in cloned
source repos under ~/.code-rag-mcp/raw/.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

DOCS_ROOT = Path.home() / ".code-rag-mcp" / "profiles" / "pay-com" / "docs"
RAW_ROOT = Path.home() / ".code-rag-mcp" / "raw"
SKIP_DIR_PARTS = {"scraped", "providers"}
# Skip auto-generated housekeeping reports (refs are code-rag-mcp internals, not in raw/).
SKIP_FILENAMES = {"_housekeeping_report.md", "_deprecated_scan_report.md", "_scripts_scan_report.md"}

# Match `repo/path/to/file.ext:N` or `:N-M` or GitHub `#LN` / `#LN-LM`.
# repo and path segments allow alnum, _, -, .  Filename must have an extension.
REF_RE = re.compile(
    r"(?<![\w/])"
    r"([A-Za-z0-9][A-Za-z0-9_.-]*"             # repo dir
    r"(?:/[A-Za-z0-9_.-]+)+"                    # /relative/path
    r"\.[A-Za-z0-9]+)"                           # .ext
    r"(?::(\d+)(?:-(\d+))?|#L(\d+)(?:-L?(\d+))?)"
)


def iter_md_files(root: Path):
    for p in root.rglob("*.md"):
        if any(part in SKIP_DIR_PARTS for part in p.parts):
            continue
        if p.name in SKIP_FILENAMES:
            continue
        yield p


def main() -> int:
    total = ok = broken_file = broken_line = unverifiable = 0
    broken: list[str] = []
    warns: list[str] = []
    for md in iter_md_files(DOCS_ROOT):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in REF_RE.finditer(line):
                ref_path = m.group(1)
                start = m.group(2) or m.group(4)
                end = m.group(3) or m.group(5)
                if not start:
                    continue
                total += 1
                start_n = int(start)
                end_n = int(end) if end else start_n
                parts = ref_path.split("/", 1)
                if len(parts) < 2:
                    continue
                repo, rel = parts[0], parts[1]
                # Repo not cloned locally → unverifiable, NOT broken (may be pre-main, in-progress, or external).
                if not (RAW_ROOT / repo).is_dir():
                    unverifiable += 1
                    if len(warns) < 30:
                        warns.append(f"{md}:{lineno} -> {ref_path}:{start_n}{'-'+str(end_n) if end else ''} -> UNVERIFIABLE (repo {repo} not in raw/, may be pre-main)")
                    continue
                target = RAW_ROOT / repo / rel
                if not target.is_file():
                    broken_file += 1
                    if len(broken) < 30:
                        broken.append(f"{md}:{lineno} -> {ref_path}:{start_n}{'-'+str(end_n) if end else ''} -> BROKEN-FILE (no {target})")
                    continue
                try:
                    line_count = sum(1 for _ in target.open("rb"))
                except OSError:
                    broken_file += 1
                    continue
                if end_n > line_count:
                    broken_line += 1
                    if len(broken) < 30:
                        broken.append(f"{md}:{lineno} -> {ref_path}:{start_n}{'-'+str(end_n) if end else ''} -> BROKEN-LINE (file has {line_count} lines)")
                else:
                    ok += 1
    print(f"file:line refs checked: total={total} ok={ok} broken_file={broken_file} broken_line={broken_line} unverifiable={unverifiable}")
    if broken:
        print("\nFirst broken refs (FAIL):")
        for b in broken:
            print(f"  {b}")
    if warns:
        print("\nFirst unverifiable refs (WARN, repos not cloned):")
        for w in warns:
            print(f"  {w}")
    # FAIL only on real drift (broken file/line). Unverifiable is informational.
    return 1 if (broken_file + broken_line) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
