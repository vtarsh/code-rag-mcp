#!/usr/bin/env python3
"""validate_doc_related_repos.py: validator for pay-com docs.
Exits non-zero on violations. Designed for pre-commit / CI integration.

Catches H8: `related_repos:` frontmatter entries that no longer exist as
cloned repos under ~/.code-rag-mcp/raw/.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

DOCS_ROOT = Path.home() / ".code-rag-mcp" / "profiles" / "pay-com" / "docs"
RAW_ROOT = Path.home() / ".code-rag-mcp" / "raw"
SUBDIRS = ("gotchas", "references")
SKIP_DIR_PARTS = {"scraped"}

try:
    import yaml  # type: ignore
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def extract_frontmatter(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    return text[3:end].lstrip("\n")


def parse_related_repos(fm: str) -> list[str]:
    if HAVE_YAML:
        try:
            data = yaml.safe_load(fm) or {}
            v = data.get("related_repos")
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []
        except Exception:
            pass
    repos: list[str] = []
    in_block = False
    for line in fm.splitlines():
        if not in_block:
            if re.match(r"^related_repos\s*:", line):
                in_block = True
                inline = line.split(":", 1)[1].strip()
                if inline.startswith("[") and inline.endswith("]"):
                    return [x.strip().strip("'\"") for x in inline[1:-1].split(",") if x.strip()]
            continue
        m = re.match(r"^\s*-\s*(.+?)\s*$", line)
        if m:
            repos.append(m.group(1).strip().strip("'\""))
        elif re.match(r"^\S", line):
            break
    return repos


def iter_md_files() -> list[Path]:
    return [p for sub in SUBDIRS for p in (DOCS_ROOT / sub).rglob("*.md")
            if not any(part in SKIP_DIR_PARTS for part in p.parts)]


def main() -> int:
    total = ok = unresolved = 0
    broken: list[str] = []
    for md in iter_md_files():
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = extract_frontmatter(text)
        if not fm:
            continue
        repos = parse_related_repos(fm)
        for repo in repos:
            total += 1
            if (RAW_ROOT / repo).is_dir():
                ok += 1
            else:
                unresolved += 1
                if len(broken) < 30:
                    broken.append(f"{md} -> related_repos: {repo} -> NOT FOUND under {RAW_ROOT}")
    print(f"related_repos entries checked: total={total} ok={ok} unresolved={unresolved} (yaml={'on' if HAVE_YAML else 'off'})")
    if broken:
        print("\nFirst unresolved:")
        for b in broken:
            print(f"  {b}")
    return 1 if unresolved > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
