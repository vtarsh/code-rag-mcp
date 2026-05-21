"""Steps-to-find benchmark — deterministic agent-iteration metric.

Measures how many MCP tool-calls a deterministic-loop agent needs to put each
expected GT file into its read-set. Resolves the architecture-debate residual:
does the reranker's −14.1pp single-shot hit@10 transfer to a multi-shot
iterating consumer, or does reformulation recover those files anyway?

Design: bench_runs/improve/steps_to_find_design.md.

Defaults (per design doc):
- N=5 steps cap
- K_READ=3 new files added to read-set per step
- pool_limit=200 (same as diagnose_recall)
- Reformulation: extract identifier tokens from top-1 NEW result's file_path,
  add to next query (max 2 new tokens). NO LLM (no_external_llm_apis).

Arm env-flags (same kill-switches as diagnose_recall.py):
- CODE_RAG_NO_RERANK=1  → reranker stubbed (raw RRF order)
- CODE_RAG_NO_VECTOR=1  → vector leg stubbed (FTS-only)

Usage:
    python3 scripts/eval/bench_steps_to_find.py --out=bench_runs/improve/s2f/b0.json \\
        --offset=0 --count=25 --n-steps=5 --k-read=3

    # Determinism check on small sample
    python3 scripts/eval/bench_steps_to_find.py --out=/tmp/det.json \\
        --offset=0 --count=20 --assert-deterministic

Run one fresh process per ~25 queries (5x more search calls than diagnose_recall
per query → halve the batch to dodge macOS sentence-transformers semaphore leak).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import sqlite3
import sys
import time
from pathlib import Path


def _rss_mb() -> float:
    """Peak resident set size in MB. macOS ru_maxrss is bytes, Linux kilobytes."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

EVAL_PATH = REPO_ROOT / "profiles" / "pay-com" / "eval" / "jira_eval_clean_v2.jsonl"
TASKS_DB_PATH = REPO_ROOT / "db" / "tasks.db"

# Step 2 (2026-05-21): body-enrichment env-gate. Default ON since 2026-05-21
# night after pod n=665 keep-decision PASSED (+3.31pp s2f@step5). Set env
# to "0" to disable for ablation runs.
_BODY_ENRICH = os.getenv("CODE_RAG_TASK_BODY_ENRICH", "1") == "1"


def _fetch_body(ticket_id: str) -> str:
    """Return task_history.description for `ticket_id` or '' on miss/error.

    Bench-side helper — production lookups happen elsewhere. Errors swallowed
    silently so a missing tasks.db just disables enrichment for the run.
    """
    if not ticket_id or not TASKS_DB_PATH.exists():
        return ""
    try:
        with sqlite3.connect(str(TASKS_DB_PATH)) as con:
            row = con.execute(
                "SELECT description FROM task_history WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


# --- Identifier extraction --------------------------------------------------

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "this",
        "that",
        "it",
        "as",
        "use",
        "get",
        "set",
        "do",
        "from",
        "into",
        "src",
        "lib",
        "libs",
        "test",
        "tests",
        "spec",
        "specs",
        "index",
        "page",
        "pages",
        "components",
        "common",
        "shared",
        "types",
        "constants",
        "utils",
        "util",
        "helpers",
        "helper",
        "core",
        "main",
        "app",
        "config",
        "configs",
        "ts",
        "tsx",
        "js",
        "jsx",
        "py",
        "go",
        "proto",
        "yml",
        "yaml",
        "json",
    }
)

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")

# Compound identifier regex — matches camelCase, PascalCase, snake_case
# tokens that are ≥8 chars long and have ≥2 word-parts (i.e. discriminating
# composite identifiers, not generic words). v2 reformulation source.
#
# Patterns matched (≥8 chars total, kept as whole-token):
#   PascalCase compound:  MerchantPricing, SettlementAccount
#   camelCase compound:   useMerchantPricing, getSettlementAccount
#   snake_case compound:  use_merchant_pricing, get_settlement_account
_COMPOUND_PASCAL_RE = re.compile(r"\b([A-Z][a-z0-9]+){2,}\b")
_COMPOUND_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+){1,}\b")
_COMPOUND_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+){1,}\b")


