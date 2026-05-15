# Security Notes

This project is a local monitoring tool. The attack surface is small. These notes document the mitigations in place and what to watch for when extending.

---

## Web dashboard (XSS)

**Risk:** The dashboard renders server-supplied strings (metal names, signal values, headlines, log lines) into the DOM.

**Mitigation:**
- All dynamic values pass through `escHtml()` in `metals_web_server.py` before any HTML insertion.
- Plain-text values use `.textContent` instead of HTML insertion where possible.
- `escHtml()` escapes `& < > " '` — covers all standard HTML injection vectors.

**Rule:** When inserting server-supplied text into the DOM, always sanitize through `escHtml()` first. Use `.textContent` for plain strings that need no HTML formatting.

---

## osascript injection

**Risk:** macOS notification text is passed to `osascript`. A malicious headline or ticker string could inject AppleScript commands.

**Mitigation:**
- `send_notification()` uses `json.dumps(message)` and `json.dumps(title)` to produce properly quoted string literals that AppleScript's expression evaluator accepts.
- `json.dumps()` escapes backslashes, double quotes, and control characters — closing the injection path.
- Do NOT revert to manual `replace("'", "\\'")` — that produces literal `\'` which is not an AppleScript escape sequence and doesn't sanitize double quotes.

---

## RSS / network fetch

**Risk:** RSS feeds are external input. A malicious or hijacked feed could supply crafted XML or oversized payloads.

**Mitigations:**
- `_fetch_feed_items()` reads at most **32 KB** per feed (`r.read(32_000)`).
- All fetches have a **10-second timeout** (`RSS_TIMEOUT_SECS = 10`).
- Exceptions are caught and logged at DEBUG; the run continues. RSS failure is never fatal.
- CDATA sections are stripped with regex before XML parsing to prevent `ElementTree` parse errors.
- Headlines are HTML-unescaped with `html.unescape()` then re-escaped with `escHtml()` at render time — they never reach the DOM raw.

---

## yfinance / external data

- yfinance data is processed entirely in Python (pandas/numpy). No external strings from it reach the DOM directly — only computed numeric values.
- Ticker symbols are hardcoded constants (`GLD`, `SLV`, `CPER`, etc.). No user-supplied tickers.

---

## State file

- `state.json` is read-write by the local user only. It stores cooldown dates and alert history — no secrets, no credentials.
- `load_state()` catches JSON parse errors and logs a warning rather than crashing. Corrupt state resets cooldowns for one run only.

---

## sed substitution (install script)

**Risk:** `sed -e "s|{{VAR}}|$VALUE|g"` — if `$VALUE` contains `&`, sed expands it to the matched text. If `$VALUE` contains `|`, it breaks the delimiter.

**Mitigation:** `_sed_escape()` in `install_launch_agent.sh` pre-escapes both `&` and `|`:
```bash
_sed_escape() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
```

---

## Log files

- Logs are append-only to a local file. No remote logging, no telemetry.
- `RotatingFileHandler(maxBytes=5MB, backupCount=3)` caps total log storage at ~20 MB.
- Log content includes signal values, close prices, and RSS headlines — no credentials or PII.

---

## Network exposure

By default the web server binds to `0.0.0.0:8080`. On a local Mac this is accessible from any device on your LAN.

**If you want localhost-only:**
```bash
uvicorn metals_web_server:app --host 127.0.0.1 --port 8080
```

**For cloud deployment:** put a reverse proxy (nginx, Caddy, Fly proxy) in front. Do not expose port 8080 directly to the public internet without authentication.

---

## No credentials stored

This project has no authentication system and stores no credentials. yfinance and the RSS feeds require no API keys. If you add authenticated data sources in the future, use environment variables (never hardcode) and add the env file to `.gitignore` / `.dockerignore`.

---

## Dependency pinning

`requirements.txt` uses `>=` version floors, not exact pins. This keeps dependencies up to date but allows supply-chain drift.

To pin for production:
```bash
.venv/bin/pip freeze > requirements.lock
# Use requirements.lock in Dockerfile COPY + pip install
```
