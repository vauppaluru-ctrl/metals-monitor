#!/usr/bin/env bash
# install_launch_agent.sh
# Sets up the Metals Monitor LaunchAgent on macOS.
# Run from any directory; the script detects its own location as the project root.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# Find a Python 3 that can create a working venv (ensurepip must work).
# Homebrew Python 3.12/3.14 on some macOS versions have a broken pyexpat that
# prevents ensurepip from running; Apple's /usr/bin/python3 is the safe fallback.
# ──────────────────────────────────────────────────────────────────────────────
find_python() {
    for py in python3.12 python3.11 python3.10 /usr/bin/python3 python3; do
        if command -v "$py" &>/dev/null; then
            local tmpdir found=0
            tmpdir="$(mktemp -d)"
            # Guarantee tmpdir cleanup regardless of how this iteration exits
            trap 'rm -rf "$tmpdir" 2>/dev/null || true' RETURN
            if "$py" -m venv "$tmpdir" 2>/dev/null; then
                local venv_py=""
                for candidate in "$tmpdir/bin/python3" "$tmpdir/bin/python"; do
                    if [ -f "$candidate" ]; then
                        venv_py="$candidate"
                        break
                    fi
                done
                if [ -n "$venv_py" ] && "$venv_py" -m pip --version &>/dev/null 2>&1; then
                    found=1
                fi
            fi
            trap - RETURN
            rm -rf "$tmpdir" 2>/dev/null || true
            if [ "$found" -eq 1 ]; then
                command -v "$py"
                return 0
            fi
        fi
    done
    echo ""
    return 1
}

PYTHON3_BIN="$(find_python)" || true
if [ -z "$PYTHON3_BIN" ]; then
    echo "ERROR: Could not find a Python 3 that can create a working venv."
    echo "       Try: brew install python@3.12  or install Python from python.org"
    exit 1
fi
echo "  Using Python: $PYTHON3_BIN  ($(${PYTHON3_BIN} --version))"

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_PATH="$VENV_DIR/bin/python3"
SCRIPT_PATH="$PROJECT_DIR/metals_live_monitor.py"
WEB_SERVER_PATH="$PROJECT_DIR/metals_web_server.py"
LOG_DIR="$PROJECT_DIR/metals_monitor_logs"
PLIST_TEMPLATE="$PROJECT_DIR/com.local.metalsmonitor.plist.template"
PLIST_GENERATED="$PROJECT_DIR/com.local.metalsmonitor.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.local.metalsmonitor.plist"
LABEL="com.local.metalsmonitor"
WEB_PLIST_TEMPLATE="$PROJECT_DIR/com.local.metalswebserver.plist.template"
WEB_PLIST_GENERATED="$PROJECT_DIR/com.local.metalswebserver.plist"
WEB_PLIST_DEST="$HOME/Library/LaunchAgents/com.local.metalswebserver.plist"
WEB_LABEL="com.local.metalswebserver"
UID_VAL=$(id -u)

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Metals Monitor — LaunchAgent Installer"
echo "═══════════════════════════════════════════════════════════════"
echo "  Project dir  : $PROJECT_DIR"
echo "  Python venv  : $VENV_DIR"
echo "  Monitor plist: $PLIST_DEST"
echo "  Web server   : $WEB_PLIST_DEST  (http://localhost:8747)"
echo ""

# ── Step 1: Create required directories ──────────────────────────────────────
echo "▶ Step 1/7  Creating required directories..."
mkdir -p "$LOG_DIR"
mkdir -p "$PROJECT_DIR/metals_monitor_state"
mkdir -p "$PROJECT_DIR/metals_backtest_output"
mkdir -p "$HOME/Library/LaunchAgents"
echo "  ✓ Directories ready."
echo ""

# ── Step 2: Virtual environment ───────────────────────────────────────────────
echo "▶ Step 2/7  Setting up Python virtual environment..."
if [ ! -f "$PYTHON_PATH" ]; then
    "$PYTHON3_BIN" -m venv "$VENV_DIR"
    echo "  ✓ Created new venv at .venv  ($($PYTHON_PATH --version))"
else
    echo "  ✓ venv already exists at .venv"
fi

echo "  Installing / upgrading packages from requirements.txt ..."
"$PYTHON_PATH" -m pip install --upgrade pip --quiet
"$PYTHON_PATH" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet
echo "  ✓ Packages installed."
echo ""

# ── Step 3: Generate plists from templates ────────────────────────────────────
echo "▶ Step 3/7  Generating LaunchAgent plists..."

# Escape & and | so sed does not misinterpret them in the replacement strings.
# & means "insert matched text" in sed replacements; | is our delimiter.
_sed_escape() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }

PROJECT_DIR_ESC="$(_sed_escape "$PROJECT_DIR")"
PYTHON_PATH_ESC="$(_sed_escape "$PYTHON_PATH")"
SCRIPT_PATH_ESC="$(_sed_escape "$SCRIPT_PATH")"
WEB_SERVER_PATH_ESC="$(_sed_escape "$WEB_SERVER_PATH")"
LOG_DIR_ESC="$(_sed_escape "$LOG_DIR")"

