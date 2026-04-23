#!/usr/bin/env python3
"""Build v12 holdout sets (Jira tickets + runtime queries).

Produces:
    profiles/pay-com/finetune_data_v12/holdout_jira_50.jsonl
    profiles/pay-com/finetune_data_v12/holdout_runtime_20.jsonl

Design (per agent-a5f14bdd / 2026-04-22):
    * Proportional stratification on the 909-ticket GT pool:
        CORE=33.9%, BO=57.6%, PI=4.8%, HS=3.6%
        -> 50-row target: CORE=17, BO=29, PI=2, HS=2
    * Filter: >= 5 files_changed AND >= 2 repos_changed.
    * Exclude PI-48, PI-54 (current v8 test PI -> baseline bias).
    * Seeded random (seed=42) for reproducibility.
    * Option 3: v12 train = 909 - holdout_50 = 859 tickets.
      We do NOT try to exclude the v8 split (every ticket is in v8).

Runtime holdout:
    * Source logs/tool_calls.jsonl (source=mcp, tool=search, 30<=len<=80).
    * Dedupe by query string.
    * Lexical-Jaccard filter: drop any query with >=0.3 token overlap
      vs any existing benchmark/v12_candidate query.
    * expected_repos_hint: fuzzy match against any ticket's
      summary/description -> use that ticket's repos_changed.
    * Target: 15-20 queries (take top-20 by length, chronological tie-break).
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: pyyaml required. pip install pyyaml", file=sys.stderr)
    raise

TASKS_DB = ROOT / "db" / "tasks.db"
LOG_PATH = ROOT / "logs" / "tool_calls.jsonl"
BENCH_YAML = ROOT / "profiles" / "pay-com" / "benchmarks.yaml"
V12_CANDS = ROOT / "profiles" / "pay-com" / "v12_candidates.jsonl"
OUT_DIR = ROOT / "profiles" / "pay-com" / "finetune_data_v12"

# Per-prefix targets (50 total). See HOLDOUT_NOTES.md.
TARGETS = {"CORE": 17, "BO": 29, "PI": 2, "HS": 2}
PI_EXCLUDE = {"PI-48", "PI-54"}  # already in v8 test -> bias baseline
MIN_FILES = 5
MIN_REPOS = 2
SEED = 42

# Runtime filters
RT_MIN_LEN = 30
RT_MAX_LEN = 80
RT_JACCARD_THRESHOLD = 0.3
RT_TARGET = 20

TOKEN_RE = re.compile(r"\w{3,}")


# --------------------------------------------------------------------------- #
#                                utilities                                    #
# --------------------------------------------------------------------------- #


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# --------------------------------------------------------------------------- #
#                           Jira ticket holdout                               #
# --------------------------------------------------------------------------- #


def _load_raw_tickets(db: Path) -> list[dict]:
    """Every ticket with a non-empty files_changed (the 909-pool)."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    rows = conn.execute(
        "SELECT ticket_id, summary, description, repos_changed, files_changed "
        "FROM task_history "
        "WHERE files_changed IS NOT NULL AND files_changed != '' AND files_changed != '[]'"
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        try:
            files = json.loads(r["files_changed"] or "[]")
            repos = json.loads(r["repos_changed"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(files, list) or not isinstance(repos, list):
            continue
        desc = (r["description"] or "").strip()
        out.append(
            {
                "ticket_id": r["ticket_id"],
                "summary": (r["summary"] or "").strip(),
                "description": desc,
                "repos_changed": list(repos),
                "files_changed": list(files),
                "prefix": r["ticket_id"].split("-", 1)[0],
                "query_length_chars": len(desc),
            }
        )
    return out


def load_pool(db: Path) -> tuple[list[dict], list[dict]]:
    """Return (sampling_pool, full_909_pool).

    sampling_pool (from the 909) is further filtered to
      files_changed >= MIN_FILES AND repos_changed >= MIN_REPOS.

    full_909_pool is used for leakage comparison since every 909-ticket
    that isn't held out will land in the v12 train set.
    """
    full = _load_raw_tickets(db)
    sampling = [
        t for t in full if len(t["files_changed"]) >= MIN_FILES and len(t["repos_changed"]) >= MIN_REPOS
    ]
    return sampling, full


def sample_jira_holdout(
    sampling_pool: list[dict],
    full_pool: list[dict],
    *,
    seed: int = SEED,
    leakage_threshold: float = 0.5,
) -> list[dict]:
    """Proportionally-stratified sample of 50 tickets.

    Candidates are drawn from `sampling_pool` (the >=5 files & >=2 repos
    subset). Leakage is checked against `full_pool` (the 909-pool), because
    every 909-ticket not in the holdout lands in v12 train.

    If a candidate has a query-Jaccard >= threshold against ANY member of
    `full_pool` outside the running holdout, we skip it and pull the next
    candidate from the same prefix bucket. Deterministic given `seed`.
    """
    token_re = re.compile(r"\w{3,}")

    def toks(t: dict) -> set[str]:
        return set(token_re.findall(f"{t['summary']} {t['description']}".lower()))

    # Precompute token sets for the full pool to keep the check O(N*50).
    full_tokens: dict[str, set[str]] = {t["ticket_id"]: toks(t) for t in full_pool}

    rng = random.Random(seed)
    by_prefix: dict[str, list[dict]] = {}
    for t in sampling_pool:
        by_prefix.setdefault(t["prefix"], []).append(t)

    picked: list[dict] = []
    skipped: list[tuple[str, str, float]] = []  # (cand_id, leaky_peer_id, jaccard)
    for prefix, target in TARGETS.items():
        bucket = [t for t in by_prefix.get(prefix, []) if t["ticket_id"] not in PI_EXCLUDE]
        if len(bucket) < target:
            raise RuntimeError(
                f"Pool too small for prefix {prefix}: have {len(bucket)}, need {target}."
            )
        rng.shuffle(bucket)
        chosen: list[dict] = []
        cursor = 0
        while len(chosen) < target and cursor < len(bucket):
            cand = bucket[cursor]
            cursor += 1
            cand_toks = full_tokens.get(cand["ticket_id"]) or toks(cand)
            if not cand_toks:
                continue
            # Everything in full_pool that isn't already picked is a train peer.
            picked_ids = {c["ticket_id"] for c in chosen} | {c["ticket_id"] for c in picked}
            leak_info: tuple[str, float] | None = None
            for other in full_pool:
                oid = other["ticket_id"]
                if oid == cand["ticket_id"] or oid in picked_ids:
                    continue
                ot = full_tokens[oid]
                if not ot:
                    continue
                inter = len(ot & cand_toks)
                if not inter:
                    continue
                union = len(ot | cand_toks)
                jac = inter / union if union else 0.0
                if jac >= leakage_threshold:
                    leak_info = (oid, jac)
                    break
            if leak_info is None:
                chosen.append(cand)
            else:
                skipped.append((cand["ticket_id"], leak_info[0], round(leak_info[1], 3)))
        if len(chosen) < target:
            raise RuntimeError(
                f"Exhausted {prefix} bucket while avoiding leakage "
                f"(target={target}, got={len(chosen)}, skipped={len(skipped)})."
            )
        picked.extend(sorted(chosen, key=lambda t: t["ticket_id"]))

    if skipped:
        print(f"NOTE: {len(skipped)} candidates skipped due to query-leakage >= {leakage_threshold}:")
        for cand_id, peer_id, jac in skipped:
            print(f"  {cand_id} ~ {peer_id} jaccard={jac}")
    return picked


# --------------------------------------------------------------------------- #
#                         Runtime-query holdout                               #
# --------------------------------------------------------------------------- #


def load_existing_bench_queries() -> list[set[str]]:
    """Return token-sets for every existing benchmark/v12-candidate query.

    Anything that overlaps >= RT_JACCARD_THRESHOLD with one of these will be
    dropped from the runtime holdout (avoid test-set leakage into training).
    """
    out: list[set[str]] = []

    if BENCH_YAML.exists():
        y = yaml.safe_load(BENCH_YAML.read_text()) or {}
        for section in y.values():
            if isinstance(section, list):
                for item in section:
                    if not isinstance(item, dict):
                        continue
                    if item.get("question"):
                        out.append(_tokens(str(item["question"])))
                    for q in item.get("search_queries") or []:
                        out.append(_tokens(str(q)))

    if V12_CANDS.exists():
        with V12_CANDS.open() as f:
            seen: set[str] = set()
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                q = d.get("query")
                if not q or q in seen:
                    continue
                seen.add(q)
                out.append(_tokens(q))

    return [s for s in out if s]


def collect_runtime_candidates() -> list[dict]:
    """Unique (by query) mcp-search rows with result_len>0 and 30<=len<=80."""
    seen: dict[str, dict] = {}
    with LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("source") != "mcp":
                continue
            if d.get("tool") != "search":
                continue
            if (d.get("result_len") or 0) <= 0:
                continue
            q = ((d.get("args") or {}).get("query") or "").strip()
            if not q:
                continue
            if not (RT_MIN_LEN <= len(q) <= RT_MAX_LEN):
                continue
            # first-seen wins (keeps the earliest timestamp)
            if q not in seen:
                seen[q] = {
                    "query": q,
                    "length_chars": len(q),
                    "first_ts": d.get("ts"),
                    "result_len": d.get("result_len"),
                    "duration_ms": d.get("duration_ms"),
                }
    return list(seen.values())


def filter_by_lexical_jaccard(
    cands: list[dict], known: list[set[str]], threshold: float
) -> list[dict]:
    kept: list[dict] = []
    for c in cands:
        toks = _tokens(c["query"])
        if not toks:
            continue
        if any(_jaccard(toks, k) >= threshold for k in known):
            continue
        kept.append(c)
    return kept


def hint_expected_repos(cands: list[dict], pool: list[dict]) -> list[dict]:
    """Best-effort hint: if query tokens subset-appear in a ticket's
    summary+description, use that ticket's repos_changed.

    We pick the ticket with the highest token-overlap (and only if >= 0.6).
    If no strong match -> None.
    """
    ticket_token_sets: list[tuple[set[str], list[str], str]] = []
    for t in pool:
        merged = f"{t['summary']} {t['description']}"
        tok = _tokens(merged)
        if tok:
            ticket_token_sets.append((tok, t["repos_changed"], t["ticket_id"]))

    for c in cands:
        qtok = _tokens(c["query"])
        best_score = 0.0
        best_ticket: str | None = None
        best_repos: list[str] | None = None
        for tt, repos, tid in ticket_token_sets:
            if not qtok:
                continue
            overlap = len(qtok & tt) / len(qtok)
            if overlap > best_score:
                best_score = overlap
                best_ticket = tid
                best_repos = repos
        if best_score >= 0.6 and best_repos:
            c["expected_repos_hint"] = best_repos
            c["_hint_source_ticket"] = best_ticket
            c["_hint_overlap"] = round(best_score, 3)
        else:
            c["expected_repos_hint"] = None
    return cands


# --------------------------------------------------------------------------- #
#                                 main                                        #
# --------------------------------------------------------------------------- #


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    # --- Jira holdout -------------------------------------------------------- #
    sampling_pool, full_pool = load_pool(TASKS_DB)
    print(f"Full 909-pool: {len(full_pool)} tickets (files>=1)")
    print(f"Sampling pool (files>={MIN_FILES} & repos>={MIN_REPOS}): {len(sampling_pool)} tickets")
    prefix_counts: dict[str, int] = {}
    for t in sampling_pool:
        prefix_counts[t["prefix"]] = prefix_counts.get(t["prefix"], 0) + 1
    for p in sorted(prefix_counts):
        print(f"  {p}: {prefix_counts[p]}")

    holdout_jira = sample_jira_holdout(sampling_pool, full_pool, seed=SEED)
    assert len(holdout_jira) == 50, f"expected 50, got {len(holdout_jira)}"
    breakdown: dict[str, int] = {}
    for t in holdout_jira:
        breakdown[t["prefix"]] = breakdown.get(t["prefix"], 0) + 1
    print(f"\nJira holdout prefix breakdown: {dict(sorted(breakdown.items()))}")
    _write_jsonl(OUT_DIR / "holdout_jira_50.jsonl", holdout_jira)
    print(f"Wrote {OUT_DIR / 'holdout_jira_50.jsonl'}")

    # --- Runtime holdout ----------------------------------------------------- #
    cands = collect_runtime_candidates()
    print(f"\nRuntime candidates (mcp+search+non-empty, {RT_MIN_LEN}<=len<={RT_MAX_LEN}): {len(cands)}")

    known = load_existing_bench_queries()
    print(f"Known-benchmark token-sets loaded: {len(known)}")

    filtered = filter_by_lexical_jaccard(cands, known, RT_JACCARD_THRESHOLD)
    print(f"After Jaccard<{RT_JACCARD_THRESHOLD} filter: {len(filtered)}")

    # Seeded random sample across the full 30-80 length band so we get
    # diversity, not just the longest queries (which cluster around 80 due
    # to the hard cap used in logging).
    rng = random.Random(SEED)
    filtered.sort(key=lambda c: c["query"])  # deterministic order first
    rng.shuffle(filtered)
    picked = filtered[:RT_TARGET]

    hinted = hint_expected_repos(picked, full_pool)
    for row in hinted:
        row.pop("_hint_source_ticket", None)
        row.pop("_hint_overlap", None)
    # Final canonical schema: query, length_chars, first_ts, result_len,
    # duration_ms, expected_repos_hint.
    schema_rows = [
        {
            "query": r["query"],
            "length_chars": r["length_chars"],
            "first_ts": r["first_ts"],
            "result_len": r["result_len"],
            "duration_ms": r["duration_ms"],
            "expected_repos_hint": r["expected_repos_hint"],
        }
        for r in hinted
    ]
    _write_jsonl(OUT_DIR / "holdout_runtime_20.jsonl", schema_rows)
    print(f"Wrote {OUT_DIR / 'holdout_runtime_20.jsonl'} ({len(schema_rows)} rows)")

    # --- Summary stats ------------------------------------------------------- #
    n_hints = sum(1 for r in schema_rows if r["expected_repos_hint"])
    lens = [r["length_chars"] for r in schema_rows]
    print(
        f"\nRuntime summary: {len(schema_rows)} queries; "
        f"hinted={n_hints}/{len(schema_rows)}; "
        f"length min={min(lens) if lens else 0} / max={max(lens) if lens else 0} / "
        f"mean={sum(lens) / len(lens):.1f}"
        if lens
        else "no queries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
