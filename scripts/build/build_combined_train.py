#!/usr/bin/env python3
"""Build a combined cross-encoder training JSONL from code + docs sources.

The reranker (CrossEncoder) trainer expects rows of the shape::

    {"query": str, "doc": str, "label": 0|1, "source": "code"|"docs"}

We assemble that file from two pre-existing sources:

  * Source A (code): ``profiles/pay-com/finetune_data_v8/train.jsonl``
    Per-query candidate lists ``{query, docs: [...], labels: [1.0, 0.0, ...]}``.
    Each row contains multiple candidate docs with float labels (1.0 = positive,
    0.0 = negative). We expand each row to one CE row per (query, doc).

  * Source B (docs): ``/tmp/r1_cosent_triplets_v3.jsonl`` (or ``--docs-src``).
    Triplets ``{anchor, positive, negative}`` mined for the docs-tower CoSENT
    fine-tune. Each triplet expands into 2 CE rows: (anchor, positive, 1) and
    (anchor, negative, 0).

The combined output is then **balanced** by random down-sampling of the
over-represented label until the positives:negatives ratio sits within ``±10%``.
This keeps the BCE-loss CrossEncoder from collapsing onto the majority class.

Output path defaults to::

    profiles/pay-com/finetune_data_combined_v1/train.jsonl

Usage::

    python scripts/build_combined_train.py
    python scripts/build_combined_train.py --code-src=PATH --docs-src=PATH \
                                           --out=PATH --seed=42

The script is import-safe (no side effects at import) and exposes
``build_combined`` so unit tests can invoke it without spawning a subprocess.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Final

# ----- defaults --------------------------------------------------------------

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_CODE_SRC: Final = REPO_ROOT / "profiles" / "pay-com" / "finetune_data_v8" / "train.jsonl"
DEFAULT_DOCS_SRC: Final = Path("/tmp/r1_cosent_triplets_v3.jsonl")
DEFAULT_OUT: Final = REPO_ROOT / "profiles" / "pay-com" / "finetune_data_combined_v1" / "train.jsonl"

# Pos/neg ratio target. We accept anything within `target * (1 ± TOLERANCE)`
# of 1.0 — i.e. positives can be at most 10% more or less than negatives.
BALANCE_TOLERANCE: Final = 0.10


# ----- source loaders --------------------------------------------------------


def _iter_jsonl(path: Path):
    """Yield JSON-decoded rows from `path`, skipping blank/whitespace lines.

    We intentionally don't validate schema here — the per-source parser below
    is responsible for raising a descriptive error if the row shape is wrong.
    """
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def load_code_pairs(path: Path) -> list[dict]:
    """Load Source A (``finetune_data_v8/train.jsonl``).

    Schema in v8: ``{query: str, docs: list[str], labels: list[float]}``.
    Each row carries multiple candidate docs with float labels in {0.0, 1.0}.
    We flatten to one CE row per (query, doc) and tag ``source="code"``.

    Float labels are coerced to 0/1 ints via ``round`` so a stray 0.5 from a
    future label-smoothing pass would fail loud (ValueError) instead of
    silently leaking into the binary CE training set.
    """
    rows: list[dict] = []
    for r in _iter_jsonl(path):
        if "query" not in r or "docs" not in r or "labels" not in r:
            raise ValueError(f"code-src row missing keys (need query/docs/labels): {r!r}")
        docs = r["docs"]
        labels = r["labels"]
        if len(docs) != len(labels):
            raise ValueError(f"code-src row docs/labels length mismatch: {len(docs)} vs {len(labels)}")
        for doc, label in zip(docs, labels, strict=False):
            # Accept int 0/1 OR float 0.0/1.0 (the v8 fixture stores floats).
            # Reject anything else loud — a 0.5 sneaking through would
            # silently leak label-smoothed data into the binary CE set.
            label_float = float(label)
            if label_float not in (0.0, 1.0):
                raise ValueError(f"code-src label not 0/1: {label!r}")
            rows.append(
                {
                    "query": r["query"],
                    "doc": doc,
                    "label": int(label_float),
                    "source": "code",
                }
            )
    return rows


def load_docs_triplets(path: Path) -> list[dict]:
    """Load Source B (``r1_cosent_triplets_v3.jsonl``).

    Schema: ``{anchor|query: str, positive: str, negative: str}``. The R1 v3
    triplet builder writes ``query`` while the original CoSENT spec uses
    ``anchor`` — accept either so we don't break on a future re-mining run.
    Each triplet expands to TWO CE rows so the cross-encoder sees both
    polarities for the same query (the canonical pos/neg construction for
    BCE training).
    """
    rows: list[dict] = []
    for r in _iter_jsonl(path):
        anchor = r.get("anchor") if "anchor" in r else r.get("query")
        if anchor is None or "positive" not in r or "negative" not in r:
            raise ValueError(f"docs-src row missing keys (need anchor|query + positive + negative): {r!r}")
        rows.append(
            {
                "query": anchor,
                "doc": r["positive"],
                "label": 1,
                "source": "docs",
            }
        )
        rows.append(
            {
                "query": anchor,
                "doc": r["negative"],
                "label": 0,
                "source": "docs",
            }
        )
    return rows


# ----- balancing -------------------------------------------------------------


def _ratio_within_tolerance(n_pos: int, n_neg: int, tol: float) -> bool:
    """Return True if positives:negatives stays within ``±tol`` of 1.0.

    Pure helper — kept separate so the unit test can pin the exact threshold
    semantics without re-deriving the arithmetic.
    """
    if n_pos == 0 or n_neg == 0:
        return False
    larger = max(n_pos, n_neg)
    smaller = min(n_pos, n_neg)
    # `larger / smaller - 1` is the over-representation factor. tol=0.10 means
    # the larger class can be at most 10% bigger than the smaller one.
    return (larger / smaller) - 1.0 <= tol


def balance_labels(
    rows: list[dict],
    *,
    seed: int = 42,
    tolerance: float = BALANCE_TOLERANCE,
) -> list[dict]:
    """Down-sample the over-represented label until pos:neg is within `tolerance`.

    Deterministic (`seed`-driven) and stable: input order is preserved among
    surviving rows so a `git diff` of the output is debuggable. We never
    up-sample — repeating training pairs would inflate apparent set size while
    teaching the model nothing new.
    """
    pos = [r for r in rows if r["label"] == 1]
    neg = [r for r in rows if r["label"] == 0]
    if _ratio_within_tolerance(len(pos), len(neg), tolerance):
        return rows

    rng = random.Random(seed)
    if len(pos) > len(neg):
        # Allow the pos class to be up to (1+tolerance) * len(neg) members.
        cap = int(len(neg) * (1.0 + tolerance))
        keep = set(rng.sample(range(len(pos)), k=min(cap, len(pos))))
        pos = [p for i, p in enumerate(pos) if i in keep]
    else:
        cap = int(len(pos) * (1.0 + tolerance))
        keep = set(rng.sample(range(len(neg)), k=min(cap, len(neg))))
        neg = [n for i, n in enumerate(neg) if i in keep]

    # Interleave rather than concatenating so a downstream `head -100`
    # doesn't see a 100% positive (or 100% negative) block.
    out: list[dict] = []
    i = j = 0
    while i < len(pos) or j < len(neg):
        if i < len(pos):
            out.append(pos[i])
            i += 1
        if j < len(neg):
            out.append(neg[j])
            j += 1
    return out


# ----- top-level builder -----------------------------------------------------


def build_combined(
    *,
    code_src: Path,
    docs_src: Path,
    out_path: Path,
    seed: int = 42,
    tolerance: float = BALANCE_TOLERANCE,
) -> dict:
    """Build the combined CE train file. Returns a stats dict for logging.

    Idempotent: writes to ``out_path`` (creating its parent dir) and overwrites
    any existing file. Returns counts so the CLI can print a sanity log and the
    unit test can assert the contract without re-parsing the output.
    """
    if not code_src.is_file():
        raise FileNotFoundError(f"code-src not found: {code_src}")
    if not docs_src.is_file():
        raise FileNotFoundError(f"docs-src not found: {docs_src}")

    code_rows = load_code_pairs(code_src)
    docs_rows = load_docs_triplets(docs_src)
    combined = code_rows + docs_rows
    balanced = balance_labels(combined, seed=seed, tolerance=tolerance)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in balanced:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_pos = sum(1 for r in balanced if r["label"] == 1)
    n_neg = sum(1 for r in balanced if r["label"] == 0)
    n_code = sum(1 for r in balanced if r["source"] == "code")
    n_docs = sum(1 for r in balanced if r["source"] == "docs")
    return {
        "out_path": str(out_path),
        "total_rows": len(balanced),
        "code_rows": n_code,
        "docs_rows": n_docs,
        "positives": n_pos,
        "negatives": n_neg,
        "pre_balance_total": len(combined),
    }


# ----- CLI -------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--code-src", type=Path, default=DEFAULT_CODE_SRC)
    p.add_argument("--docs-src", type=Path, default=DEFAULT_DOCS_SRC)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--tolerance",
        type=float,
        default=BALANCE_TOLERANCE,
        help="Max allowed pos:neg ratio drift (default 0.10 = ±10%%)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    stats = build_combined(
        code_src=args.code_src,
        docs_src=args.docs_src,
        out_path=args.out,
        seed=args.seed,
        tolerance=args.tolerance,
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
