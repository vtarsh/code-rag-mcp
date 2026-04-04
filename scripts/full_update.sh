#!/usr/bin/env bash
set -euo pipefail

# Full pipeline: clone → extract → index → graph → vectors
# Designed for cron/launchd. Logs to ~/.code-rag/logs/
#
# Usage:
#   ./full_update.sh           # incremental (only changed repos)
#   ./full_update.sh --full    # force full rebuild

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="${CODE_RAG_HOME:-$HOME/.code-rag}"
LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"

# Resolve active profile
export ACTIVE_PROFILE="${ACTIVE_PROFILE:-$(cat "$BASE_DIR/.active_profile" 2>/dev/null || echo "example")}"
PROFILE_CONFIG="$BASE_DIR/profiles/$ACTIVE_PROFILE/config.json"
LEGACY_CONFIG="$BASE_DIR/config.json"
if [[ -f "$PROFILE_CONFIG" ]]; then
  CONFIG_FILE="$PROFILE_CONFIG"
elif [[ -f "$LEGACY_CONFIG" ]]; then
  CONFIG_FILE="$LEGACY_CONFIG"
else
  echo "No config found. Run: python3 setup_wizard.py"
  exit 1
fi
MODEL_KEY=$(jq -r '.embedding_model // "coderank"' "$CONFIG_FILE")

LOG_FILE="$LOG_DIR/update_$(date +%Y%m%d_%H%M%S).log"
LATEST_LOG="$LOG_DIR/latest.log"

# Tee to both log file and stdout
exec > >(tee "$LOG_FILE") 2>&1
ln -sf "$LOG_FILE" "$LATEST_LOG"

FULL_FLAG=""
SKIP_VECTORS="0"
for arg in "$@"; do
  case "$arg" in
    --full) FULL_FLAG="--full" ;;
    --skip-vectors) SKIP_VECTORS="1" ;;
  esac
done
STATE_FILE="$BASE_DIR/repo_state.json"
STATE_BEFORE="/tmp/code-rag-state-before.json"

echo "=========================================="
echo "Knowledge Base — Full Update"
echo "Profile: $ACTIVE_PROFILE"
echo "Model: $MODEL_KEY"
echo "Started: $(date)"
echo "Mode: ${FULL_FLAG:-incremental}"
echo "=========================================="

# Save state before to detect changes
if [[ -f "$STATE_FILE" ]]; then
  cp "$STATE_FILE" "$STATE_BEFORE"
else
  echo '{}' > "$STATE_BEFORE"
fi

# Step 1: Clone/fetch repos
echo ""
echo "[1/5] Fetching repos..."
"$SCRIPTS_DIR/clone_repos.sh" 2>&1 | tail -5
echo ""

