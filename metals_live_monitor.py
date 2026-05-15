#!/usr/bin/env python3
"""
Metals Live Monitor — runs hourly via macOS LaunchAgent.
Evaluates Gold, Silver, and Copper INDEPENDENTLY using yfinance OHLCV.

No paid APIs. No Perplexity / Claude / OpenAI calls.
Safe to run while the Mac is awake; silent when no signal cluster fires.
"""

from __future__ import annotations

import os
import sys
import re
import html as html_mod
import json
import logging
import logging.handlers
import subprocess
import urllib.request
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance"])
    import yfinance as yf

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.resolve()

METALS = {
    "Gold":   {"primary": "GLD",  "secondary": "IAU"},
    "Silver": {"primary": "SLV",  "secondary": "SIVR"},
    "Copper": {"primary": "CPER", "secondary": "COPX"},
}

# Pull 200 calendar days ≈ 140 trading days (safely > 120 minimum for 60d lookback)
HISTORY_CAL_DAYS = 200
COOLDOWN_DAYS    = 3   # calendar days

# ── Optional news enrichment (does NOT affect the 4-signal cluster trigger logic) ──
# Set to True to fetch recent headlines and include sentiment context in alerts/logs.
# Runs entirely offline-after-fetch: no LLM, no paid API.
RSS_ENABLED      = True
RSS_MAX_AGE_HOURS = 24   # only consider articles published within this window
RSS_TIMEOUT_SECS  = 10

# Kitco RSS is dead (Next.js migration, all endpoints return HTML as of 2026-05).
# Working confirmed feeds as of 2026-05-15:
RSS_FEEDS = [
    # Source                  URL                                            metals scope
    ("Mining.com",            "https://www.mining.com/feed/"),               # gold/silver/copper mining news
    ("King World News",       "https://kingworldnews.com/feed/"),             # precious metals commentary
    ("TF Metals Report",      "https://www.tfmetalsreport.com/rss.xml"),      # metals trading community
    ("GoldBroker",            "https://goldbroker.com/news.rss"),             # analytical gold/macro
]

# Per-metal keyword lists used for headline scoring (lowercase)
RSS_KEYWORDS: dict = {
    "Gold": {
        "bullish": ["gold rally", "gold surges", "gold hits", "gold rises", "gold breakout",
                    "gold record", "gold bull", "gold demand", "safe haven", "gold gains"],
        "bearish": ["gold falls", "gold drops", "gold slides", "gold plunges", "gold weakens",
                    "gold selloff", "gold decline", "gold bear", "gold pressure"],
        "neutral": ["gold", "gld", "xau"],
    },
    "Silver": {
        "bullish": ["silver rally", "silver surges", "silver rises", "silver breakout",
                    "silver bull", "silver demand", "silver gains", "silver record"],
        "bearish": ["silver falls", "silver drops", "silver slides", "silver plunges",
                    "silver selloff", "silver decline", "silver bear"],
        "neutral": ["silver", "slv", "xag"],
    },
    "Copper": {
        "bullish": ["copper rally", "copper surges", "copper rises", "copper demand",
                    "copper bull", "copper gains", "copper breakout", "dr copper"],
        "bearish": ["copper falls", "copper drops", "copper slides", "copper plunges",
                    "copper selloff", "copper decline", "copper bear", "copper weakness"],
        "neutral": ["copper", "cper", "hg futures"],
    },
}

STATE_FILE = BASE_DIR / "metals_monitor_state" / "state.json"
LOG_FILE   = BASE_DIR / "metals_monitor_logs"  / "metals_monitor.log"

# Ensure output directories exist
(BASE_DIR / "metals_monitor_state").mkdir(exist_ok=True)
(BASE_DIR / "metals_monitor_logs").mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING  (file + stdout so the LaunchAgent log captures both)
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        # Rotate at 5 MB, keep 3 backups — prevents unbounded growth on a long-running daemon
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ──────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            # Corrupt state disables cooldown deduplication for this run; log clearly
            log.warning(f"state.json unreadable ({exc}) — starting with empty state, cooldowns reset")
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# DATA DOWNLOAD
# ──────────────────────────────────────────────────────────────────────────────

