#!/usr/bin/env python3
"""Merge two independently-labeled v12 candidate files into a consensus set.

Implements the P1b dual-judge pattern (see `feedback_code_rag_judge_bias.md`
and `project_p1b_opus_judge_verdict.md` in memory): Opus is code-biased,
MiniLM is prose-biased, so agreement = confident consensus, disagreement =
flagged for human arbitration.

Merge truth table (per-row, joined on (query, file_path, rank)):

  Opus | MiniLM | Consensus           | Reason
  -----+--------+---------------------+------------------------------------
    +  |   +    | +                   | both_positive
    -  |   -    | -                   | both_negative
    +  |   -    | ?_CONFLICT          | conflict_opus_plus_minilm_minus
    -  |   +    | ?_CONFLICT          | conflict_opus_minus_minilm_plus
    +  |   ?    | +                   | opus_trumps_minilm_ambiguous
    -  |   ?    | -                   | opus_trumps_minilm_ambiguous
    ?  |   +    | +                   | minilm_trumps_opus_ambiguous
    ?  |   -    | -                   | minilm_trumps_opus_ambiguous
    ?  |   ?    | ?                   | both_ambiguous

Outputs:
  1. Consensus jsonl — all 197 rows with merged fields.
  2. Disagreement markdown — conflict rows only (human arbitration focus).

Usage:
  python3.12 scripts/merge_dual_judge_labels.py \
    --opus profiles/pay-com/v12_candidates_regen_labeled_opus.jsonl \
    --minilm profiles/pay-com/v12_candidates_regen_labeled_minilm.jsonl \
    --out profiles/pay-com/v12_candidates_regen_labeled_consensus.jsonl \
    --disagreements profiles/pay-com/v12_candidates_regen_labeled_DISAGREEMENTS.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# ---- Truth table -----------------------------------------------------------

# Map (opus_label, minilm_label) -> (consensus_label, reason).
CONSENSUS_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    ("+", "+"): ("+", "both_positive"),
    ("-", "-"): ("-", "both_negative"),
    ("+", "-"): ("?_CONFLICT", "conflict_opus_plus_minilm_minus"),
    ("-", "+"): ("?_CONFLICT", "conflict_opus_minus_minilm_plus"),
    ("+", "?"): ("+", "opus_trumps_minilm_ambiguous"),
    ("-", "?"): ("-", "opus_trumps_minilm_ambiguous"),
    ("?", "+"): ("+", "minilm_trumps_opus_ambiguous"),
    ("?", "-"): ("-", "minilm_trumps_opus_ambiguous"),
    ("?", "?"): ("?", "both_ambiguous"),
}


def merge_labels(opus_label: str, minilm_label: str) -> tuple[str, str]:
    """Return (consensus_label, consensus_reason) for a row pair."""
    key = (opus_label, minilm_label)
    if key not in CONSENSUS_TABLE:
        raise ValueError(f"unexpected label pair: opus={opus_label!r} minilm={minilm_label!r}")
    return CONSENSUS_TABLE[key]


# ---- I/O helpers -----------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load one JSON object per non-blank line; fail loudly on parse error."""
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON ({e})") from e
    return rows


def _row_key(row: dict[str, Any]) -> tuple[str, str, int]:
    """Canonical join key — (query, file_path, rank).

    (query, file_path) alone is NOT unique because the same file can appear
    at multiple ranks (different chunks of the same file). Rank is unique
    per-query and present in both judge outputs.
    """
    return (row.get("query", ""), row.get("file_path", ""), int(row.get("rank", -1)))


