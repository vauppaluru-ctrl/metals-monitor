#!/usr/bin/env python3
"""
Metals Web Server — FastAPI dashboard for the Metals Monitor.

The scheduler is ON by default. On startup the server runs the monitor once
immediately, then every SCHEDULER_INTERVAL_SECS (default 3600). No manual
"Run Now" clicks needed for normal use.

  SCHEDULER_ENABLED=true   (default) built-in asyncio scheduler — runs on
                            startup + every hour. Works locally and in Docker.
  SCHEDULER_ENABLED=false  scheduler off; data only updates via "Run Now" or
                            an external process (e.g. LaunchAgent).

  NOTE: if the LaunchAgent is also active, both will run independently.
  They are safe to run concurrently (cooldown state prevents duplicate alerts),
  but you only need one. Prefer the web server scheduler for local use so the
  dashboard cache stays warm.

Start:
  uvicorn metals_web_server:app --host 0.0.0.0 --port 8747
  or: python metals_web_server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator

import pandas as pd
import uvicorn
import yfinance as yf
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ── Import monitor functions (same process, no subprocess overhead) ───────────
sys.path.insert(0, str(Path(__file__).parent))
from metals_live_monitor import (
    METALS, STATE_FILE, LOG_FILE,
    load_state, save_state,
    download_ohlcv, compute_metrics, generate_signals, evaluate_latest,
    download_macro_data, compute_context_signals,
    fetch_news_sentiment, format_news_context,
    build_notification, send_notification,
    record_cooldown, cooldown_allows, _append_recent_event,
    COOLDOWN_DAYS, SIGNAL_COLS, RSS_ENABLED,
)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

SCHEDULER_ENABLED       = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
SCHEDULER_INTERVAL_SECS = int(os.getenv("SCHEDULER_INTERVAL_SECS", "3600"))
PORT                    = int(os.getenv("PORT", "8747"))
LOG_TAIL_LINES          = 200
BASE_DIR                = Path(__file__).parent.resolve()

# ──────────────────────────────────────────────────────────────────────────────
# IN-MEMORY CACHE  (populated by each monitor run)
# ──────────────────────────────────────────────────────────────────────────────

_cache: dict = {
    "last_run":          None,   # ISO timestamp
    "last_run_ok":       None,   # bool
    "metals":            {},     # { metal: evaluate_latest() result }
    "news":              {},     # fetch_news_sentiment() result
    "running":           False,  # True while a monitor run is in progress
    "backtest":          None,   # backtest results dict
    "backtest_running":  False,  # True while backtest is in progress
}

# ──────────────────────────────────────────────────────────────────────────────
# SSE BROADCAST
# ──────────────────────────────────────────────────────────────────────────────

_sse_queues: list[asyncio.Queue] = []


def _broadcast(event_type: str, data: dict) -> None:
    payload = json.dumps({"type": event_type, "data": data, "ts": datetime.now().isoformat()})
    dead: list[asyncio.Queue] = []
    for q in _sse_queues:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_queues.remove(q)
        except ValueError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# MONITOR EXECUTION (runs in thread pool — never blocks the event loop)
# ──────────────────────────────────────────────────────────────────────────────

_executor    = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor")
_bt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")


def _run_monitor_sync() -> dict:
    """Execute one full monitor cycle; returns summary dict."""
    state        = load_state()
    alerts_fired = 0
    metal_results: dict = {}

    sentiment  = fetch_news_sentiment(list(METALS.keys()))
    macro_data = download_macro_data()

    for metal, cfg in METALS.items():
        ticker = cfg["primary"]
        try:
            raw         = download_ohlcv(ticker)
            metrics     = compute_metrics(raw)
            signals     = generate_signals(metrics)
            ctx_signals = compute_context_signals(metal, macro_data)
            result      = evaluate_latest(signals, ctx_signals)
        except Exception as exc:
            metal_results[metal] = {"error": str(exc)}
            continue

        metal_results[metal] = result
        today_str = result["date"]
        close     = result["close"]

        for direction, n_sig, cats in [
            ("bullish", result["n_bullish"], result["bull_cats"]),
            ("bearish", result["n_bearish"], result["bear_cats"]),
        ]:
            if n_sig < 2:
                continue
            if not cooldown_allows(state, metal, direction, today_str):
                continue
            news_ctx    = format_news_context(metal, direction, sentiment)
            title, body = build_notification(metal, ticker, direction, cats, close, news_ctx)
            send_notification(title, body)
            record_cooldown(state, metal, direction, today_str)
            _append_recent_event(state, metal, ticker, direction, cats, close, today_str)
            alerts_fired += 1

    save_state(state)

    _cache["last_run"]    = datetime.now().isoformat(timespec="seconds")
    _cache["last_run_ok"] = True
    _cache["metals"]      = metal_results
    _cache["news"]        = sentiment

    return {"alerts_fired": alerts_fired, "metals": metal_results}


async def _run_monitor_async() -> dict:
    loop = asyncio.get_running_loop()
    _cache["running"] = True
    _broadcast("run_start", {})
    try:
        result = await loop.run_in_executor(_executor, _run_monitor_sync)
        _broadcast("run_complete", result)
        return result
    except Exception as exc:
        _cache["last_run_ok"] = False
        _broadcast("run_error", {"error": str(exc)})
        raise
    finally:
        _cache["running"] = False


# ──────────────────────────────────────────────────────────────────────────────
# SCHEDULER
# ──────────────────────────────────────────────────────────────────────────────

async def _scheduler_loop() -> None:
    """Run immediately on first call, then sleep and repeat."""
    while True:
        try:
            await _run_monitor_async()
        except Exception:
            pass
        await asyncio.sleep(SCHEDULER_INTERVAL_SECS)


# ──────────────────────────────────────────────────────────────────────────────
# BACKTEST  (runs in separate thread pool — never blocks the event loop)
# ──────────────────────────────────────────────────────────────────────────────

def _run_backtest_sync(years: int = 10) -> dict:
    """Download {years} years of OHLCV per metal; detect cluster events; return forward returns.

    Uses the same compute_metrics / generate_signals logic as the live monitor so
    signal definitions stay in sync.  No state.json writes — read-only.
    """
    end_dt   = datetime.now()
    warmup   = 150  # calendar days before the analysis window (covers 60d vol warmup)
    start_dt = end_dt - timedelta(days=years * 365 + warmup)

    events: list = []
    summary: dict = {}

    for metal, cfg in METALS.items():
        ticker = cfg["primary"]
        try:
            raw = yf.download(
                ticker,
                start=start_dt.strftime("%Y-%m-%d"),
                end=(end_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
                auto_adjust=True, progress=False,
            )
        except Exception:
            continue
        if raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)
        df.index.name = "Date"
        df.sort_index(inplace=True)
        df.dropna(how="all", inplace=True)

        try:
            metrics = compute_metrics(df)
            signals = generate_signals(metrics)
        except Exception:
            continue

        # Filter to the 3-year analysis window; drop warmup rows before cutoff.
        # reset_index moves the DatetimeIndex into a "Date" column.
        cutoff = pd.Timestamp(end_dt - timedelta(days=years * 365))
        window = (
            signals[signals.index >= cutoff]
            .dropna(subset=["vol_60d"])
            .reset_index(drop=False)
        )
        if window.empty:
            continue

        last_i = -999  # positional index of the last event (3-trading-day cooldown)
        for i, row in window.iterrows():
            n_bull = sum(1 for c in SIGNAL_COLS if row[c] == "bullish")
            n_bear = sum(1 for c in SIGNAL_COLS if row[c] == "bearish")
            if n_bull < 2 and n_bear < 2:
                continue
            direction = "bullish" if n_bull >= n_bear else "bearish"
            if i - last_i < 3:  # positional cooldown (3 trading days)
                continue
            last_i = i

            close = float(row["Close"])
            fwd: dict = {}
            for d in (1, 3, 5, 10, 20):
                j = i + d
                if j < len(window):
                    fwd_close = float(window.iloc[j]["Close"])
                    fwd[f"fwd_{d}d"] = round((fwd_close - close) / close * 100, 2)
                else:
                    fwd[f"fwd_{d}d"] = None

            events.append({
                "date":       row["Date"].strftime("%Y-%m-%d"),
                "metal":      metal,
                "ticker":     ticker,
                "direction":  direction,
                "n_signals":  n_bull if direction == "bullish" else n_bear,
                "categories": [c for c in SIGNAL_COLS if row[c] == direction],
                "close":      round(close, 2),
                **fwd,
            })

        # Summary stats for this metal
        for dir_ in ("bullish", "bearish"):
            evs = [e for e in events if e["metal"] == metal and e["direction"] == dir_]
            if not evs:
                continue
            s: dict = {"metal": metal, "direction": dir_, "count": len(evs)}
            for days in (5, 10):
                rets = [e[f"fwd_{days}d"] for e in evs if e[f"fwd_{days}d"] is not None]
                if rets:
                    wins = [r for r in rets
                            if (dir_ == "bullish" and r > 0) or (dir_ == "bearish" and r < 0)]
                    s[f"win_rate_{days}d"] = round(len(wins) / len(rets) * 100, 1)
                    s[f"avg_ret_{days}d"]  = round(sum(rets) / len(rets), 2)
                else:
                    s[f"win_rate_{days}d"] = None
                    s[f"avg_ret_{days}d"]  = None
            summary[f"{metal}_{dir_}"] = s

    events.sort(key=lambda e: e["date"], reverse=True)

    # ── Per-signal accuracy stats ──────────────────────────────────────────────
    # For each trigger signal category, collect forward returns from every cluster
    # event in which that signal fired.  Win = price moved in the signalled direction.
    _sig_perf: dict = {}
    for ev in events:
        for cat in (ev.get("categories") or []):
            key = f"{ev['metal']}|{ev['direction']}|{cat}"
            if key not in _sig_perf:
                _sig_perf[key] = {
                    "metal": ev["metal"], "direction": ev["direction"],
                    "signal": cat, "count": 0,
                    "r5": [], "r10": [],
                }
            _sig_perf[key]["count"] += 1
            r5  = ev.get("fwd_5d")
            r10 = ev.get("fwd_10d")
            if r5  is not None: _sig_perf[key]["r5"].append(r5)
            if r10 is not None: _sig_perf[key]["r10"].append(r10)

    def _wr(rets: list, direction: str):
        if not rets: return None
        wins = [r for r in rets if (direction == "bullish" and r > 0) or (direction == "bearish" and r < 0)]
        return round(len(wins) / len(rets) * 100, 1)

    signal_stats = []
    for s in _sig_perf.values():
        d, r5, r10 = s["direction"], s["r5"], s["r10"]
        signal_stats.append({
            "metal":        s["metal"],
            "direction":    d,
            "signal":       s["signal"],
            "count":        s["count"],
            "win_rate_5d":  _wr(r5,  d),
            "avg_ret_5d":   round(sum(r5)  / len(r5),  2) if r5  else None,
            "win_rate_10d": _wr(r10, d),
            "avg_ret_10d":  round(sum(r10) / len(r10), 2) if r10 else None,
        })
    signal_stats.sort(key=lambda x: (x["metal"], x["direction"], x["signal"]))

    return {"events": events, "summary": summary, "signal_stats": signal_stats, "years": years}


async def _run_backtest_async() -> None:
    loop = asyncio.get_running_loop()
    _cache["backtest_running"] = True
    try:
        result = await loop.run_in_executor(_bt_executor, _run_backtest_sync)
        _cache["backtest"] = result
    except Exception as exc:
        _cache["backtest"] = {"error": str(exc), "events": [], "summary": {}, "years": 10}
    finally:
        _cache["backtest_running"] = False


# ──────────────────────────────────────────────────────────────────────────────
# FASTAPI APP
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Metals Monitor Dashboard", version="1.0")


@app.on_event("startup")
async def _startup() -> None:
    if SCHEDULER_ENABLED:
        # Scheduler runs the monitor immediately on startup, then every interval.
        # No manual "Run Now" needed for normal use.
        asyncio.create_task(_scheduler_loop())
    else:
        # Scheduler off — still run once on startup so the dashboard isn't empty.
        asyncio.create_task(_run_monitor_async())


# ── /stream — Server-Sent Events ──────────────────────────────────────────────

async def _sse_generator(queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    try:
        yield "data: {\"type\":\"connected\"}\n\n"
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=25)
                yield f"data: {payload}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        try:
            _sse_queues.remove(queue)
        except ValueError:
            pass


@app.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_queues.append(q)
    return StreamingResponse(
        _sse_generator(q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── /api/status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status() -> JSONResponse:
    state = load_state()
    return JSONResponse({
        "last_run":            _cache["last_run"],
        "last_run_ok":         _cache["last_run_ok"],
        "running":             _cache["running"],
        "scheduler_enabled":   SCHEDULER_ENABLED,
        "scheduler_interval":  SCHEDULER_INTERVAL_SECS,
        "cooldown_days":       COOLDOWN_DAYS,
        "rss_enabled":         RSS_ENABLED,
        "cooldowns":           {k: v for k, v in state.items()
                                if k.endswith("_last")},
    })


# ── /api/metals ───────────────────────────────────────────────────────────────

@app.get("/api/metals")
async def api_metals() -> JSONResponse:
    if not _cache["metals"]:
        state = load_state()
        return JSONResponse({"metals": {}, "from_cache": False,
                             "events": state.get("recent_events", [])})
    return JSONResponse({
        "metals":      _cache["metals"],
        "from_cache":  True,
        "last_run":    _cache["last_run"],
    })


# ── /api/events ───────────────────────────────────────────────────────────────

@app.get("/api/events")
async def api_events() -> JSONResponse:
    state = load_state()
    return JSONResponse({"events": state.get("recent_events", [])})


# ── /api/news ─────────────────────────────────────────────────────────────────

@app.get("/api/news")
async def api_news() -> JSONResponse:
    return JSONResponse({"news": _cache.get("news", {})})


# ── /api/logs ─────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_logs() -> JSONResponse:
    if not LOG_FILE.exists():
        return JSONResponse({"lines": []})
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return JSONResponse({"lines": lines[-LOG_TAIL_LINES:]})


# ── /api/run ──────────────────────────────────────────────────────────────────

@app.post("/api/run")
async def api_run() -> JSONResponse:
    if _cache["running"]:
        raise HTTPException(status_code=409, detail="Run already in progress")
    asyncio.create_task(_run_monitor_async())
    return JSONResponse({"status": "started"})


# ── /api/backtest ─────────────────────────────────────────────────────────────

@app.get("/api/backtest")
async def api_backtest() -> JSONResponse:
    return JSONResponse({
        "running": _cache["backtest_running"],
        "data":    _cache["backtest"],
    })


@app.post("/api/backtest/run")
async def api_backtest_run() -> JSONResponse:
    if _cache["backtest_running"]:
        raise HTTPException(status_code=409, detail="Backtest already running")
    asyncio.create_task(_run_backtest_async())
    return JSONResponse({"status": "started"})


# ──────────────────────────────────────────────────────────────────────────────
# DASHBOARD HTML
# ──────────────────────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Metals Monitor</title>
<style>
  /* ════════════════════════════════════════════════════════════
     THEME TOKENS — single source of truth for all visual style.
     Component rules below reference only var(--*) tokens.
     To add a new theme, add a [data-theme="x"] block here.
     ════════════════════════════════════════════════════════════ */

  /* ── Typography (shared across all themes) ──────────────── */
  :root {
    --font:            "SF Mono", Consolas, "Courier New", monospace;
    --font-size-base:  14px;
    --font-size-lg:    16px;
    --font-size-md:    15px;
    --font-size-sm:    12px;
    --font-size-xs:    11px;
    --font-size-2xs:   10px;
    --line-height:     1.5;
    --radius-sm:       3px;
    --radius-md:       4px;
    --radius-lg:       8px;
    --radius-pill:     12px;
  }

  /* ── Signal & metal accent colors (fixed across themes) ─── */
  :root {
    --gold:    #f5c842;
    --silver:  #a8b4c8;
    --copper:  #d4804a;
    --bull:    #3ddc84;
    --bear:    #ff5f5f;
    --accent:  #5b8dee;
  }

  /* ── Dark theme (default) ───────────────────────────────── */
  :root,
  [data-theme="dark"] {
    --bg:             #0f1117;
    --card:           #1a1d27;
    --border:         #2a2d3a;
    --text:           #e2e4ec;
    --muted:          #6b6f80;
    --log-bg:         #0a0c12;
    --tab-active-bg:  #141824;
    --bull-bg:        #1a3a28;
    --bear-bg:        #3a1a1a;
    --neutral-bg:     #252830;
    --run-btn-hover-text: #ffffff;
  }

  /* ── Light theme ────────────────────────────────────────── */
  [data-theme="light"] {
    --bg:             #f3f4f9;
    --card:           #ffffff;
    --border:         #dde1ed;
    --text:           #1c1f2e;
    --muted:          #6b7080;
    --log-bg:         #f8f9fc;
    --tab-active-bg:  #eaedf8;
    --bull-bg:        #e4f5ec;
    --bear-bg:        #fce8e8;
    --neutral-bg:     #eef0f5;
    --run-btn-hover-text: #ffffff;
  }

  /* ════════════════════════════════════════════════════════════
     COMPONENTS — all colors and fonts from tokens only
     ════════════════════════════════════════════════════════════ */

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background:   var(--bg);
    color:        var(--text);
    font-family:  var(--font);
    font-size:    var(--font-size-base);
    line-height:  var(--line-height);
    transition:   background-color 0.2s, color 0.2s, border-color 0.2s;
  }

  /* ── Header ─────────────────────────────────────────────── */
  header {
    display:        flex;
    align-items:    center;
    gap:            12px;
    padding:        16px 20px;
    border-bottom:  1px solid var(--border);
    background:     var(--card);
  }
  header h1 {
    font-size:      var(--font-size-lg);
    font-weight:    700;
    letter-spacing: .5px;
  }
  #status-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--muted);
    flex-shrink: 0;
  }
  #status-dot.ok  { background: var(--bull); }
  #status-dot.err { background: var(--bear); }
  #status-dot.run { background: var(--gold); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.3 } }

  #last-run { margin-left: auto; font-size: var(--font-size-xs); color: var(--muted); }

  /* ── Header buttons (shared base) ───────────────────────── */
  .hdr-btn {
    padding:        5px 14px;
    border-radius:  var(--radius-md);
    cursor:         pointer;
    font-size:      var(--font-size-sm);
    font-family:    var(--font);
    background:     transparent;
    transition:     background-color 0.15s, color 0.15s;
  }
  #run-btn {
    border: 1px solid var(--accent);
    color:  var(--accent);
  }
  #run-btn:hover:not(:disabled) {
    background: var(--accent);
    color:      var(--run-btn-hover-text);
  }
  #run-btn:disabled { opacity: 0.55; cursor: default; }

  #notif-btn {
    border: 1px solid var(--muted);
    color:  var(--muted);
  }
  #notif-btn.granted { border-color: var(--bull); color: var(--bull); }

  #theme-btn {
    border: 1px solid var(--border);
    color:  var(--muted);
    min-width: 68px;
  }
  #theme-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* ── Main layout ─────────────────────────────────────────── */
  main { padding: 20px; max-width: 1200px; margin: 0 auto; }

  .section-title {
    font-size:      var(--font-size-xs);
    text-transform: uppercase;
    letter-spacing: 1px;
    color:          var(--muted);
    margin:         24px 0 10px;
  }

  /* ── Metal cards ─────────────────────────────────────────── */
  .metals-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 14px;
  }
  .metal-card {
    background:    var(--card);
    border:        1px solid var(--border);
    border-radius: var(--radius-lg);
    padding:       16px;
  }
  .metal-card .name {
    font-size:     var(--font-size-md);
    font-weight:   700;
    margin-bottom: 2px;
  }
  .metal-card .name.gold   { color: var(--gold); }
  .metal-card .name.silver { color: var(--silver); }
  .metal-card .name.copper { color: var(--copper); }
  .metal-card .meta { font-size: var(--font-size-xs); color: var(--muted); margin-bottom: 10px; }

  /* ── Signal rows ─────────────────────────────────────────── */
  .signals { display: flex; flex-direction: column; gap: 5px; }
  .sig-row {
    display:         flex;
    justify-content: space-between;
    align-items:     center;
    font-size:       var(--font-size-sm);
    padding:         3px 0;
    border-bottom:   1px solid var(--border);
  }
  .sig-row:last-child { border: none; }
  .sig-label { color: var(--muted); }
  .sig-val {
    font-weight:   700;
    padding:       1px 7px;
    border-radius: var(--radius-sm);
    font-size:     var(--font-size-xs);
  }
  .sig-val.bullish { background: var(--bull-bg); color: var(--bull); }
  .sig-val.bearish { background: var(--bear-bg); color: var(--bear); }
  .sig-val.neutral { background: var(--neutral-bg); color: var(--muted); }

  /* ── Cluster badge ───────────────────────────────────────── */
  .cluster-badge {
    display:       inline-block;
    padding:       3px 10px;
    border-radius: var(--radius-pill);
    font-size:     var(--font-size-xs);
    font-weight:   700;
    margin-top:    10px;
  }
  .cluster-badge.bull { background: var(--bull-bg); color: var(--bull); }
  .cluster-badge.bear { background: var(--bear-bg); color: var(--bear); }
  .cluster-badge.none { background: var(--neutral-bg); color: var(--muted); }

  /* ── Table ───────────────────────────────────────────────── */
  table { width: 100%; border-collapse: collapse; font-size: var(--font-size-sm); }
  th {
    text-align:     left;
    padding:        7px 10px;
    font-size:      var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: .8px;
    color:          var(--muted);
    border-bottom:  1px solid var(--border);
  }
  td { padding: 7px 10px; border-bottom: 1px solid var(--border); }
  tr:last-child td { border: none; }

  .pill {
    display:       inline-block;
    padding:       1px 7px;
    border-radius: var(--radius-pill);
    font-size:     var(--font-size-2xs);
  }
  .pill.bull { background: var(--bull-bg); color: var(--bull); }
  .pill.bear { background: var(--bear-bg); color: var(--bear); }

  /* ── Tabs ────────────────────────────────────────────────── */
  .tabs { display: flex; gap: 2px; margin-bottom: 10px; }
  .tab {
    padding:       5px 14px;
    border:        1px solid var(--border);
    border-radius: var(--radius-md);
    cursor:        pointer;
    font-size:     var(--font-size-sm);
    font-family:   var(--font);
    color:         var(--muted);
    background:    transparent;
  }
  .tab.active {
    border-color: var(--accent);
    color:        var(--accent);
    background:   var(--tab-active-bg);
  }
  .tab-pane          { display: none; }
  .tab-pane.active   { display: block; }

  /* ── Log box ─────────────────────────────────────────────── */
  #log-box {
    background:   var(--log-bg);
    border:       1px solid var(--border);
    border-radius: var(--radius-lg);
    padding:      12px;
    height:       320px;
    overflow-y:   auto;
    font-size:    var(--font-size-xs);
    line-height:  1.6;
    white-space:  pre-wrap;
    word-break:   break-all;
    color:        var(--text);
  }

  /* ── News ────────────────────────────────────────────────── */
  .news-item { padding: 8px 0; border-bottom: 1px solid var(--border); font-size: var(--font-size-sm); }
  .news-item:last-child { border: none; }
  .news-source { font-size: var(--font-size-2xs); color: var(--muted); margin-bottom: 2px; }

  /* ── Utility ─────────────────────────────────────────────── */
  .no-data   { color: var(--muted); font-size: var(--font-size-sm); padding: 16px 0; }
  .error-msg { color: var(--bear);  font-size: var(--font-size-sm); padding: 16px 0; }

  /* ── Backtest ────────────────────────────────────────────── */
  .bt-header {
    display:         flex;
    align-items:     center;
    justify-content: space-between;
    margin-bottom:   12px;
  }
  .bt-status { font-size: var(--font-size-xs); color: var(--muted); }
  #bt-run-btn { border: 1px solid var(--accent); color: var(--accent); }
  #bt-run-btn:hover:not(:disabled) { background: var(--accent); color: var(--run-btn-hover-text); }
  #bt-run-btn:disabled { opacity: 0.55; cursor: default; }
  .bt-summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
  }
  .bt-card {
    background:    var(--card);
    border:        1px solid var(--border);
    border-radius: var(--radius-lg);
    padding:       14px;
  }
  .bt-card-title { font-size: var(--font-size-sm); font-weight: 700; margin-bottom: 8px; }
  .bt-stat {
    display:         flex;
    justify-content: space-between;
    font-size:       var(--font-size-xs);
    padding:         3px 0;
    border-bottom:   1px solid var(--border);
  }
  .bt-stat:last-child { border: none; }
  .bt-stat-label { color: var(--muted); }
  .bt-stat-val   { font-weight: 700; }
  .bt-table-wrap { overflow-x: auto; }
  .fwd-pos { color: var(--bull); font-weight: 700; }
  .fwd-neg { color: var(--bear); font-weight: 700; }
  .fwd-nil { color: var(--muted); }

  /* ── Info icon & signal explanation popup ───────────────────── */
  .info-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px; height: 14px;
    border-radius: 50%;
    font-size: 9px;
    border: 1px solid var(--muted);
    color: var(--muted);
    cursor: pointer;
    margin-left: 5px;
    background: transparent;
    font-family: var(--font);
    line-height: 1;
    flex-shrink: 0;
    user-select: none;
    vertical-align: middle;
  }
  .info-icon:hover { border-color: var(--accent); color: var(--accent); }

  #sig-popup {
    position: fixed;
    background: var(--card);
    border: 1px solid var(--accent);
    border-radius: var(--radius-lg);
    padding: 12px 36px 12px 14px;
    width: 280px;
    font-size: var(--font-size-xs);
    line-height: 1.65;
    z-index: 1000;
    display: none;
    box-shadow: 0 6px 24px rgba(0,0,0,0.35);
  }
  #sig-popup.visible { display: block; }
  #sig-popup-title {
    font-weight: 700;
    margin-bottom: 6px;
    color: var(--accent);
    font-size: var(--font-size-sm);
  }
  #sig-popup-body { color: var(--text); }
  #sig-popup-close {
    position: absolute; top: 6px; right: 8px;
    cursor: pointer; color: var(--muted);
    background: transparent; border: none;
    font-family: var(--font); font-size: 18px; line-height: 1;
  }
  #sig-popup-close:hover { color: var(--text); }

  /* ── Context signal section (displayed below trigger signals) ── */
  .context-divider {
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: .8px;
    color: var(--muted);
    margin: 8px 0 4px;
    padding-top: 6px;
    border-top: 1px dashed var(--border);
  }

  /* ── Backtest chart & signal accuracy table ─────────────────── */
  .bt-sub-title {
    font-size: var(--font-size-sm);
    font-weight: 700;
    color: var(--muted);
    margin: 18px 0 8px;
    text-transform: uppercase;
    letter-spacing: .5px;
  }
  .bt-chart-wrap {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 16px 16px 10px;
    margin-bottom: 16px;
  }
  #bt-chart { display: block; width: 100%; height: 220px; cursor: default; }
  .bt-chart-legend {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-top: 8px;
    font-size: var(--font-size-2xs);
    color: var(--muted);
  }
  .bt-legend-item { display: flex; align-items: center; gap: 5px; }
  .bt-legend-swatch { width: 14px; height: 8px; border-radius: 2px; display: inline-block; }
  .bt-sig-wrap { overflow-x: auto; margin-bottom: 16px; }
  .bt-sig-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-xs);
  }
  .bt-sig-table th {
    text-align: left;
    padding: 5px 8px;
    border-bottom: 2px solid var(--border);
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: .5px;
    color: var(--muted);
    white-space: nowrap;
  }
  .bt-sig-table td { padding: 5px 8px; border-bottom: 1px solid var(--border); }
  .wr-good { color: var(--bull); font-weight: 700; }
  .wr-bad  { color: var(--bear); font-weight: 700; }
  .wr-mid  { color: var(--text); }
</style>
</head>
<body>
<header>
  <div id="status-dot"></div>
  <h1>Metals Monitor</h1>
  <span id="last-run">No data yet</span>
  <button id="theme-btn"  class="hdr-btn" onclick="toggleTheme()">Light</button>
  <button id="notif-btn"  class="hdr-btn" onclick="requestNotifPermission()">Enable Notifications</button>
  <button id="run-btn"    class="hdr-btn" onclick="triggerRun()">Run Now</button>
</header>
<main>
  <div class="section-title">Signal Status</div>
  <div class="metals-grid" id="metals-grid">
    <div class="no-data">Waiting for first run…</div>
  </div>

  <div class="section-title">Recent Alerts</div>
  <table id="events-table">
    <thead><tr>
      <th>Date</th><th>Metal</th><th>Ticker</th>
      <th>Direction</th><th>Signals</th><th>Close</th>
    </tr></thead>
    <tbody id="events-body">
      <tr><td colspan="6" class="no-data" style="padding:12px 10px">No alerts yet</td></tr>
    </tbody>
  </table>

  <div class="section-title">News &amp; Logs</div>
  <div class="tabs">
    <button class="tab active" onclick="switchTab('news')">News</button>
    <button class="tab"        onclick="switchTab('logs')">Logs</button>
  </div>
  <div id="pane-news" class="tab-pane active">
    <div class="tabs" id="news-metal-tabs">
      <button class="tab active" onclick="switchNewsMetal('Gold')">Gold</button>
      <button class="tab"        onclick="switchNewsMetal('Silver')">Silver</button>
      <button class="tab"        onclick="switchNewsMetal('Copper')">Copper</button>
    </div>
    <div id="news-content"><div class="no-data">No news loaded yet</div></div>
  </div>
  <div id="pane-logs" class="tab-pane">
    <div id="log-box">Loading logs…</div>
  </div>

  <div class="section-title">10-Year Backtest</div>
  <div class="bt-header">
    <span class="bt-status" id="bt-status">No backtest data — click Run Backtest</span>
    <button class="hdr-btn" id="bt-run-btn" onclick="triggerBacktest()">Run Backtest</button>
  </div>
  <div class="bt-summary-grid" id="bt-summary-grid"></div>

  <div class="bt-sub-title">Win Rate by Metal &amp; Direction</div>
  <div class="bt-chart-wrap">
    <canvas id="bt-chart"></canvas>
    <div class="bt-chart-legend" id="bt-chart-legend"></div>
  </div>

  <div class="bt-sub-title">Per-Signal Lead Indicator Accuracy</div>
  <div class="bt-sig-wrap">
    <table class="bt-sig-table">
      <thead><tr>
        <th>Metal</th><th>Direction</th><th>Indicator</th><th>Events</th>
        <th>Win% 5d</th><th>Avg Ret 5d</th><th>Win% 10d</th><th>Avg Ret 10d</th>
      </tr></thead>
      <tbody id="bt-sig-body">
        <tr><td colspan="8" class="no-data" style="padding:12px 8px">Run backtest to see per-indicator accuracy</td></tr>
      </tbody>
    </table>
  </div>

  <div class="bt-sub-title">All Cluster Events</div>
  <div class="bt-table-wrap">
    <table id="bt-events-table">
      <thead><tr>
        <th>Date</th><th>Metal</th><th>Dir</th><th>Sigs</th><th>Close</th>
        <th>1d%</th><th>3d%</th><th>5d%</th><th>10d%</th><th>20d%</th>
      </tr></thead>
      <tbody id="bt-events-body">
        <tr><td colspan="10" class="no-data" style="padding:12px 10px">No backtest data yet — click Run Backtest (~60–120s)</td></tr>
      </tbody>
    </table>
  </div>
</main>

<div id="sig-popup">
  <button id="sig-popup-close" onclick="hideSigInfo()">×</button>
  <div id="sig-popup-title"></div>
  <div id="sig-popup-body"></div>
</div>

<script>
"use strict";

// ── Theme ─────────────────────────────────────────────────────────────────────
let _btData = null;  // hoisted before applyTheme IIFE to avoid TDZ
// Single function that owns ALL theme state. Everything else calls applyTheme().
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = document.getElementById("theme-btn");
  if (btn) btn.textContent = theme === "dark" ? "Light" : "Dark";
  try { localStorage.setItem("mm-theme", theme); } catch(_) {}
  if (_btData) renderBtChart(_btData.summary);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(current === "dark" ? "light" : "dark");
}

// Resolve initial theme: localStorage > OS preference > dark
(function() {
  let saved;
  try { saved = localStorage.getItem("mm-theme"); } catch(_) {}
  const osLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  applyTheme(saved || (osLight ? "light" : "dark"));
})();

// ── XSS-safe escaping ─────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ── State ─────────────────────────────────────────────────────────────────────
let _newsData = {};
let _activeNewsMetal = "Gold";

// ── SSE ───────────────────────────────────────────────────────────────────────
const es = new EventSource("/stream");
es.onmessage = function(e) {
  try {
    const msg = JSON.parse(e.data);
    if (msg.type === "connected")    { setDot("ok"); }
    if (msg.type === "run_start")    { setDot("run"); }
    if (msg.type === "run_complete") {
      setDot("ok");
      refreshAll();
      if (Notification.permission === "granted") {
        const alerts = msg.data && msg.data.alerts_fired;
        if (alerts && alerts > 0) {
          new Notification("Metals Monitor", {
            body: String(alerts) + " cluster alert(s) fired — check the dashboard.",
          });
        }
      }
    }
    if (msg.type === "run_error")    { setDot("err"); }
  } catch(_) {}
};
es.onerror = function() { setDot("err"); };

function setDot(state) {
  const d = document.getElementById("status-dot");
  d.className = state;
}

// ── Data refresh ──────────────────────────────────────────────────────────────
async function refreshAll() {
  await Promise.all([refreshMetals(), refreshEvents(), refreshNews(), refreshLogs()]);
}

async function refreshMetals() {
  try {
    const r = await fetch("/api/metals");
    const d = await r.json();
    if (d.last_run) {
      document.getElementById("last-run").textContent =
        "Last run: " + escHtml(d.last_run);
    }
    renderMetals(d.metals || {});
  } catch(e) { console.error("metals fetch", e); }
}

async function refreshEvents() {
  try {
    const r = await fetch("/api/events");
    const d = await r.json();
    renderEvents(d.events || []);
  } catch(e) { console.error("events fetch", e); }
}

async function refreshNews() {
  try {
    const r = await fetch("/api/news");
    const d = await r.json();
    _newsData = d.news || {};
    renderNews(_activeNewsMetal);
  } catch(e) { console.error("news fetch", e); }
}

async function refreshLogs() {
  try {
    const r = await fetch("/api/logs");
    const d = await r.json();
    const box = document.getElementById("log-box");
    const lines = (d.lines || []).map(l => escHtml(l)).join("\\n");
    box.innerHTML = lines || "<span style='color:var(--muted)'>No log entries</span>";
    box.scrollTop = box.scrollHeight;
  } catch(e) { console.error("logs fetch", e); }
}

// ── Render helpers ────────────────────────────────────────────────────────────
const METAL_CLASS = { Gold:"gold", Silver:"silver", Copper:"copper" };

// SIG_INFO — label and explanation for every signal (trigger and context).
// body text is shown in the popup when the user clicks the info icon (i).
const SIG_INFO = {
  futures_curve_proxy: {
    label: "Futures Curve",
    body: "3-day price momentum vs a volatility-adjusted threshold. Bullish when the 3-day gain exceeds 1.25x the 60-day daily volatility x sqrt(3) — a rapid, outsized move. This proxies futures backwardation: when near-term supply is tight, buyers pay up urgently over 1-3 days. Counts toward cluster trigger."
  },
  etf_pressure_proxy: {
    label: "ETF Pressure",
    body: "Abnormal trading volume paired with a directional price move. Bullish: volume z-score > 1.5 (well above the 60-day average) AND the price rises on that day. High volume + direction = large investors moving in or out via ETF creation/redemption flow. Counts toward cluster trigger."
  },
  physical_tightness_proxy: {
    label: "Physical Tightness",
    body: "Price breaking to a new 20-day high (or low) while the intraday range expands above its 60-day average. An expanding range on a breakout signals urgency — buyers paying up to acquire physical metal, not just passive paper accumulation. Counts toward cluster trigger."
  },
  demand_expectations_proxy: {
    label: "Demand Expectations",
    body: "Three moving averages aligning across different timeframes. Bullish: 10-day MA above 30-day MA, price above 50-day MA, and positive 20-day return — all three must agree. This captures sustained directional repricing across weeks, not a single-day spike. Counts toward cluster trigger."
  },
  dollar_trend: {
    label: "Dollar Trend (Context)",
    body: "10-day momentum of UUP (US Dollar ETF), inverted for metals. Bullish when the dollar fell more than 1.5% in 10 days — a weaker dollar makes metals cheaper for non-US buyers globally, lifting demand. Bearish when the dollar rose more than 1.5%. Gold has the strongest inverse relationship (historical correlation -0.6 to -0.8). Context only — does not count toward cluster trigger."
  },
  vix_regime: {
    label: "Fear Index / VIX (Context)",
    body: "CBOE Volatility Index level and 5-day direction. For Gold and Silver: bullish when VIX is 20 or above and rising (fear building, safe-haven demand activates). Bearish when VIX is below 15 and falling (calm markets, no safe-haven need). For Copper: the logic inverts — high VIX signals recession fear, which reduces industrial demand. Context only — does not count toward cluster trigger."
  },
  cross_metal_ratio: {
    label: "Cross-Metal Ratio (Context)",
    body: "Relative value signal, specific to each metal. Silver: Gold/Silver ratio above 80 = historically undervalued (after 2008 peak of 84:1, silver rallied 391%). Gold: 10-day momentum of the G/S ratio — rising means institutional rotation toward gold. Copper: 10-day momentum of Copper/Gold ratio — rising signals an economic growth regime. Context only — does not count toward cluster trigger."
  }
};

function renderMetals(metals) {
  const grid = document.getElementById("metals-grid");
  if (!Object.keys(metals).length) {
    grid.innerHTML = "<div class='no-data'>No signal data — click Run Now</div>";
    return;
  }
  let html = "";
  for (const [metal, data] of Object.entries(metals)) {
    const cls = escHtml(METAL_CLASS[metal] || "");
    const eName = escHtml(metal);
    if (data.error) {
      html += `<div class="metal-card">
        <div class="name ${cls}">${eName}</div>
        <div class="error-msg">${escHtml(data.error)}</div></div>`;
      continue;
    }
    const close = typeof data.close === "number" ? data.close.toFixed(2) : "—";
    const date  = escHtml(data.date || "");
    let sigRows = "";
    // Trigger signals — count toward the cluster (>=2 fires an alert)
    for (const [key, val] of Object.entries(data.all_signals || {})) {
      const info  = SIG_INFO[key] || {};
      const label = escHtml(info.label || key.replace(/_/g, " "));
      const v     = escHtml(val);
      sigRows += `<div class="sig-row">
        <span class="sig-label">${label}<button class="info-icon" onclick="showSigInfo('${key}',event)">i</button></span>
        <span class="sig-val ${v}">${v}</span></div>`;
    }
    // Context signals — cross-asset macro signals; display-only, do NOT affect cluster trigger
    const ctx = data.context_signals || {};
    if (Object.keys(ctx).length > 0) {
      sigRows += `<div class="context-divider">Context signals (not in cluster)</div>`;
      for (const [key, val] of Object.entries(ctx)) {
        const info  = SIG_INFO[key] || {};
        const label = escHtml(info.label || key.replace(/_/g, " "));
        const v     = escHtml(val);
        sigRows += `<div class="sig-row">
          <span class="sig-label">${label}<button class="info-icon" onclick="showSigInfo('${key}',event)">i</button></span>
          <span class="sig-val ${v}">${v}</span></div>`;
      }
    }
    const nBull = data.n_bullish || 0;
    const nBear = data.n_bearish || 0;
    let badge = "";
    if (nBull >= 2) {
      badge = `<div class="cluster-badge bull">▲ Bullish cluster (${nBull}/4)</div>`;
    } else if (nBear >= 2) {
      badge = `<div class="cluster-badge bear">▼ Bearish cluster (${nBear}/4)</div>`;
    } else {
      badge = `<div class="cluster-badge none">No cluster (bull:${nBull} bear:${nBear})</div>`;
    }
    html += `<div class="metal-card">
      <div class="name ${cls}">${eName}</div>
      <div class="meta">${escHtml(data.ticker || "")} · $${escHtml(String(close))} · ${date}</div>
      <div class="signals">${sigRows}</div>
      ${badge}</div>`;
  }
  grid.innerHTML = html;
}

function renderEvents(events) {
  const tbody = document.getElementById("events-body");
  if (!events.length) {
    tbody.innerHTML = "<tr><td colspan='6' class='no-data' style='padding:12px 10px'>No alerts yet</td></tr>";
    return;
  }
  let html = "";
  for (const ev of events) {
    const dir  = escHtml(ev.direction || "");
    const cats = (ev.categories || []).map(c => escHtml(c.replace("_proxy","").replace(/_/g," "))).join(", ");
    html += `<tr>
      <td>${escHtml(ev.date || "")}</td>
      <td>${escHtml(ev.metal || "")}</td>
      <td>${escHtml(ev.ticker || "")}</td>
      <td><span class="pill ${dir === "bullish" ? "bull" : "bear"}">${dir}</span></td>
      <td>${cats}</td>
      <td>$${escHtml(String(typeof ev.close === "number" ? ev.close.toFixed(2) : ev.close))}</td>
    </tr>`;
  }
  tbody.innerHTML = html;
}

function renderNews(metal) {
  const box  = document.getElementById("news-content");
  const data = _newsData[metal];
  if (!data) {
    box.innerHTML = "<div class='no-data'>No news data — run the monitor first</div>";
    return;
  }
  const headlines = data.relevant || [];
  if (!headlines.length) {
    box.innerHTML = `<div class='no-data'>No recent ${escHtml(metal)} headlines found</div>`;
    return;
  }
  let html = "";
  for (const h of headlines) {
    html += `<div class="news-item"><div>${escHtml(h)}</div></div>`;
  }
  box.innerHTML = html;
}

// ── Tab switching ──────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
  document.getElementById("pane-" + name).classList.add("active");
  document.querySelectorAll(".tabs:first-of-type .tab").forEach((t, i) => {
    t.classList.toggle("active", ["news","logs"][i] === name);
  });
  if (name === "logs") refreshLogs();
}

function switchNewsMetal(metal) {
  _activeNewsMetal = metal;
  document.querySelectorAll("#news-metal-tabs .tab").forEach(t => {
    t.classList.toggle("active", t.textContent === metal);
  });
  renderNews(metal);
}

// ── Run now ───────────────────────────────────────────────────────────────────
async function triggerRun() {
  const btn = document.getElementById("run-btn");
  btn.disabled = true;
  btn.textContent = "Running…";
  setDot("run");

  try {
    const resp = await fetch("/api/run", { method: "POST" });
    // 409 = already running — just let the existing poll below catch completion
    if (!resp.ok && resp.status !== 409) throw new Error("HTTP " + resp.status);
  } catch(e) {
    console.error("run start failed:", e);
    btn.disabled = false;
    btn.textContent = "Run Now";
    setDot("err");
    return;
  }

  // Show loading placeholder in the signal cards immediately.
  document.getElementById("metals-grid").innerHTML =
    "<div class='no-data'>Downloading &amp; computing signals… (~30s)</div>";

  // Poll /api/status every 2 s.
  // Stop only when last_run is set (a run actually completed), not just when
  // running=false — avoids a race where the first poll fires before the asyncio
  // task has started, sees running=false + last_run=null, and quits early.
  const deadline = Date.now() + 180_000;
  const poll = setInterval(async () => {
    try {
      const s = await fetch("/api/status");
      const d = await s.json();
      const done = d.last_run !== null && !d.running;
      const timedOut = Date.now() > deadline;
      if (done || timedOut) {
        clearInterval(poll);
        btn.disabled = false;
        btn.textContent = "Run Now";
        setDot(d.last_run_ok ? "ok" : "err");
        refreshAll();
      }
    } catch(_) {}
  }, 2000);
}

// ── Web Notifications ─────────────────────────────────────────────────────────
function requestNotifPermission() {
  if (!("Notification" in window)) {
    alert("This browser does not support Web Notifications.");
    return;
  }
  Notification.requestPermission().then(p => {
    const btn = document.getElementById("notif-btn");
    if (p === "granted") {
      btn.textContent = "Notifications On";
      btn.className   = "granted";
    }
  });
}

// ── Backtest ──────────────────────────────────────────────────────────────────
async function triggerBacktest() {
  const btn    = document.getElementById("bt-run-btn");
  const status = document.getElementById("bt-status");
  btn.disabled       = true;
  btn.textContent    = "Running…";
  status.textContent = "Downloading 10 years of data per metal (~60–120s)…";

  try {
    const resp = await fetch("/api/backtest/run", { method: "POST" });
    if (!resp.ok && resp.status !== 409) throw new Error("HTTP " + resp.status);
  } catch(e) {
    console.error("backtest start failed:", e);
    btn.disabled       = false;
    btn.textContent    = "Run Backtest";
    status.textContent = "Error starting backtest — check logs";
    return;
  }

  const deadline = Date.now() + 600_000;  // 10 min for 10yr download
  const poll = setInterval(async () => {
    try {
      const r = await fetch("/api/backtest");
      const d = await r.json();
      if (!d.running && d.data) {
        clearInterval(poll);
        btn.disabled       = false;
        btn.textContent    = "Run Backtest";
        const n = (d.data.events || []).length;
        status.textContent = "Complete — " + n + " cluster events in 10yr window";
        _btData = d.data;
        renderBtChart(d.data.summary);
        renderBtSignalStats(d.data.signal_stats || []);
        renderBtSummary(d.data.summary);
        renderBtEvents(d.data.events);
      } else if (Date.now() > deadline) {
        clearInterval(poll);
        btn.disabled       = false;
        btn.textContent    = "Run Backtest";
        status.textContent = "Timed out — check logs";
      }
    } catch(_) {}
  }, 3000);
}

function fwdCell(val) {
  if (val === null || val === undefined) return "<td class='fwd-nil'>—</td>";
  const cls  = val > 0 ? "fwd-pos" : val < 0 ? "fwd-neg" : "fwd-nil";
  const sign = val > 0 ? "+" : "";
  return "<td class='" + cls + "'>" + sign + val.toFixed(2) + "%</td>";
}

function renderBtSummary(summary) {
  const grid = document.getElementById("bt-summary-grid");
  if (!summary || !Object.keys(summary).length) { grid.innerHTML = ""; return; }
  let html = "";
  for (const [, s] of Object.entries(summary)) {
    const cls    = escHtml(METAL_CLASS[s.metal] || "");
    const dirCls = s.direction === "bullish" ? "bull" : "bear";
    const dirLbl = s.direction === "bullish" ? "▲ Bullish" : "▼ Bearish";
    const fmt = (v, pct) =>
      v !== null && v !== undefined ? (pct && v >= 0 ? "+" : "") + v + "%" : "—";
    html +=
      "<div class='bt-card'>" +
        "<div class='bt-card-title'>" +
          "<span style='color:var(--" + cls + ")'>" + escHtml(s.metal) + "</span>" +
          "<span class='pill " + dirCls + "' style='margin-left:6px'>" + dirLbl + "</span>" +
        "</div>" +
        "<div class='bt-stat'><span class='bt-stat-label'>Events (10yr)</span>" +
          "<span class='bt-stat-val'>" + s.count + "</span></div>" +
        "<div class='bt-stat'><span class='bt-stat-label'>Win rate 5d</span>" +
          "<span class='bt-stat-val'>" + fmt(s.win_rate_5d, false) + "</span></div>" +
        "<div class='bt-stat'><span class='bt-stat-label'>Avg return 5d</span>" +
          "<span class='bt-stat-val'>" + fmt(s.avg_ret_5d, true) + "</span></div>" +
        "<div class='bt-stat'><span class='bt-stat-label'>Win rate 10d</span>" +
          "<span class='bt-stat-val'>" + fmt(s.win_rate_10d, false) + "</span></div>" +
        "<div class='bt-stat'><span class='bt-stat-label'>Avg return 10d</span>" +
          "<span class='bt-stat-val'>" + fmt(s.avg_ret_10d, true) + "</span></div>" +
      "</div>";
  }
  grid.innerHTML = html;
}

function renderBtEvents(events) {
  const tbody = document.getElementById("bt-events-body");
  if (!events || !events.length) {
    tbody.innerHTML = "<tr><td colspan='10' class='no-data' style='padding:12px 10px'>No cluster events detected in 3-year window</td></tr>";
    return;
  }
  let html = "";
  for (const ev of events) {
    const dir  = escHtml(ev.direction || "");
    const cls  = escHtml(METAL_CLASS[ev.metal] || "");
    const cats = (ev.categories || []).map(c => c.replace("_proxy","").replace(/_/g," ")).join(", ");
    html +=
      "<tr>" +
        "<td>" + escHtml(ev.date || "") + "</td>" +
        "<td style='color:var(--" + cls + ")'>" + escHtml(ev.metal || "") + "</td>" +
        "<td><span class='pill " + (dir === "bullish" ? "bull" : "bear") + "'>" + dir + "</span></td>" +
        "<td title='" + escHtml(cats) + "'>" + (ev.n_signals || 0) + "/4</td>" +
        "<td>$" + escHtml(String(ev.close || "")) + "</td>" +
        fwdCell(ev.fwd_1d) + fwdCell(ev.fwd_3d) + fwdCell(ev.fwd_5d) +
        fwdCell(ev.fwd_10d) + fwdCell(ev.fwd_20d) +
      "</tr>";
  }
  tbody.innerHTML = html;
}

// ── Backtest chart (Canvas 2D) ────────────────────────────────────────────────
function renderBtChart(summary) {
  const canvas = document.getElementById("bt-chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const DPR = window.devicePixelRatio || 1;
  const W   = canvas.offsetWidth || 600;
  const H   = 220;
  canvas.width  = Math.round(W * DPR);
  canvas.height = Math.round(H * DPR);
  canvas.style.width  = W + "px";
  canvas.style.height = H + "px";
  ctx.scale(DPR, DPR);
  ctx.clearRect(0, 0, W, H);

  const st = getComputedStyle(document.documentElement);
  const C = {
    gold:   st.getPropertyValue("--gold").trim()   || "#d4a843",
    silver: st.getPropertyValue("--silver").trim() || "#9eaab5",
    copper: st.getPropertyValue("--copper").trim() || "#b87044",
    text:   st.getPropertyValue("--text").trim()   || "#e2e8f0",
    muted:  st.getPropertyValue("--muted").trim()  || "#6b7280",
    border: st.getPropertyValue("--border").trim() || "#374151",
  };
  const ORDER = [
    { metal:"Gold",   dir:"bullish", color:C.gold   },
    { metal:"Gold",   dir:"bearish", color:C.gold   },
    { metal:"Silver", dir:"bullish", color:C.silver },
    { metal:"Silver", dir:"bearish", color:C.silver },
    { metal:"Copper", dir:"bullish", color:C.copper },
    { metal:"Copper", dir:"bearish", color:C.copper },
  ];
  const lookup = {};
  for (const s of Object.values(summary || {})) lookup[s.metal + "|" + s.direction] = s;

  const PAD_L = 40, PAD_R = 10, PAD_T = 18, PAD_B = 38;
  const cW = W - PAD_L - PAD_R;
  const cH = H - PAD_T - PAD_B;
  const groupW = cW / ORDER.length;
  const barW   = Math.min(groupW * 0.3, 18);
  const gap    = barW * 0.35;

  ctx.font = "9px sans-serif";
  ctx.textAlign = "right";
  [0, 25, 50, 75, 100].forEach(pct => {
    const y = PAD_T + cH * (1 - pct / 100);
    ctx.beginPath();
    ctx.moveTo(PAD_L, y);
    ctx.lineTo(PAD_L + cW, y);
    if (pct === 50) {
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = C.muted;
      ctx.lineWidth   = 1.5;
    } else {
      ctx.setLineDash([]);
      ctx.strokeStyle = C.border;
      ctx.lineWidth   = 1;
    }
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = C.muted;
    ctx.fillText(pct + "%", PAD_L - 4, y + 3);
  });

  ORDER.forEach((grp, gi) => {
    const s   = lookup[grp.metal + "|" + grp.dir];
    const cx  = PAD_L + groupW * gi + groupW / 2;
    const x5  = cx - gap / 2 - barW;
    const x10 = cx + gap / 2;
    function drawBar(x, wr, alpha) {
      if (wr == null) return;
      const barH = cH * wr / 100;
      const y    = PAD_T + cH - barH;
      ctx.globalAlpha = alpha;
      ctx.fillStyle   = grp.color;
      ctx.fillRect(x, y, barW, barH);
      ctx.globalAlpha = 1;
      ctx.fillStyle   = C.text;
      ctx.font        = "9px sans-serif";
      ctx.textAlign   = "center";
      if (wr >= 5) ctx.fillText(Math.round(wr) + "%", x + barW / 2, y - 3);
    }
    drawBar(x5,  s ? s.win_rate_5d  : null, 1.0);
    drawBar(x10, s ? s.win_rate_10d : null, 0.45);

    const arrow = grp.dir === "bullish" ? "▲" : "▼";
    ctx.fillStyle  = grp.color;
    ctx.font       = "10px sans-serif";
    ctx.textAlign  = "center";
    ctx.fillText(grp.metal.slice(0, 2) + arrow, cx, H - PAD_B + 14);
  });

  // Legend via DOM to satisfy escHtml requirement
  const legend = document.getElementById("bt-chart-legend");
  if (legend) {
    legend.textContent = "";
    [
      ["var(--muted)", 1.0,  "5-day win rate"],
      ["var(--muted)", 0.45, "10-day win rate"],
    ].forEach(([bg, op, label]) => {
      const wrap = document.createElement("div");
      wrap.className = "bt-legend-item";
      const sw = document.createElement("span");
      sw.className = "bt-legend-swatch";
      sw.style.background = bg;
      sw.style.opacity = op;
      const txt = document.createTextNode(" " + label);
      wrap.appendChild(sw);
      wrap.appendChild(txt);
      legend.appendChild(wrap);
    });
    const base = document.createElement("div");
    base.className = "bt-legend-item";
    const line = document.createElement("span");
    line.style.cssText = "border-top:1px dashed var(--muted);display:inline-block;width:14px;height:0;vertical-align:middle";
    const baseTxt = document.createTextNode("  50% = random chance");
    base.appendChild(line);
    base.appendChild(baseTxt);
    legend.appendChild(base);
  }
}

// ── Per-signal accuracy table ─────────────────────────────────────────────────
function renderBtSignalStats(signalStats) {
  const tbody = document.getElementById("bt-sig-body");
  if (!tbody) return;
  tbody.textContent = "";
  if (!signalStats || !signalStats.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.className = "no-data";
    td.style.padding = "12px 8px";
    td.textContent = "No signal data";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  const SIG_LBL = {
    futures_curve_proxy:       "Futures Curve",
    etf_pressure_proxy:        "ETF Pressure",
    physical_tightness_proxy:  "Physical Tightness",
    demand_expectations_proxy: "Demand Expectations",
  };
  const METAL_C = { Gold:"gold", Silver:"silver", Copper:"copper" };
  function wrCls(wr) {
    if (wr == null) return "fwd-nil";
    return wr >= 55 ? "wr-good" : wr < 45 ? "wr-bad" : "wr-mid";
  }
  function makeCell(text, cls) {
    const td = document.createElement("td");
    if (cls) td.className = cls;
    td.textContent = text;
    return td;
  }
  let lastMetal = "";
  for (const s of signalStats) {
    const tr = document.createElement("tr");
    const mc  = METAL_C[s.metal] || "";
    const lbl = SIG_LBL[s.signal] || s.signal.replace(/_proxy$/, "").replace(/_/g, " ");

    const tdMetal = document.createElement("td");
    tdMetal.textContent = s.metal !== lastMetal ? s.metal : "";
    tdMetal.style.color = "var(--" + mc + ")";

    const tdDir = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = "pill " + (s.direction === "bullish" ? "bull" : "bear");
    pill.textContent = s.direction === "bullish" ? "▲ Bullish" : "▼ Bearish";
    tdDir.appendChild(pill);

    const wr5  = s.win_rate_5d  != null ? Math.round(s.win_rate_5d)  + "%" : "—";
    const wr10 = s.win_rate_10d != null ? Math.round(s.win_rate_10d) + "%" : "—";
    const r5   = s.avg_ret_5d  != null ? (s.avg_ret_5d  >= 0 ? "+" : "") + s.avg_ret_5d  + "%" : "—";
    const r10  = s.avg_ret_10d != null ? (s.avg_ret_10d >= 0 ? "+" : "") + s.avg_ret_10d + "%" : "—";
    const r5cls  = s.avg_ret_5d  != null ? (s.avg_ret_5d  > 0 ? "fwd-pos" : s.avg_ret_5d  < 0 ? "fwd-neg" : "fwd-nil") : "fwd-nil";
    const r10cls = s.avg_ret_10d != null ? (s.avg_ret_10d > 0 ? "fwd-pos" : s.avg_ret_10d < 0 ? "fwd-neg" : "fwd-nil") : "fwd-nil";

    tr.appendChild(tdMetal);
    tr.appendChild(tdDir);
    tr.appendChild(makeCell(lbl, ""));
    tr.appendChild(makeCell(String(s.count), ""));
    tr.appendChild(makeCell(wr5,  wrCls(s.win_rate_5d)));
    tr.appendChild(makeCell(r5,   r5cls));
    tr.appendChild(makeCell(wr10, wrCls(s.win_rate_10d)));
    tr.appendChild(makeCell(r10,  r10cls));
    tbody.appendChild(tr);
    lastMetal = s.metal;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
(async () => {
  try {
    const s = await fetch("/api/status");
    const d = await s.json();
    if (d.running)             setDot("run");
    else if (d.last_run_ok)    setDot("ok");
  } catch(_) {}
  refreshAll();
  // Restore backtest results if already computed this session
  try {
    const br = await fetch("/api/backtest");
    const bd = await br.json();
    if (bd.data && bd.data.events) {
      const n = bd.data.events.length;
      document.getElementById("bt-status").textContent =
        "Complete — " + n + " cluster events in 10yr window";
      _btData = bd.data;
      renderBtChart(bd.data.summary);
      renderBtSignalStats(bd.data.signal_stats || []);
      renderBtSummary(bd.data.summary);
      renderBtEvents(bd.data.events);
    }
  } catch(_) {}
})();
setInterval(refreshAll, 60_000);

if (Notification.permission === "granted") {
  const btn = document.getElementById("notif-btn");
  btn.textContent = "Notifications On";
  btn.className   = "granted";
}

// ── Signal info popup ─────────────────────────────────────────────────────────
function showSigInfo(key, event) {
  event.stopPropagation();
  const info = SIG_INFO[key];
  if (!info) return;
  const popup = document.getElementById("sig-popup");
  document.getElementById("sig-popup-title").textContent = info.label;
  document.getElementById("sig-popup-body").textContent  = info.body;
  const rect = event.target.getBoundingClientRect();
  const W = 280;
  let left = rect.right + 8;
  if (left + W > window.innerWidth - 8) left = rect.left - W - 8;
  if (left < 8) left = 8;
  let top = rect.top - 4;
  if (top + 220 > window.innerHeight - 8) top = window.innerHeight - 228;
  if (top < 8) top = 8;
  popup.style.left = left + "px";
  popup.style.top  = top  + "px";
  popup.classList.add("visible");
}

function hideSigInfo() {
  document.getElementById("sig-popup").classList.remove("visible");
}

document.addEventListener("click", hideSigInfo);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(
        _DASHBOARD_HTML,
        headers={"Cache-Control": "no-store"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("metals_web_server:app", host="0.0.0.0", port=PORT, reload=False)
