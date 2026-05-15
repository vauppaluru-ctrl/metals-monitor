#!/usr/bin/env python3
"""
Metals Web Server — FastAPI dashboard for the Metals Monitor.

Modes:
  SCHEDULER_ENABLED=true   asyncio loop runs the monitor every SCHEDULER_INTERVAL_SECS
                            (container / cloud mode — replaces the LaunchAgent)
  SCHEDULER_ENABLED=false  server only exposes data already in state.json/logs
                            (macOS LaunchAgent still runs the monitor)

Start:
  uvicorn metals_web_server:app --host 0.0.0.0 --port 8080 [--reload]
  or: python metals_web_server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ── Import monitor functions (same process, no subprocess overhead) ───────────
sys.path.insert(0, str(Path(__file__).parent))
from metals_live_monitor import (
    METALS, STATE_FILE, LOG_FILE,
    load_state, save_state,
    download_ohlcv, compute_metrics, generate_signals, evaluate_latest,
    fetch_news_sentiment, format_news_context,
    build_notification, send_notification,
    record_cooldown, cooldown_allows, _append_recent_event,
    COOLDOWN_DAYS, SIGNAL_COLS, RSS_ENABLED,
)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

SCHEDULER_ENABLED       = os.getenv("SCHEDULER_ENABLED", "false").lower() == "true"
SCHEDULER_INTERVAL_SECS = int(os.getenv("SCHEDULER_INTERVAL_SECS", "3600"))
PORT                    = int(os.getenv("PORT", "8080"))
LOG_TAIL_LINES          = 200
BASE_DIR                = Path(__file__).parent.resolve()

# ──────────────────────────────────────────────────────────────────────────────
# IN-MEMORY CACHE  (populated by each monitor run)
# ──────────────────────────────────────────────────────────────────────────────

_cache: dict = {
    "last_run":     None,   # ISO timestamp
    "last_run_ok":  None,   # bool
    "metals":       {},     # { metal: evaluate_latest() result }
    "news":         {},     # fetch_news_sentiment() result
    "running":      False,  # True while a run is in progress
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

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor")


def _run_monitor_sync() -> dict:
    """Execute one full monitor cycle; returns summary dict."""
    state        = load_state()
    alerts_fired = 0
    metal_results: dict = {}

    sentiment = fetch_news_sentiment(list(METALS.keys()))

    for metal, cfg in METALS.items():
        ticker = cfg["primary"]
        try:
            raw     = download_ohlcv(ticker)
            metrics = compute_metrics(raw)
            signals = generate_signals(metrics)
            result  = evaluate_latest(signals)
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
# SCHEDULER  (only active when SCHEDULER_ENABLED=true)
# ──────────────────────────────────────────────────────────────────────────────

async def _scheduler_loop() -> None:
    while True:
        try:
            await _run_monitor_async()
        except Exception:
            pass
        await asyncio.sleep(SCHEDULER_INTERVAL_SECS)


# ──────────────────────────────────────────────────────────────────────────────
# FASTAPI APP
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Metals Monitor Dashboard", version="1.0")


@app.on_event("startup")
async def _startup() -> None:
    if SCHEDULER_ENABLED:
        asyncio.create_task(_scheduler_loop())


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


# ──────────────────────────────────────────────────────────────────────────────
# DASHBOARD HTML
# ──────────────────────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Metals Monitor</title>
<style>
  :root {
    --bg:#0f1117; --card:#1a1d27; --border:#2a2d3a;
    --text:#e2e4ec; --muted:#6b6f80; --gold:#f5c842;
    --silver:#a8b4c8; --copper:#d4804a;
    --bull:#3ddc84; --bear:#ff5f5f; --neutral:#6b6f80;
    --accent:#5b8dee;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font:14px/1.5 "SF Mono",Consolas,monospace; }
  header { display:flex; align-items:center; gap:12px; padding:16px 20px;
           border-bottom:1px solid var(--border); background:var(--card); }
  header h1 { font-size:16px; font-weight:700; letter-spacing:.5px; }
  #status-dot { width:10px; height:10px; border-radius:50%; background:var(--muted); flex-shrink:0; }
  #status-dot.ok  { background:var(--bull); }
  #status-dot.err { background:var(--bear); }
  #status-dot.run { background:var(--gold); animation:pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  #last-run { margin-left:auto; font-size:11px; color:var(--muted); }
  #run-btn { padding:5px 14px; border:1px solid var(--accent); background:transparent;
             color:var(--accent); border-radius:4px; cursor:pointer; font-size:12px; }
  #run-btn:hover { background:var(--accent); color:#fff; }
  #notif-btn { padding:5px 14px; border:1px solid var(--muted); background:transparent;
               color:var(--muted); border-radius:4px; cursor:pointer; font-size:12px; }
  #notif-btn.granted { border-color:var(--bull); color:var(--bull); }
  main { padding:20px; max-width:1200px; margin:0 auto; }
  .section-title { font-size:11px; text-transform:uppercase; letter-spacing:1px;
                   color:var(--muted); margin:24px 0 10px; }
  .metals-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:14px; }
  .metal-card { background:var(--card); border:1px solid var(--border);
                border-radius:8px; padding:16px; }
  .metal-card .name { font-size:15px; font-weight:700; margin-bottom:2px; }
  .metal-card .name.gold   { color:var(--gold); }
  .metal-card .name.silver { color:var(--silver); }
  .metal-card .name.copper { color:var(--copper); }
  .metal-card .meta { font-size:11px; color:var(--muted); margin-bottom:10px; }
  .signals { display:flex; flex-direction:column; gap:5px; }
  .sig-row { display:flex; justify-content:space-between; align-items:center;
             font-size:12px; padding:3px 0; border-bottom:1px solid var(--border); }
  .sig-row:last-child { border:none; }
  .sig-label { color:var(--muted); }
  .sig-val { font-weight:700; padding:1px 7px; border-radius:3px; font-size:11px; }
  .sig-val.bullish  { background:#1a3a28; color:var(--bull); }
  .sig-val.bearish  { background:#3a1a1a; color:var(--bear); }
  .sig-val.neutral  { background:#252830; color:var(--muted); }
  .cluster-badge { display:inline-block; padding:3px 10px; border-radius:12px;
                   font-size:11px; font-weight:700; margin-top:10px; }
  .cluster-badge.bull { background:#1a3a28; color:var(--bull); }
  .cluster-badge.bear { background:#3a1a1a; color:var(--bear); }
  .cluster-badge.none { background:#252830; color:var(--muted); }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { text-align:left; padding:7px 10px; font-size:10px; text-transform:uppercase;
       letter-spacing:.8px; color:var(--muted); border-bottom:1px solid var(--border); }
  td { padding:7px 10px; border-bottom:1px solid var(--border); }
  tr:last-child td { border:none; }
  .pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:10px; }
  .pill.bull { background:#1a3a28; color:var(--bull); }
  .pill.bear { background:#3a1a1a; color:var(--bear); }
  .tabs { display:flex; gap:2px; margin-bottom:10px; }
  .tab { padding:5px 14px; border:1px solid var(--border); border-radius:4px;
         cursor:pointer; font-size:12px; color:var(--muted); background:transparent; }
  .tab.active { border-color:var(--accent); color:var(--accent); background:#141824; }
  .tab-pane { display:none; }
  .tab-pane.active { display:block; }
  #log-box { background:#0a0c12; border:1px solid var(--border); border-radius:6px;
             padding:12px; height:320px; overflow-y:auto; font-size:11px;
             line-height:1.6; white-space:pre-wrap; word-break:break-all; }
  .news-item { padding:8px 0; border-bottom:1px solid var(--border); font-size:12px; }
  .news-item:last-child { border:none; }
  .news-source { font-size:10px; color:var(--muted); margin-bottom:2px; }
  .no-data { color:var(--muted); font-size:12px; padding:16px 0; }
  .error-msg { color:var(--bear); font-size:12px; padding:16px 0; }
</style>
</head>
<body>
<header>
  <div id="status-dot"></div>
  <h1>Metals Monitor</h1>
  <span id="last-run">No data yet</span>
  <button id="notif-btn" onclick="requestNotifPermission()">Enable Notifications</button>
  <button id="run-btn" onclick="triggerRun()">Run Now</button>
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
</main>

<script>
"use strict";

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
    if (msg.type === "run_start")    { setDot("run"); }
    if (msg.type === "run_complete") { setDot("ok"); refreshAll(); }
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
    const lines = (d.lines || []).map(l => escHtml(l)).join("\n");
    box.innerHTML = lines || "<span style='color:var(--muted)'>No log entries</span>";
    box.scrollTop = box.scrollHeight;
  } catch(e) { console.error("logs fetch", e); }
}

// ── Render helpers ────────────────────────────────────────────────────────────
const METAL_CLASS = { Gold:"gold", Silver:"silver", Copper:"copper" };
const SIG_LABELS  = {
  futures_curve_proxy:       "Futures Curve",
  etf_pressure_proxy:        "ETF Pressure",
  physical_tightness_proxy:  "Physical Tightness",
  demand_expectations_proxy: "Demand Expectations",
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
    for (const [key, val] of Object.entries(data.all_signals || {})) {
      const label = escHtml(SIG_LABELS[key] || key);
      const v     = escHtml(val);
      sigRows += `<div class="sig-row">
        <span class="sig-label">${label}</span>
        <span class="sig-val ${v}">${v}</span></div>`;
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
  try {
    await fetch("/api/run", { method:"POST" });
  } catch(e) { console.error(e); }
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = "Run Now";
  }, 5000);
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

// Wire SSE-fired run_complete to also send a Web Notification if permission granted
const _origOnMessage = es.onmessage;
es.onmessage = function(e) {
  _origOnMessage(e);
  try {
    const msg = JSON.parse(e.data);
    if (msg.type === "run_complete" && Notification.permission === "granted") {
      const alerts = msg.data && msg.data.alerts_fired;
      if (alerts && alerts > 0) {
        new Notification("Metals Monitor", {
          body: escHtml(String(alerts)) + " cluster alert(s) fired — check the dashboard.",
          icon: "",
        });
      }
    }
  } catch(_) {}
};

// ── Init ──────────────────────────────────────────────────────────────────────
refreshAll();
setInterval(refreshAll, 60_000);

if (Notification.permission === "granted") {
  const btn = document.getElementById("notif-btn");
  btn.textContent = "Notifications On";
  btn.className   = "granted";
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


# ──────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("metals_web_server:app", host="0.0.0.0", port=PORT, reload=False)
