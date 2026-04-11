#!/usr/bin/env python3
"""Flag curated docs whose linked repos have newer commits than the doc itself.

Contract: each curated doc opts in by declaring its source repos in YAML
frontmatter:

    ---
    name: ...
    related_repos:
      - grpc-providers-nuvei
      - workflow-foo
    ---

Docs without `related_repos` (or with empty list) are skipped — they are
considered cross-cutting or not tied to code. No fuzzy matching, no age
fallback: explicit opt-in, deterministic result.

Reads commit timestamps from `profiles/{PROFILE}/generated/repo_facts.json`
(produced by gen_repo_facts.py earlier in the pipeline) to avoid spawning
551 git subprocesses.

Writes: logs/doc_staleness.json
"""
import json
import os
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(os.environ.get("CODE_RAG_HOME", os.path.expanduser("~/.code-rag")))


def _resolve_profile() -> str:
    env = os.environ.get("ACTIVE_PROFILE", "").strip()
    if env:
        return env
    f = BASE_DIR / ".active_profile"
    if f.exists():
        try:
            val = f.read_text(encoding="utf-8", errors="replace").strip()
            if val:
                return val
        except Exception:
            pass
    return "example"


PROFILE = _resolve_profile()
DOCS_DIR = BASE_DIR / "profiles" / PROFILE / "docs"
FACTS_JSON = BASE_DIR / "profiles" / PROFILE / "generated" / "repo_facts.json"
LOG_DIR = BASE_DIR / "logs"
OUT = LOG_DIR / "doc_staleness.json"

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
RELATED_REPOS_INLINE_RE = re.compile(r"^related_repos\s*:\s*\[(.*?)\]", re.M)
RELATED_REPOS_BLOCK_HEAD_RE = re.compile(r"^(\s*)related_repos\s*:\s*$", re.M)
LIST_ITEM_RE = re.compile(r"^(\s*)-\s+(.+?)\s*$")


def load_repo_commit_epochs() -> dict:
    """Map repo_name → last_commit epoch, from gen_repo_facts.py output."""
    if not FACTS_JSON.exists():
        return {}
    try:
        data = json.loads(FACTS_JSON.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    result = {}
    for entry in data:
        lc = entry.get("last_commit") or {}
        epoch = lc.get("epoch")
        if isinstance(epoch, int) and epoch > 0:
            result[entry["name"]] = epoch
    return result


def _clean_item(s: str) -> str:
    # Drop YAML inline comments and surrounding quotes/whitespace.
    s = s.split("#", 1)[0].strip()
    return s.strip("'\"")


def parse_related_repos(md_path: Path) -> list[str]:
    """Extract `related_repos` list from YAML frontmatter.

    Supports two forms:
        related_repos: [foo, bar]
        related_repos:
          - foo
          - bar

    Block form is parsed with strict indentation: list items must be indented
    deeper than the `related_repos:` key; stops at the first line with lesser
    or equal indent (another key or dedent). This avoids over-capturing
    adjacent list keys like `notes: [ - something ]`.
    """
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    fm_match = FRONTMATTER_RE.match(text)
    if not fm_match:
        return []
    fm = fm_match.group(1)

    inline = RELATED_REPOS_INLINE_RE.search(fm)
    if inline:
        return [_clean_item(s) for s in inline.group(1).split(",") if _clean_item(s)]

    block_head = RELATED_REPOS_BLOCK_HEAD_RE.search(fm)
    if not block_head:
        return []
    key_indent = len(block_head.group(1))
    lines = fm[block_head.end() :].lstrip("\n").splitlines()
    items: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        m = LIST_ITEM_RE.match(line)
        if not m or len(m.group(1)) <= key_indent:
            break
        val = _clean_item(m.group(2))
        if val:
            items.append(val)
    return items


def main() -> int:
    if not DOCS_DIR.is_dir():
        print(f"no docs dir at {DOCS_DIR}, skipping", file=sys.stderr)
        return 0
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    commit_epochs = load_repo_commit_epochs()
    if not commit_epochs:
        print(f"⚠️ no repo facts at {FACTS_JSON}, run gen_repo_facts.py first", file=sys.stderr)

    report = []
    docs_with_frontmatter = 0
    for md in sorted(DOCS_DIR.rglob("*.md")):
        if md.is_symlink():
            continue
        related = parse_related_repos(md)
        if not related:
            continue
        docs_with_frontmatter += 1
        doc_mtime = md.stat().st_mtime
        newer = []
        unknown_repos = []
        for repo in related:
            epoch = commit_epochs.get(repo)
            if epoch is None:
                unknown_repos.append(repo)
                continue
            if epoch > doc_mtime:
                newer.append({
                    "repo": repo,
                    "commit_age_days": round((time.time() - epoch) / 86400.0, 1),
                    "doc_age_days": round((time.time() - doc_mtime) / 86400.0, 1),
                })
        if newer or unknown_repos:
            report.append({
                "doc": str(md.relative_to(DOCS_DIR)),
                "mtime": time.strftime("%Y-%m-%d", time.gmtime(doc_mtime)),
                "newer_repo_commits": newer,
                "unknown_repos": unknown_repos,
            })

    report.sort(key=lambda r: -len(r["newer_repo_commits"]))
    OUT.write_text(json.dumps({
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": PROFILE,
        "docs_with_frontmatter": docs_with_frontmatter,
        "stale_candidates": len(report),
        "candidates": report,
    }, indent=2))
    flagged = sum(1 for r in report if r["newer_repo_commits"])
    print(
        f"✓ {docs_with_frontmatter} opted-in docs, "
        f"{flagged} stale, {len(report) - flagged} with unknown repos → logs/doc_staleness.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
