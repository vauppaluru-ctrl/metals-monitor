# Metals Monitor

A fully local macOS system for monitoring Gold, Silver, and Copper signals using free
public market data (yfinance). Runs hourly via macOS LaunchAgent without requiring
Perplexity, Claude, OpenAI, or any paid API after installation.

---

## What this project does

1. **Backtest** (`metals_backtest.py`): Runs a one-year first-pass event-study backtest
   (2025-05-15 → 2026-05-15) for Gold (GLD), Silver (SLV), and Copper (CPER) using four
   proxy signal categories. Produces charts, CSVs, and a Markdown report in
   `metals_backtest_output/`.

2. **Live Monitor** (`metals_live_monitor.py`): Evaluates the same signal logic on the
   most recent daily data, sends macOS native notifications when a 2-of-4 cluster trigger
   fires, writes every run to `metals_monitor_logs/metals_monitor.log`, and stores
   cooldown state in `metals_monitor_state/state.json`.

3. **LaunchAgent** (`install_launch_agent.sh`): Installs the monitor as a macOS user-level
   scheduled job that runs at login and every hour while the Mac is awake. No terminal
   window needed after installation.

---

## Why local execution — no Perplexity credits consumed

The hourly LaunchAgent job runs `metals_live_monitor.py` directly via the project's Python
virtual environment. It:

- Downloads OHLCV data from yfinance (free, unofficial).
- Computes all signals locally in Python (numpy / pandas).
- Writes logs and state to local files.
- Sends notifications via macOS `osascript` (native, free).

No Perplexity, Claude, or OpenAI call is made from the scheduled job. Perplexity scheduled
tasks are entirely bypassed.

---

## Project structure

```
metals_monitor/
├── metals_backtest.py              # One-year backtest
├── metals_live_monitor.py          # Hourly live monitor
├── requirements.txt                # Python dependencies
├── install_launch_agent.sh         # Install the LaunchAgent
├── uninstall_launch_agent.sh       # Remove the LaunchAgent
├── com.local.metalsmonitor.plist.template  # Plist template (uses {{placeholders}})
├── com.local.metalsmonitor.plist   # Generated plist (after install)
├── README.md
├── .venv/                          # Created by install script
├── metals_backtest_output/         # Backtest results
│   ├── metals-monitor-backtest.md
│   ├── metals_backtest_summary.csv
│   ├── metals_backtest_events.csv
│   ├── metals_proxy_performance.png
│   ├── metals_event_counts.png
│   └── metals_forward_returns.png
├── metals_monitor_logs/            # Logs (not deleted by uninstall)
│   ├── metals_monitor.log          # Written by Python logging
│   ├── metals_monitor_stdout.log   # Written by launchd
│   └── metals_monitor_stderr.log   # Written by launchd
└── metals_monitor_state/           # Cooldown state (not deleted by uninstall)
    └── state.json
```

---

## Quick start

### 1. Set up the virtual environment manually

```bash
cd metals_monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run the backtest

```bash
python metals_backtest.py
```

Outputs appear in `metals_backtest_output/`.

### 3. Run the live monitor once (manual test)

```bash
python metals_live_monitor.py
```

### 4. Install the LaunchAgent (automated hourly job)

```bash
bash install_launch_agent.sh
```

This creates the venv, installs packages, generates the plist, registers the LaunchAgent,
and runs one immediate test.

### 5. Uninstall the LaunchAgent

```bash
bash uninstall_launch_agent.sh
```

Logs, state, and backtest outputs are preserved.

---

## How to view logs

```bash
# Live monitor log (written by Python logging)
tail -f metals_monitor_logs/metals_monitor.log

