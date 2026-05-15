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
LOG_DIR="$PROJECT_DIR/metals_monitor_logs"
PLIST_TEMPLATE="$PROJECT_DIR/com.local.metalsmonitor.plist.template"
PLIST_GENERATED="$PROJECT_DIR/com.local.metalsmonitor.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.local.metalsmonitor.plist"
LABEL="com.local.metalsmonitor"
UID_VAL=$(id -u)

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Metals Monitor — LaunchAgent Installer"
echo "═══════════════════════════════════════════════════════════════"
echo "  Project dir : $PROJECT_DIR"
echo "  Python venv : $VENV_DIR"
echo "  Plist dest  : $PLIST_DEST"
echo ""

# ── Step 1: Create required directories ──────────────────────────────────────
echo "▶ Step 1/6  Creating required directories..."
mkdir -p "$LOG_DIR"
mkdir -p "$PROJECT_DIR/metals_monitor_state"
mkdir -p "$PROJECT_DIR/metals_backtest_output"
mkdir -p "$HOME/Library/LaunchAgents"
echo "  ✓ Directories ready."
echo ""

# ── Step 2: Virtual environment ───────────────────────────────────────────────
echo "▶ Step 2/6  Setting up Python virtual environment..."
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

# ── Step 3: Generate plist from template ──────────────────────────────────────
echo "▶ Step 3/6  Generating LaunchAgent plist..."
if [ ! -f "$PLIST_TEMPLATE" ]; then
    echo "  ERROR: Template not found: $PLIST_TEMPLATE"
    exit 1
fi

# Escape & and | so sed does not misinterpret them in the replacement strings.
# & means "insert matched text" in sed replacements; | is our delimiter.
_sed_escape() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }

PROJECT_DIR_ESC="$(_sed_escape "$PROJECT_DIR")"
PYTHON_PATH_ESC="$(_sed_escape "$PYTHON_PATH")"
SCRIPT_PATH_ESC="$(_sed_escape "$SCRIPT_PATH")"
LOG_DIR_ESC="$(_sed_escape "$LOG_DIR")"

sed \
    -e "s|{{PROJECT_DIR}}|${PROJECT_DIR_ESC}|g"  \
    -e "s|{{PYTHON_PATH}}|${PYTHON_PATH_ESC}|g"  \
    -e "s|{{SCRIPT_PATH}}|${SCRIPT_PATH_ESC}|g"  \
    -e "s|{{LOG_DIR}}|${LOG_DIR_ESC}|g"          \
    "$PLIST_TEMPLATE" > "$PLIST_GENERATED"

cp "$PLIST_GENERATED" "$PLIST_DEST"
echo "  ✓ Plist generated  →  $PLIST_DEST"
echo ""

# ── Step 4: Unload existing agent if already loaded ───────────────────────────
echo "▶ Step 4/6  Checking for an existing LaunchAgent..."
if launchctl print "gui/$UID_VAL/$LABEL" &>/dev/null 2>&1; then
    echo "  Found a running agent — stopping and unloading..."
    launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
    sleep 1
    echo "  ✓ Existing agent stopped."
else
    echo "  No existing agent found."
fi
echo ""

# ── Step 5: Bootstrap, enable, and kick off the agent ────────────────────────
echo "▶ Step 5/6  Loading LaunchAgent..."
launchctl bootstrap "gui/$UID_VAL" "$PLIST_DEST"
echo "  ✓ Bootstrapped."

launchctl enable "gui/$UID_VAL/$LABEL"
echo "  ✓ Enabled (will survive reboots / log-outs)."

launchctl kickstart -k "gui/$UID_VAL/$LABEL"
echo "  ✓ Kickstarted."
echo ""

# ── Step 6: Immediate test execution ──────────────────────────────────────────
echo "▶ Step 6/6  Running immediate test execution of metals_live_monitor.py..."
echo "───────────────────────────────────────────────────────────────"
"$PYTHON_PATH" "$SCRIPT_PATH"
echo "───────────────────────────────────────────────────────────────"
echo "  ✓ Test run complete."
echo ""

# ── Done ─────────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  Installation complete!"
echo ""
echo "  The monitor will now run automatically every hour while your"
echo "  Mac is awake, and on every login. No terminal window needed."
echo ""
echo "  ── Useful commands ────────────────────────────────────────"
echo ""
echo "  Check agent status:"
echo "    launchctl print gui/$UID_VAL/$LABEL"
echo ""
echo "  View live monitor log:"
echo "    tail -f $LOG_DIR/metals_monitor.log"
echo ""
echo "  View LaunchAgent stdout / stderr:"
echo "    tail -f $LOG_DIR/metals_monitor_stdout.log"
echo "    tail -f $LOG_DIR/metals_monitor_stderr.log"
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
