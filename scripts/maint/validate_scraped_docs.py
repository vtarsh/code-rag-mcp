#!/usr/bin/env python3
"""validate_scraped_docs.py — integrity checks for SCRAPED provider docs.

Closes the gap the existing validators miss: validate_doc_size enforces *max*
line counts on curated docs, validate_provider_paths probes API paths, and
finalize_scrape checks the public façade — but NOTHING verifies that a scraped
provider doc was actually fetched in full. JS-rendered pages routinely come back
truncated, as 404/JS-wall stubs, or empty, and get indexed as if real.

This validator scans a provider-docs tree and flags, per file:
  - stub / 404 / JS-wall / bot-challenge pages saved as content
  - truncated content (unclosed code fence, partial trailing table, mid-cut)
  - empty / near-empty scrapes (little visible text after stripping markup)
  - size outliers and same-size stub clusters within a provider dir
And, per provider dir:
  - crawl-summary failures (failed > 0, errors present, extracted < discovered)

Generic + profile-parameterized; no hardcoded provider names. Exit non-zero in
--check mode when any HIGH-severity issue is found, so it can gate a pipeline
(e.g. before a reindex) the same way the other maint validators do.

Usage:
    python3 scripts/maint/validate_scraped_docs.py                # report all providers
    python3 scripts/maint/validate_scraped_docs.py --check        # exit 1 on HIGH issues
    python3 scripts/maint/validate_scraped_docs.py --json out.json
    python3 scripts/maint/validate_scraped_docs.py --root <dir>   # custom providers root
    python3 scripts/maint/validate_scraped_docs.py --provider paypal
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# --- thresholds (tunable) --------------------------------------------------- #
MIN_VISIBLE_CHARS = 200  # below this = effectively empty
STUB_VISIBLE_CHARS = 1500  # a stub marker in a doc this small = real stub
SIZE_OUTLIER_FRACTION = 0.15  # < 15% of the dir median size = outlier
SIZE_OUTLIER_ABS_BYTES = 800  # ...and below this absolute size
STUB_CLUSTER_MIN = 3  # N+ files at the same small byte size = stub cluster
STUB_CLUSTER_MAX_BYTES = 20_000  # only sizes below this count as a stub cluster

# Strong markers of a page that was NOT really fetched (404 / JS wall / bot gate).
# Matched case-insensitively against visible text.
STUB_MARKERS = (
    # NB: only stub-SPECIFIC phrases — bare "404 not found" / "error 404" are
    # excluded because API reference pages legitimately document a 404 response
    # (e.g. volt "Recalled amendments return 404 Not Found"), which would false-flag.
    "page not found",
    "this page could not be found",
    "we couldn't find that page",
    "we can't find the page",
    "you need to enable javascript",
    "enable javascript to run this app",
    "javascript is required",
    "please enable javascript",
    # NB: "access denied" / "403 forbidden" are intentionally NOT markers — they
    # appear verbatim in legit auth/error docs (e.g. ecp authorization pages),
    # producing false stub_page hits. JS-wall + 404 + bot-gate markers are enough.
    "are you a robot",
    "just a moment...",
    "checking your browser before",
    "attention required! | cloudflare",
    "verify you are human",
)

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.S)
_CODE_FENCE_RE = re.compile(r"^```", re.M)
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_MD_MARKUP_RE = re.compile(r"[#>*_`\[\]()|!-]+")


@dataclass
class Issue:
    path: str  # repo-relative path of the doc (or provider dir for summary issues)
    provider: str
    severity: str  # high | medium | low
    code: str  # machine-readable issue code
    detail: str


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def visible_text_len(text: str) -> int:
    """Chars of human-visible text: drop frontmatter, markdown markup, whitespace."""
    body = strip_frontmatter(text)
    body = _MD_MARKUP_RE.sub(" ", body)
    return len(re.sub(r"\s+", "", body))


def find_stub_markers(text: str) -> list[str]:
    low = text.lower()
    return [m for m in STUB_MARKERS if m in low]


def has_unclosed_code_fence(text: str) -> bool:
    return len(_CODE_FENCE_RE.findall(text)) % 2 == 1


def trailing_partial_table(text: str) -> bool:
    """True only if the doc ends on a table row that is CUT OFF — the last row of
    the TRAILING table block has fewer columns than that block's own header. A doc
    that simply ends with a complete table (a common, legit layout for reference /
    error-code pages) is NOT flagged, and a narrow final table after a wider
    earlier table is judged against its own header (not the first table's)."""
    lines = [ln for ln in text.rstrip().splitlines() if ln.strip()]
    if len(lines) < 3 or lines[-1].count("|") < 2:
        return False
    # Isolate the contiguous trailing block of table-ish lines.
    block: list[str] = []
    for ln in reversed(lines):
        if "|" in ln:
            block.append(ln)
        else:
            break
    block.reverse()
    if len(block) < 3 or not _TABLE_SEP_RE.match(block[1]):  # need header + separator + >=1 row
        return False
    return block[-1].count("|") < block[0].count("|")


def looks_truncated(text: str) -> str | None:
    """Return a reason string if the doc looks cut off, else None.

    Deliberately conservative — only signals with high precision are used. A bare
    "ends mid-sentence" heuristic was dropped: legit markdown routinely ends on a
    heading, list item, or table, so it produced ~400 false positives."""
    if has_unclosed_code_fence(text):
        return "unclosed code fence (odd number of ``` markers)"
    if trailing_partial_table(text):
        return "ends on a cut-off table row (fewer columns than the header)"
    return None


def check_crawl_summary(summary: dict, provider: str) -> list[Issue]:
    issues: list[Issue] = []
    rel = f"{provider}/_crawl_summary.json"
    discovered = summary.get("discovered")
    extracted = summary.get("extracted")
    failed = summary.get("failed") or 0
    errors = summary.get("errors") or []
    if extracted == 0:
        issues.append(Issue(rel, provider, "high", "crawl_extracted_zero", "crawl extracted 0 pages"))
    if failed:
        issues.append(Issue(rel, provider, "high", "crawl_failed", f"{failed} page(s) failed to fetch"))
    if errors:
        issues.append(Issue(rel, provider, "high", "crawl_errors", f"crawl reported errors: {errors!r}"[:200]))
    if isinstance(discovered, int) and isinstance(extracted, int) and 0 < extracted < discovered:
        issues.append(
            Issue(rel, provider, "medium", "crawl_incomplete", f"extracted {extracted} of {discovered} discovered")
        )
    return issues


def is_content_md(path: Path) -> bool:
    """A content doc worth checking — skip generated reports, indexes, JSON sidecars."""
    if path.suffix != ".md":
        return False
    name = path.name
    if name.startswith("_"):  # _index.md, _*_report.md, _crawl_summary etc.
        return False
    if name in ("index.md", "README.md"):  # curated, not raw scrape output
        return False
    return True


# --------------------------------------------------------------------------- #
# Per-file and per-dir checks
# --------------------------------------------------------------------------- #
def check_file(path: Path, provider: str, rel: str) -> list[Issue]:
    issues: list[Issue] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [Issue(rel, provider, "high", "unreadable", f"cannot read: {exc}")]

    visible = visible_text_len(text)
    markers = find_stub_markers(text)

    if markers and visible < STUB_VISIBLE_CHARS:
        issues.append(
            Issue(rel, provider, "high", "stub_page", f"stub/404/JS-wall markers {markers} in a {visible}-char doc")
        )
    elif visible < MIN_VISIBLE_CHARS:
        # MEDIUM, not HIGH: a near-empty doc is often a legit-tiny page (API
        # enum-case, terse reference fragment, version stub) rather than a broken
        # scrape — worth review, but not a definite failure like a 404 stub.
        issues.append(Issue(rel, provider, "medium", "near_empty", f"only {visible} chars of visible text"))

    trunc = looks_truncated(text)
    if trunc:
        issues.append(Issue(rel, provider, "medium", "truncated", trunc))
    return issues


def check_provider_dir(provider_dir: Path, root: Path) -> list[Issue]:
    provider = provider_dir.name
    issues: list[Issue] = []

    summary_path = provider_dir / "_crawl_summary.json"
    if summary_path.exists():
        try:
            issues += check_crawl_summary(json.loads(summary_path.read_text(encoding="utf-8")), provider)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(Issue(f"{provider}/_crawl_summary.json", provider, "medium", "summary_unreadable", str(exc)))

    files = [p for p in sorted(provider_dir.rglob("*.md")) if is_content_md(p)]
    sizes: dict[Path, int] = {p: p.stat().st_size for p in files}
    for p in files:
        issues += check_file(p, provider, str(p.relative_to(root)))

    # size outliers vs the dir median (only meaningful with several files)
    if len(files) >= 4:
        median = statistics.median(sizes.values())
        for p, sz in sizes.items():
            if median and sz < median * SIZE_OUTLIER_FRACTION and sz < SIZE_OUTLIER_ABS_BYTES:
                issues.append(
                    Issue(
                        str(p.relative_to(root)), provider, "low", "size_outlier",
                        f"{sz}B is <{int(SIZE_OUTLIER_FRACTION * 100)}% of dir median {int(median)}B",
                    )
                )

    # same-size stub clusters (≥N small files at an identical byte size = template/404)
    by_size: dict[int, list[Path]] = {}
    for p, sz in sizes.items():
        if sz < STUB_CLUSTER_MAX_BYTES:
            by_size.setdefault(sz, []).append(p)
    for sz, paths in by_size.items():
        if len(paths) >= STUB_CLUSTER_MIN:
            for p in paths:
                issues.append(
                    Issue(
                        str(p.relative_to(root)), provider, "medium", "stub_cluster",
                        f"{len(paths)} files share identical size {sz}B (likely 404/template stub)",
                    )
                )
    return issues


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def default_root() -> Path:
    base = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
    profile = os.getenv("ACTIVE_PROFILE")
    if not profile:
        marker = base / ".active_profile"
        if marker.exists():
            profile = marker.read_text(encoding="utf-8").strip()
    profile = profile or "pay-com"
    return base / "profiles" / profile / "docs" / "providers"


def validate(root: Path, only_provider: str | None = None) -> list[Issue]:
    issues: list[Issue] = []
    for provider_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if only_provider and provider_dir.name != only_provider:
            continue
        issues += check_provider_dir(provider_dir, root)
    return issues


_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate integrity of scraped provider docs.")
    ap.add_argument("--root", type=Path, default=None, help="providers root (default: active profile)")
    ap.add_argument("--provider", default=None, help="restrict to one provider dir")
    ap.add_argument("--check", action="store_true", help="exit 1 if any HIGH-severity issue is found")
    ap.add_argument("--json", type=Path, default=None, help="write full report as JSON to this path")
    ap.add_argument("--quiet", action="store_true", help="only print the summary line")
    args = ap.parse_args(argv)

    root = args.root or default_root()
    if not root.exists():
        print(f"ERROR: providers root not found: {root}", file=sys.stderr)
        return 2

    issues = validate(root, args.provider)
    issues.sort(key=lambda i: (_SEV_ORDER.get(i.severity, 9), i.provider, i.path))
    counts = {s: sum(1 for i in issues if i.severity == s) for s in ("high", "medium", "low")}

    if args.json:
        args.json.write_text(
            json.dumps({"root": str(root), "counts": counts, "issues": [asdict(i) for i in issues]}, indent=2),
            encoding="utf-8",
        )

    if not args.quiet:
        if not issues:
            print(f"OK: no scrape-integrity issues under {root}")
        else:
            cur = None
            for i in issues:
                if i.provider != cur:
                    cur = i.provider
                    print(f"\n[{i.provider}]")
                print(f"  {i.severity.upper():6} {i.code:16} {i.path}\n         {i.detail}")

    print(f"\nscrape-integrity: {counts['high']} high, {counts['medium']} medium, {counts['low']} low")
    if args.check and counts["high"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
