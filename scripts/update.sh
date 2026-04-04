#!/usr/bin/env bash
set -euo pipefail

# Incremental Update
# Fetches new commits for all repos and re-extracts only changed ones.
# Usage: ./update.sh [--full]
#   --full: force re-extract all repos (ignore SHA cache)

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="${CODE_RAG_HOME:-$HOME/.code-rag}"
RAW_DIR="$BASE_DIR/raw"
STATE_FILE="$BASE_DIR/repo_state.json"
STATE_BEFORE="/tmp/code-rag-state-before.json"

# Cleanup on exit/interrupt
cleanup() {
  rm -f "$STATE_BEFORE"
}
trap cleanup EXIT

FULL=false
[[ "${1:-}" == "--full" ]] && FULL=true

echo "=== Knowledge Base Update ==="
echo "$(date)"
echo ""

# Step 1: Save state before
if [[ -f "$STATE_FILE" ]]; then
  cp "$STATE_FILE" "$STATE_BEFORE"
else
  echo '{}' > "$STATE_BEFORE"
fi

# Step 2: Clone/update repos
echo "--- Phase 1: Fetching repos ---"
"$SCRIPTS_DIR/clone_repos.sh"
echo ""

# Step 3: Determine what changed
if $FULL; then
  echo "--- Phase 2: Full re-extraction (--full) ---"
  python3 "$SCRIPTS_DIR/extract_artifacts.py"

  echo ""
  echo "--- Phase 3: Full FTS index ---"
  python3 "$SCRIPTS_DIR/build_index.py"

  echo ""
  echo "--- Phase 4: Full vector embeddings ---"
  python3 "$SCRIPTS_DIR/build_vectors.py" --force
else
  echo "--- Phase 2: Extracting changed repos ---"
  # Compare before/after state to find changed repos
  CHANGED=$(python3 -c "
import json, sys
before = json.load(open('$STATE_BEFORE'))
after = json.load(open('$STATE_FILE'))
changed = []
for repo, sha in after.items():
    if repo not in before or before[repo] != sha:
        changed.append(repo)
# Also extract repos that were never extracted
import os
extracted_dir = os.path.expanduser('~/.code-rag/extracted')
for repo in after:
    repo_extracted = os.path.join(extracted_dir, repo)
    if not os.path.isdir(repo_extracted):
        if repo not in changed:
            changed.append(repo)
print(len(changed))
for c in changed:
    print(c)
")

  CHANGED_COUNT=$(echo "$CHANGED" | head -1)
  CHANGED_LIST=$(echo "$CHANGED" | tail -n +2 | tr '\n' ',' | sed 's/,$//')

  echo "Changed repos: $CHANGED_COUNT"
  if [[ -n "$CHANGED_LIST" ]]; then
    echo "  $CHANGED_LIST"
  fi

  if [[ "$CHANGED_COUNT" == "0" ]]; then
    echo "Nothing changed. All up to date."
  else
    echo ""
    echo "--- Phase 3: Incremental extraction ---"
    python3 "$SCRIPTS_DIR/extract_artifacts.py" "--repos=$CHANGED_LIST"

    echo ""
    echo "--- Phase 4: Incremental FTS index ---"
    python3 "$SCRIPTS_DIR/build_index.py" "--repos=$CHANGED_LIST"

    echo ""
    echo "--- Phase 5: Incremental vector embeddings ---"
    python3 "$SCRIPTS_DIR/build_vectors.py" "--repos=$CHANGED_LIST"
  fi
fi

echo ""
echo "=== Update Complete ==="
echo "$(date)"
