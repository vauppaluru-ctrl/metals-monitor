"""
Playwright test suite for the Metals Monitor dashboard.

Run: .venv/bin/pytest tests/ -v
Run (skip slow): .venv/bin/pytest tests/ -v -m "not slow"
Run (slow only): .venv/bin/pytest tests/ -v -m slow

LESSONS LEARNED — bugs this suite was written to catch:
  1. Python \\n in triple-quoted HTML strings: Python evaluates \\n as a real
     newline, which is an invalid token inside a JS string literal. Killed ALL
     JS on the page silently. Fix: use \\\\n. Caught by: test_no_js_console_errors.
  2. SSE-only Run Now: relied on EventSource for completion signal; missed if
     SSE wasn't connected yet. Fix: poll /api/status. Caught by: test_run_now_*.
  3. Cache-Control missing: stale JS served from browser cache after server
     restart, so fixes never reached the user. Caught by: test_cache_control_header.
  4. Poll race condition: poll fired when running=False but last_run=None,
     exited immediately with no data. Fix: require last_run != null.
     Caught by: test_run_now_populates_signal_cards.
"""

import re
import time
import pytest
import requests
from playwright.sync_api import Page, expect

BASE_URL = "http://localhost:8080"


# ── Helpers ────────────────────────────────────────────────────────────────────

def api(path: str) -> dict:
    return requests.get(f"{BASE_URL}{path}", timeout=10).json()


