"""H6 repro: index drift impact on hit@10 floor.

Expected: ~86 queries (9.5%) have ZERO indexed GT (mathematically unhittable, 0/86 hit).
Indexable subset hit@10 ~45.99% (only +4.4pp above global 41.63%) — drift is NOT the dominant driver.
"""

import json
import sqlite3

d = json.load(open("bench_runs/jira_e2e_wide_off_session2.json"))
conn = sqlite3.connect("db/knowledge.db")
cur = conn.cursor()

# Bulk-load all indexed (repo, file_path) pairs for fast lookup.
indexed = set()
cur.execute("SELECT DISTINCT repo_name, file_path FROM chunks")
for repo, fp in cur:
    indexed.add((repo, fp))
print(f"indexed_pairs={len(indexed)}")

q_zero = q_some = q_some_hit = total = total_hit = zero_hit = 0
for r in d["eval_per_query"]:
    n_idx = sum(
        1
        for ep in r["expected_paths"]
        if ((ep[0], ep[1]) if isinstance(ep, list) else (ep["repo_name"], ep["file_path"])) in indexed
    )
    total += 1
    h10 = r.get("hit_at_10", 0) >= 1
    if h10:
        total_hit += 1
    if n_idx == 0:
        q_zero += 1
        if h10:
            zero_hit += 1
    else:
        q_some += 1
        if h10:
            q_some_hit += 1

print(
    f"total={total} hit={total_hit} zero_indexed={q_zero} (hits={zero_hit}) some_indexed={q_some} some_hit={q_some_hit}"
)
print(f"hit10_indexable={q_some_hit / q_some:.4f}")
print(f"hit10_full={total_hit / total:.4f}")
