"""Round 1 H6 repro — jira queries concentrate GT in a single repo.

Measures the % of queries whose top GT repo holds ≥50% of expected paths.
Confirms dataset shape: most jira tickets touch one dominant repo.

Expected output: 775/908=85.4%
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

EVAL = Path("/Users/vaceslavtarsevskij/.code-rag-mcp/profiles/pay-com/jira_eval_n900.jsonl")


def main() -> None:
    rows = [json.loads(line) for line in EVAL.read_text().splitlines() if line.strip()]
    n_concentrated = 0
    for row in rows:
        repo_counts = collections.Counter(p["repo_name"] for p in row["expected_paths"])
        if not repo_counts:
            continue
        top_repo, top_count = repo_counts.most_common(1)[0]
        share = top_count / sum(repo_counts.values())
        if share >= 0.5:
            n_concentrated += 1
    n_total = len(rows)
    print(f"{n_concentrated}/{n_total}={100 * n_concentrated / n_total:.1f}%")


if __name__ == "__main__":
    main()
