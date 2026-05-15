"""Round 1 SY parity bench — H1/H5 resolution.

Three modes on the same 50-query jira subset:
  (a) bench-baseline: hybrid_search(q) directly  — current bench protocol
  (b) bench+expand:   hybrid_search(expand_query(q)) — bench, but with prod glossary
  (c) prod-via-svc:   service.search_tool(q) end-to-end — exact prod path

Outputs mode | hit@5 | hit@10 | n
H1 confirmed if (a) - (c) materially differs (≥3pp)
H5 confirmed if (b) - (a) is materially negative (glossary hurts jira)
H1 excluded if all three within ±1pp
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/vaceslavtarsevskij/.code-rag-mcp")

from src.search.fts import expand_query
from src.search.hybrid import hybrid_search
from src.search.service import search_tool

EVAL = Path(__file__).parent / "jira_n50.jsonl"
TOP_K = 10


def expected_pairs(row):
    out = []
    for p in row.get("expected_paths", []):
        if isinstance(p, dict):
            out.append((p.get("repo_name", ""), p.get("file_path", "")))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            out.append((p[0], p[1]))
    return out


def hit(top_files, expected, k):
    seen = {(h.get("repo_name", ""), h.get("file_path", "")) for h in top_files[:k]}
    return 1 if any((r, p) in seen for r, p in expected) else 0


def parse_service_output(text):
    """Parse `**repo** | `path` (...)` lines from service.search_tool() output."""
    files = []
    pat = re.compile(r"\*\*([^*]+)\*\*\s*\|\s*`([^`]+)`")
    for m in pat.finditer(text):
        files.append({"repo_name": m.group(1).strip(), "file_path": m.group(2).strip()})
        if len(files) >= TOP_K:
            break
    return files


def run_mode(rows, mode):
    sum5 = sum10 = 0
    n = 0
    t0 = time.time()
    for row in rows:
        q = row.get("query", "").strip()
        if not q:
            continue
        expected = expected_pairs(row)
        if not expected:
            continue
        try:
            if mode == "a":
                ranked, _, _ = hybrid_search(q, limit=TOP_K)
                top_files = [
                    {"repo_name": r.get("repo_name", ""), "file_path": r.get("file_path", "")} for r in ranked[:TOP_K]
                ]
            elif mode == "b":
                eq = expand_query(q)
                ranked, _, _ = hybrid_search(eq, limit=TOP_K)
                top_files = [
                    {"repo_name": r.get("repo_name", ""), "file_path": r.get("file_path", "")} for r in ranked[:TOP_K]
                ]
            else:  # mode c
                txt = search_tool(query=q, brief=True, limit=TOP_K)
                top_files = parse_service_output(txt)
        except Exception as exc:
            print(f"  ERR {row.get('id')}: {exc}", file=sys.stderr)
            continue
        sum5 += hit(top_files, expected, 5)
        sum10 += hit(top_files, expected, 10)
        n += 1
    elapsed = time.time() - t0
    return n, sum5 / max(n, 1), sum10 / max(n, 1), elapsed


def main():
    rows = [json.loads(l) for l in EVAL.read_text().splitlines() if l.strip()]
    print(f"loaded {len(rows)} eval rows from {EVAL}", flush=True)
    print(f"\n{'mode':<25} {'n':>4} {'hit@5':>8} {'hit@10':>8} {'time_s':>8}", flush=True)
    for mode, label in [
        ("a", "(a) bench-baseline"),
        ("b", "(b) bench+expand"),
        ("c", "(c) prod-via-service"),
    ]:
        n, h5, h10, t = run_mode(rows, mode)
        print(f"{label:<25} {n:>4} {h5:>8.4f} {h10:>8.4f} {t:>8.1f}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
