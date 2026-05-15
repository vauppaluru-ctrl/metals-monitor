# Testing Guide

## Setup

Tests use `pytest-playwright`. Install once per environment:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## Running tests

The server must be running before you run the test suite:

```bash
# Terminal 1 — start server
.venv/bin/uvicorn metals_web_server:app --host 0.0.0.0 --port 8080

# Terminal 2 — run tests
cd metals_monitor

# Fast tests only (~50s) — run these before every push
.venv/bin/pytest tests/ -v -m "not slow"

# Full suite including live monitor run (~2-3 min)
.venv/bin/pytest tests/ -v

# Single class
.venv/bin/pytest tests/ -v -k TestTheme

# Single test
.venv/bin/pytest tests/ -v -k test_no_js_console_errors
```

## Test categories

| Class | What it covers | Speed |
|---|---|---|
| `TestServer` | HTTP status codes, response shapes, Cache-Control header | Fast |
| `TestJSIntegrity` | No console errors, no hardcoded hex in CSS, SSE endpoint | Fast |
| `TestTheme` | Toggle, localStorage persistence, distinct backgrounds | Fast |
| `TestHeaderButtons` | Buttons visible, enabled, show loading state on click | Fast |
| `TestTabs` | News/Logs tab switching, Gold/Silver/Copper metal tabs | Fast |
| `TestAPIContract` | /api/run start/409, /api/metals tickers | Fast |
| `TestRunAndData` | Full run cycle: signal cards appear, dot turns green | **Slow** |

## Bugs each test was written to catch

### `test_no_js_console_errors`
**Bug:** Python evaluates `\n` in triple-quoted strings as a real newline. Embedding `"\n"` in a JS string literal inside a Python `"""..."""` string produces an `Invalid or unexpected token` SyntaxError that kills ALL JavaScript silently. Every button becomes unresponsive.

**Rule:** Any `\n` in JavaScript code inside a Python triple-quoted string must be written as `\\n`.

**Example:**
```python
# WRONG — Python evaluates \n as real newline → JS syntax error
html = """const lines = arr.join("\n");"""

# CORRECT — Python outputs literal \n for JavaScript
html = """const lines = arr.join("\\n");"""
```

---

### `test_cache_control_header`
**Bug:** Missing `Cache-Control: no-store` on the dashboard route caused browsers to cache the HTML+JS. After a server restart with a bug fix, users kept seeing the old broken JavaScript. Bug fixes appeared to have no effect.

**Rule:** The `/` route must always return `Cache-Control: no-store`.

---

### `test_run_now_click_shows_loading_state`
**Bug:** The original `triggerRun()` fired a POST and then set a 5-second timeout to re-enable the button. No visual feedback appeared during the run (~30–60s). Users saw a dead-looking page.

**Rule:** Clicking Run Now must immediately show a loading placeholder in the signal cards grid.

---

### `test_run_now_populates_signal_cards` (slow)
**Bug:** The poll checking for run completion used `!d.running` as its only condition. On the first poll tick (2 s after click), if the asyncio task had not yet started, `running` was `false` and `last_run` was `null`. The poll exited immediately, `refreshAll()` fired with no data, and the page showed nothing.

**Rule:** The completion condition must be `!d.running && d.last_run !== null`.

---

### `test_no_hardcoded_hex_in_component_css`
**Bug:** Component rules with hardcoded `#RRGGBB` values bypass the CSS token system. When theme tokens are overridden for light/dark mode, components with hardcoded colors don't update, breaking the theme.

**Rule:** All component rules reference only `var(--)` tokens. Hardcoded hex belongs only in the `:root` / `[data-theme]` token definition blocks.

## Adding new tests

1. Put new tests in `tests/test_dashboard.py`, grouped into the relevant class.
2. Mark slow tests with `@pytest.mark.slow`.
3. Write the failure message to explain *why* the assertion matters, not just what failed — future developers need to understand the original bug.
4. If a new bug is fixed, add a test for it and document the lesson in the docstring at the top of the test file.
