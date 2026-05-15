# Metals Monitor — User Guide

A local macOS system that watches Gold, Silver, and Copper using free public data and fires native notifications when a signal cluster forms. No paid APIs. No cloud dependency. Optionally runs as a Docker container for cloud deployment.

---

## Table of contents

1. [What this does](#1-what-this-does)
2. [Quick start](#2-quick-start)
3. [Understanding the signals](#3-understanding-the-signals)
4. [Reading the dashboard](#4-reading-the-dashboard)
5. [Notifications](#5-notifications)
6. [Running the backtest](#6-running-the-backtest)
7. [Logs and state files](#7-logs-and-state-files)
8. [Changing the schedule](#8-changing-the-schedule)
9. [Docker / cloud deployment](#9-docker--cloud-deployment)
10. [Uninstalling](#10-uninstalling)
11. [Troubleshooting](#11-troubleshooting)
12. [Disclaimers](#12-disclaimers)

---

## 1. What this does

The system has three components:

| Component | What it does | When it runs |
|---|---|---|
| **Live Monitor** (`metals_live_monitor.py`) | Downloads recent OHLCV data for GLD, SLV, CPER; computes 4 proxy signals per metal; fires a macOS notification if ≥2 signals align | Hourly via LaunchAgent (or Docker scheduler) |
| **Web Dashboard** (`metals_web_server.py`) | FastAPI server at `http://localhost:8080` showing signal cards, alert history, news, and logs in real time | Manual start or Docker always-on |
| **Backtest** (`metals_backtest.py`) | One-year event study (2025-05-15 → 2026-05-15) to validate the signal logic on historical data | Manual only |

The three metals are evaluated **independently**. No cross-metal inference is ever made.

---

## 2. Quick start

### macOS — LaunchAgent (recommended for local use)

```bash
# One command installs everything and runs a test
cd /path/to/metals_monitor
bash install_launch_agent.sh
```

This will:
1. Detect a working Python 3 and create `.venv`
2. Install all Python packages
3. Generate and load the `com.local.metalsmonitor` LaunchAgent
4. Run one immediate test cycle

The monitor will now run **every hour while your Mac is awake** and at every login. No terminal window needed.

### Start the web dashboard

```bash
.venv/bin/uvicorn metals_web_server:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080` in your browser and bookmark it.

### Docker (container / cloud)

```bash
cd metals_monitor
docker compose up --build -d
```

Open `http://localhost:8080`. The container runs the scheduler internally — no LaunchAgent needed.

---

## 3. Understanding the signals

Each metal is scored on four **proxy** signal categories derived entirely from OHLCV data:

### Signal categories

| Signal | What it proxies | Bullish condition | Bearish condition |
|---|---|---|---|
| **Futures Curve** | Backwardation / contango pressure | 3-day return > 1.25σ√3 | 3-day return < −1.25σ√3 |
| **ETF Pressure** | ETF creation/redemption flows | Volume z-score > 1.5 AND 1d return positive | Volume z-score > 1.5 AND 1d return negative |
| **Physical Tightness** | Physical premium vs spot | Close > prior 20d high AND range elevated | Close < prior 20d low AND range elevated |
| **Demand Expectations** | Demand repricing via trend | MA10 > MA30 AND Close > MA50 AND 20d return > 0 | Opposite |

Volatility (σ) is the rolling 60-day daily-return standard deviation. All thresholds use the same σ to adapt to each metal's current regime.

### Cluster trigger

A **cluster trigger** fires when **≥ 2 of 4 categories align** in the same direction (all bullish or all bearish) for the same metal. This reduces false positives vs. any single signal.

### Cooldown

After a trigger fires, a **3-calendar-day cooldown** suppresses further alerts in the same direction for that metal. This prevents repeated pinging during a single sustained move. Cooldown state is stored in `metals_monitor_state/state.json`.

### What this is NOT

These are statistical proxies. They are not:
- CME futures term structure
- ETF creation/redemption flow data
- Physical spot premiums (LBMA/LME)
- News or sentiment

Treat all signals as indicative only. See full disclaimer in [Section 12](#12-disclaimers).

---

## 4. Reading the dashboard

Open `http://localhost:8080` after starting the web server.

### Signal cards (top section)
One card per metal. Each card shows:
- **Current close price** and data date
- **Four signal badges**: `bullish` (green), `bearish` (red), or `neutral` (grey)
- **Cluster badge**: confirms whether a cluster has formed and in which direction

### Recent alerts table
All cluster triggers that have fired, most recent first. Pulled from `state.json` — persists across restarts. Columns: date, metal, ticker, direction, signals that fired, close price at trigger.

### News tab
Recent headlines from free RSS feeds (Mining.com, King World News, TF Metals Report, GoldBroker) filtered by metal-specific keywords. Updates each time the monitor runs. Select a metal with the Gold / Silver / Copper tab buttons.

### Logs tab
Last 200 lines of `metals_monitor.log`. Useful for debugging. Auto-scrolls to the bottom.

### Run Now button
Triggers an immediate monitor cycle without waiting for the next scheduled run. The status dot pulses amber while running, then turns green on success.

### Live updates
The dashboard uses **Server-Sent Events (SSE)** — it updates automatically when a run completes. No manual refresh needed. It also polls every 60 seconds as a fallback.

---

## 5. Notifications

### macOS notifications (LaunchAgent mode)
The live monitor calls `osascript` to fire native macOS notifications. If notifications don't appear:
1. Open **System Settings → Notifications**
2. Allow notifications for **Script Editor** and your terminal app (Terminal, iTerm2)
3. If running via LaunchAgent, also allow `osascript`

### Web notifications (dashboard)
Click **Enable Notifications** in the dashboard header. The browser will request permission. Once granted, a browser notification fires whenever the SSE stream reports a new cluster alert — even when the browser tab is in the background.

Web Notifications require the tab to be open (standard browser limitation). For background delivery from a cloud server, a Push API service would be needed, but this is not implemented.

---

## 6. Running the backtest

```bash
cd metals_monitor
.venv/bin/python metals_backtest.py
```

The backtest covers **2025-05-15 → 2026-05-15** using the same signal logic as the live monitor.

### Output files (`metals_backtest_output/`)

| File | Contents |
|---|---|
| `metals-monitor-backtest.md` | Full markdown report |
| `metals_backtest_summary.csv` | Per-metal summary stats |
| `metals_backtest_events.csv` | Every detected event |
| `metals_proxy_performance.png` | Win rate and avg return by signal type |
| `metals_event_counts.png` | Event count breakdown |
| `metals_forward_returns.png` | Forward return distributions (1d, 3d, 5d, 10d, 20d) |

The backtest uses a **positional-index cooldown** (true trading days), while the live monitor uses calendar-day comparison (stored ISO dates). Both enforce a 3-day cooling-off period.

---

## 7. Logs and state files

### Log file
`metals_monitor_logs/metals_monitor.log`
- Rotates at 5 MB, keeps 3 backups (so max ~20 MB total)
- Logs every run: data dates, all 4 signal values per metal, cluster decisions, RSS headlines, cooldown suppressions, notifications sent

```bash
tail -f metals_monitor_logs/metals_monitor.log
```

### LaunchAgent captured output
`metals_monitor_logs/metals_monitor_stdout.log`
`metals_monitor_logs/metals_monitor_stderr.log`
Written by launchd when running as a LaunchAgent.

### State file
`metals_monitor_state/state.json`

Example structure:
```json
{
  "Gold_bullish_last": "2026-05-14",
  "Silver_bearish_last": "2026-05-10",
  "recent_events": [
    {
      "date": "2026-05-14",
      "metal": "Gold",
      "ticker": "GLD",
      "direction": "bullish",
      "categories": ["futures_curve_proxy", "demand_expectations_proxy"],
      "close": 312.45,
      "timestamp": "2026-05-14T14:00:01"
    }
  ]
}
```

If `state.json` becomes corrupted, the monitor logs a warning and starts fresh (no crash). Cooldown history is lost for that run only.

---

## 8. Changing the schedule

### LaunchAgent interval
Edit `~/Library/LaunchAgents/com.local.metalsmonitor.plist`:

```xml
<key>StartInterval</key>
<integer>3600</integer>   <!-- seconds; 1800 = 30 min, 7200 = 2 hr -->
```

Then reload:
```bash
launchctl bootout gui/$(id -u)/com.local.metalsmonitor
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.metalsmonitor.plist
```

### Docker scheduler interval
Set in `docker-compose.yml` or pass as environment variable:
```yaml
environment:
  - SCHEDULER_INTERVAL_SECS=1800
```

---

## 9. Docker / cloud deployment

Full instructions: `docs/DEPLOYMENT.md`

### Quick summary

```bash
# Build and start (foreground)
docker compose up --build

# Background
docker compose up --build -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

The container:
- Runs `metals_web_server.py` with `SCHEDULER_ENABLED=true`
- Fetches data and evaluates signals every `SCHEDULER_INTERVAL_SECS` (default 3600)
- Exposes port 8080
- Persists state and logs via named Docker volumes

**For cloud deployment** (AWS, GCP, Fly.io, Railway, etc.) expose port 8080 and set `SCHEDULER_ENABLED=true`. No other changes needed. Note that cloud servers do not run macOS, so `osascript` notifications will silently fail — use web notifications from the dashboard instead.

---

## 10. Uninstalling

### Remove the LaunchAgent
```bash
bash uninstall_launch_agent.sh
```
This stops and removes the LaunchAgent. Logs, state, backtest output, and the `.venv` are **not deleted** — remove them manually if needed.

### Remove Docker resources
```bash
docker compose down -v   # also removes named volumes (state + logs)
docker rmi metals-monitor
```

---

## 11. Troubleshooting

### No notifications appearing
- Check System Settings → Notifications → allow Script Editor / Terminal / osascript
- Check `metals_monitor_logs/metals_monitor.log` for `osascript failed` lines
- Verify the LaunchAgent is loaded: `launchctl print gui/$(id -u)/com.local.metalsmonitor`

### LaunchAgent status shows error
```bash
launchctl print gui/$(id -u)/com.local.metalsmonitor
# Look for: last exit code, last crash date
tail -50 metals_monitor_logs/metals_monitor_stderr.log
```

### `venv` creation fails (Homebrew Python)
On this machine, Homebrew Python 3.12 / 3.14 has a broken `pyexpat.so`. The install script auto-detects and falls back to `/usr/bin/python3` (Apple system Python 3.9.6). Do not override `PYTHON3_BIN` manually.

### yfinance returns no data
yfinance is unofficial and can go down. Check `metals_monitor.log` for `ValueError: No data returned for <ticker>`. Try again in a few minutes. If persistent, check if Yahoo Finance is accessible in your browser.

### Web server import error
Ensure both `metals_live_monitor.py` and `metals_web_server.py` are in the same directory.

### RSS feeds silent / failing
RSS failures are non-fatal — logged at DEBUG level and the run continues. To see RSS errors:
```bash
# Add DEBUG logging temporarily
.venv/bin/python -c "
import logging; logging.basicConfig(level=logging.DEBUG)
from metals_live_monitor import fetch_news_sentiment
print(fetch_news_sentiment(['Gold']))
"
```

---

## 12. Disclaimers

### Data quality
yfinance provides unofficial, delayed data sourced from Yahoo Finance. It is not guaranteed to be available, accurate, or timely. It may have gaps, stale prices, or corporate-action errors. Not a substitute for exchange-direct or Bloomberg/Refinitiv feeds.

### Signal quality
All four signal categories are **OHLCV proxies only**. They are not equivalent to:
- CME futures term structure (requires CME or paid data)
- ETF creation/redemption flows (requires ETF issuer or AP data)
- Physical gold/silver/copper premiums (requires LBMA, LME, or OTC data)
- News sentiment (requires real-time news feeds)

The backtest covers one year of historical data. Past performance on backtested signals does not indicate future results.

**This system is for informational and educational purposes only. Nothing here constitutes financial advice.**
