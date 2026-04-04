#!/bin/bash
# One-shot CodeRankEmbed vector build for A/B testing.
# Builds into db/vectors.lance.coderank/ — does NOT touch the active db/vectors.lance/
#
# Usage: ./scripts/build_vectors_coderank.sh
# After build: compare benchmarks, then swap if better.

set -euo pipefail
cd ~/.code-rag-mcp

LOG="build_coderank_$(date +%Y%m%d_%H%M%S).log"

echo "=== CodeRankEmbed A/B Build ==="
echo "Output: $LOG"
echo "Started: $(date)"
echo ""
echo "This will take ~3-4 hours. Safe to sleep — nothing is touched until you decide."
echo ""

# Run the Python build script with full output to log + terminal
PYTHONUNBUFFERED=1 python3 scripts/build_vectors_coderank.py 2>&1 | tee "$LOG"

echo ""
echo "=== Done: $(date) ==="
echo "Log saved to: $LOG"
echo ""
echo "Next steps:"
echo "  1. Run benchmarks:  python3 scripts/benchmark_queries.py"
echo "  2. Compare with baseline in ab_test_baseline.json"
echo "  3. If better — swap: mv db/vectors.lance db/vectors.lance.old-minilm && mv db/vectors.lance.coderank db/vectors.lance"
echo "  4. Update container.py and vector.py for CodeRankEmbed"
echo "  5. Restart daemon"
