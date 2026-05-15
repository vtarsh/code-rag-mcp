#!/usr/bin/env python3.12
"""Print one query's bundle for inline scoring."""

import json
import sys

b = json.load(open("/tmp/p10_a2_judge_bundle_opus2.json"))
idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
r = b[idx]
print(f"=== Q{idx + 1} [{r['query_id']}] stratum={r['stratum']} ===")
print(f"QUERY: {r['query']}")
print(f"EXPECTED ({len(r['expected_paths'])}): {[p['file_path'].split('/')[-1] for p in r['expected_paths'][:8]]}")
print(f"A2 R@10 heur={r['a2_r10_heuristic']}, ON R@10 heur={r['on_r10_heuristic']}")
print()
for tag in ("a2", "on"):
    print(f"--- {tag.upper()} top-10 ---")
    for c in r[tag]:
        snip = c["snippet"].replace("\n", " ")[:600]
        path = c["file_path"]
        if len(path) > 90:
            path = "..." + path[-87:]
        print(f"R{c['rank']:>2} [{c['repo_name']}] {path}")
        print(f"     {snip}")
    print()