def _split_identifier(s: str) -> list[str]:
    """Split path/identifier into normalized lowercase tokens.

    Splits on /, ., -, _ and camelCase boundaries. Drops stop-words and tokens
    shorter than 3 chars. Used for query-overlap dedup.
    """
    flat = _NON_ALNUM_SPLIT_RE.sub(" ", s)
    out: list[str] = []
    for chunk in flat.split():
        for sub in _CAMEL_SPLIT_RE.split(chunk):
            tok = sub.lower().strip()
            if len(tok) >= 3 and tok not in _STOP_WORDS:
                out.append(tok)
    return out


def _extract_followup_path_tokens(file_path: str, query_tokens: set[str], k: int = 2) -> list[str]:
    """Fallback when snippet has no discriminating compounds (e.g. config files).
    Uses last 2 path segments — same logic as v1 path-token reformulation but
    isolated as a fallback rather than the primary policy.
    """
    segments = [s for s in file_path.split("/") if s]
    if not segments:
        return []
    tail = "/".join(segments[-2:])
    toks = _split_identifier(tail)
    seen: set[str] = set()
    out: list[str] = []
    for tok in toks:
        if tok in query_tokens or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= k:
            break
    return out


def _extract_content_tokens(snippet: str, query_tokens: set[str], k: int = 2) -> list[str]:
    """v2 reformulation: extract discriminating compound identifiers from a
    result's snippet/content preview. Models a real agent reading top-K files
    and picking up `useMerchantPricing`, `SettlementAccount`, `get_payout_status`
    type names rather than generic path tokens.

    Selection: collect all PascalCase / camelCase / snake_case compounds ≥8 chars
    appearing in the snippet, keep the kept-form (whole token), rank by
    occurrence count (most-mentioned = most central to file), drop query-overlap,
    return top-k. Whole-token form is what's fed back to FTS so it matches the
    BM25 tokenizer's split form (FTS5 porter unicode61 splits camelCase, so
    `useMerchantPricing` → `use merchant pricing` will match).
    """
    if not snippet:
        return []
    counts: dict[str, int] = {}
    for rx in (_COMPOUND_PASCAL_RE, _COMPOUND_CAMEL_RE, _COMPOUND_SNAKE_RE):
        for m in rx.finditer(snippet):
            tok = m.group(0)
            if len(tok) < 8:
                continue
            counts[tok] = counts.get(tok, 0) + 1
    # Rank: frequency desc, then length desc (prefer longer = more discriminating),
    # then alpha for determinism.
    ranked_toks = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    out: list[str] = []
    seen: set[str] = set()
    for tok, _cnt in ranked_toks:
        # Dedup against query overlap: split the compound and check word-overlap.
        # If ALL split-parts are already in query, skip — token adds no signal.
        parts = set(_split_identifier(tok))
        if parts and parts.issubset(query_tokens):
            continue
        if tok.lower() in seen:
            continue
        seen.add(tok.lower())
        out.append(tok)
        if len(out) >= k:
            break
    return out


# --- Per-task simulation ----------------------------------------------------


def _norm(p: str) -> str:
    return (p or "").strip().lstrip("/")


def _key(repo: str, path: str) -> tuple[str, str]:
    return (_norm(repo), _norm(path))