if [ ! -f "$PLIST_TEMPLATE" ]; then
    echo "  ERROR: Template not found: $PLIST_TEMPLATE"
    exit 1
fi
sed \
    -e "s|{{PROJECT_DIR}}|${PROJECT_DIR_ESC}|g"  \
    -e "s|{{PYTHON_PATH}}|${PYTHON_PATH_ESC}|g"  \
    -e "s|{{SCRIPT_PATH}}|${SCRIPT_PATH_ESC}|g"  \
    -e "s|{{LOG_DIR}}|${LOG_DIR_ESC}|g"          \
    "$PLIST_TEMPLATE" > "$PLIST_GENERATED"
cp "$PLIST_GENERATED" "$PLIST_DEST"
echo "  ✓ Monitor plist  →  $PLIST_DEST"

if [ ! -f "$WEB_PLIST_TEMPLATE" ]; then
    echo "  ERROR: Template not found: $WEB_PLIST_TEMPLATE"
    exit 1
fi
sed \
    -e "s|{{PROJECT_DIR}}|${PROJECT_DIR_ESC}|g"         \
    -e "s|{{PYTHON_PATH}}|${PYTHON_PATH_ESC}|g"         \
    -e "s|{{WEB_SERVER_PATH}}|${WEB_SERVER_PATH_ESC}|g" \
    -e "s|{{LOG_DIR}}|${LOG_DIR_ESC}|g"                 \
    "$WEB_PLIST_TEMPLATE" > "$WEB_PLIST_GENERATED"
cp "$WEB_PLIST_GENERATED" "$WEB_PLIST_DEST"
echo "  ✓ Web server plist  →  $WEB_PLIST_DEST"
echo ""

# ── Step 4: Unload existing agents if already loaded ──────────────────────────
echo "▶ Step 4/7  Checking for existing LaunchAgents..."
if launchctl print "gui/$UID_VAL/$LABEL" &>/dev/null 2>&1; then
    echo "  Found running monitor agent — stopping..."
    launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
    sleep 1
    echo "  ✓ Monitor agent stopped."
else
    echo "  No existing monitor agent found."
fi
if launchctl print "gui/$UID_VAL/$WEB_LABEL" &>/dev/null 2>&1; then
    echo "  Found running web server agent — stopping..."
    launchctl bootout "gui/$UID_VAL/$WEB_LABEL" 2>/dev/null || true
    sleep 1
    echo "  ✓ Web server agent stopped."
else
    echo "  No existing web server agent found."
fi
echo ""

# ── Step 5: Bootstrap and enable the monitor agent ───────────────────────────
echo "▶ Step 5/7  Loading monitor LaunchAgent..."
launchctl bootstrap "gui/$UID_VAL" "$PLIST_DEST"
launchctl enable "gui/$UID_VAL/$LABEL"
launchctl kickstart -k "gui/$UID_VAL/$LABEL"
echo "  ✓ Monitor agent bootstrapped and kickstarted."
echo ""

# ── Step 6: Bootstrap and enable the web server agent ────────────────────────
echo "▶ Step 6/7  Loading web server LaunchAgent..."
launchctl bootstrap "gui/$UID_VAL" "$WEB_PLIST_DEST"
launchctl enable "gui/$UID_VAL/$WEB_LABEL"
launchctl kickstart -k "gui/$UID_VAL/$WEB_LABEL"
echo "  ✓ Web server agent bootstrapped and kickstarted."
echo "  ✓ Dashboard will be available at http://localhost:8747"
echo ""

# ── Step 7: Immediate test execution ──────────────────────────────────────────
echo "▶ Step 7/7  Running immediate test execution of metals_live_monitor.py..."
echo "───────────────────────────────────────────────────────────────"
"$PYTHON_PATH" "$SCRIPT_PATH"
echo "───────────────────────────────────────────────────────────────"
echo "  ✓ Test run complete."
echo ""

# ── Done ─────────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  Installation complete!"
echo ""
echo "  Both agents will start automatically on every login:"
echo "    • Monitor  — runs hourly, sends macOS alerts on signals"
echo "    • Web server — persistent, dashboard at http://localhost:8747"
echo ""
echo "  ── Useful commands ────────────────────────────────────────"
echo ""
echo "  Check agent status:"
echo "    launchctl print gui/$UID_VAL/$LABEL"
echo "    launchctl print gui/$UID_VAL/$WEB_LABEL"
echo ""
echo "  View logs:"
echo "    tail -f $LOG_DIR/metals_monitor.log"
echo "    tail -f $LOG_DIR/metals_webserver_stdout.log"
echo "    tail -f $LOG_DIR/metals_webserver_stderr.log"
echo ""
echo "  Run live monitor manually:"
echo "    $PYTHON_PATH $SCRIPT_PATH"
echo ""
echo "  Run backtest:"
echo "    cd $PROJECT_DIR && $PYTHON_PATH metals_backtest.py"
echo ""
echo "  Uninstall:"
echo "    bash $PROJECT_DIR/uninstall_launch_agent.sh"
echo "═══════════════════════════════════════════════════════════════"
