#!/usr/bin/env python3
"""LLM-as-judge for churn_replay diff pairs.

Reads a JSONL of diff pairs (from scripts/analyze_churn.py --diff-pairs) and
asks Claude to decide which reranker returned more relevant top-10 results for
each query. Aggregates win rate as a cheap gold signal for v8 vs base.

Defaults to **dry-run** — the script prints what *would* be sent and exits
without any API calls. Pass `--run` and set `ANTHROPIC_API_KEY` to actually
judge. Uses Haiku 4.5 for cost (~$1/1M input, ~$5/1M output).

Cost estimate at 50 pairs × ~1500 input + 200 output tokens = ~$0.13 per run.

Usage:
  # dry-run (default, no API cost)
  python3.12 scripts/churn_llm_judge.py \
    --input profiles/pay-com/churn_replay/diff_pairs.jsonl

  # live judge (requires ANTHROPIC_API_KEY)
  python3.12 scripts/churn_llm_judge.py \
    --input profiles/pay-com/churn_replay/diff_pairs.jsonl \
    --output profiles/pay-com/churn_replay/judge_v8_vs_base.jsonl \
    --run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Literal


def _load_pairs(path: Path, limit: int | None) -> list[dict]:
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out.append(rec)
            if limit is not None and len(out) >= limit:
                break
    return out


def _format_keys(keys: list[str], label: str) -> str:
    lines = [f"{label}:"]
    for i, k in enumerate(keys[:10], 1):
        lines.append(f"  {i}. {k}")
    return "\n".join(lines)


def _build_prompt(pair: dict) -> tuple[str, str]:
    query = pair.get("query", "")
    base_keys = pair.get("base_top10_keys") or [
        f"{r.get('repo_name','')}::{r.get('file_path','')}" for r in (pair.get("base_top10") or [])
    ]
    v8_keys = pair.get("v8_top10_keys") or [
        f"{r.get('repo_name','')}::{r.get('file_path','')}" for r in (pair.get("v8_top10") or [])
    ]

    system = (
        "You are an expert code-search relevance judge for a payments platform's "
        "internal RAG system. You compare two ranked lists of code search results "
        "(each item is `repo::file_path`) and decide which list is more relevant "
        "to the user's query. Penalize: generic index.js files when a specific "
        "named file exists (e.g. `payout.js` beats `methods/index.js` for a "
        "payout query); docs/readme/test files if the query is about implementation. "
        "Reward: specific named files matching the query's key terms, production "
        "code over derived docs, repos clearly related to the query's concepts. "
        "A 'tie' verdict is valid if lists are near-equivalent or both poor."
    )
    user = (
        f"QUERY: {query}\n\n"
        f"{_format_keys(base_keys, 'LIST A (base)')}\n\n"
        f"{_format_keys(v8_keys, 'LIST B (v8)')}\n\n"
        "Which list is more relevant to the query? Output JSON with:\n"
        "  verdict: 'a' (list A more relevant), 'b' (list B more relevant), or 'tie'\n"
        "  confidence: 0.0-1.0\n"
        "  reasoning: one sentence explaining the decision"
    )
    return system, user


def _judge_one_live(client, model: str, pair: dict) -> dict:
    from pydantic import BaseModel, Field

    class JudgeVerdict(BaseModel):
        verdict: Literal["a", "b", "tie"]
        confidence: float = Field(ge=0.0, le=1.0)
        reasoning: str

    system, user = _build_prompt(pair)
    response = client.messages.parse(
        model=model,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=JudgeVerdict,
    )
    parsed: JudgeVerdict = response.parsed_output
    return {
        "verdict": parsed.verdict,
        "confidence": parsed.confidence,
        "reasoning": parsed.reasoning,
        "model": response.model,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }


def _aggregate(results: list[dict]) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0}
    counts = {"a": 0, "b": 0, "tie": 0}
    conf_sum = {"a": 0.0, "b": 0.0, "tie": 0.0}
    for r in results:
        v = r.get("judge", {}).get("verdict")
        c = r.get("judge", {}).get("confidence", 0.0)
        if v in counts:
            counts[v] += 1
            conf_sum[v] += c

    input_toks = sum(r.get("judge", {}).get("usage", {}).get("input_tokens", 0) for r in results)
    output_toks = sum(r.get("judge", {}).get("usage", {}).get("output_tokens", 0) for r in results)

    return {
        "n": n,
        "base_wins": counts["a"],
        "v8_wins": counts["b"],
        "ties": counts["tie"],
        "base_win_rate": round(counts["a"] / n, 4),
        "v8_win_rate": round(counts["b"] / n, 4),
        "tie_rate": round(counts["tie"] / n, 4),
        "mean_confidence": {
            "base": round(conf_sum["a"] / counts["a"], 3) if counts["a"] else None,
            "v8": round(conf_sum["b"] / counts["b"], 3) if counts["b"] else None,
            "tie": round(conf_sum["tie"] / counts["tie"], 3) if counts["tie"] else None,
        },
        "usage_total": {"input_tokens": input_toks, "output_tokens": output_toks},
    }


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--input", type=Path, default=Path("profiles/pay-com/churn_replay/diff_pairs.jsonl"))
    p.add_argument("--output", type=Path, default=Path("profiles/pay-com/churn_replay/judge_v8_vs_base.jsonl"))
    p.add_argument("--summary", type=Path, default=Path("profiles/pay-com/churn_replay/judge_summary.json"))
    p.add_argument("--model", default="claude-haiku-4-5", help="Claude model (default: haiku 4.5 for cost)")
    p.add_argument("--max-pairs", type=int, default=50, help="process at most N pairs (default 50, ~$0.13 at haiku)")
    p.add_argument("--run", action="store_true", help="actually call the API (default: dry-run, no cost)")
    args = p.parse_args()

    if not args.input.exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1

    pairs = _load_pairs(args.input, args.max_pairs)
    if not pairs:
        print("ERROR: no diff pairs in input", file=sys.stderr)
        return 1
    print(f"loaded {len(pairs)} diff pairs from {args.input}", flush=True)

    if not args.run:
        print("\n=== DRY RUN — no API calls will be made ===", flush=True)
        print(f"Would judge {len(pairs)} pairs with model={args.model}", flush=True)
        print(f"Estimated cost: ~${len(pairs) * 0.0026:.2f} at ~1500 in + 200 out tokens/pair\n", flush=True)
        print("--- sample prompt (pair #1) ---", flush=True)
        system, user = _build_prompt(pairs[0])
        print(f"[system]\n{system}\n", flush=True)
        print(f"[user]\n{user}\n", flush=True)
        print("To actually run: pass --run and set ANTHROPIC_API_KEY", flush=True)
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: --run requires ANTHROPIC_API_KEY env var", file=sys.stderr)
        return 1

    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        print("ERROR: pip install anthropic", file=sys.stderr)
        return 1

    client = anthropic.Anthropic()
    results: list[dict] = []

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for i, pair in enumerate(pairs, 1):
            t0 = time.perf_counter()
            try:
                judge = _judge_one_live(client, args.model, pair)
            except anthropic.RateLimitError as e:
                retry = int(e.response.headers.get("retry-after", "30"))
                print(f"[{i}/{len(pairs)}] RATE LIMIT; sleeping {retry}s", flush=True)
                time.sleep(retry)
                judge = _judge_one_live(client, args.model, pair)
            except anthropic.APIStatusError as e:
                print(f"[{i}/{len(pairs)}] API error {e.status_code}: {e.message}", flush=True)
                judge = {"verdict": None, "error": str(e)}

            lat = time.perf_counter() - t0
            entry = {
                "query": pair.get("query"),
                "base_top10_keys": pair.get("base_top10_keys"),
                "v8_top10_keys": pair.get("v8_top10_keys"),
                "overlap_at_10": pair.get("overlap_at_10"),
                "judge": judge,
                "latency_s": round(lat, 2),
            }
            results.append(entry)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            if i % 10 == 0 or i == len(pairs):
                print(f"[{i}/{len(pairs)}] verdict={judge.get('verdict')} lat={lat:.2f}s", flush=True)

    summary = _aggregate(results)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== SUMMARY ===", flush=True)
    for k, v in summary.items():
        print(f"  {k}: {v}", flush=True)
    print(f"\nresults: {args.output}", flush=True)
    print(f"summary: {args.summary}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
