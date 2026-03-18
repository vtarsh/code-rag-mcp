#!/usr/bin/env bash
set -euo pipefail

# Clone all repos from a GitHub org (shallow)
# Usage: ./clone_repos.sh [--dry-run]

BASE_DIR="${CODE_RAG_HOME:-$HOME/.code-rag}"

# Resolve active profile config
PROFILE="${ACTIVE_PROFILE:-$(cat "$BASE_DIR/.active_profile" 2>/dev/null || echo "example")}"
PROFILE_CONFIG="$BASE_DIR/profiles/$PROFILE/config.json"
LEGACY_CONFIG="$BASE_DIR/config.json"

if [[ -f "$PROFILE_CONFIG" ]]; then
  CONFIG_FILE="$PROFILE_CONFIG"
elif [[ -f "$LEGACY_CONFIG" ]]; then
  CONFIG_FILE="$LEGACY_CONFIG"
else
  echo "No config found. Run: python3 setup_wizard.py"
  exit 1
fi

ORG=$(jq -r '.org // "my-org"' "$CONFIG_FILE")

RAW_DIR="$BASE_DIR/raw"
LOG_FILE="$BASE_DIR/clone_log.json"
STATE_FILE="$BASE_DIR/repo_state.json"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

mkdir -p "$RAW_DIR"

echo "Fetching repo list from $ORG..."

# Determine API endpoint: org, user, or authenticated user
CURRENT_USER=$(gh api user --jq '.login' 2>/dev/null || echo "")

if gh api "orgs/$ORG/repos?per_page=1" --jq 'length' >/dev/null 2>&1; then
  API_PATH="orgs/$ORG/repos?per_page=100&type=all"
  echo "  Fetching from org: $ORG"
elif [[ "$ORG" == "$CURRENT_USER" ]]; then
  # Authenticated user — use /user/repos to include private repos
  API_PATH="user/repos?per_page=100&type=owner&sort=updated"
  echo "  Fetching own repos for: $ORG (authenticated)"
else
  API_PATH="users/$ORG/repos?per_page=100&type=owner"
  echo "  Fetching from user: $ORG (public repos only)"
fi

# Fetch all repos (non-archived, non-disabled) with pagination
REPOS=$(gh api --paginate "$API_PATH" \
  --jq '.[] | select(.archived == false and .disabled == false) | {name: .name, clone_url: .clone_url, default_branch: .default_branch, size: .size}' \
  2>&1)

REPO_COUNT=$(echo "$REPOS" | jq -s 'length')
echo "Found $REPO_COUNT active repos"

if [[ "$REPO_COUNT" -eq 0 ]]; then
  echo ""
  echo "No repos found for '$ORG'."
  echo "Possible causes:"
  echo "  - Org/user name is misspelled"
  echo "  - No public repos (for other users)"
  echo "  - gh auth: currently logged in as '$CURRENT_USER'"
  echo "  - Switch account with: gh auth switch"
  exit 1
fi

if $DRY_RUN; then
  echo "[DRY RUN] Would clone $REPO_COUNT repos to $RAW_DIR"
  echo "$REPOS" | jq -s '[.[].name]' | head -30
  exit 0
fi

# Initialize state file if not exists
if [[ ! -f "$STATE_FILE" ]]; then
  echo '{}' > "$STATE_FILE"
fi

CLONED=0
SKIPPED=0
ERRORS=0
ERROR_REPOS=""

echo "$REPOS" | jq -c '.' | while read -r repo; do
  NAME=$(echo "$repo" | jq -r '.name')
  CLONE_URL=$(echo "$repo" | jq -r '.clone_url')
  BRANCH=$(echo "$repo" | jq -r '.default_branch')
  REPO_DIR="$RAW_DIR/$NAME"

  if [[ -d "$REPO_DIR/.git" ]]; then
    # Already cloned — fetch latest
    echo "  ↻ Updating $NAME..."
    if git -C "$REPO_DIR" fetch --depth=1 origin "$BRANCH" 2>/dev/null; then
      git -C "$REPO_DIR" reset --hard "origin/$BRANCH" --quiet 2>/dev/null || true
      SHA=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
      # Update state
      TMP=$(mktemp)
      jq --arg name "$NAME" --arg sha "$SHA" '.[$name] = $sha' "$STATE_FILE" > "$TMP" && mv "$TMP" "$STATE_FILE"
    else
      echo "  ✗ Failed to update $NAME"
    fi
  else
    # Fresh clone
    echo "  ↓ Cloning $NAME..."
    if git clone --depth=1 --single-branch --branch "$BRANCH" "$CLONE_URL" "$REPO_DIR" 2>/dev/null; then
      SHA=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
      TMP=$(mktemp)
      jq --arg name "$NAME" --arg sha "$SHA" '.[$name] = $sha' "$STATE_FILE" > "$TMP" && mv "$TMP" "$STATE_FILE"
    else
      echo "  ✗ Failed to clone $NAME"
    fi
  fi
done

# Summary
TOTAL_DIRS=$(ls -d "$RAW_DIR"/*/.git 2>/dev/null | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh "$RAW_DIR" 2>/dev/null | cut -f1)

echo ""
echo "=== Clone Summary ==="
echo "Repos in org:    $REPO_COUNT"
echo "Repos on disk:   $TOTAL_DIRS"
echo "Total size:      $TOTAL_SIZE"
echo "State file:      $STATE_FILE"
echo "===================="

# Write log
jq -n \
  --arg date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg org "$ORG" \
  --argjson repo_count "$REPO_COUNT" \
  --argjson on_disk "$TOTAL_DIRS" \
  --arg total_size "$TOTAL_SIZE" \
  '{date: $date, org: $org, repo_count: $repo_count, on_disk: $on_disk, total_size: $total_size}' \
  > "$LOG_FILE"

echo "Log written to $LOG_FILE"
