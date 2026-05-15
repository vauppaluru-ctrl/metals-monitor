#!/usr/bin/env python3
"""
Metals Monitor — One-Year First-Pass Event-Study Backtest
Period: 2025-05-15 through 2026-05-15
Data: yfinance OHLCV only. No paid APIs.

Gold, Silver, and Copper are evaluated INDEPENDENTLY.
A trigger for one metal is never inferred from another.
"""

from __future__ import annotations

import os
import sys
import logging
import warnings
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance"])
    import yfinance as yf

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

METALS = {
    "Gold":   {"primary": "GLD",  "secondary": "IAU"},
    "Silver": {"primary": "SLV",  "secondary": "SIVR"},
    "Copper": {"primary": "CPER", "secondary": "COPX"},
}

BACKTEST_START  = "2025-05-15"
BACKTEST_END    = "2026-05-15"
# Start earlier so the 60-day lookback is fully populated by BACKTEST_START
LOOKBACK_START  = "2024-11-01"

OUTPUT_DIR      = Path("metals_backtest_output")
OUTPUT_DIR.mkdir(exist_ok=True)

COOLDOWN_DAYS   = 3
FORWARD_HORIZONS = [1, 3, 5, 10, 20]

SIGNAL_COLS = [
    "futures_curve_proxy",
    "etf_pressure_proxy",
    "physical_tightness_proxy",
    "demand_expectations_proxy",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# DATA DOWNLOAD
# ──────────────────────────────────────────────────────────────────────────────

def download_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Download daily OHLCV. end is made inclusive by adding 1 day."""
    end_dt = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    log.info(f"  Downloading {ticker}  ({start} → {end})")
    raw = yf.download(ticker, start=start, end=end_dt, auto_adjust=True, progress=False)
    if raw.empty:
        raise ValueError(f"No data returned for {ticker}")
    # Flatten MultiIndex columns (yfinance >= 0.2 behaviour)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index = pd.to_datetime(df.index)
    df.index.name = "Date"
    df.sort_index(inplace=True)
    df.dropna(how="all", inplace=True)
    log.info(f"    {len(df)} rows, last date: {df.index[-1].date()}")
    return df


# ──────────────────────────────────────────────────────────────────────────────
# METRICS COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    m = df.copy()
    c = m["Close"]

    # Returns
    m["ret_1d"]  = c.pct_change(1)
    m["ret_3d"]  = c.pct_change(3)
    m["ret_5d"]  = c.pct_change(5)
    m["ret_10d"] = c.pct_change(10)
    m["ret_20d"] = c.pct_change(20)

    # 60-day rolling daily volatility (std of daily returns, not annualised)
    m["vol_60d"] = m["ret_1d"].rolling(60).std()

    # 60-day volume z-score
    vol_mean = m["Volume"].rolling(60).mean()
    vol_std  = m["Volume"].rolling(60).std().replace(0, np.nan)
    m["vol_zscore"] = (m["Volume"] - vol_mean) / vol_std

    # Daily high-low range as % of close
    m["hl_range_pct"] = (m["High"] - m["Low"]) / c

    # 60-day range z-score
    rng_mean = m["hl_range_pct"].rolling(60).mean()
    rng_std  = m["hl_range_pct"].rolling(60).std().replace(0, np.nan)
    m["range_zscore"] = (m["hl_range_pct"] - rng_mean) / rng_std

    # Prior 20-day high/low (exclusive of current day, using rolling of shifted close)
    m["prior_20d_high"] = c.shift(1).rolling(20).max()
    m["prior_20d_low"]  = c.shift(1).rolling(20).min()

    # Moving averages
    m["ma_10"] = c.rolling(10).mean()
    m["ma_30"] = c.rolling(30).mean()
    m["ma_50"] = c.rolling(50).mean()

    return m


# ──────────────────────────────────────────────────────────────────────────────
# SIGNAL GENERATION
# ──────────────────────────────────────────────────────────────────────────────

def generate_signals(m: pd.DataFrame) -> pd.DataFrame:
    s = m.copy()

    # 1. futures_curve_proxy — proxy for futures curve tightness / backwardation
    thresh_3d = 1.25 * s["vol_60d"] * np.sqrt(3)
    s["futures_curve_proxy"] = "neutral"
    s.loc[s["ret_3d"] >  thresh_3d, "futures_curve_proxy"] = "bullish"
    s.loc[s["ret_3d"] < -thresh_3d, "futures_curve_proxy"] = "bearish"

    # 2. etf_pressure_proxy — proxy for ETF inflow/outflow pressure
    vol_thresh = 0.25 * s["vol_60d"]
    s["etf_pressure_proxy"] = "neutral"
    s.loc[(s["vol_zscore"] > 1.5) & (s["ret_1d"] >  vol_thresh), "etf_pressure_proxy"] = "bullish"
    s.loc[(s["vol_zscore"] > 1.5) & (s["ret_1d"] < -vol_thresh), "etf_pressure_proxy"] = "bearish"

    # 3. physical_tightness_proxy — proxy for physical premium / tightness
    s["physical_tightness_proxy"] = "neutral"
    s.loc[(s["Close"] > s["prior_20d_high"]) & (s["range_zscore"] > 0), "physical_tightness_proxy"] = "bullish"
    s.loc[(s["Close"] < s["prior_20d_low"])  & (s["range_zscore"] > 0), "physical_tightness_proxy"] = "bearish"

    # 4. demand_expectations_proxy — proxy for demand expectations / market repricing
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
# EVENT DETECTION WITH COOLDOWN
# ──────────────────────────────────────────────────────────────────────────────

def detect_events(signals_df: pd.DataFrame, start_date: str, cooldown: int = 3) -> pd.DataFrame:
    """
    Detect cluster trigger events (≥ 2 same-direction signals).
    Cooldown is measured in trading days (positional index in the filtered DataFrame).
    Only events on or after start_date are returned.
    """
    start_dt = pd.Timestamp(start_date)
    # Filter to backtest window and rows with sufficient lookback data
    valid = signals_df.loc[
        (signals_df.index >= start_dt) & signals_df["vol_60d"].notna()
    ].copy()

    events = []
    last_bull_idx = -(cooldown + 1)
    last_bear_idx = -(cooldown + 1)

    for i, (date, row) in enumerate(valid.iterrows()):
        bull_cats = [c for c in SIGNAL_COLS if row[c] == "bullish"]
        bear_cats = [c for c in SIGNAL_COLS if row[c] == "bearish"]
        n_bull = len(bull_cats)
        n_bear = len(bear_cats)

        if n_bull >= 2 and (i - last_bull_idx) > cooldown:
            events.append({
                "date":       date,
                "direction":  "bullish",
                "n_signals":  n_bull,
                "categories": ", ".join(bull_cats),
                "close":      float(row["Close"]),
                "vol_60d":    float(row["vol_60d"]),
            })
            last_bull_idx = i

        if n_bear >= 2 and (i - last_bear_idx) > cooldown:
            events.append({
                "date":       date,
                "direction":  "bearish",
                "n_signals":  n_bear,
                "categories": ", ".join(bear_cats),
                "close":      float(row["Close"]),
                "vol_60d":    float(row["vol_60d"]),
            })
            last_bear_idx = i

    if not events:
        return pd.DataFrame()
    return pd.DataFrame(events).set_index("date")


# ──────────────────────────────────────────────────────────────────────────────
# FORWARD RETURN COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def compute_forward_returns(price_series: pd.Series, events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute forward returns at each horizon.
    Signed return: bullish → raw return; bearish → negated return.
    Events near the end of the sample will have NaN for long horizons.
    """
    if events_df.empty:
        return events_df.copy()

    result  = events_df.copy()
    prices  = price_series.sort_index()
    dates   = list(prices.index)
    date_to_idx = {d: i for i, d in enumerate(dates)}

    for h in FORWARD_HORIZONS:
        result[f"fwd_{h}d"]    = np.nan
        result[f"signed_{h}d"] = np.nan

    for event_date in result.index:
        if event_date not in date_to_idx:
            continue
        idx        = date_to_idx[event_date]
        evt_price  = prices.iloc[idx]
        direction  = result.at[event_date, "direction"]

        for h in FORWARD_HORIZONS:
            fwd_idx = idx + h
            if fwd_idx >= len(dates):
                continue
            fwd_price  = prices.iloc[fwd_idx]
            fwd_return = (fwd_price - evt_price) / evt_price
            result.at[event_date, f"fwd_{h}d"]    = fwd_return
            result.at[event_date, f"signed_{h}d"] = (
                fwd_return if direction == "bullish" else -fwd_return
            )

    return result


# ──────────────────────────────────────────────────────────────────────────────
# SUMMARY STATISTICS
# ──────────────────────────────────────────────────────────────────────────────

def compute_summary(events_df: pd.DataFrame, metal_name: str) -> list:
    rows = []
    for direction in ("bullish", "bearish"):
        if events_df.empty:
            sub = pd.DataFrame()
        else:
            sub = events_df[events_df["direction"] == direction]
        n   = len(sub)
        row = {"metal": metal_name, "direction": direction, "n_events": n}
        for h in FORWARD_HORIZONS:
            col  = f"signed_{h}d"
            vals = sub[col].dropna() if (n > 0 and col in sub.columns) else pd.Series([], dtype=float)
            row[f"mean_{h}d"]   = vals.mean()   if len(vals) else np.nan
            row[f"median_{h}d"] = vals.median() if len(vals) else np.nan
            row[f"hit_{h}d"]    = (vals > 0).mean() if len(vals) else np.nan
            row[f"worst_{h}d"]  = vals.min()    if len(vals) else np.nan
        rows.append(row)
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# CHART GENERATION
# ──────────────────────────────────────────────────────────────────────────────

def plot_proxy_performance(price_data: dict, start_date: str, out: Path) -> None:
    start_dt = pd.Timestamp(start_date)
    colors   = {"GLD": "#FFD700", "SLV": "#B0B0C0", "CPER": "#B87333"}
    labels   = {"GLD": "Gold (GLD)", "SLV": "Silver (SLV)", "CPER": "Copper (CPER)"}

    fig, ax = plt.subplots(figsize=(13, 6))
    for ticker, df in price_data.items():
        series = df["Close"].loc[df.index >= start_dt]
        if series.empty:
            continue
        norm = series / series.iloc[0] * 100
        ax.plot(norm.index, norm.values, label=labels.get(ticker, ticker),
                color=colors.get(ticker), linewidth=2.2)

    ax.axhline(100, color="gray", linestyle="--", linewidth=0.8, alpha=0.5)
    ax.set_title("Metals ETF Proxy Performance  (Normalised to 100 at backtest start)",
                 fontsize=13, fontweight="bold")
    ax.set_ylabel("Normalised Price (base = 100)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    plt.xticks(rotation=30)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    log.info(f"  Chart saved: {out.name}")


def plot_event_counts(all_events: dict, out: Path) -> None:
    metals = list(all_events.keys())
    x      = np.arange(len(metals))
    width  = 0.35

    bulls = [int((ev["direction"] == "bullish").sum()) if not ev.empty else 0
             for ev in all_events.values()]
    bears = [int((ev["direction"] == "bearish").sum()) if not ev.empty else 0
             for ev in all_events.values()]

    fig, ax = plt.subplots(figsize=(9, 5))
    b1 = ax.bar(x - width/2, bulls, width, label="Bullish", color="#2ecc71", alpha=0.85)
    b2 = ax.bar(x + width/2, bears, width, label="Bearish", color="#e74c3c", alpha=0.85)
    for bar in list(b1) + list(b2):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.15,
                str(int(bar.get_height())), ha="center", va="bottom", fontsize=11)

    ax.set_title("Cluster Trigger Event Counts by Metal and Direction",
                 fontsize=13, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(metals, fontsize=12)
    ax.set_ylabel("Number of Events")
    ax.legend(fontsize=11)
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    log.info(f"  Chart saved: {out.name}")


def plot_forward_returns(summary_rows: list, out: Path) -> None:
    metals   = ["Gold", "Silver", "Copper"]
    horizons = FORWARD_HORIZONS
    x        = np.arange(len(horizons))
    width    = 0.35
    xlabels  = [f"{h}d" for h in horizons]

    fig, axes = plt.subplots(1, 3, figsize=(16, 5), sharey=False)
    for ax, metal in zip(axes, metals):
        bull = next((r for r in summary_rows if r["metal"] == metal and r["direction"] == "bullish"), None)
        bear = next((r for r in summary_rows if r["metal"] == metal and r["direction"] == "bearish"), None)

        def safe_means(row):
            if row is None:
                return [0.0] * len(horizons)
            return [
                (row.get(f"mean_{h}d", np.nan) or 0.0) * 100
                for h in horizons
            ]

        ax.bar(x - width/2, safe_means(bull), width, label="Bullish", color="#2ecc71", alpha=0.85)
        ax.bar(x + width/2, safe_means(bear), width, label="Bearish", color="#e74c3c", alpha=0.85)
        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_title(metal, fontsize=12, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels(xlabels)
        ax.set_ylabel("Avg Signed Return (%)")
        ax.legend(fontsize=9)
        ax.grid(True, axis="y", alpha=0.3)

    fig.suptitle("Average Signed Forward Returns by Metal and Horizon",
                 fontsize=14, fontweight="bold")
    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    log.info(f"  Chart saved: {out.name}")


# ──────────────────────────────────────────────────────────────────────────────
# MARKDOWN REPORT
# ──────────────────────────────────────────────────────────────────────────────

def _pct(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v*100:+.1f}%"

def _hr(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v*100:.0f}%"


def generate_markdown_report(
    all_events: dict,
    summary_rows: list,
    out: Path,
) -> None:
    now          = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_events = sum(0 if ev.empty else len(ev) for ev in all_events.values())

    lines = [
        "# One-Year First-Pass Backtest: Gold, Silver, and Copper Signal Monitor",
        "",
        f"*Generated: {now}*  ",
        f"*Backtest period: {BACKTEST_START} — {BACKTEST_END}*",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        (
            "This document presents a first-pass event-study backtest of a proxy-based signal monitor for "
            "**Gold (GLD)**, **Silver (SLV)**, and **Copper (CPER)** over the one-year period "
            f"{BACKTEST_START} — {BACKTEST_END}."
        ),
        "",
        (
            "> **Important disclaimer:** This is a **proxy backtest**, not a full historical replay of the "
            "live monitoring system. Historical point-in-time data for futures curves, ETF "
            "creations/redemptions, physical premiums, and demand-expectation news is **not fully "
            "reconstructed**. All four signal categories are derived entirely from daily OHLCV data via "
            "yfinance as statistical proxies."
        ),
        "",
        f"Total cluster trigger events detected across all metals: **{total_events}**",
        "",
        (
            "Key rule preserved throughout: **Gold, Silver, and Copper are evaluated independently.** "
            "A trigger for one metal is never inferred from another. A cluster event requires at least "
            "**2 aligned signal categories within the same metal on the same trading day.**"
        ),
        "",
        (
            "The local hourly monitor is designed to run entirely on macOS without consuming Perplexity, "
            "Claude, or OpenAI credits during scheduled operation."
        ),
        "",
        "---",
        "",
        "## Methodology",
        "",
        "### ETF Proxies",
        "",
        "| Metal  | Primary ETF | Secondary ETF |",
        "|--------|-------------|---------------|",
        "| Gold   | GLD         | IAU           |",
        "| Silver | SLV         | SIVR          |",
        "| Copper | CPER        | COPX          |",
        "",
        "Backtest event generation uses **primary ETFs only**. Secondary ETFs listed for reference.",
        "",
        "### Daily Metrics",
        "",
        "- Close-to-close returns: 1d, 3d, 5d, 10d, 20d",
        "- 60-day rolling daily volatility (std of daily returns)",
        "- 60-day volume z-score",
        "- Daily high-low range as % of close",
        "- 60-day range z-score",
        "- Prior 20-day high/low (rolling max/min of prior-day close over 20 trading days)",
        "- Moving averages: 10d, 30d, 50d",
        "",
        "### Four Proxy Signal Categories",
        "",
        "**1. `futures_curve_proxy`** *(proxy for futures curve tightness / backwardation)*",
        "- Bullish: 3d return > 1.25 × σ₆₀ × √3",
        "- Bearish: 3d return < −1.25 × σ₆₀ × √3",
        "",
        "**2. `etf_pressure_proxy`** *(proxy for ETF inflow/outflow pressure)*",
        "- Bullish: volume z-score > 1.5 AND 1d return > 0.25 × σ₆₀",
        "- Bearish: volume z-score > 1.5 AND 1d return < −0.25 × σ₆₀",
        "",
        "**3. `physical_tightness_proxy`** *(proxy for physical premium / tightness)*",
        "- Bullish: close > prior 20d high AND range z-score > 0",
        "- Bearish: close < prior 20d low AND range z-score > 0",
        "",
        "**4. `demand_expectations_proxy`** *(proxy for demand expectations / market repricing)*",
        "- Bullish: 10d MA > 30d MA AND close > 50d MA AND 20d return > 0",
        "- Bearish: 10d MA < 30d MA AND close < 50d MA AND 20d return < 0",
        "",
        "### Cluster Trigger Rule",
        "",
        "- **Bullish event**: ≥ 2 categories bullish for the same metal on the same day",
        "- **Bearish event**: ≥ 2 categories bearish for the same metal on the same day",
        "- 3-trading-day cooldown suppresses repeated same-direction events per metal",
        "- No cross-metal confirmation (gold signals never trigger silver/copper events)",
        "",
        "### Forward Return Analysis",
        "",
        (
            "For event at date t, forward return at horizon h = (close[t+h] − close[t]) / close[t]. "
            "Signed return: bullish events use raw return; bearish events use the negated return "
            "(positive signed return = signal was directionally correct)."
        ),
        "",
        "---",
        "",
        "## Charts",
        "",
        "### Proxy ETF Performance (Normalised to 100)",
        "![Proxy Performance](metals_proxy_performance.png)",
        "",
        "### Event Counts by Metal and Direction",
        "![Event Counts](metals_event_counts.png)",
        "",
        "### Average Signed Forward Returns",
        "![Forward Returns](metals_forward_returns.png)",
        "",
        "---",
        "",
        "## Summary Results",
        "",
        "| Metal | Direction | N Events | Mean 1d | Mean 3d | Mean 5d | Mean 10d | Mean 20d "
        "| Hit 5d | Hit 10d | Hit 20d | Worst 20d |",
        "|-------|-----------|----------|---------|---------|---------|----------|----------"
        "|--------|---------|---------|-----------|",
    ]

    for r in summary_rows:
        lines.append(
            f"| {r['metal']} | {r['direction'].capitalize()} | {r['n_events']} "
            f"| {_pct(r.get('mean_1d'))} | {_pct(r.get('mean_3d'))} "
            f"| {_pct(r.get('mean_5d'))} | {_pct(r.get('mean_10d'))} "
            f"| {_pct(r.get('mean_20d'))} | {_hr(r.get('hit_5d'))} "
            f"| {_hr(r.get('hit_10d'))} | {_hr(r.get('hit_20d'))} "
            f"| {_pct(r.get('worst_20d'))} |"
        )

    lines += ["", "---", "", "## Detailed Event Log by Metal", ""]

    for metal, events_df in all_events.items():
        lines.append(f"### {metal}")
        lines.append("")
        if events_df.empty:
            lines.append("*No cluster trigger events detected in the backtest period.*")
            lines.append("")
            continue

        n_bull = int((events_df["direction"] == "bullish").sum())
        n_bear = int((events_df["direction"] == "bearish").sum())
        lines.append(f"**Bullish events:** {n_bull}  |  **Bearish events:** {n_bear}")
        lines.append("")
        lines.append(
            "| Date | Dir | N | Categories | Close | Signed 5d | Signed 10d | Signed 20d |"
        )
        lines.append(
            "|------|-----|---|------------|-------|-----------|------------|------------|"
        )
        for date, row in events_df.iterrows():
            lines.append(
                f"| {date.strftime('%Y-%m-%d')} "
                f"| {row['direction'].capitalize()} "
                f"| {int(row['n_signals'])} "
                f"| {row['categories']} "
                f"| ${row['close']:.2f} "
                f"| {_pct(row.get('signed_5d'))} "
                f"| {_pct(row.get('signed_10d'))} "
                f"| {_pct(row.get('signed_20d'))} |"
            )
        lines.append("")

    # Most recent events (last 10 across all metals)
    all_ev_parts = []
    for metal, events_df in all_events.items():
        if not events_df.empty:
            tmp = events_df.copy()
            tmp["metal"] = metal
            all_ev_parts.append(tmp)

    lines += ["---", "", "## Most Recent Detected Events (up to 10)", ""]
    if all_ev_parts:
        combined = pd.concat(all_ev_parts).sort_index(ascending=False).head(10)
        lines.append("| Date | Metal | Direction | N | Categories | Close |")
        lines.append("|------|-------|-----------|---|------------|-------|")
        for date, row in combined.iterrows():
            lines.append(
                f"| {date.strftime('%Y-%m-%d')} | {row['metal']} "
                f"| {row['direction'].capitalize()} | {int(row['n_signals'])} "
                f"| {row['categories']} | ${row['close']:.2f} |"
            )
    else:
        lines.append("*No events detected across any metal.*")

    lines += [
        "",
        "---",
        "",
        "## Interpretation",
        "",
        (
            "The backtest measures whether requiring ≥ 2 aligned proxy signals within a single metal "
            "produces directionally consistent forward returns. A positive mean signed return at a given "
            "horizon suggests the cluster rule identified conditions where prices subsequently moved in "
            "the signalled direction."
        ),
        "",
        (
            "With ≈250 trading days per year and four independent signals, some cluster coincidence is "
            "expected. Event counts per direction are small, so standard errors on mean returns are large. "
            "Results should be treated as directional hypothesis generation, not definitive alpha."
        ),
        "",
        "---",
        "",
        "## Limitations",
        "",
        (
            "1. **Proxy data only.** The four signal categories are approximated from OHLCV data alone. "
            "A live implementation would incorporate CME futures curves, ETF creation/redemption data, "
            "physical premiums, and news sentiment."
        ),
        "",
        (
            "2. **Short sample.** One year of daily data per metal yields at most ~250 observations. "
            "Event counts per direction are typically < 30, limiting statistical confidence."
        ),
        "",
        (
            "3. **In-sample signal design.** The proxy definitions were chosen knowing which factors "
            "matter — a form of look-ahead in the signal construction, even though all computations "
            "are point-in-time."
        ),
        "",
        (
            "4. **No transaction costs.** Forward returns are gross; bid-ask spread, slippage, and "
            "borrow costs are excluded."
        ),
        "",
        (
            "5. **yfinance data quality.** yfinance provides unofficial, delayed data and may have "
            "gaps, corporate-action errors, or stale prices. It is not a substitute for exchange-direct feeds."
        ),
        "",
        (
            "6. **No macro context.** Equity market conditions, USD/FX, interest rates, and calendar "
            "events (FOMC, NFP, PMI) are not incorporated."
        ),
        "",
        "---",
        "",
        "## Recommended Next Iteration",
        "",
        "1. Extend lookback to 3–5 years to increase event sample size and statistical power.",
        "2. Add public-source enrichment (CME public futures pages, LME copper data, RSS news sentiment "
        "   from Kitco / Reuters / Mining.com / S&P Global PMI / The Silver Institute / ICSG).",
        "3. Model position sizing and stop-loss rules to profile realistic P&L distributions.",
        "4. Add permutation-based significance tests on mean signed forward returns.",
        "5. Evaluate secondary ETF proxies (IAU, SIVR, COPX) for internal cross-confirmation.",
        "6. Consider intraday data for tighter signal timing.",
        "",
        "---",
        "",
        "*This backtest was generated by the local Metals Monitor system running on macOS.*  ",
        "*The hourly LaunchAgent does not consume Perplexity, Claude, or OpenAI credits.*  ",
        "*All computations use yfinance (free, unofficial) and local state files only.*",
    ]

    out.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"  Markdown report saved: {out.name}")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 64)
    log.info("Metals Monitor Backtest")
    log.info(f"Period: {BACKTEST_START}  →  {BACKTEST_END}")
    log.info("=" * 64)

    all_events: dict   = {}
    all_summary: list  = []
    price_data: dict   = {}

    for metal, cfg in METALS.items():
        ticker = cfg["primary"]
        log.info(f"\n── {metal} ({ticker}) ──────────────────────────────────")

        raw     = download_ohlcv(ticker, LOOKBACK_START, BACKTEST_END)
        price_data[ticker] = raw

        metrics = compute_metrics(raw)
        signals = generate_signals(metrics)
        events  = detect_events(signals, BACKTEST_START, cooldown=COOLDOWN_DAYS)

        if not events.empty:
            events = compute_forward_returns(raw["Close"], events)

        all_events[metal] = events

        n_bull = 0 if events.empty else int((events["direction"] == "bullish").sum())
        n_bear = 0 if events.empty else int((events["direction"] == "bearish").sum())
        log.info(f"  Events — bullish: {n_bull},  bearish: {n_bear}")

        summary = compute_summary(events, metal)
        all_summary.extend(summary)

    # ── Charts ──────────────────────────────────────────────────────────────
    log.info("\n── Generating charts ────────────────────────────────────────")
    plot_proxy_performance(price_data, BACKTEST_START, OUTPUT_DIR / "metals_proxy_performance.png")
    plot_event_counts(all_events,                       OUTPUT_DIR / "metals_event_counts.png")
    plot_forward_returns(all_summary,                   OUTPUT_DIR / "metals_forward_returns.png")

    # ── CSV outputs ──────────────────────────────────────────────────────────
    log.info("\n── Writing CSV outputs ──────────────────────────────────────")
    summary_df = pd.DataFrame(all_summary)
    summary_df.to_csv(OUTPUT_DIR / "metals_backtest_summary.csv", index=False)
    log.info("  metals_backtest_summary.csv")

    parts = []
    for metal, ev in all_events.items():
        if not ev.empty:
            tmp = ev.copy()
            tmp["metal"] = metal
            parts.append(tmp)

    if parts:
        all_ev_df = pd.concat(parts).sort_index()
        all_ev_df.to_csv(OUTPUT_DIR / "metals_backtest_events.csv")
        log.info(f"  metals_backtest_events.csv  ({len(all_ev_df)} events)")
    else:
        pd.DataFrame().to_csv(OUTPUT_DIR / "metals_backtest_events.csv")
        log.warning("  No events detected — empty events CSV written")

    # ── Markdown report ──────────────────────────────────────────────────────
    log.info("\n── Generating markdown report ──────────────────────────────")
    generate_markdown_report(all_events, all_summary, OUTPUT_DIR / "metals-monitor-backtest.md")

    log.info("\n" + "=" * 64)
    log.info("Backtest complete. Output files:")
    for f in sorted(OUTPUT_DIR.iterdir()):
        size = f.stat().st_size
        log.info(f"  {f.name:<45}  {size:>8,} bytes")
    log.info("=" * 64)


if __name__ == "__main__":
    main()