def index_by_key(rows: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Build {key -> row}. Raise on duplicate keys."""
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for r in rows:
        k = _row_key(r)
        if k in out:
            raise ValueError(f"duplicate key in input: {k}")
        out[k] = r
    return out


# ---- Merge core ------------------------------------------------------------


def merge_rows(
    opus_rows: list[dict[str, Any]],
    minilm_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one consensus row per matched pair.

    Fields on each output row:
      - All original fields from the Opus row (query, rank, repo_name, etc.).
      - label_consensus:  "+" | "-" | "?" | "?_CONFLICT"
      - consensus_reason: truth-table reason string
      - label_opus:       Opus judge label
      - label_minilm:     MiniLM judge label
      - note_opus:        note field from Opus row (rationale string)
      - minilm_score:     score from MiniLM row (None if absent)
    The outer `label` field (left over from whichever upstream judge) is
    replaced by `label_consensus` for clarity but also preserved under its
    judge-specific name so reviewers never lose provenance.
    """
    opus_by_key = index_by_key(opus_rows)
    minilm_by_key = index_by_key(minilm_rows)

    opus_keys = set(opus_by_key.keys())
    minilm_keys = set(minilm_by_key.keys())
    missing_in_minilm = opus_keys - minilm_keys
    missing_in_opus = minilm_keys - opus_keys
    if missing_in_minilm or missing_in_opus:
        raise ValueError(
            "judge outputs out of sync: "
            f"{len(missing_in_minilm)} opus rows unmatched, "
            f"{len(missing_in_opus)} minilm rows unmatched"
        )

    # Preserve opus ordering for deterministic output.
    merged: list[dict[str, Any]] = []
    for opus_row in opus_rows:
        k = _row_key(opus_row)
        minilm_row = minilm_by_key[k]

        opus_label = opus_row.get("label", "")
        minilm_label = minilm_row.get("label", "")
        consensus_label, reason = merge_labels(opus_label, minilm_label)

        out = dict(opus_row)  # keep full original schema
        out["label_consensus"] = consensus_label
        out["consensus_reason"] = reason
        out["label_opus"] = opus_label
        out["label_minilm"] = minilm_label
        out["note_opus"] = opus_row.get("note", "")
        out["minilm_score"] = minilm_row.get("minilm_score")
        # Drop the ambiguous top-level `label` / `judge` (they're now encoded
        # in label_opus/label_minilm + consensus columns) to avoid downstream
        # readers accidentally consuming one judge's verdict.
        out.pop("label", None)
        out.pop("judge", None)
        merged.append(out)

    return merged


# ---- Disagreement report ---------------------------------------------------


def _disagreement_hint(row: dict[str, Any]) -> str:
    """One-sentence guess at WHY judges might disagree on this row.

    Heuristic based on:
      - query_tag (doc-intent vs repo-intent/general)
      - category (doc vs code)
      - consensus_reason
    """
    query_tag = row.get("query_tag", "")
    category = row.get("category", "")
    reason = row.get("consensus_reason", "")
    minilm_score = row.get("minilm_score")
    score_str = f"{minilm_score:.3f}" if isinstance(minilm_score, int | float) else "n/a"

    if reason == "conflict_opus_plus_minilm_minus":
        if query_tag == "doc-intent" and category == "code":
            return (
                f"doc-intent query hit a code file — Opus elevated the code "
                f"(reads as answer-bearing), MiniLM rejected it for low prose "
                f"density (score={score_str})."
            )
        if query_tag == "doc-intent" and category in ("doc", "mixed"):
            return (
                f"doc-intent query on a doc file — Opus judged the doc relevant, "
                f"MiniLM's prose similarity fell below 0.15 (score={score_str}); "
                f"likely a keyword-sparse doc or topic drift."
            )
        if category == "code":
            return (
                f"code file where Opus saw semantic relevance but MiniLM's "
                f"prose-leaning scorer found weak lexical/semantic overlap "
                f"(score={score_str})."
            )
        return (
            f"Opus (code-biased but countermeasured) said relevant; MiniLM "
            f"(prose-biased) said no (score={score_str}) — review chunk content."
        )

    if reason == "conflict_opus_minus_minilm_plus":
        if query_tag == "doc-intent" and category in ("doc", "mixed"):
            return (
                f"doc-intent query on a doc file — MiniLM saw high prose "
                f"similarity (score={score_str}), Opus judged it off-topic; "
                f"possible keyword match without true topical coverage."
            )
        if category == "code":
            return (
                f"code file with high prose overlap (score={score_str}) but "
                f"Opus found the code path doesn't actually answer the query."
            )
        return (
            f"MiniLM (prose-biased) said relevant (score={score_str}); Opus "
            f"(code-biased, countermeasured) rejected — may be shallow "
            f"lexical overlap without semantic fit."
        )

    return "no heuristic — inspect chunk manually."


def _classify_intent(row: dict[str, Any]) -> str:
    """Bucket a row by query_tag for summary stats."""
    qt = row.get("query_tag", "")
    if qt == "doc-intent":
        return "doc-intent"
    if qt == "repo-intent":
        return "repo-intent"
    return qt or "general"


def build_disagreement_markdown(merged: list[dict[str, Any]]) -> str:
    """Return markdown text listing all conflict rows with arbitration hints."""
    conflicts = [r for r in merged if r.get("label_consensus") == "?_CONFLICT"]

    intent_counter: Counter[str] = Counter(_classify_intent(r) for r in conflicts)
    category_counter: Counter[str] = Counter(r.get("category", "") for r in conflicts)
    reason_counter: Counter[str] = Counter(r.get("consensus_reason", "") for r in conflicts)

    lines: list[str] = []
    lines.append("# v12 Candidate Labels — Disagreement Report")
    lines.append("")
    lines.append(
        "Rows below are the ones where the Opus and MiniLM judges disagreed. "
        "Per the P1b dual-judge pattern (see memory: "
        "`feedback_code_rag_judge_bias.md`), these are flagged for **human "
        "arbitration** — Opus is code-biased, MiniLM is prose-biased, so "
        "neither vote should be accepted unilaterally."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Total conflicts:** {len(conflicts)} / {len(merged)}")
    lines.append(f"- **By query intent:** {dict(intent_counter)}")
    lines.append(f"- **By category:** {dict(category_counter)}")
    lines.append(f"- **By direction:** {dict(reason_counter)}")
    lines.append("")
    lines.append("### Most important fields to decide each case")
    lines.append("")
    lines.append("- `query_tag` + `category` — surfaces the doc/code axis where judges typically split.")
    lines.append(
        "- `note_opus` — Opus's per-row reasoning (the MiniLM side only has a "
        "numeric score, so Opus's prose is the richer signal)."
    )
    lines.append(
        "- `minilm_score` — raw 0..1 relevance; near the 0.15/0.45 thresholds "
        'usually means "really ambiguous", far above/below means MiniLM is '
        "confidently disagreeing with Opus."
    )
    lines.append(
        "- `chunk_type` + `file_path` — e.g. `env_config` files are "
        "expected-low-prose (MiniLM almost always rejects) while `doc_section` "
        "files are MiniLM's sweet spot."
    )
    lines.append("")

    if not conflicts:
        lines.append("## Conflict rows")
        lines.append("")
        lines.append("_No conflicts found — judges agree on every row._")
        lines.append("")
        return "\n".join(lines) + "\n"

    lines.append("## Conflict rows")
    lines.append("")
    for i, row in enumerate(conflicts, 1):
        query = row.get("query", "")
        query_tag = row.get("query_tag", "")
        rank = row.get("rank", "?")
        repo = row.get("repo_name", "")
        fp = row.get("file_path", "")
        chunk_type = row.get("chunk_type", "")
        category = row.get("category", "")
        note_opus = row.get("note_opus", "") or "_(no rationale provided)_"
        minilm_score = row.get("minilm_score")
        score_str = f"{minilm_score:.4f}" if isinstance(minilm_score, int | float) else "n/a"
        direction = row.get("consensus_reason", "")

        lines.append(f"### {i}. `{query}`")
        lines.append("")
        lines.append(f"- **query_tag:** {query_tag}")
        lines.append(f"- **rank:** {rank}")
        lines.append(f"- **chunk_file:** `{repo}/{fp}`")
        lines.append(f"- **chunk_type / category:** {chunk_type} / {category}")
        lines.append(f"- **direction:** `{direction}`")
        lines.append(f"- **Opus** (`{row.get('label_opus', '')}`) note: {note_opus}")
        lines.append(f"- **MiniLM** (`{row.get('label_minilm', '')}`) score: {score_str}")
        lines.append(f"- **Arbitration hint:** {_disagreement_hint(row)}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---- CLI -------------------------------------------------------------------


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def summarize(merged: list[dict[str, Any]]) -> dict[str, Any]:
    dist: Counter[str] = Counter(r["label_consensus"] for r in merged)
    reasons: Counter[str] = Counter(r["consensus_reason"] for r in merged)
    conflicts = [r for r in merged if r["label_consensus"] == "?_CONFLICT"]
    conflict_intents: Counter[str] = Counter(_classify_intent(r) for r in conflicts)
    conflict_categories: Counter[str] = Counter(r.get("category", "") for r in conflicts)
    return {
        "total": len(merged),
        "label_distribution": dict(dist),
        "reason_distribution": dict(reasons),
        "conflicts_by_intent": dict(conflict_intents),
        "conflicts_by_category": dict(conflict_categories),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--opus", type=Path, required=True, help="Opus-labeled jsonl")
    p.add_argument("--minilm", type=Path, required=True, help="MiniLM-labeled jsonl")
    p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output consensus jsonl (all rows)",
    )
    p.add_argument(
        "--disagreements",
        type=Path,
        required=True,
        help="Output disagreement markdown (conflicts only)",
    )
    args = p.parse_args()

    for path in (args.opus, args.minilm):
        if not path.exists():
            print(f"ERROR: input not found: {path}", file=sys.stderr)
            return 1

    opus_rows = load_jsonl(args.opus)
    minilm_rows = load_jsonl(args.minilm)
    print(f"loaded {len(opus_rows)} opus rows from {args.opus}", flush=True)
    print(f"loaded {len(minilm_rows)} minilm rows from {args.minilm}", flush=True)

    if len(opus_rows) != len(minilm_rows):
        print(
            f"ERROR: row count mismatch — opus={len(opus_rows)} vs minilm={len(minilm_rows)}",
            file=sys.stderr,
        )
        return 1

    merged = merge_rows(opus_rows, minilm_rows)

    write_jsonl(args.out, merged)
    args.disagreements.parent.mkdir(parents=True, exist_ok=True)
    args.disagreements.write_text(build_disagreement_markdown(merged), encoding="utf-8")

    stats = summarize(merged)
    print("\n=== SUMMARY ===", flush=True)
    print(f"output:         {args.out}", flush=True)
    print(f"disagreements:  {args.disagreements}", flush=True)
    print(f"rows:           {stats['total']}", flush=True)
    print(f"label_distribution:    {stats['label_distribution']}", flush=True)
    print(f"reason_distribution:   {stats['reason_distribution']}", flush=True)
    print(f"conflicts_by_intent:   {stats['conflicts_by_intent']}", flush=True)
    print(f"conflicts_by_category: {stats['conflicts_by_category']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
