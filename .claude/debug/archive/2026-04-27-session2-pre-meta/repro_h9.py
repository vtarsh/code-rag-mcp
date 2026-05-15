"""H9 repro: boost multiplier ranking-inversion mechanism + bench correlational signal.

Demonstrates that GOTCHAS_BOOST=1.5 can lift a rank-30 gotchas chunk above an unboosted
rank-5 code chunk, validating the boost-vs-penalty asymmetry described in hybrid.py:817-824
(boosts multiply raw RRF) vs hybrid.py:550-552 (penalties subtract from normalized post-rerank).
"""

import json

K = 60
KW = 2.0  # KEYWORD_WEIGHT
GOTCHAS = 1.5
REFERENCE = 1.3
DICTIONARY = 1.4

print("Mechanism (arithmetic over RRF formula `KW_WEIGHT/(K + rank + 1)` × boost):")
print(f"  unboosted FTS5 rank  5: rrf = {KW / (K + 6):.4f}")
print(f"  unboosted FTS5 rank 10: rrf = {KW / (K + 11):.4f}")
print(f"  unboosted FTS5 rank 30: rrf = {KW / (K + 31):.4f}")
print(f"  GOTCHAS rank 30 boosted ×1.5: rrf = {KW / (K + 31) * GOTCHAS:.4f}  (> rank-5 unboosted ✓)")
print(f"  REFERENCE rank 20 boosted ×1.3: rrf = {KW / (K + 21) * REFERENCE:.4f}")
print(f"  DICTIONARY rank 25 boosted ×1.4: rrf = {KW / (K + 26) * DICTIONARY:.4f}")
print()

# Bench correlational signal
d = json.load(open("bench_runs/jira_e2e_wide_off_session2.json"))
BOOST_TYPES = {"gotchas", "reference", "dictionary"}
boost_top1 = []
for r in d["eval_per_query"]:
    tops = r.get("top_files", [])
    if not tops:
        continue
    if tops[0].get("file_type", "") in BOOST_TYPES:
        boost_top1.append(r)

n = len(boost_top1)
hits = sum(1 for r in boost_top1 if r.get("hit_at_10", 0) >= 1)
print(f"Bench: queries with boost-type (gotchas/reference/dictionary) top-1: {n}/{len(d['eval_per_query'])}")
print(f"  of those, hit@10: {hits} = {hits / n:.4f}" if n else "  none")
