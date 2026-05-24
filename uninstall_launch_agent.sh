#!/usr/bin/env bash
# uninstall_launch_agent.sh
# Removes the Metals Monitor LaunchAgent from macOS.
# Logs, state files, and backtest output are NOT deleted.

set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.local.metalsmonitor.plist"
LABEL="com.local.metalsmonitor"
WEB_PLIST_DEST="$HOME/Library/LaunchAgents/com.local.metalswebserver.plist"
WEB_LABEL="com.local.metalswebserver"
UID_VAL=$(id -u)

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Metals Monitor — LaunchAgent Uninstaller"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Bootout the monitor agent
echo "▶ Stopping and unloading monitor LaunchAgent..."
if launchctl print "gui/$UID_VAL/$LABEL" &>/dev/null 2>&1; then
    launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
    echo "  ✓ Monitor agent stopped and unloaded."
else
    echo "  Monitor agent was not loaded (or already removed)."
fi
if [ -f "$PLIST_DEST" ]; then
    launchctl bootout "gui/$UID_VAL" "$PLIST_DEST" 2>/dev/null || true
    rm "$PLIST_DEST"
    echo "  ✓ Plist removed: $PLIST_DEST"
else
    echo "  Plist not found (already removed?): $PLIST_DEST"
fi

# Bootout the web server agent
echo ""
echo "▶ Stopping and unloading web server LaunchAgent..."
if launchctl print "gui/$UID_VAL/$WEB_LABEL" &>/dev/null 2>&1; then
    launchctl bootout "gui/$UID_VAL/$WEB_LABEL" 2>/dev/null || true
    echo "  ✓ Web server agent stopped and unloaded."
else
    echo "  Web server agent was not loaded (or already removed)."
fi
if [ -f "$WEB_PLIST_DEST" ]; then
    launchctl bootout "gui/$UID_VAL" "$WEB_PLIST_DEST" 2>/dev/null || true
    rm "$WEB_PLIST_DEST"
    echo "  ✓ Plist removed: $WEB_PLIST_DEST"
else
    echo "  Plist not found (already removed?): $WEB_PLIST_DEST"
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
echo "    rm -f  $SCRIPT_DIR/com.local.metalswebserver.plist"
echo "    rm -rf $SCRIPT_DIR/.venv"
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Uninstall complete."
echo "═══════════════════════════════════════════════════════════════"
