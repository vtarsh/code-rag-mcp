#!/usr/bin/env python3
"""Stratified sampler for ``bench_v2.yaml``.

Reads ``profiles/pay-com/real_queries/sampled.jsonl`` (400 real queries from
MCP traffic) and produces a 200-row YAML skeleton ready for human labeling
(``answerable`` / ``gt_files`` / ``gt_symbols`` left empty).

Strata (see ``profiles/pay-com/bench/BENCH_REDESIGN_PROPOSAL.md``):

  Intent    : 112 code  / 62 concept / 24 doc / 2 repo
  Length    : 40 short  / 90 medium  / 50 long / 20 verylong
  Provider  : 160 provider-tagged (top-10) + 40 provider-free

Iterative proportional fit — on each draw a score is computed from how short
each stratum is; highest-score candidate is picked. If a target cannot be met
because the source is too thin (e.g. ``sampled.jsonl`` holds 0 ``verylong``
queries), we fall back to best-fit. Observed shortfalls are printed in the
``--verbose`` stats.

Usage::

    python3 scripts/sample_bench_v2.py \
        --seed 42 --n 200 \
        --out profiles/pay-com/bench/bench_v2.yaml
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Intent classifier (rule-based, proposal §2)
# ---------------------------------------------------------------------------

# Mirrors ``src/search/hybrid.py:_DOC_QUERY_RE`` — kept in sync manually to
# avoid importing the full src stack (sampler must run without LanceDB).
_DOC_QUERY_RE = re.compile(
    r"\b("
    r"test|tests|spec|specs|"
    r"docs?|documentation|readme|guide|guides|tutorial|"
    r"checklist|framework|matrix|severity|sandbox|overview|reference|rules"
    r")\b",
    re.IGNORECASE,
)

# Extra doc hints from the proposal: .md/README/CLAUDE/rules/instructions.
_DOC_HINT_RE = re.compile(
    r"(\.md\b|README|CLAUDE|\brules\b|\binstructions\b)",
    re.IGNORECASE,
)

# Code-shaped tokens: camelCase, snake_case, or .js/.ts/.py/.proto/.go etc.
_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")
_EXT_RE = re.compile(r"\S+\.(js|ts|tsx|jsx|py|proto|go)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Length buckets
# ---------------------------------------------------------------------------

_LEN_BUCKETS = ("short", "medium", "long", "verylong")


def length_bucket(query: str) -> str:
    """Return one of short / medium / long / verylong per token count."""
    n = len(query.split())
    if n <= 3:
        return "short"
    if n <= 7:
        return "medium"
    if n <= 15:
        return "long"
    return "verylong"


# ---------------------------------------------------------------------------
# Provider classifier (top-10 from real traffic per proposal §2)
# ---------------------------------------------------------------------------

TOP_PROVIDERS = (
    "payper",
    "nuvei",
    "interac",
    "trustly",
    "paynearme",
    "worldpay",
    "epx",
    "cashapp",
    "wise",
    "fortumo",
)


def detect_provider(query: str) -> Optional[str]:
    """Return first matching top-10 provider or None."""
    q = query.lower()
    for p in TOP_PROVIDERS:
        # Word boundary to avoid "wisely"/"paynearmex" false hits.
        if re.search(rf"\b{re.escape(p)}\b", q):
            return p
    return None


# ---------------------------------------------------------------------------
# Intent classifier (doc > repo > code > concept priority)
# ---------------------------------------------------------------------------


def classify_intent(query: str, repo_names: set[str]) -> str:
    """Return ``code`` | ``concept`` | ``doc`` | ``repo``.

    Priority (proposal §2):
      1. ``doc`` if query matches _DOC_QUERY_RE or _DOC_HINT_RE.
      2. ``repo`` if query is exactly one known repo name (≤3 tokens).
      3. ``code`` if query has camelCase / snake_case / known file-ext tokens.
      4. ``concept`` otherwise.
    """
    if _DOC_HINT_RE.search(query) or _DOC_QUERY_RE.search(query):
        return "doc"

    tokens = query.split()
    if 1 <= len(tokens) <= 3:
        joined = query.strip().lower()
        # Exact repo name match — handles both single-token ("payper") and
        # hyphenated multi-word ("grpc-apm-trustly") names since tokens split
        # on whitespace only.
        if joined in repo_names:
            return "repo"

    if _CAMEL_RE.search(query) or _SNAKE_RE.search(query) or _EXT_RE.search(query):
        return "code"

    return "concept"


# ---------------------------------------------------------------------------
# Repo list — loaded from knowledge.db when available, empty set otherwise.
# ---------------------------------------------------------------------------


def load_repo_names(db_path: Path) -> set[str]:
    """Return set of repo names from the knowledge DB, or empty set on any error.

    Sampler remains usable (fewer ``repo``-intent hits) when the DB is absent
    — useful for CI environments where the index is not built.
    """
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT name FROM repos").fetchall()
        conn.close()
        return {r[0].strip().lower() for r in rows if r and r[0]}
    except sqlite3.Error:
        return set()


# ---------------------------------------------------------------------------
# Stratified sampling — iterative proportional fit (proposal §2)
# ---------------------------------------------------------------------------

TARGET_INTENT = {"code": 112, "concept": 62, "doc": 24, "repo": 2}
TARGET_LENGTH = {"short": 40, "medium": 90, "long": 50, "verylong": 20}
TARGET_PROVIDER_TAGGED = 160
TARGET_PROVIDER_FREE = 40


def _score_candidate(
    cand: dict,
    current: Counter,
    targets_intent: dict,
    targets_length: dict,
    target_tagged: int,
    target_free: int,
    sampled_tagged: int,
    sampled_free: int,
) -> float:
    """Higher score = more helpful for hitting the quotas.

    Sum of three per-stratum normalized deficits. Candidates with a saturated
    stratum (current >= target) score 0 on that axis.
    """
    intent_key = f"intent:{cand['intent']}"
    length_key = f"length:{cand['length_bucket']}"

    def _deficit(counter_key: str, target: int) -> float:
        if target <= 0:
            return 0.0
        gap = target - current[counter_key]
        if gap <= 0:
            return 0.0
        return gap / target

    s = _deficit(intent_key, targets_intent[cand["intent"]])
    s += _deficit(length_key, targets_length[cand["length_bucket"]])

    # Provider stratum = tagged-vs-free balance.
    if cand["provider"] is not None:
        tagged_gap = target_tagged - sampled_tagged
        s += max(tagged_gap, 0) / target_tagged if target_tagged else 0
    else:
        free_gap = target_free - sampled_free
        s += max(free_gap, 0) / target_free if target_free else 0

    return s


def stratified_sample(
    candidates: list[dict],
    *,
    n: int,
    seed: int,
) -> tuple[list[dict], dict]:
    """IPF-style sampler. Returns (selected, stats)."""
    rng = random.Random(seed)
    pool = candidates.copy()
    rng.shuffle(pool)

    selected: list[dict] = []
    current: Counter[str] = Counter()
    sampled_tagged = 0
    sampled_free = 0

    # Drive targets by N so caller can ask for <200 in tests. We scale the
    # published targets proportionally rather than hard-coding 200.
    scale = n / 200.0
    t_intent = {k: round(v * scale) for k, v in TARGET_INTENT.items()}
    t_length = {k: round(v * scale) for k, v in TARGET_LENGTH.items()}
    t_tagged = round(TARGET_PROVIDER_TAGGED * scale)
    t_free = round(TARGET_PROVIDER_FREE * scale)

    # Greedy: repeatedly pick the candidate that most reduces current deficit.
    # We iterate until we hit N OR the pool is exhausted.
    while len(selected) < n and pool:
        best_idx = -1
        best_score = -1.0
        for idx, cand in enumerate(pool):
            sc = _score_candidate(
                cand,
                current,
                t_intent,
                t_length,
                t_tagged,
                t_free,
                sampled_tagged,
                sampled_free,
            )
            if sc > best_score:
                best_score = sc
                best_idx = idx
        if best_idx < 0:
            break
        if best_score <= 0:
            # All strata saturated — fill remaining slots with the leftover
            # shuffled pool (still deterministic because pool was seeded).
            remaining = n - len(selected)
            selected.extend(pool[:remaining])
            for c in pool[:remaining]:
                current[f"intent:{c['intent']}"] += 1
                current[f"length:{c['length_bucket']}"] += 1
                if c["provider"] is not None:
                    sampled_tagged += 1
                else:
                    sampled_free += 1
            break

        cand = pool.pop(best_idx)
        selected.append(cand)
        current[f"intent:{cand['intent']}"] += 1
        current[f"length:{cand['length_bucket']}"] += 1
        if cand["provider"] is not None:
            sampled_tagged += 1
        else:
            sampled_free += 1

    stats = {
        "requested": n,
        "selected": len(selected),
        "source_pool": len(candidates),
        "intent": {k: current[f"intent:{k}"] for k in TARGET_INTENT},
        "length": {k: current[f"length:{k}"] for k in TARGET_LENGTH},
        "provider_tagged": sampled_tagged,
        "provider_free": sampled_free,
        "provider_breakdown": Counter(
            c["provider"] for c in selected if c["provider"]
        ),
        "target_intent": t_intent,
        "target_length": t_length,
        "target_provider_tagged": t_tagged,
        "target_provider_free": t_free,
    }
    return selected, stats


# ---------------------------------------------------------------------------
# Input / output
# ---------------------------------------------------------------------------


def load_candidates(path: Path, repo_names: set[str]) -> list[dict]:
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            q = (rec.get("query") or "").strip()
            if not q:
                continue
            out.append(
                {
                    "lineno": lineno,
                    "query": q,
                    "sampled_ts": rec.get("sampled_ts"),
                    "intent": classify_intent(q, repo_names),
                    "length_bucket": length_bucket(q),
                    "provider": detect_provider(q),
                }
            )
    return out


def _escape_yaml_string(s: str) -> str:
    """Quote per YAML 1.2 double-quoted string rules (minimal — enough here)."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_yaml(path: Path, sampled: list[dict], source: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("version: 2")
    lines.append(f"sampled_from: {source}")
    lines.append("labeled_by: null")
    lines.append("labeled_on: null")
    lines.append("")
    lines.append("queries:")
    for i, row in enumerate(sampled, 1):
        qid = f"BV2-{i:04d}"
        lines.append(f"  - id: {qid}")
        lines.append(f"    query: {_escape_yaml_string(row['query'])}")
        lines.append("    answerable: null")
        lines.append(f"    intent: {row['intent']}")
        lines.append(f"    length_bucket: {row['length_bucket']}")
        prov = row["provider"] if row["provider"] else "null"
        lines.append(f"    provider: {prov}")
        lines.append("    gt_files: []")
        lines.append("    gt_symbols: []")
        notes = f"from sampled.jsonl line {row['lineno']}"
        lines.append(f"    notes: {_escape_yaml_string(notes)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_stats(stats: dict) -> None:
    print("=" * 60)
    print(f"sample_bench_v2 — requested={stats['requested']}, selected={stats['selected']}, pool={stats['source_pool']}")
    print("=" * 60)
    print("\nIntent (target / selected):")
    for k in TARGET_INTENT:
        print(f"  {k:<8} {stats['target_intent'][k]:>4} / {stats['intent'][k]:>4}")
    print("\nLength (target / selected):")
    for k in TARGET_LENGTH:
        print(f"  {k:<9} {stats['target_length'][k]:>4} / {stats['length'][k]:>4}")
    print("\nProvider:")
    print(f"  tagged  target={stats['target_provider_tagged']} selected={stats['provider_tagged']}")
    print(f"  free    target={stats['target_provider_free']} selected={stats['provider_free']}")
    print("\nTop-10 provider breakdown:")
    for p in TOP_PROVIDERS:
        c = stats["provider_breakdown"].get(p, 0)
        print(f"  {p:<12} {c}")
    print("=" * 60)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--input",
        type=Path,
        default=Path("profiles/pay-com/real_queries/sampled.jsonl"),
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("profiles/pay-com/bench/bench_v2.yaml"),
    )
    p.add_argument(
        "--db",
        type=Path,
        default=Path("db/knowledge.db"),
        help="Path to knowledge.db (for repo-name intent lookup). If missing, repo-intent is skipped.",
    )
    p.add_argument("-n", "--n", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    if not args.input.exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1

    repos = load_repo_names(args.db)
    if not repos:
        print(f"WARN: no repos from {args.db} — repo-intent will be empty", file=sys.stderr)

    candidates = load_candidates(args.input, repos)
    if not candidates:
        print(f"ERROR: no candidates in {args.input}", file=sys.stderr)
        return 1

    selected, stats = stratified_sample(candidates, n=args.n, seed=args.seed)
    write_yaml(args.out, selected, args.input)

    _print_stats(stats)
    print(f"\noutput: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