def simulate_one(
    row: dict,
    *,
    n_steps: int,
    k_read: int,
    pool_limit: int,
    exclude_file_types: str,
    use_expand: bool,
) -> dict:
    from src.search.fts import expand_query
    from src.search.hybrid import hybrid_search
    from src.search.service import _detect_intent_adjustments, preprocess_query

    base_query = row["query"]
    expected = [_key(ep["repo_name"], ep["file_path"]) for ep in row.get("expected_paths", [])]
    expected_set = set(expected)
    n_exp = max(1, len(expected_set))
    # Step 2: stable body string passed to every hybrid_search call. Env-gated
    # at module-load so the lookup happens once per task at most. Empty string
    # is a valid no-signal value — hybrid_search treats it as no body.
    task_body = _fetch_body(row.get("id", "")) if _BODY_ENRICH else ""

    read_set: set[tuple[str, str]] = set()
    found_at: dict[tuple[str, str], int] = {}
    new_per_step: list[int] = []
    queries_used: list[str] = []
    cur_query = base_query
    last_step_tokens: list[str] = []  # slide-window: replaced each step, not accumulated
    base_tokens = set(_split_identifier(base_query))
    early_break = None

    for step in range(1, n_steps + 1):
        queries_used.append(cur_query)
        # --- Replicate diagnose_recall.py preprocessing exactly ---
        expanded = expand_query(cur_query) if use_expand else cur_query
        processed_query, entities = preprocess_query(cur_query)
        use_entity_boost = len(cur_query.split()) >= 6 and bool(entities)
        if os.getenv("CODE_RAG_QUERY_V2", "0") == "1" and len(entities) < 3:
            use_entity_boost = False
        search_query = processed_query if use_entity_boost else expanded
        repo_boost, repo_prefix_boost, *_ = _detect_intent_adjustments(cur_query)

        try:
            ranked, *_ = hybrid_search(
                search_query,
                "",
                "",
                exclude_file_types,
                pool_limit,
                cross_provider=False,
                docs_index=None,
                entity_boost=1.3 if use_entity_boost else 1.0,
                repo_boost=repo_boost,
                repo_prefix_boost=repo_prefix_boost,
                body=task_body,
            )
            if use_entity_boost and len(ranked) < 5:
                ranked, *_ = hybrid_search(
                    expanded,
                    "",
                    "",
                    exclude_file_types,
                    pool_limit,
                    cross_provider=False,
                    docs_index=None,
                    repo_boost=repo_boost,
                    repo_prefix_boost=repo_prefix_boost,
                    body=task_body,
                )
        except Exception as exc:
            return {"id": row.get("id", ""), "query": base_query, "error": repr(exc), "step_failed": step}

        # Take top-K_READ NEW files into read-set, capture their snippets
        new_reads: list[tuple[str, str]] = []
        new_snippets: list[str] = []
        for r in ranked:
            k = _key(r["repo_name"], r["file_path"])
            if k in read_set:
                continue
            new_reads.append(k)
            new_snippets.append(r.get("snippet", "") or "")
            if len(new_reads) >= k_read:
                break
        new_per_step.append(len(new_reads))

        for k in new_reads:
            read_set.add(k)
            if k in expected_set and k not in found_at:
                found_at[k] = step

        if not new_reads:
            early_break = "no_new_results"
            break
        if len(found_at) >= len(expected_set):
            early_break = "full_recall"
            break

        # --- v2 reformulate: extract content tokens (compound identifiers) from
        # the AGGREGATED snippets of new reads. Slide-window — next query =
        # base + last-step's tokens (NOT accumulated). Bounded query length →
        # no FTS5 OR-explosion → kills the long-tail seen in v1.
        joined_snippet = "\n".join(new_snippets)
        prev_tokens = base_tokens | {t.lower() for t in last_step_tokens}
        new_toks = _extract_content_tokens(joined_snippet, prev_tokens, k=2)
        if not new_toks:
            # Fall back to path-token extraction from top-1 NEW file when content
            # has no discriminating identifiers (e.g. JSON config files).
            new_toks = _extract_followup_path_tokens(new_reads[0][1], prev_tokens, k=2)
        if not new_toks:
            # Truly nothing novel — keep last query, will trigger no_new next iter.
            continue
        last_step_tokens = new_toks
        cur_query = base_query + " " + " ".join(new_toks)

    # Aggregates
    steps_to_first_hit = min(found_at.values()) if found_at else None
    steps_to_full = max(found_at.values()) if (found_at and len(found_at) == len(expected_set)) else None
    terminal_recall = len(found_at) / n_exp

    return {
        "id": row.get("id", ""),
        "query": base_query,
        "n_expected": len(expected_set),
        "n_steps_run": len(queries_used),
        "steps_to_first_hit": steps_to_first_hit,
        "steps_to_full_recall": steps_to_full,
        "terminal_recall": round(terminal_recall, 4),
        "early_break": early_break,
        "new_per_step": new_per_step,
        "queries_used": queries_used,
        "found_at": [
            {"repo_name": k[0], "file_path": k[1], "step": v} for k, v in sorted(found_at.items(), key=lambda kv: kv[1])
        ],
        "strata": row.get("strata", []),
    }


# --- Aggregation -----------------------------------------------------------


