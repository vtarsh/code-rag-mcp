"""Repro for H3-confirmation: jira_eval GT pairs vs indexed chunks.

Reproduces the bound:
- 22459 GT pairs total
- 9458 indexed (42.1%)
- 13001 not indexed (57.9%)
- 86 queries (9.5%) with ZERO indexed expected paths => mathematically unhittable

Run: python3.12 .claude/debug/current/repro_h3.py

Expected output (run 2026-04-27):
  indexed pairs in chunks: 29682
  eval queries: 908
  GT pairs total: 22459, indexed: 9458 (42.1%)
  queries with 0 indexed GT: 86 (9.5%)
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "db" / "knowledge.db"
EVAL = ROOT / "profiles" / "pay-com" / "jira_eval_n900.jsonl"

con = sqlite3.connect(DB)
cur = con.cursor()
indexed = set()
for repo, fp in cur.execute("SELECT DISTINCT repo_name, file_path FROM chunks"):
    indexed.add((repo, fp))
print(f"indexed pairs in chunks: {len(indexed)}")

queries = []
with open(EVAL) as f:
    for line in f:
        if line.strip():
            queries.append(json.loads(line))
print(f"eval queries: {len(queries)}")

total = 0
indexed_count = 0
zero_idx_queries = 0
for q in queries:
    eps = q.get("expected_paths", []) or []
    q_indexed = 0
    q_total = 0
    for ep in eps:
        if not isinstance(ep, dict):
            continue
        repo = ep.get("repo_name")
        fp = ep.get("file_path")
        if not (repo and fp):
            continue
        q_total += 1
        total += 1
        if (repo, fp) in indexed:
            q_indexed += 1
            indexed_count += 1
    if q_total > 0 and q_indexed == 0:
        zero_idx_queries += 1

print(f"GT pairs total: {total}, indexed: {indexed_count} ({100 * indexed_count / total:.1f}%)")
print(f"queries with 0 indexed GT: {zero_idx_queries} ({100 * zero_idx_queries / len(queries):.1f}%)")
