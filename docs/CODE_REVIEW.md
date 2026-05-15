# Code Review Checklist

Use this before merging any change to the signal logic, web server, or infrastructure files.

**Run the fast test suite first. If any test fails, fix it before reviewing the rest.**
```bash
.venv/bin/pytest tests/ -v -m "not slow"
```
See `docs/TESTING.md` for full test instructions.

---

## Previously-bitten gotchas (check these first)

- [ ] **Python `\n` in `_DASHBOARD_HTML`** — any JavaScript newline escape inside the triple-quoted HTML string must be `\\n`, never `\n`. Python evaluates `\n` as a real newline, producing an `Invalid or unexpected token` JS SyntaxError that kills all JavaScript silently.  
  Test: `TestJSIntegrity::test_no_js_console_errors`

- [ ] **`Cache-Control: no-store` on `/`** — the dashboard route must include this header. Without it, browsers cache old JS after a server restart and bug fixes appear to have no effect.  
  Test: `TestServer::test_cache_control_header`

- [ ] **Run Now poll condition** — `triggerRun()` must stop polling only when `!d.running && d.last_run !== null`. Using `!d.running` alone fires on the first tick (2 s) before the asyncio task sets `running=true`, showing no data.  
  Test: `TestHeaderButtons::test_run_now_click_shows_loading_state`

- [ ] **No hardcoded hex in component CSS** — component rules must use only `var(--)` tokens. Hardcoded `#RRGGBB` values bypass the theme token system and break light/dark switching.  
  Test: `TestJSIntegrity::test_no_hardcoded_hex_in_component_css`

---

## Signal integrity

- [ ] `generate_signals()` and `evaluate_latest()` evaluate each metal **independently** — no cross-metal lookups or shared state between metals in a single run
- [ ] The four proxy categories (`futures_curve_proxy`, `etf_pressure_proxy`, `physical_tightness_proxy`, `demand_expectations_proxy`) remain the only inputs to the cluster trigger
- [ ] Cluster threshold is still **≥ 2** categories aligned — not 1, not 3
- [ ] Cooldown check (`cooldown_allows()`) is called **before** `send_notification()` and `record_cooldown()`, not after
- [ ] `prior_20d_high` / `prior_20d_low` use `c.shift(1).rolling(20)` — the `shift(1)` is mandatory to exclude the current day from the window. Removing it introduces look-ahead bias
- [ ] `vol_60d` uses `ret_1d.rolling(60).std()` — any change to the lookback must be re-validated in the backtest
- [ ] If signal thresholds changed: re-run `metals_backtest.py` and document the event count change in the PR

---

## Data pipeline

- [ ] `download_ohlcv()` handles `pd.MultiIndex` columns (yfinance sometimes returns MultiIndex for single-ticker downloads — `raw.columns.get_level_values(0)` flattens it)
- [ ] `compute_metrics()` handles NaN gracefully — early rows with insufficient history for rolling windows return NaN, not errors
- [ ] `evaluate_latest()` calls `.dropna(subset=["vol_60d"])` before selecting the last row — this drops the warm-up period

---

## Scheduler / concurrency (web server)

- [ ] `_run_monitor_sync()` runs inside `run_in_executor(_executor, ...)` — it must not call any `asyncio` primitives directly (no `await`, no `asyncio.get_event_loop()`)
- [ ] `_executor` has `max_workers=1` — this is intentional. Do not increase it; concurrent yfinance downloads for the same tickers will produce duplicate state writes
- [ ] `_broadcast()` is called from the executor thread via the already-completed future's result path — it only calls `queue.put_nowait()`, which is thread-safe for asyncio queues
- [ ] `POST /api/run` checks `_cache["running"]` before creating a new task — verify the 409 guard is present

---

## Web dashboard (XSS)

- [ ] Every server-supplied string inserted into the DOM passes through `escHtml()` first
- [ ] `.textContent` is used instead of HTML insertion for all strings that need no HTML formatting
- [ ] No raw server values appear in template literals assigned to `.innerHTML`
- [ ] `escHtml()` implementation escapes: `&`, `<`, `>`, `"`, `'`

---

## osascript / notification safety

- [ ] `send_notification()` builds the AppleScript using `json.dumps(title)` and `json.dumps(message)` — not f-string interpolation or quote replacement
- [ ] `subprocess.run(["osascript", "-e", script], ...)` passes the script as a list argument — the script string is never shell-expanded

---

## RSS / external fetch

- [ ] `_fetch_feed_items()` uses `r.read(32_000)` — payload size is capped
- [ ] `RSS_TIMEOUT_SECS` (default 10) is applied to `urlopen`
- [ ] All exceptions in `_fetch_feed_items()` are caught; failure returns `[]` and logs at DEBUG
- [ ] CDATA stripping regex uses `re.DOTALL` to handle multi-line CDATA

---

## State management

- [ ] `save_state()` is called exactly **once** per `main()` run, after the loop over all metals — not inside the metal loop
- [ ] `_append_recent_event()` caps the list at 50 entries: `events[:50]`
- [ ] `load_state()` logs a warning on JSON parse error and returns `{}`; it does not raise

---

## Logging

- [ ] `logging.handlers.RotatingFileHandler(maxBytes=5*1024*1024, backupCount=3)` — do not replace with `FileHandler`
- [ ] No credentials, API keys, or PII are logged at any level

---

## Install script

- [ ] All path variables used in `sed` replacements are passed through `_sed_escape()` first
- [ ] `find_python()` cleans up its temp directory in all exit paths (uses `trap RETURN`)
- [ ] The script uses `set -euo pipefail`

---

## Docker

- [ ] `.dockerignore` excludes `.venv/`, `metals_monitor_state/`, `metals_monitor_logs/`, `*.log`
- [ ] `Dockerfile` copies `requirements.txt` before source files (layer cache optimization)
- [ ] `HEALTHCHECK` is present and targets `/api/status`
- [ ] Named volumes are defined for state and logs — data must not be stored inside the container layer

---

## Backtest consistency

Signal logic in `metals_live_monitor.py` and `metals_backtest.py` must stay identical. Key functions to keep in sync:

| Function | File | Must match |
|---|---|---|
| `compute_metrics()` | live monitor | backtest version |
| `generate_signals()` | live monitor | backtest version |
| `SIGNAL_COLS` constant | both | same list, same order |

If you change signal logic in one file, update both and re-run the backtest.
