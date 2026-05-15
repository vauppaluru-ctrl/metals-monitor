#!/usr/bin/env bash
# uninstall_launch_agent.sh
# Removes the Metals Monitor LaunchAgent from macOS.
# Logs, state files, and backtest output are NOT deleted.

set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.local.metalsmonitor.plist"
LABEL="com.local.metalsmonitor"
UID_VAL=$(id -u)

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Metals Monitor — LaunchAgent Uninstaller"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Bootout the agent if it is loaded
echo "▶ Stopping and unloading LaunchAgent..."
if launchctl print "gui/$UID_VAL/$LABEL" &>/dev/null 2>&1; then
    launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
    echo "  ✓ Agent stopped and unloaded."
else
    echo "  Agent was not loaded (or already removed)."
fi

# Also try the plist-path form in case the above didn't work
if [ -f "$PLIST_DEST" ]; then
    launchctl bootout "gui/$UID_VAL" "$PLIST_DEST" 2>/dev/null || true
fi

# Remove the plist
if [ -f "$PLIST_DEST" ]; then
    rm "$PLIST_DEST"
    echo "  ✓ Plist removed: $PLIST_DEST"
else
    echo "  Plist not found (already removed?): $PLIST_DEST"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "  Logs, state files, and backtest output were NOT deleted."
echo "  To remove them manually:"
echo ""
echo "    rm -rf $SCRIPT_DIR/metals_monitor_logs"
echo "    rm -rf $SCRIPT_DIR/metals_monitor_state"
echo "    rm -rf $SCRIPT_DIR/metals_backtest_output"
echo "    rm -f  $SCRIPT_DIR/com.local.metalsmonitor.plist"
echo "    rm -rf $SCRIPT_DIR/.venv"
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Uninstall complete."
echo "═══════════════════════════════════════════════════════════════"