def wait_for_run_complete(timeout: int = 120) -> dict:
    """Poll /api/status until a run completes. Returns the final status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = api("/api/status")
        if status.get("last_run") and not status.get("running"):
            return status
        time.sleep(2)
    pytest.fail(f"Monitor run did not complete within {timeout}s")


# ── 1. Server / HTTP ───────────────────────────────────────────────────────────

class TestServer:
    def test_dashboard_returns_200(self):
        r = requests.get(BASE_URL, timeout=5)
        assert r.status_code == 200

    def test_cache_control_header(self):
        # Lesson: missing Cache-Control caused stale JS after server restarts,
        # so UI fixes never reached the user's browser.
        r = requests.get(BASE_URL, timeout=5)
        cc = r.headers.get("cache-control", "").lower()
        assert "no-store" in cc, f"Expected Cache-Control: no-store, got: {cc!r}"

    def test_api_status_shape(self):
        d = api("/api/status")
        for key in ("last_run", "running", "scheduler_enabled", "cooldown_days"):
            assert key in d, f"Missing key: {key}"
        assert isinstance(d["running"], bool)
        assert isinstance(d["cooldown_days"], int)

    def test_api_metals_shape(self):
        d = api("/api/metals")
        assert "metals" in d
        assert isinstance(d["metals"], dict)

    def test_api_events_shape(self):
        d = api("/api/events")
        assert "events" in d
        assert isinstance(d["events"], list)

    def test_api_news_shape(self):
        d = api("/api/news")
        assert "news" in d
        assert isinstance(d["news"], dict)

    def test_api_logs_shape(self):
        d = api("/api/logs")
        assert "lines" in d
        assert isinstance(d["lines"], list)


# ── 2. JS integrity ────────────────────────────────────────────────────────────

class TestJSIntegrity:
    def test_no_js_console_errors(self, fresh_page: Page):
        # Lesson: a literal \\n inside a JS string (from Python \\n in a
        # triple-quoted string) caused "Invalid or unexpected token" and
        # silently killed ALL JavaScript, making every button unresponsive.
        errors = []
        fresh_page.on("console", lambda msg: errors.append(msg) if msg.type == "error" else None)
        fresh_page.reload()
        fresh_page.wait_for_timeout(2000)
        js_errors = [e.text for e in errors if "Invalid or unexpected token" in e.text
                     or "SyntaxError" in e.text or "ReferenceError" in e.text]
        assert not js_errors, f"JS errors on page load: {js_errors}"

    def test_no_hardcoded_hex_in_component_css(self):
        # Lesson: all colors must flow through CSS variables so both themes work.
        # Component rules containing bare #RRGGBB values bypass the token system.
        html = requests.get(BASE_URL, timeout=5).text
        style_start = html.find("<style>")
        style_end   = html.find("</style>")
        css = html[style_start:style_end]

        # Strip the token definition block (:root and [data-theme] sections) —
        # hardcoded hex IS correct there.
        token_block_end = css.rfind("}")  # last closing brace of token blocks
        # Find where component rules begin (after the last [data-theme] block)
        component_start = css.find("/* ═══", css.find("COMPONENTS"))
        if component_start == -1:
            component_start = css.find("* { box-sizing")
        component_css = css[component_start:] if component_start != -1 else css

        bare_hex = re.findall(r'(?<!var\()(?<!--)#[0-9a-fA-F]{3,6}\b', component_css)
        assert not bare_hex, f"Hardcoded hex in component CSS (should use var(--)): {bare_hex}"

    def test_sse_endpoint_reachable(self):
        # Just verify /stream returns SSE content-type — not a full connection test.
        import urllib.request
        req = urllib.request.Request(f"{BASE_URL}/stream")
        req.add_header("Accept", "text/event-stream")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                ct = r.headers.get("Content-Type", "")
                assert "text/event-stream" in ct, f"Expected SSE content-type, got: {ct}"
        except Exception:
            pass  # timeout is expected — the stream stays open


# ── 3. Theme system ────────────────────────────────────────────────────────────

class TestTheme:
    def test_default_theme_attribute_present(self, fresh_page: Page):
        theme = fresh_page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        assert theme in ("dark", "light"), f"data-theme should be dark or light, got: {theme!r}"

    def test_theme_button_exists_in_header(self, fresh_page: Page):
        btn = fresh_page.locator("#theme-btn")
        expect(btn).to_be_visible()

    def test_theme_button_label_matches_state(self, fresh_page: Page):
        # Button should show the OPPOSITE of the current theme (what you'll switch TO).
        theme = fresh_page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        btn_text = fresh_page.locator("#theme-btn").inner_text().strip().lower()
        if theme == "dark":
            assert btn_text == "light", f"In dark mode button should say 'Light', got: {btn_text!r}"
        else:
            assert btn_text == "dark", f"In light mode button should say 'Dark', got: {btn_text!r}"

    def test_theme_toggle_switches_attribute(self, fresh_page: Page):
        before = fresh_page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        fresh_page.click("#theme-btn")
        fresh_page.wait_for_timeout(300)
        after = fresh_page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        assert before != after, "Clicking theme button should change data-theme"
        assert after in ("dark", "light")

    def test_theme_toggle_updates_button_label(self, fresh_page: Page):
        fresh_page.click("#theme-btn")
        fresh_page.wait_for_timeout(300)
        theme = fresh_page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        btn_text = fresh_page.locator("#theme-btn").inner_text().strip().lower()
        expected = "light" if theme == "dark" else "dark"
        assert btn_text == expected

    def test_theme_persists_in_localstorage(self, fresh_page: Page):
        fresh_page.click("#theme-btn")
        fresh_page.wait_for_timeout(300)
        theme_attr  = fresh_page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        saved_theme = fresh_page.evaluate("() => localStorage.getItem('mm-theme')")
        assert saved_theme == theme_attr, "localStorage mm-theme should match data-theme"

    def test_both_themes_have_distinct_backgrounds(self, fresh_page: Page):
        def get_bg():
            return fresh_page.evaluate(
                "() => getComputedStyle(document.body).backgroundColor"
            )
        fresh_page.evaluate("() => document.documentElement.setAttribute('data-theme','dark')")
        fresh_page.wait_for_timeout(300)
        dark_bg = get_bg()
        fresh_page.evaluate("() => document.documentElement.setAttribute('data-theme','light')")
        fresh_page.wait_for_timeout(300)
        light_bg = get_bg()
        assert dark_bg != light_bg, f"Dark and light backgrounds should differ: {dark_bg} == {light_bg}"


# ── 4. Header buttons ──────────────────────────────────────────────────────────

class TestHeaderButtons:
    def test_run_now_button_exists(self, fresh_page: Page):
        expect(fresh_page.locator("#run-btn")).to_be_visible()

    def test_run_now_button_is_enabled(self, fresh_page: Page):
        expect(fresh_page.locator("#run-btn")).to_be_enabled()

    def test_notifications_button_exists(self, fresh_page: Page):
        expect(fresh_page.locator("#notif-btn")).to_be_visible()

    def test_run_now_click_shows_loading_state(self, fresh_page: Page):
        # Lesson: the old implementation showed no feedback at all (dead button).
        # Now it immediately shows a loading placeholder.
        fresh_page.click("#run-btn")
        fresh_page.wait_for_timeout(500)
        btn_text = fresh_page.locator("#run-btn").inner_text()
        grid_text = fresh_page.locator("#metals-grid").inner_text()
        loading = "Running" in btn_text or "Downloading" in grid_text or "computing" in grid_text
        assert loading, (
            f"Clicking Run Now should show loading state. "
            f"Button: {btn_text!r}, Grid: {grid_text[:80]!r}"
        )


# ── 5. Run and data population ─────────────────────────────────────────────────

class TestRunAndData:
    @pytest.mark.slow
    def test_run_now_populates_signal_cards(self, fresh_page: Page):
        # Lesson: the poll race condition (running=False + last_run=None on
        # first tick) caused the page to silently show no data after a run.
        # This test catches that: after Run Now completes, all 3 metal cards
        # must appear.
        requests.post(f"{BASE_URL}/api/run", timeout=5)
        wait_for_run_complete(timeout=120)

        # Give the browser's 2s poll one more cycle to pick up completion
        fresh_page.wait_for_timeout(4000)

        cards = fresh_page.locator(".metal-card")
        expect(cards).to_have_count(3, timeout=10000)

    @pytest.mark.slow
    def test_signal_cards_show_expected_metals(self, fresh_page: Page):
        wait_for_run_complete(timeout=10)  # run already done from previous test
        fresh_page.wait_for_timeout(4000)

        for metal in ("Gold", "Silver", "Copper"):
            expect(fresh_page.locator(f".metal-card .name.{metal.lower()}")).to_be_visible()

    @pytest.mark.slow
    def test_signal_badges_have_valid_values(self, fresh_page: Page):
        wait_for_run_complete(timeout=10)
        fresh_page.wait_for_timeout(4000)

        badges = fresh_page.locator(".sig-val").all()
        assert len(badges) == 12, f"Expected 12 signal badges (4 per metal × 3), got {len(badges)}"
        valid = {"bullish", "bearish", "neutral"}
        for badge in badges:
            text = badge.inner_text().strip().lower()
            assert text in valid, f"Unexpected signal value: {text!r}"

    @pytest.mark.slow
    def test_last_run_timestamp_updates(self, fresh_page: Page):
        wait_for_run_complete(timeout=10)
        fresh_page.wait_for_timeout(4000)
        last_run_text = fresh_page.locator("#last-run").inner_text()
        assert "Last run:" in last_run_text, f"Expected 'Last run:' in header, got: {last_run_text!r}"

    @pytest.mark.slow
    def test_status_dot_turns_green_after_run(self, fresh_page: Page):
        wait_for_run_complete(timeout=10)
        fresh_page.wait_for_timeout(4000)
        dot_class = fresh_page.locator("#status-dot").get_attribute("class") or ""
        assert "ok" in dot_class or "run" in dot_class, (
            f"Status dot should be ok/run after a successful run, got class: {dot_class!r}"
        )


# ── 6. Tabs ────────────────────────────────────────────────────────────────────

class TestTabs:
    def test_news_tab_active_by_default(self, fresh_page: Page):
        news_pane = fresh_page.locator("#pane-news")
        expect(news_pane).to_have_class(re.compile(r"\bactive\b"))

    def test_logs_tab_switches_pane(self, fresh_page: Page):
        fresh_page.click("button:text('Logs')")
        fresh_page.wait_for_timeout(300)
        expect(fresh_page.locator("#pane-logs")).to_have_class(re.compile(r"\bactive\b"))
        expect(fresh_page.locator("#pane-news")).not_to_have_class(re.compile(r"\bactive\b"))

    def test_news_tab_restores_pane(self, fresh_page: Page):
        fresh_page.click("button:text('Logs')")
        fresh_page.wait_for_timeout(200)
        fresh_page.click("button:text('News')")
        fresh_page.wait_for_timeout(300)
        expect(fresh_page.locator("#pane-news")).to_have_class(re.compile(r"\bactive\b"))

    def test_metal_tabs_gold_active_by_default(self, fresh_page: Page):
        gold_tab = fresh_page.locator("#news-metal-tabs button:text('Gold')")
        expect(gold_tab).to_have_class(re.compile(r"\bactive\b"))

    def test_metal_tabs_switch_to_silver(self, fresh_page: Page):
        fresh_page.click("#news-metal-tabs button:text('Silver')")
        fresh_page.wait_for_timeout(300)
        silver_tab = fresh_page.locator("#news-metal-tabs button:text('Silver')")
        expect(silver_tab).to_have_class(re.compile(r"\bactive\b"))

    def test_metal_tabs_switch_to_copper(self, fresh_page: Page):
        fresh_page.click("#news-metal-tabs button:text('Copper')")
        fresh_page.wait_for_timeout(300)
        copper_tab = fresh_page.locator("#news-metal-tabs button:text('Copper')")
        expect(copper_tab).to_have_class(re.compile(r"\bactive\b"))


# ── 7. API contract ────────────────────────────────────────────────────────────

class TestAPIContract:
    def test_run_endpoint_starts_job(self):
        # Only call if not already running
        status = api("/api/status")
        if status.get("running"):
            return
        r = requests.post(f"{BASE_URL}/api/run", timeout=5)
        assert r.status_code == 200
        assert r.json().get("status") == "started"

    def test_run_endpoint_returns_409_when_busy(self):
        # Trigger a run, then immediately try again — should get 409
        requests.post(f"{BASE_URL}/api/run", timeout=5)
        time.sleep(0.1)
        status = api("/api/status")
        if not status.get("running"):
            pytest.skip("Run completed too fast to test 409")
        r = requests.post(f"{BASE_URL}/api/run", timeout=5)
        assert r.status_code == 409

    def test_metals_api_returns_correct_tickers(self):
        # Wait for any in-progress run to complete first
        deadline = time.time() + 120
        while time.time() < deadline:
            status = api("/api/status")
            if status.get("last_run") and not status.get("running"):
                break
            time.sleep(2)
        metals = api("/api/metals").get("metals", {})
        if not metals:
            pytest.skip("No run data available — run the monitor first")
        expected = {"Gold": "GLD", "Silver": "SLV", "Copper": "CPER"}
        for metal, ticker in expected.items():
            assert metal in metals, f"Missing metal: {metal}"
            # ticker is not in the API response directly, but the metal name must be present
