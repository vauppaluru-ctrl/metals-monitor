# CLAUDE.md — Metals Monitor

Concise project rules. Full detail lives in `docs/`. Read this file first; follow links for depth.

---

## Project in one sentence
A fully local macOS metals signal monitor (Gold / Silver / Copper) plus a FastAPI web dashboard, deployable via macOS LaunchAgent or Docker. No paid APIs after setup. See `README.md` for feature overview.

## Key paths
| Purpose | Path |
|---|---|
| Live signal logic | `metals_live_monitor.py` |
| Web dashboard server | `metals_web_server.py` |
| Backtest | `metals_backtest.py` |
| State (cooldowns, alert history) | `metals_monitor_state/state.json` |
| Logs | `metals_monitor_logs/metals_monitor.log` |
| **Test suite** | `tests/test_dashboard.py` |
| Deployment docs | `docs/DEPLOYMENT.md` |
| Security notes | `docs/SECURITY.md` |
| Code review checklist | `docs/CODE_REVIEW.md` |
| **Testing guide** | `docs/TESTING.md` |
| User guide | `USER_GUIDE.md` |

---

## Rules (12)

### 1 — Think before coding
State assumptions. Ask rather than guess. Push back when simpler. Stop when confused — name what's unclear.

### 2 — Simplicity first
Minimum code. Nothing speculative. No abstractions for single-use code. Would a senior say it's overcomplicated? Simplify.

### 3 — Surgical changes
Touch only what you must. Don't improve adjacent code. Match existing style.

### 4 — Goal-driven execution
Define success criteria before starting. Loop until verified. Don't follow steps blindly.

### 5 — Use the model for judgment, not determinism
Use AI for: classification, drafting, summarization. Do NOT use for routing, retries, or deterministic transforms — code answers those.

### 6 — Token budgets are not advisory
Per-task: 4,000 tokens. Per-session: 30,000 tokens. Summarize and start fresh when approaching limit. Surface the breach.

### 7 — Surface conflicts, don't average them
Two contradicting patterns → pick one (more recent / more tested). Explain the choice. Flag the other for cleanup.

### 8 — Read before you write
Before adding code read: exports, immediate callers, shared utilities. "Looks orthogonal" is dangerous. Ask if unsure.

### 9 — Tests verify intent, not just behavior
Encode WHY behavior matters. A test that can't fail when business logic changes is wrong.

### 10 — Checkpoint after every significant step
Summarize what's done, what's verified, what's left. Stop and restate if you lose track.

### 11 — Match codebase conventions, even if you disagree
Conformance > taste. Surface genuinely harmful conventions. Don't fork silently.

### 12 — Fail loud
"Completed" is wrong if anything was skipped silently. "Tests pass" is wrong if any were skipped. Default to surfacing uncertainty.

---

## Critical constraints (do not violate)

- **No paid APIs from the scheduled job.** `metals_live_monitor.py` must stay free at runtime. yfinance + RSS only.
- **Signal logic is per-metal, isolated.** No cross-metal inference in `generate_signals()` or `evaluate_latest()`.
- **Homebrew Python 3.12/3.14 breaks venv** on this machine (`pyexpat.so` symbol missing). The install script auto-detects; don't hardcode a Python path. See `docs/DEPLOYMENT.md`.
- **osascript escaping via `json.dumps()`** — never revert to manual quote replacement.
- **All dynamic content in the web dashboard goes through `escHtml()`** before `innerHTML`. Use `textContent` for plain strings. See `docs/SECURITY.md`.
- **Log rotation is mandatory.** `RotatingFileHandler(maxBytes=5MB, backupCount=3)` — never replace with bare `FileHandler`.

## Known gotchas (bugs that have already bitten us)

### `\n` in Python triple-quoted HTML strings → JS syntax error
Python evaluates escape sequences inside `"""..."""` strings. Writing `"\n"` in JavaScript code embedded in a Python triple-quoted string embeds a *real newline* in the JS string literal, which is an `Invalid or unexpected token` SyntaxError. This kills ALL JavaScript on the page silently — every button becomes unresponsive with no visible error to the user.

**Rule:** Any JavaScript newline escape inside `_DASHBOARD_HTML` must be `\\n` (two characters: backslash + n), not `\n`.

```python
# WRONG — Python turns \n into a real newline → JS crash
html = """arr.join("\n")"""

# CORRECT
html = """arr.join("\\n")"""
```

Caught by: `tests/test_dashboard.py::TestJSIntegrity::test_no_js_console_errors`

---

### SSE-only completion signal → Run Now appears dead
The Run Now button originally relied solely on the SSE `run_complete` event to trigger a page refresh. If the EventSource hadn't registered before the run finished (race condition), the event was broadcast to nobody and the page stayed empty. There was also no loading feedback at all.

**Rule:** `triggerRun()` must poll `/api/status` every 2 s as a fallback. Stop condition: `!d.running && d.last_run !== null` — not just `!d.running` alone (which fires on the first tick before the asyncio task has started).

Caught by: `tests/test_dashboard.py::TestHeaderButtons::test_run_now_click_shows_loading_state`

---

### Missing `Cache-Control: no-store` → stale JS served after restart
Without this header, browsers cache the dashboard HTML+JS. Bug fixes deployed in a server restart appeared to have no effect because users continued running old JavaScript.

**Rule:** The `/` FastAPI route must always include `headers={"Cache-Control": "no-store"}`.

Caught by: `tests/test_dashboard.py::TestServer::test_cache_control_header`

---

### Hardcoded hex in component CSS → theme toggle broken
Any `#RRGGBB` value in a CSS component rule bypasses the token system. Light/dark theme switches override `[data-theme]` variables, but hardcoded values stay unchanged, producing a broken mixed theme.

**Rule:** Component rules use only `var(--)` tokens. Hardcoded hex belongs only in `:root` and `[data-theme]` blocks.

Caught by: `tests/test_dashboard.py::TestJSIntegrity::test_no_hardcoded_hex_in_component_css`

---

## Changing the signal logic
Changes to `generate_signals()` or signal thresholds should be validated against the backtest first:
```bash
.venv/bin/python metals_backtest.py
```
Do not modify the four proxy categories without re-running the backtest and documenting the outcome.

---

## Before you deploy
Read `docs/DEPLOYMENT.md`. There are two distinct modes (LaunchAgent vs Docker) with different scheduler semantics. Do not mix them.