def download_ohlcv(ticker: str) -> pd.DataFrame:
    end_dt   = datetime.today()
    start_dt = end_dt - timedelta(days=HISTORY_CAL_DAYS)
    start    = start_dt.strftime("%Y-%m-%d")
    end      = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")  # inclusive end

    raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        raise ValueError(f"No data returned for {ticker}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index = pd.to_datetime(df.index)
    df.index.name = "Date"
    df.sort_index(inplace=True)
    df.dropna(how="all", inplace=True)
    return df


# ──────────────────────────────────────────────────────────────────────────────
# METRICS  (same logic as backtest)
# ──────────────────────────────────────────────────────────────────────────────

def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    m = df.copy()
    c = m["Close"]

    m["ret_1d"]  = c.pct_change(1)
    m["ret_3d"]  = c.pct_change(3)
    m["ret_5d"]  = c.pct_change(5)
    m["ret_10d"] = c.pct_change(10)
    m["ret_20d"] = c.pct_change(20)

    m["vol_60d"] = m["ret_1d"].rolling(60).std()

    vol_mean = m["Volume"].rolling(60).mean()
    vol_std  = m["Volume"].rolling(60).std().replace(0, np.nan)
    m["vol_zscore"] = (m["Volume"] - vol_mean) / vol_std

    m["hl_range_pct"] = (m["High"] - m["Low"]) / c

    rng_mean = m["hl_range_pct"].rolling(60).mean()
    rng_std  = m["hl_range_pct"].rolling(60).std().replace(0, np.nan)
    m["range_zscore"] = (m["hl_range_pct"] - rng_mean) / rng_std

    m["prior_20d_high"] = c.shift(1).rolling(20).max()
    m["prior_20d_low"]  = c.shift(1).rolling(20).min()

    m["ma_10"] = c.rolling(10).mean()
    m["ma_30"] = c.rolling(30).mean()
    m["ma_50"] = c.rolling(50).mean()

    return m


SIGNAL_COLS = [
    "futures_curve_proxy",
    "etf_pressure_proxy",
    "physical_tightness_proxy",
    "demand_expectations_proxy",
]


def generate_signals(m: pd.DataFrame) -> pd.DataFrame:
    s = m.copy()

    thresh_3d = 1.25 * s["vol_60d"] * np.sqrt(3)
    s["futures_curve_proxy"] = "neutral"
    s.loc[s["ret_3d"] >  thresh_3d, "futures_curve_proxy"] = "bullish"
    s.loc[s["ret_3d"] < -thresh_3d, "futures_curve_proxy"] = "bearish"

    vol_thresh = 0.25 * s["vol_60d"]
    s["etf_pressure_proxy"] = "neutral"
    s.loc[(s["vol_zscore"] > 1.5) & (s["ret_1d"] >  vol_thresh), "etf_pressure_proxy"] = "bullish"
    s.loc[(s["vol_zscore"] > 1.5) & (s["ret_1d"] < -vol_thresh), "etf_pressure_proxy"] = "bearish"

    s["physical_tightness_proxy"] = "neutral"
    s.loc[(s["Close"] > s["prior_20d_high"]) & (s["range_zscore"] > 0), "physical_tightness_proxy"] = "bullish"
    s.loc[(s["Close"] < s["prior_20d_low"])  & (s["range_zscore"] > 0), "physical_tightness_proxy"] = "bearish"

    s["demand_expectations_proxy"] = "neutral"
    s.loc[
        (s["ma_10"] > s["ma_30"]) & (s["Close"] > s["ma_50"]) & (s["ret_20d"] > 0),
        "demand_expectations_proxy",
    ] = "bullish"
    s.loc[
        (s["ma_10"] < s["ma_30"]) & (s["Close"] < s["ma_50"]) & (s["ret_20d"] < 0),
        "demand_expectations_proxy",
    ] = "bearish"

    return s


# ──────────────────────────────────────────────────────────────────────────────
# EVALUATE LATEST TRADING DAY
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_latest(signals_df: pd.DataFrame) -> dict:
    """Return signal summary for the most recent row with valid vol_60d."""
    valid  = signals_df.dropna(subset=["vol_60d"])
    if valid.empty:
        raise ValueError("Insufficient history to compute 60-day volatility.")
    latest = valid.iloc[-1]

    bull_cats = [c for c in SIGNAL_COLS if latest[c] == "bullish"]
    bear_cats = [c for c in SIGNAL_COLS if latest[c] == "bearish"]

    return {
        "date":      latest.name.strftime("%Y-%m-%d"),
        "close":     float(latest["Close"]),
        "n_bullish": len(bull_cats),
        "n_bearish": len(bear_cats),
        "bull_cats": bull_cats,
        "bear_cats": bear_cats,
        "vol_60d":   float(latest["vol_60d"]),
        "all_signals": {c: latest[c] for c in SIGNAL_COLS},
    }


# ──────────────────────────────────────────────────────────────────────────────
# COOLDOWN
# ──────────────────────────────────────────────────────────────────────────────

def cooldown_allows(state: dict, metal: str, direction: str, today_str: str) -> bool:
    key      = f"{metal}_{direction}_last"
    last_str = state.get(key)
    if last_str is None:
        return True
    last_date  = datetime.strptime(last_str, "%Y-%m-%d").date()
    today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
    return (today_date - last_date).days > COOLDOWN_DAYS


def record_cooldown(state: dict, metal: str, direction: str, today_str: str) -> None:
    state[f"{metal}_{direction}_last"] = today_str


def _append_recent_event(state: dict, metal: str, ticker: str, direction: str,
                          cats: list, close: float, date_str: str) -> None:
    events = state.setdefault("recent_events", [])
    events.insert(0, {
        "date":       date_str,
        "metal":      metal,
        "ticker":     ticker,
        "direction":  direction,
        "categories": cats,
        "close":      close,
        "timestamp":  datetime.now().isoformat(timespec="seconds"),
    })
    state["recent_events"] = events[:50]  # keep last 50 live alerts


# ──────────────────────────────────────────────────────────────────────────────
# NEWS ENRICHMENT  (optional, disabled by RSS_ENABLED = False)
# Kitco RSS dead as of 2026-05 (Next.js migration; all endpoints return HTML).
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_feed_items(source_name: str, url: str) -> list:
    """
    Fetch an RSS feed and return a list of (title, pubdate_str) tuples.
    Uses regex to strip CDATA before parsing — handles malformed feeds that
    break xml.etree.ElementTree with "unclosed CDATA" errors.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MetalsMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=RSS_TIMEOUT_SECS) as r:
            raw = r.read(32_000).decode("utf-8", errors="replace")
        # Strip CDATA wrappers so the regex below sees plain text
        raw = re.sub(r"<!\[CDATA\[(.*?)\]\]>",
                     lambda m: html_mod.escape(m.group(1)), raw, flags=re.DOTALL)
        titles = re.findall(r"<title[^>]*>(.*?)</title>", raw, re.DOTALL)
        dates  = re.findall(r"<pubDate>(.*?)</pubDate>", raw, re.DOTALL)
        # titles[0] is usually the channel title; skip it
        items = []
        for i, title in enumerate(titles[1:], start=0):
            t    = html_mod.unescape(title).strip()
            date = dates[i].strip() if i < len(dates) else ""
            if t:
                items.append((t, date))
        return items
    except Exception as exc:
        log.debug(f"  RSS fetch failed [{source_name}]: {exc}")
        return []


def _parse_pubdate(date_str: str) -> datetime | None:
    """Parse RFC 2822 pubDate strings into an aware UTC datetime."""
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M %z",
    ):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def fetch_news_sentiment(metals: list) -> dict:
    """
    Fetch all configured RSS feeds and return per-metal sentiment context.
    Returns { metal: { "bullish": [headlines], "bearish": [headlines],
                       "relevant": [headlines], "sources_ok": int } }
    Only articles published within RSS_MAX_AGE_HOURS are considered.
    Does not modify or influence the 4-signal cluster trigger logic.
    """
    if not RSS_ENABLED:
        return {}

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=RSS_MAX_AGE_HOURS)
    all_items: list = []

    for source_name, url in RSS_FEEDS:
        items = _fetch_feed_items(source_name, url)
        fresh = 0
        for title, date_str in items:
            dt = _parse_pubdate(date_str)
            # Accept item if date parses and is recent, OR if date is missing (be permissive)
            if dt is None or dt >= cutoff:
                all_items.append((source_name, title))
                fresh += 1
        log.debug(f"  RSS [{source_name}]: {len(items)} items, {fresh} within {RSS_MAX_AGE_HOURS}h")

    result: dict = {}
    for metal in metals:
        kw = RSS_KEYWORDS.get(metal, {})
        bull_kw    = kw.get("bullish", [])
        bear_kw    = kw.get("bearish", [])
        neutral_kw = kw.get("neutral", [])

        bull_hits, bear_hits, relevant = [], [], []
        for source, title in all_items:
            tl = title.lower()
            is_relevant = any(k in tl for k in neutral_kw)
            if not is_relevant:
                continue
            relevant.append(title)
            if any(k in tl for k in bull_kw):
                bull_hits.append(title)
            elif any(k in tl for k in bear_kw):
                bear_hits.append(title)

        result[metal] = {
            "bullish":    bull_hits[:3],
            "bearish":    bear_hits[:3],
            "relevant":   relevant[:5],
            "n_relevant": len(relevant),
        }

    return result


def format_news_context(metal: str, direction: str, sentiment: dict) -> str:
    """Return a one-line news context string to append to notifications/logs."""
    metal_data = sentiment.get(metal, {})
    if not metal_data:
        return ""
    bull = metal_data.get("bullish", [])
    bear = metal_data.get("bearish", [])
    n    = metal_data.get("n_relevant", 0)
    if n == 0:
        return "No recent news."
    tone = "mixed"
    if direction == "bullish" and bull:
        tone = f"news supports: \"{bull[0][:60]}…\""
    elif direction == "bearish" and bear:
        tone = f"news supports: \"{bear[0][:60]}…\""
    elif direction == "bullish" and bear:
        tone = f"news contradicts: \"{bear[0][:60]}…\""
    elif direction == "bearish" and bull:
        tone = f"news contradicts: \"{bull[0][:60]}…\""
    return f"[{n} recent {metal} headlines — {tone}]"


# ──────────────────────────────────────────────────────────────────────────────
# NOTIFICATIONS
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_signal(sig: str) -> str:
    return sig.replace("_proxy", "").replace("_", " ").title()


CATEGORY_INTERP = {
    "futures_curve_proxy":       "momentum consistent with backwardation/curve tightness",
    "etf_pressure_proxy":        "elevated volume with price pressure (ETF flow proxy)",
    "physical_tightness_proxy":  "price breakout with elevated range (physical tightness)",
    "demand_expectations_proxy": "trend alignment across 10/30/50d MAs (demand repricing)",
}


def build_notification(metal: str, ticker: str, direction: str,
                       cats: list, close: float,
                       news_context: str = "") -> tuple[str, str]:
    adj   = "Bullish" if direction == "bullish" else "Bearish"
    title = f"[{adj} Cluster] Metals Monitor"
    cat_names   = ", ".join(_fmt_signal(c) for c in cats)
    interp_bits = "; ".join(CATEGORY_INTERP.get(c, c) for c in cats[:2])
    body  = (
        f"{metal} ({ticker}) | {adj} cluster | "
        f"Signals: {cat_names} | Close: ${close:.2f} | {interp_bits}."
    )
    if news_context:
        body += f" {news_context}"
    return title, body


def send_notification(title: str, message: str) -> None:
    # json.dumps produces a properly double-quote-escaped string literal that
    # osascript's expression evaluator accepts — avoids the broken \' AppleScript
    # non-escape and prevents any special-character injection in the body text.
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True, timeout=10, capture_output=True,
        )
        log.info(f"  Notification sent ▶ [{title}]")
    except Exception as exc:
        log.warning(f"  osascript failed ({exc}). Logging alert instead.")
        print(f"\nALERT: [{title}]  {message}\n")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("─" * 64)
    log.info("Metals Live Monitor  —  run started")

    state        = load_state()
    alerts_fired = 0

    # Fetch news once per run (not per metal) to avoid hammering the feeds
    sentiment = fetch_news_sentiment(list(METALS.keys()))
    if RSS_ENABLED:
        for metal, data in sentiment.items():
            log.info(f"  RSS [{metal}]: {data['n_relevant']} relevant headline(s)  "
                     f"bull={len(data['bullish'])}  bear={len(data['bearish'])}")
            for h in data["relevant"][:2]:
                log.info(f"    · {h[:100]}")

    for metal, cfg in METALS.items():
        ticker = cfg["primary"]
        log.info(f"Evaluating {metal} ({ticker}) ...")

        try:
            raw     = download_ohlcv(ticker)
            metrics = compute_metrics(raw)
            signals = generate_signals(metrics)
            result  = evaluate_latest(signals)
        except Exception as exc:
            log.error(f"  Failed to evaluate {metal}: {exc}")
            continue

        today_str = result["date"]
        close     = result["close"]
        log.info(
            f"  {metal}  date={today_str}  close=${close:.2f}  "
            f"bull_signals={result['n_bullish']}  bear_signals={result['n_bearish']}"
        )
        for sig, val in result["all_signals"].items():
            log.info(f"    {sig:<35} {val}")

        for direction, n_sig, cats in [
            ("bullish", result["n_bullish"], result["bull_cats"]),
            ("bearish", result["n_bearish"], result["bear_cats"]),
        ]:
            if n_sig < 2:
                continue

            if not cooldown_allows(state, metal, direction, today_str):
                log.info(
                    f"  [{metal} {direction}] Suppressed — within {COOLDOWN_DAYS}-day cooldown "
                    f"(last: {state.get(f'{metal}_{direction}_last')})"
                )
                continue

            news_ctx    = format_news_context(metal, direction, sentiment)
            title, body = build_notification(metal, ticker, direction, cats, close, news_ctx)
            log.info(f"  ALERT: {body}")
            send_notification(title, body)
            record_cooldown(state, metal, direction, today_str)
            # Persist event so the web dashboard can display alert history
            _append_recent_event(state, metal, ticker, direction, cats, close, today_str)
            alerts_fired += 1

    save_state(state)

    if alerts_fired == 0:
        log.info("No new cluster trigger events this run. No notifications sent.")
    else:
        log.info(f"{alerts_fired} alert(s) fired this run.")

    log.info("Metals Live Monitor  —  run complete.")
    log.info("─" * 64)


if __name__ == "__main__":
    main()
