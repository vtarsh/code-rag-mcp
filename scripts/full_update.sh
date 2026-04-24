set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="${CODE_RAG_HOME:-$HOME/.code-rag}"
LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"

LOCK_DIR="$LOG_DIR/full_update.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  OLD_PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[$(date -Iseconds)] full_update.sh already running (pid=$OLD_PID). Exiting." >&2
    exit 0
  fi
  echo "[$(date -Iseconds)] Stale lock (pid=${OLD_PID:-unknown} not running). Removing." >&2
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

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

if [[ -f "$STATE_FILE" ]]; then
  cp "$STATE_FILE" "$STATE_BEFORE"
else
  echo '{}' > "$STATE_BEFORE"
fi

echo ""
echo "[1/7] Fetching repos..."
"$SCRIPTS_DIR/clone_repos.sh" 2>&1 | tail -5
echo ""

CHANGED_COUNT=0
if [[ "$FULL_FLAG" != "--full" ]] && [[ -f "$STATE_BEFORE" ]]; then
  CHANGED_COUNT=$(python3 -c "
import json, os
before = json.load(open('$STATE_BEFORE'))
after = json.load(open('$STATE_FILE'))
changed = [r for r, sha in after.items() if r not in before or before[r] != sha]
extracted_dir = os.environ.get('CODE_RAG_HOME', os.path.expanduser('~/.code-rag')) + '/extracted'
for repo in after:
    if not os.path.isdir(os.path.join(extracted_dir, repo)) and repo not in changed:
        changed.append(repo)
print(len(changed))
")
  echo "Changed repos since last run: $CHANGED_COUNT"
fi

if [[ "$FULL_FLAG" == "--full" ]] || [[ "$CHANGED_COUNT" -gt 0 ]]; then

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

  echo ""
  echo "[2/7] Extracting artifacts..."
  python3 "$SCRIPTS_DIR/extract_artifacts.py" $REPOS_FLAG 2>&1 | tail -3

  echo ""
  echo "[3/7] Building search index..."
  python3 "$SCRIPTS_DIR/build_index.py" $REPOS_FLAG 2>&1 | tail -3

  echo ""
  echo "[4/7] Building dependency graph..."
  python3 "$SCRIPTS_DIR/build_graph.py" 2>&1 | tail -3

  if [[ "${SKIP_VECTORS:-}" == "1" ]]; then
    echo ""
    echo "[5/7] Skipping vector embeddings (daytime mode)"
  else
    BATCH_SIZE=30
    echo ""
    echo "[5/7] Building vector embeddings..."
    if [[ -n "$REPOS_FLAG" ]]; then
      REPO_LIST="${REPOS_FLAG#--repos=}"
      IFS=',' read -ra ALL_REPOS <<< "$REPO_LIST"
      TOTAL_REPOS=${#ALL_REPOS[@]}
      BATCH_NUM=0
      BATCH_TOTAL=$(( (TOTAL_REPOS + BATCH_SIZE - 1) / BATCH_SIZE ))
      for ((i=0; i<TOTAL_REPOS; i+=BATCH_SIZE)); do
        BATCH=("${ALL_REPOS[@]:i:BATCH_SIZE}")
        BATCH_STR=$(IFS=','; echo "${BATCH[*]}")
        BATCH_NUM=$((BATCH_NUM + 1))
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

    echo ""
    echo "[5b/7] Syncing doc vectors (missing + orphan cleanup)..."
    bash "$SCRIPTS_DIR/run_with_timeout.sh" 10800 \
        python3 "$SCRIPTS_DIR/embed_missing_vectors.py" --model=coderank 2>&1 | tail -10 || \
        echo "  ⚠️ sync failed or timed out — chunks/vectors will reconcile next run"

    echo ""
    echo "[5c/7] Building docs vector tower (nomic-embed-text-v1.5)..."
    DOCS_ARGS=()
    if [[ "$FULL_FLAG" == "--full" ]]; then
      DOCS_ARGS+=(--force)
    elif [[ -n "$REPOS_FLAG" ]]; then
      DOCS_ARGS+=("$REPOS_FLAG")
    fi
    python3 "$SCRIPTS_DIR/build_docs_vectors.py" "${DOCS_ARGS[@]}" 2>&1 | tail -6 || \
        echo "  ⚠️ docs tower build failed — router will fall back to code tower only"
  fi

  echo ""
  echo "[6/7] Building shadow types..."
  if [[ -f "$SCRIPTS_DIR/build_shadow_types.py" ]]; then
    PROFILE_PATH="$BASE_DIR/profiles/$ACTIVE_PROFILE"
    if [[ -d "$PROFILE_PATH/provider_types" ]]; then
      for yaml_file in "$PROFILE_PATH/provider_types"/*.yaml; do
        [[ -f "$yaml_file" ]] || continue
        provider=$(basename "$yaml_file" .yaml)
        python3 "$SCRIPTS_DIR/build_shadow_types.py" --provider "$provider" 2>&1 | tail -1
      done
    else
      echo "  Skipping — no provider_types/ directory in profile"
    fi
  else
    echo "  Skipping — build_shadow_types.py not found"
  fi

  echo ""
  echo "[7/7] Running diagnostics..."

  echo "  Benchmark (synthetic):"
  python3 "$SCRIPTS_DIR/benchmark_queries.py" 2>&1 | grep "Average\|PASS"

  echo "  Benchmark (real-world):"
  python3 "$SCRIPTS_DIR/benchmark_realworld.py" 2>&1 | grep "Average\|PASS"

  echo "  Blind spot detection:"
  python3 "$SCRIPTS_DIR/detect_blind_spots.py" 2>&1 | grep "Visibility\|Blind spots"

  echo ""
  echo "=========================================="
  echo "Update COMPLETE (${REPOS_FLAG:+incremental}${REPOS_FLAG:-full}, 7 steps)"
  echo "Finished: $(date)"
  echo "=========================================="
else
  echo ""
  echo "No changes detected. Skipping rebuild."
  echo "Finished: $(date)"
fi

echo ""
echo "[post] Regenerating repo facts + staleness report..."
python3 "$SCRIPTS_DIR/gen_repo_facts.py" 2>&1 | tail -3
python3 "$SCRIPTS_DIR/detect_doc_staleness.py" 2>&1 | tail -3

echo ""
echo "[post] Appending health check to history..."
HEALTH_LOG="$LOG_DIR/health_history.log"
{
  echo "=== $(date -Iseconds) ==="
  if curl -s --max-time 10 -X POST "http://localhost:8742/tool/health_check" \
       -H "Content-Type: application/json" -d '{}' 2>&1; then
    echo ""
  else
    echo "ERROR: daemon not responding at localhost:8742"
  fi
  echo ""
} >> "$HEALTH_LOG" 2>&1
tail -n 4000 "$HEALTH_LOG" > "$HEALTH_LOG.tmp" && mv "$HEALTH_LOG.tmp" "$HEALTH_LOG" 2>/dev/null || true

rm -f "$STATE_BEFORE"

ls -t "$LOG_DIR"/update_*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
