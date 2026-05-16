# Graph Report - .  (2026-05-16)

## Corpus Check
- Corpus is ~27,352 words - fits in a single context window. You may not need a graph.

## Summary
- 190 nodes · 297 edges · 17 communities detected
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 33 edges (avg confidence: 0.84)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Alert & State Pipeline|Alert & State Pipeline]]
- [[_COMMUNITY_Backtest Event Analysis|Backtest Event Analysis]]
- [[_COMMUNITY_Dashboard Integration Tests|Dashboard Integration Tests]]
- [[_COMMUNITY_Web Server API Layer|Web Server API Layer]]
- [[_COMMUNITY_Backtest Computation Engine|Backtest Computation Engine]]
- [[_COMMUNITY_JS Integrity & Known Bugs|JS Integrity & Known Bugs]]
- [[_COMMUNITY_Signal Logic & Sync Constraint|Signal Logic & Sync Constraint]]
- [[_COMMUNITY_ETF Performance Data|ETF Performance Data]]
- [[_COMMUNITY_Test Infrastructure|Test Infrastructure]]
- [[_COMMUNITY_UI Tab Tests|UI Tab Tests]]
- [[_COMMUNITY_Run Button & SSE Tests|Run Button & SSE Tests]]
- [[_COMMUNITY_XSS & Cache Defenses|XSS & Cache Defenses]]
- [[_COMMUNITY_Data Limitations & Disclaimers|Data Limitations & Disclaimers]]
- [[_COMMUNITY_Deployment Modes|Deployment Modes]]
- [[_COMMUNITY_Cluster Trigger Logic|Cluster Trigger Logic]]
- [[_COMMUNITY_Slow E2E Test|Slow E2E Test]]
- [[_COMMUNITY_Project Overview|Project Overview]]

## God Nodes (most connected - your core abstractions)
1. `_run_monitor_sync()` - 21 edges
2. `main()` - 15 edges
3. `main()` - 12 edges
4. `Average Signed Forward Returns by Metal and Horizon` - 11 edges
5. `api()` - 10 edges
6. `TestServer` - 10 edges
7. `Gold (GLD) — 16 Bullish, 5 Bearish cluster events` - 10 edges
8. `wait_for_run_complete()` - 9 edges
9. `TestTheme` - 9 edges
10. `Silver (SLV) — 24 Bullish, 5 Bearish cluster events` - 9 edges

## Surprising Connections (you probably didn't know these)
- `Log rotation constraint — RotatingFileHandler 5MB/3 backups` --rationale_for--> `main()`  [INFERRED]
  docs/SECURITY.md → metals_live_monitor.py
- `Gotcha: SSE-only completion signal → Run Now race condition` --rationale_for--> `_run_monitor_async()`  [EXTRACTED]
  CLAUDE.md → metals_web_server.py
- `TestServer` --references--> `Gotcha: missing Cache-Control no-store → stale JS`  [EXTRACTED]
  tests/test_dashboard.py → CLAUDE.md
- `Backtest consistency rule — compute_metrics/generate_signals must match in both files` --references--> `compute_metrics()`  [EXTRACTED]
  docs/CODE_REVIEW.md → metals_live_monitor.py
- `Signal integrity checklist — per-metal isolation, shift(1) look-ahead guard` --references--> `compute_metrics()`  [EXTRACTED]
  docs/CODE_REVIEW.md → metals_live_monitor.py

## Hyperedges (group relationships)
- **Per-metal signal pipeline: download → metrics → signals → evaluate → alert** — metals_live_monitor_download_ohlcv, metals_live_monitor_compute_metrics, metals_live_monitor_generate_signals, metals_live_monitor_evaluate_latest [EXTRACTED 0.95]
- **Backtest consistency constraint: compute_metrics and generate_signals must match across live monitor, backtest, and web server** — metals_live_monitor_compute_metrics, metals_backtest_compute_metrics, metals_web_server_run_backtest_sync [EXTRACTED 0.95]
- **XSS defense chain: escHtml in JS → docs/SECURITY.md policy → CODE_REVIEW checklist** — metals_web_server_eschml, docs_security_xss, docs_code_review_xss [EXTRACTED 0.90]

