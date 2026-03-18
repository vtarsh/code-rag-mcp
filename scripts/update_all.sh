#!/bin/bash
# Full pipeline: pull repos → rebuild index, graph, vectors, viz
# Designed to run via cron (weekly)

set -euo pipefail

LOGFILE="$HOME/.code-rag/logs/update_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOGFILE")"

exec > >(tee -a "$LOGFILE") 2>&1
echo "=== Code RAG update started: $(date) ==="

cd "$HOME/.code-rag"

# 1. Pull latest from all cloned repos
echo "--- Pulling repos ---"
REPOS_DIR="$HOME/.code-rag/raw"
if [ -d "$REPOS_DIR" ]; then
  for repo in "$REPOS_DIR"/*/; do
    if [ -d "$repo/.git" ]; then
      echo "Pulling $(basename "$repo")..."
      git -C "$repo" pull --ff-only 2>/dev/null || echo "  (skipped — not on tracking branch)"
    fi
  done
fi

# 2. Extract artifacts
echo "--- Extracting artifacts ---"
python3 scripts/extract_artifacts.py

# 3. Build FTS index
echo "--- Building index ---"
python3 scripts/build_index.py

# 4. Build dependency graph
echo "--- Building graph ---"
python3 scripts/build_graph.py

# 5. Build vector embeddings
echo "--- Building vectors ---"
python3 scripts/build_vectors.py

# 6. Generate visualization
echo "--- Generating graph HTML ---"
python3 scripts/visualize_graph.py

echo "=== Update completed: $(date) ==="