# LaunchAgent captured stdout/stderr (written by launchd)
tail -f metals_monitor_logs/metals_monitor_stdout.log
tail -f metals_monitor_logs/metals_monitor_stderr.log
```

---

## How to check whether the job is loaded

```bash
launchctl print gui/$(id -u)/com.local.metalsmonitor
```

You should see `state = running` or `state = waiting`.

---

## How to run the backtest manually

```bash
cd metals_monitor
source .venv/bin/activate
python metals_backtest.py
```

Or with the full venv path (no activation needed):

```bash
.venv/bin/python metals_backtest.py
```

---

## How to modify the hourly interval

Edit the generated plist at `~/Library/LaunchAgents/com.local.metalsmonitor.plist`
(or edit the template before running the install script), changing:

```xml
<key>StartInterval</key>
<integer>3600</integer>
```

to your desired interval in seconds (e.g., `1800` for 30 minutes). Then reload:

```bash
launchctl bootout gui/$(id -u)/com.local.metalsmonitor
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.metalsmonitor.plist
```

---

## Signal logic overview

Each metal (Gold, Silver, Copper) is evaluated **independently** using its primary ETF:

| Metal  | Primary ETF | Secondary ETF |
|--------|-------------|---------------|
| Gold   | GLD         | IAU           |
| Silver | SLV         | SIVR          |
| Copper | CPER        | COPX          |

Four proxy signal categories are computed daily:

| Signal | Proxy for | Bullish condition | Bearish condition |
|--------|-----------|-------------------|-------------------|
| `futures_curve_proxy` | Curve tightness | 3d return > 1.25×σ×√3 | 3d return < −1.25×σ×√3 |
| `etf_pressure_proxy` | ETF flow pressure | vol z-score > 1.5 AND 1d return positive | vol z-score > 1.5 AND 1d return negative |
| `physical_tightness_proxy` | Physical premium | Close above 20d high AND range elevated | Close below 20d low AND range elevated |
| `demand_expectations_proxy` | Demand repricing | 10d MA > 30d MA AND close > 50d MA AND 20d return > 0 | Opposite |

A **cluster trigger** fires when ≥ 2 categories align in the same direction for the same
metal. Cross-metal confirmation is **never** used.

---

## macOS sleep and LaunchAgent timing

macOS suspends all jobs while the laptop is asleep. The LaunchAgent:
- Runs immediately when you log in (`RunAtLoad = true`).
- Runs every hour (`StartInterval = 3600`) while the Mac is awake.
- Resumes on wake — it will catch up on the next scheduled interval.
- Does **not** run accumulated "missed" intervals when the Mac wakes from a long sleep.

---

## Notification permissions

macOS may require you to grant notification permission to the terminal or Python process
the first time it calls `osascript`. If notifications do not appear:

1. Open **System Settings → Notifications**.
2. Allow notifications for **Script Editor** and/or **Terminal** / **iTerm2**.
3. If running via LaunchAgent, you may also need to allow `osascript` itself.

If `osascript` fails, the alert is written to the log instead and the job does not crash.

---

## yfinance disclaimer

yfinance provides **unofficial, free, delayed** data sourced from Yahoo Finance. It:

- Is not guaranteed to be available, accurate, or timely.
- May have gaps, stale prices, or corporate-action errors.
- Is not a substitute for exchange-direct or Bloomberg/Refinitiv feeds.
- Should be treated as indicative only, not as a basis for real financial decisions.

---

## Proxy signal disclaimer

The live monitor and backtest use **OHLCV proxies only**. They are **not equivalent** to:

- CME futures term structure (requires CME or paid data).
- ETF creation/redemption flows (requires ETF issuer or AP data).
- Physical gold/silver/copper premiums (requires LBMA, LME, or OTC data).
- News / sentiment (requires real-time news feeds).

The four signal categories are statistical proxies that approximate the *intent* of those
factors using publicly available OHLCV data.

---

## Optional future public-source enrichment (disabled by default)

The codebase is structured so that additional data sources can be added to
`metals_live_monitor.py` without breaking the core logic. Potential free / public sources:

- CME public gold/silver/copper futures pages
- ETF issuer pages for GLD, IAU, SLV, SIVR, CPER, COPX
- World Gold Council ETF flow data
- LBMA precious metals data
- LME Copper public data
- RSS/news feeds from Reuters, Kitco, Mining.com, S&P Global PMI, The Silver Institute, ICSG

All are disabled by default to keep the hourly job robust and free of API dependencies.

---

## Quick reference commands

```bash
# Navigate to project
cd metals_monitor

# Manual venv setup
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Run backtest
python metals_backtest.py

# Run live monitor (manual test)
python metals_live_monitor.py

# Install LaunchAgent
bash install_launch_agent.sh

# Uninstall LaunchAgent
bash uninstall_launch_agent.sh

# View live log
tail -f metals_monitor_logs/metals_monitor.log

# Check agent status
launchctl print gui/$(id -u)/com.local.metalsmonitor
```