## Communities

### Community 0 - "Alert & State Pipeline"
Cohesion: 0.12
Nodes (29): Log rotation constraint — RotatingFileHandler 5MB/3 backups, osascript injection mitigation — json.dumps() escaping, RSS fetch security — 32KB cap, 10s timeout, exception swallow, _append_recent_event(), build_notification(), cooldown_allows(), download_ohlcv(), evaluate_latest() (+21 more)

### Community 1 - "Backtest Event Analysis"
Cohesion: 0.15
Nodes (24): metals_backtest.py — backtest engine producing forward return data, Cluster Trigger Event Counts by Metal and Direction (Bar Chart), Average Signed Forward Returns by Metal and Horizon, Cluster Trigger Event — fired when ≥2 proxy signals align in same direction, 10-Day Forward Return Horizon, 1-Day Forward Return Horizon, 20-Day Forward Return Horizon, 3-Day Forward Return Horizon (+16 more)

### Community 2 - "Dashboard Integration Tests"
Cohesion: 0.14
Nodes (12): api(), Playwright test suite for the Metals Monitor dashboard.  Run: .venv/bin/pytest t, Poll /api/status until a run completes. Returns the final status., test_last_run_timestamp_updates(), test_run_now_populates_signal_cards(), test_signal_badges_have_valid_values(), test_signal_cards_show_expected_metals(), test_status_dot_turns_green_after_run() (+4 more)

### Community 3 - "Web Server API Layer"
Cohesion: 0.18
Nodes (13): api_backtest_run(), api_events(), api_metals(), api_run(), _broadcast(), _cache — in-memory state cache, Run immediately on first call, then sleep and repeat., _run_backtest_async() (+5 more)

### Community 4 - "Backtest Computation Engine"
Cohesion: 0.2
Nodes (16): Backtest results — 73 total events, Gold/Silver/Copper summary table, compute_forward_returns(), compute_summary(), detect_events(), download_ohlcv(), generate_markdown_report(), generate_signals(), _hr() (+8 more)

### Community 5 - "JS Integrity & Known Bugs"
Cohesion: 0.12
Nodes (6): Gotcha: hardcoded hex in component CSS → theme toggle broken, Gotcha: \n in Python triple-quoted HTML → JS syntax error, Test categories and bug rationale documentation, FastAPI app — Metals Monitor Dashboard, TestJSIntegrity, TestTheme

### Community 6 - "Signal Logic & Sync Constraint"
Cohesion: 0.25
Nodes (11): Critical constraints — no paid APIs, per-metal isolation, osascript safety, Concept: Per-metal signal isolation — no cross-metal inference, Backtest consistency rule — compute_metrics/generate_signals must match in both files, Signal integrity checklist — per-metal isolation, shift(1) look-ahead guard, compute_metrics(), compute_metrics(), generate_signals(), SIGNAL_COLS constant — 4 proxy signal categories (+3 more)

### Community 7 - "ETF Performance Data"
Cohesion: 0.33
Nodes (11): Metals ETF Proxy Performance Chart (Normalised to 100 at backtest start), Copper ETF (CPER), Gold ETF (GLD), Silver ETF (SLV), SLV spike peak ~358 around Jan 2026, followed by sharp correction, Normalised Price (base = 100 at backtest start May 2025), Backtest period: May 2025 to May 2026 (approximately 12 months), Metals proxy signal system (futures_curve_proxy, etf_pressure_proxy, physical_tightness_proxy, demand_expectations_proxy) (+3 more)

### Community 8 - "Test Infrastructure"
Cohesion: 0.22
Nodes (6): fresh_page(), Pytest fixtures for the Metals Monitor dashboard tests.  Assumes the web server, Fail fast if the server is not reachable before any test runs., Navigate to the dashboard and wait for initial JS to settle., require_server(), api_status()

### Community 9 - "UI Tab Tests"
Cohesion: 0.29
Nodes (1): TestTabs

