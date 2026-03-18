#!/usr/bin/env bash
set -euo pipefail

# Install/update launchd job for automatic knowledge base updates.
# Usage: ./install_launchd.sh [--uninstall]

LABEL="com.code-rag.update"
BASE_DIR="${CODE_RAG_HOME:-$HOME/.code-rag}"
PLIST_SRC="$BASE_DIR/com.code-rag.update.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling $LABEL..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "Done. Auto-update disabled."
    exit 0
fi

echo "Installing $LABEL..."

# Replace __HOME__ placeholder with actual home directory
sed "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"

# Unload if already loaded
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

# Load
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo "Installed! Knowledge base will update every 4 hours."
echo "  Plist: $PLIST_DST"
echo "  Logs:  $BASE_DIR/logs/"
echo ""
echo "Commands:"
echo "  Check status:  launchctl print gui/$(id -u)/$LABEL"
echo "  Run now:       launchctl kickstart gui/$(id -u)/$LABEL"
echo "  Uninstall:     $0 --uninstall"