def _aggregate(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    n = len(ok) or 1
    hits = [r for r in ok if r["steps_to_first_hit"] is not None]
    full = [r for r in ok if r["steps_to_full_recall"] is not None]

    # hit_rate_at_step_K for K = 1..5
    max_n_steps = max((len(r["queries_used"]) for r in ok), default=5)
    hit_rate_at_step = {}
    for K in range(1, max_n_steps + 1):
        hit_rate_at_step[K] = round(sum(1 for r in hits if r["steps_to_first_hit"] <= K) / n, 4)

    return {
        "n": len(ok),
        "n_error": len(rows) - len(ok),
        "n_hit": len(hits),
        "n_full_recall": len(full),
        "mean_steps_to_first_hit": round(sum(r["steps_to_first_hit"] for r in hits) / max(1, len(hits)), 4),
        "mean_steps_to_full_recall": round(sum(r["steps_to_full_recall"] for r in full) / max(1, len(full)), 4),
        "mean_terminal_recall": round(sum(r["terminal_recall"] for r in ok) / n, 4),
        "full_recall_rate": round(len(full) / n, 4),
        "hit_rate_at_step": hit_rate_at_step,
        "mean_new_per_step": [
            round(sum(r["new_per_step"][k] if k < len(r["new_per_step"]) else 0 for r in ok) / n, 3)
            for k in range(max_n_steps)
        ],
    }


# --- CLI -------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--eval", type=Path, default=EVAL_PATH)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--count", type=int, default=0, help="0 = all")
    p.add_argument("--n-steps", type=int, default=5)
    p.add_argument("--k-read", type=int, default=3)
    p.add_argument("--pool-limit", type=int, default=200)
    p.add_argument(
        "--assert-deterministic", action="store_true", help="Run each task twice, fail if results differ. Slow."
    )
    args = p.parse_args()

    # Apply env-arm stubs (same as diagnose_recall.py).
    if os.getenv("CODE_RAG_NO_VECTOR", "0") == "1":
        import src.search.hybrid as _H

        _H.vector_search = lambda *_a, **_k: ([], None)
        print("[CODE_RAG_NO_VECTOR] vector leg disabled — FTS-only run", flush=True)

    if os.getenv("CODE_RAG_NO_RERANK", "0") == "1":
        import src.search.hybrid as _H

        _H.rerank = lambda _q, ranked, limit, **_kw: ranked[:limit]
        print("[CODE_RAG_NO_RERANK] reranker disabled — raw RRF order", flush=True)

    from src.search.service import _USE_EXPAND_QUERY

    exclude_file_types = os.environ.get("CODE_RAG_DEFAULT_EXCLUDE", "")
    use_expand = _USE_EXPAND_QUERY

    all_rows = [json.loads(line) for line in args.eval.open() if line.strip()]
    count = args.count if args.count > 0 else len(all_rows)
    rows = all_rows[args.offset : args.offset + count]

    per_query: list[dict] = []
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        result = simulate_one(
            row,
            n_steps=args.n_steps,
            k_read=args.k_read,
            pool_limit=args.pool_limit,
            exclude_file_types=exclude_file_types,
            use_expand=use_expand,
        )

        if args.assert_deterministic and "error" not in result:
            result2 = simulate_one(
                row,
                n_steps=args.n_steps,
                k_read=args.k_read,
                pool_limit=args.pool_limit,
                exclude_file_types=exclude_file_types,
                use_expand=use_expand,
            )
            # Compare ignoring 'queries_used' (deterministic by construction) — focus on outcomes.
            outcome_keys = (
                "steps_to_first_hit",
                "steps_to_full_recall",
                "terminal_recall",
                "early_break",
                "new_per_step",
                "found_at",
            )
            for k in outcome_keys:
                if result.get(k) != result2.get(k):
                    print(
                        f"[NON-DETERMINISTIC] {row.get('id')} key={k}: {result.get(k)!r} vs {result2.get(k)!r}",
                        flush=True,
                    )
                    result["non_deterministic"] = True
                    break

        per_query.append(result)
        print(
            f"[{i}/{len(rows)}] id={row.get('id', '?')} steps={result.get('n_steps_run', '?')} "
            f"first_hit={result.get('steps_to_first_hit')} rss={_rss_mb():.0f}MB "
            f"elapsed={time.time() - t0:.0f}s",
            flush=True,
        )

    out = {
        "aggregates": _aggregate(per_query),
        "config": {
            "n_steps": args.n_steps,
            "k_read": args.k_read,
            "pool_limit": args.pool_limit,
            "exclude_file_types": exclude_file_types,
            "use_expand_query": use_expand,
            "no_rerank": os.getenv("CODE_RAG_NO_RERANK", "0") == "1",
            "no_vector": os.getenv("CODE_RAG_NO_VECTOR", "0") == "1",
            "task_body_enrich": _BODY_ENRICH,
            "per_token_union": os.getenv("CODE_RAG_PER_TOKEN_UNION", "0") == "1",
        },
        "eval_per_query": per_query,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    print("=== STEPS-TO-FIND ===", flush=True)
    print(json.dumps(out["aggregates"], indent=2), flush=True)
    print(f"Wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