### Community 10 - "Run Button & SSE Tests"
Cohesion: 0.33
Nodes (2): Gotcha: SSE-only completion signal → Run Now race condition, TestHeaderButtons

### Community 11 - "XSS & Cache Defenses"
Cohesion: 0.4
Nodes (5): Gotcha: missing Cache-Control no-store → stale JS, XSS checklist — escHtml() requirement for all DOM insertions, XSS mitigation — escHtml() for all dynamic DOM content, dashboard(), escHtml() — XSS sanitizer in JS

### Community 12 - "Data Limitations & Disclaimers"
Cohesion: 0.4
Nodes (5): Backtest limitations — proxy data, short sample, in-sample signal design, Recommended next iteration — 3-5yr lookback, CME/LME enrichment, permutation tests, Concept: OHLCV-only proxy signals — no paid real-time data, Design rationale: no paid API from scheduled job, Signal quality disclaimer — OHLCV proxies only

### Community 13 - "Deployment Modes"
Cohesion: 0.5
Nodes (4): Docker deployment mode — Mode B, LaunchAgent deployment mode — Mode A, Python version constraint — Homebrew 3.12/3.14 broken pyexpat.so, Two deployment modes — LaunchAgent vs Docker

### Community 14 - "Cluster Trigger Logic"
Cohesion: 0.67
Nodes (3): Concept: Cluster trigger — ≥2 aligned signals per metal, Cluster trigger rule — ≥2 of 4 signals aligned, Cluster trigger explanation — ≥2 of 4 aligned

### Community 15 - "Slow E2E Test"
Cohesion: 1.0
Nodes (1): TestRunAndData — full run cycle, signal cards populated

### Community 16 - "Project Overview"
Cohesion: 1.0
Nodes (1): Project structure overview

## Ambiguous Edges - Review These
- `Cluster Trigger Event — fired when ≥2 proxy signals align in same direction` → `Insight: Identical bearish count (5) across Gold, Silver, and Copper implies a common macro bearish event set`  [AMBIGUOUS]
  metals_backtest_output/metals_event_counts.png · relation: conceptually_related_to

## Knowledge Gaps
- **48 isolated node(s):** `Return signal summary for the most recent row with valid vol_60d.`, `Fetch an RSS feed and return a list of (title, pubdate_str) tuples.     Uses reg`, `Parse RFC 2822 pubDate strings into an aware UTC datetime.`, `Fetch all configured RSS feeds and return per-metal sentiment context.     Retur`, `Return a one-line news context string to append to notifications/logs.` (+43 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Slow E2E Test`** (1 nodes): `TestRunAndData — full run cycle, signal cards populated`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Project Overview`** (1 nodes): `Project structure overview`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Cluster Trigger Event — fired when ≥2 proxy signals align in same direction` and `Insight: Identical bearish count (5) across Gold, Silver, and Copper implies a common macro bearish event set`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `_run_monitor_sync()` connect `Alert & State Pipeline` to `Web Server API Layer`, `Backtest Computation Engine`, `Signal Logic & Sync Constraint`?**
  _High betweenness centrality (0.221) - this node is a cross-community bridge._
- **Why does `api_status()` connect `Test Infrastructure` to `Alert & State Pipeline`, `Dashboard Integration Tests`, `Web Server API Layer`?**
  _High betweenness centrality (0.213) - this node is a cross-community bridge._
- **Why does `wait_for_run_complete()` connect `Dashboard Integration Tests` to `Test Infrastructure`?**
  _High betweenness centrality (0.174) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `_run_monitor_sync()` (e.g. with `download_ohlcv()` and `compute_metrics()`) actually correct?**
  _`_run_monitor_sync()` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Return signal summary for the most recent row with valid vol_60d.`, `Fetch an RSS feed and return a list of (title, pubdate_str) tuples.     Uses reg`, `Parse RFC 2822 pubDate strings into an aware UTC datetime.` to the rest of the system?**
  _48 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Alert & State Pipeline` be split into smaller, more focused modules?**
  _Cohesion score 0.12 - nodes in this community are weakly interconnected._