# Step 2: Check what changed
CHANGED_COUNT=0
if [[ "$FULL_FLAG" != "--full" ]] && [[ -f "$STATE_BEFORE" ]]; then
  CHANGED_COUNT=$(python3 -c "
import json, os
before = json.load(open('$STATE_BEFORE'))
after = json.load(open('$STATE_FILE'))
changed = [r for r, sha in after.items() if r not in before or before[r] != sha]
# Also count repos never extracted
extracted_dir = os.environ.get('CODE_RAG_HOME', os.path.expanduser('~/.code-rag')) + '/extracted'
for repo in after:
    if not os.path.isdir(os.path.join(extracted_dir, repo)) and repo not in changed:
        changed.append(repo)
print(len(changed))
")
  echo "Changed repos since last run: $CHANGED_COUNT"
fi

if [[ "$FULL_FLAG" == "--full" ]] || [[ "$CHANGED_COUNT" -gt 0 ]]; then

  # Determine changed repo list for incremental mode
  REPOS_FLAG=""
  if [[ "$FULL_FLAG" != "--full" ]] && [[ -f "$STATE_BEFORE" ]]; then
    CHANGED_LIST=$(python3 -c "
import json, os
before = json.load(open('$STATE_BEFORE'))
after = json.load(open('$STATE_FILE'))
changed = [r for r, sha in after.items() if r not in before or before[r] != sha]
extracted_dir = os.environ.get('CODE_RAG_HOME', os.path.expanduser('~/.code-rag')) + '/extracted'
for repo in after:
    if not os.path.isdir(os.path.join(extracted_dir, repo)) and repo not in changed:
        changed.append(repo)
print(','.join(changed))
")
    if [[ -n "$CHANGED_LIST" ]]; then
      REPOS_FLAG="--repos=$CHANGED_LIST"
      echo "Incremental update for: $CHANGED_LIST"
    fi
  fi

  # Step 2: Extract artifacts
  echo ""
  echo "[2/5] Extracting artifacts..."
  python3 "$SCRIPTS_DIR/extract_artifacts.py" $REPOS_FLAG 2>&1 | tail -3

  # Step 3: Build FTS5 index
  echo ""
  echo "[3/5] Building search index..."
  python3 "$SCRIPTS_DIR/build_index.py" $REPOS_FLAG 2>&1 | tail -3

  # Step 4: Build dependency graph
  echo ""
  echo "[4/5] Building dependency graph..."
  python3 "$SCRIPTS_DIR/build_graph.py" 2>&1 | tail -3

  # Step 5: Build vector embeddings (batched to limit memory)
  # Skip vectors during daytime runs (--skip-vectors flag)
  if [[ "${SKIP_VECTORS:-}" == "1" ]]; then
    echo ""
    echo "[5/5] Skipping vector embeddings (daytime mode)"
  else
    BATCH_SIZE=30
    echo ""
    echo "[5/5] Building vector embeddings..."
    # If model is gemini, verify API is available; fallback to coderank if not
    if [[ "$MODEL_KEY" == "gemini" ]]; then
      if python3 -c "
from src.config import GEMINI_API_KEY
from google import genai
client = genai.Client(api_key=GEMINI_API_KEY)
client.models.embed_content(model='gemini-embedding-001', contents=['test'], config={'output_dimensionality': 768})
print('ok')
" 2>/dev/null | grep -q "ok"; then
        echo "  Gemini API available — building gemini vectors"
      else
        echo "  ⚠️ Gemini API unavailable — falling back to coderank (local)"
        MODEL_KEY="coderank"
      fi
    fi
    if [[ -n "$REPOS_FLAG" ]]; then
      # Split repos into batches to avoid OOM — each batch is a separate process
      REPO_LIST="${REPOS_FLAG#--repos=}"
      IFS=',' read -ra ALL_REPOS <<< "$REPO_LIST"
      TOTAL_REPOS=${#ALL_REPOS[@]}
      BATCH_NUM=0
      BATCH_TOTAL=$(( (TOTAL_REPOS + BATCH_SIZE - 1) / BATCH_SIZE ))
      for ((i=0; i<TOTAL_REPOS; i+=BATCH_SIZE)); do
        BATCH=("${ALL_REPOS[@]:i:BATCH_SIZE}")
        BATCH_STR=$(IFS=','; echo "${BATCH[*]}")
        BATCH_NUM=$((BATCH_NUM + 1))
        # Skip ANN reindex for all batches except the last one
        REINDEX_FLAG=""
        if [[ "$BATCH_NUM" -lt "$BATCH_TOTAL" ]]; then
          REINDEX_FLAG="--no-reindex"
        fi
        echo "  Batch $BATCH_NUM/$BATCH_TOTAL (${#BATCH[@]} repos)..."
        python3 "$SCRIPTS_DIR/build_vectors.py" --model="$MODEL_KEY" --repos="$BATCH_STR" $REINDEX_FLAG 2>&1 | tail -3
      done
    else
      python3 "$SCRIPTS_DIR/build_vectors.py" --model="$MODEL_KEY" --force 2>&1 | tail -3
    fi
  fi

  # Step 6: Post-build diagnostics
  echo ""
  echo "[6/6] Running diagnostics..."

  echo "  Benchmark (synthetic):"
  python3 "$SCRIPTS_DIR/benchmark_queries.py" 2>&1 | grep "Average\|PASS"

  echo "  Benchmark (real-world):"
  python3 "$SCRIPTS_DIR/benchmark_realworld.py" 2>&1 | grep "Average\|PASS"

  echo "  Blind spot detection:"
  python3 "$SCRIPTS_DIR/detect_blind_spots.py" 2>&1 | grep "Visibility\|Blind spots"

  echo ""
  echo "=========================================="
  echo "Update COMPLETE (${REPOS_FLAG:+incremental}${REPOS_FLAG:-full})"
  echo "Finished: $(date)"
  echo "=========================================="
else
  echo ""
  echo "No changes detected. Skipping rebuild."
  echo "Finished: $(date)"
fi

# Cleanup
rm -f "$STATE_BEFORE"

# Prune old logs (keep last 30)
ls -t "$LOG_DIR"/update_*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
