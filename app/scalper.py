"""
NIFTY 50 Scalping Screener - a separate, faster signal engine from the
main swing-style dashboard (indicators.py/background.py), purpose-built
for fast intraday scalp entries on the 50 NIFTY 50 constituent stocks.

Runs on its own 3-minute timeframe and its own background loop
(SCALP_SCAN_INTERVAL_SECONDS, much faster than the main scanner's
configurable interval) - deliberately independent of settings.TIMEFRAME/
settings.WATCHLIST so it never interferes with, or gets reconfigured by,
changes made to the main screener on the Settings page.

Four scalp-specific parameters, each contributing to a 0-4 "confirmed"
count (same N-of-4 confluence shape as the main screener, MIN_REQUIRED_SCALP
of them needed):
  1. Fast EMA cross      - EMA(5) vs EMA(13), which side is currently ahead
  2. Session VWAP         - price vs the running intraday VWAP
  3. RSI(7) momentum      - RSI(7) above/below the 50 midline
  4. Relative Volume      - today's volume vs its own 20-bar average (a
                             confirmation-only 4th parameter, same
                             directionless-magnitude role Relative Volume
                             plays in the main screener)

Every signal also carries an ATR-based suggested stop-loss/target -
informational only, meant to help you size a manual order, never an
instruction to place one and never auto-executed.

A NIFTY 50 index-direction bias is attached to every row (index_agrees) -
reuses scanner.fetch_index_direction on this same 3-minute timeframe -
so you can see at a glance whether a stock's scalp signal is swimming
with or against the index's own current momentum. Purely informational
here (no hard gate/setting to enable/disable it, unlike the main
screener's REQUIRE_INDEX_AGREEMENT) - keeps this a fast, simple v1.
"""
import datetime as dt
import json
import logging
import os
import threading
import time

import numpy as np
import pandas as pd

from . import kite_auth
from .config import settings, SCALP_RESULTS_FILE
from .indicators import rsi, compute_atr, session_vwap_series, _cross_up, _cross_down
from .scanner import (
    _load_instrument_map, fetch_candles, fetch_index_direction, is_market_open, now_ist,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# NIFTY 50 constituents, as of Dec 2025 (NSE's own weightage table cross-
# checked against its published top-10-by-weightage). The index rebalances
# semi-annually (typically March/September) - a stock that's been swapped
# out since will simply come back "symbol not found on NSE" from
# scan_scalp_watchlist below (same graceful per-symbol degradation the main
# scan_watchlist already uses for a bad symbol) rather than breaking the
# scan, but this list is worth refreshing after each rebalance.
# --------------------------------------------------------------------------
NIFTY50_STOCKS = sorted([
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
    "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TMPV", "TATASTEEL",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
])

SCALP_TIMEFRAME = "3minute"

FAST_EMA_LENGTH = 5
SLOW_EMA_LENGTH = 13
RSI_FAST_LENGTH = 7
# Cross thresholds used only for a (currently unused-for-gating) momentum
# pulse; the *state* vote below uses a plain 50 midline instead - see
# compute_scalp_signal's docstring for why these are kept separate.
RSI_MOMENTUM_BULL = 55
RSI_MOMENTUM_BEAR = 45

# Deliberately its own constant, not settings.REL_VOLUME_THRESHOLD - a
# 3-minute bar's own 20-bar rolling average behaves very differently from
# a 15-minute bar's, so reusing the main screener's tunable would silently
# import a bar that was never calibrated for this timeframe.
SCALP_REL_VOLUME_THRESHOLD = 1.5

MIN_REQUIRED_SCALP = 3  # of 4 - see compute_scalp_signal

ATR_LENGTH = 14
ATR_STOP_MULT = 0.75   # suggested stop = entry -/+ 0.75x ATR
ATR_TARGET_MULT = 1.5  # suggested target = entry +/- 1.5x ATR (~1:2 R:R)

SCALP_SCAN_INTERVAL_SECONDS = 45  # much faster than the main scanner - scalping is time-sensitive


def compute_scalp_series(df: pd.DataFrame) -> dict:
    """Computes every scalp indicator series for the full df. Returns
    {"error": ...} if there isn't enough history yet for the slowest of
    the four (currently SLOW_EMA_LENGTH=13, well under the ATR/RSI
    lengths' own warm-up needs, so ATR_LENGTH is really the binding one)."""
    warmup = max(SLOW_EMA_LENGTH, RSI_FAST_LENGTH, ATR_LENGTH) + 5
    if len(df) < warmup:
        return {"error": "not enough candles yet"}

    close = df["close"]
    ema_fast = close.ewm(span=FAST_EMA_LENGTH, adjust=False).mean()
    ema_slow = close.ewm(span=SLOW_EMA_LENGTH, adjust=False).mean()
    ema_up, ema_dn = _cross_up(ema_fast, ema_slow), _cross_down(ema_fast, ema_slow)

    vwap_series = session_vwap_series(df, SCALP_TIMEFRAME)
    vwap_up, vwap_dn = _cross_up(close, vwap_series), _cross_down(close, vwap_series)

    rsi_line = rsi(close, RSI_FAST_LENGTH)
    bull_level = pd.Series(RSI_MOMENTUM_BULL, index=df.index)
    bear_level = pd.Series(RSI_MOMENTUM_BEAR, index=df.index)
    rsi_up = _cross_up(rsi_line, bull_level)
    rsi_dn = _cross_down(rsi_line, bear_level)

    vol_avg = df["volume"].rolling(20, min_periods=5).mean()
    atr = compute_atr(df, ATR_LENGTH)

    return {
        "df": df,
        "ema_fast": ema_fast, "ema_slow": ema_slow,
        "ema_up": ema_up, "ema_dn": ema_dn,
        "vwap_series": vwap_series, "vwap_up": vwap_up, "vwap_dn": vwap_dn,
        "rsi_line": rsi_line, "rsi_up": rsi_up, "rsi_dn": rsi_dn,
        "vol_avg": vol_avg,
        "atr": atr,
    }


def compute_scalp_signal(df: pd.DataFrame) -> dict:
    """df must have open/high/low/close/volume columns, oldest row first.
    Returns the latest bar's scalp state, or {"error": ...} if there
    isn't enough history / VWAP hasn't started accumulating yet (only
    possible in the first sliver of a session before any volume has
    printed - vwap_series is NaN there).

    Direction is decided by 3 STATE votes (which side each parameter is
    CURRENTLY on, not whether it just crossed): EMA5 vs EMA13, price vs
    running VWAP, and RSI(7) vs the 50 midline. This mirrors
    indicators.compute_signal's own state-vs-pulse split - a state vote
    is always decisive (2-way, no abstention), so a "confirmed" reading
    doesn't require the rarer, stricter event of multiple indicators
    crossing on the exact same 3-minute bar (that stricter, same-bar
    version is what backtest.py's _signal_series uses instead, because a
    backtest replay has to pick one literal entry bar per trade - a live
    screener doesn't have that constraint). Relative Volume is a 4th,
    confirmation-only vote (no direction of its own, same role it plays
    in the main screener)."""
    series = compute_scalp_series(df)
    if "error" in series:
        return series

    i = len(df) - 1
    close = series["df"]["close"]
    vwap_now = series["vwap_series"].iloc[i]
    if pd.isna(vwap_now):
        return {"error": "VWAP not available yet (no volume printed this session)"}

    ema_bull = bool(series["ema_fast"].iloc[i] > series["ema_slow"].iloc[i])
    vwap_bull = bool(close.iloc[i] > vwap_now)
    rsi_now = float(series["rsi_line"].iloc[i])
    rsi_bull = bool(rsi_now > 50)

    align_count = int(ema_bull) + int(vwap_bull) + int(rsi_bull)
    direction = "Bullish" if align_count >= 2 else "Bearish"
    dir_match_count = max(align_count, 3 - align_count)  # 2 or 3, how many of the 3 agree with the majority

    vol_avg = series["vol_avg"].iloc[i]
    latest_vol = df["volume"].iloc[i]
    vol_multiple = round(float(latest_vol / vol_avg), 2) if vol_avg and pd.notna(vol_avg) and vol_avg > 0 else None
    vol_confirmed = bool(vol_multiple is not None and vol_multiple >= SCALP_REL_VOLUME_THRESHOLD)

    confirmed_count = dir_match_count + int(vol_confirmed)  # 2-4
    signal_confirmed = confirmed_count >= MIN_REQUIRED_SCALP

    atr_val = series["atr"].iloc[i]
    entry = float(close.iloc[i])
    stop = target = None
    if pd.notna(atr_val) and atr_val > 0:
        atr_f = float(atr_val)  # numpy.float64 -> plain float, so json.dumps/jsonify
        if direction == "Bullish":              # doesn't choke on the arithmetic result below
            stop = round(entry - ATR_STOP_MULT * atr_f, 2)
            target = round(entry + ATR_TARGET_MULT * atr_f, 2)
        else:
            stop = round(entry + ATR_STOP_MULT * atr_f, 2)
            target = round(entry - ATR_TARGET_MULT * atr_f, 2)

    return {
        "close": round(entry, 2),
        "ema_state": "Bullish" if ema_bull else "Bearish",
        "vwap": round(float(vwap_now), 2),
        "vwap_state": "Bullish" if vwap_bull else "Bearish",
        "rsi": round(rsi_now, 1),
        "rsi_state": "Bullish" if rsi_bull else "Bearish",
        "vol_multiple": vol_multiple,
        "vol_confirmed": vol_confirmed,
        "direction": direction,
        "confirmed_count": confirmed_count,
        "signal_confirmed": signal_confirmed,
        "atr": round(float(atr_val), 2) if pd.notna(atr_val) else None,
        "stop": stop,
        "target": target,
        "risk_reward": round(ATR_TARGET_MULT / ATR_STOP_MULT, 2) if stop is not None else None,
        "timestamp": df.index[i].isoformat(),
    }


def scan_scalp_watchlist(kite) -> list:
    """Same per-symbol error-isolation pattern as scanner.scan_watchlist -
    one bad/renamed symbol never aborts the rest of the scan."""
    instruments = _load_instrument_map(kite)
    results = []
    for symbol in NIFTY50_STOCKS:
        token = instruments.get(symbol)
        if not token:
            results.append({"symbol": symbol, "error": "symbol not found on NSE"})
            continue
        try:
            df = fetch_candles(kite, token, SCALP_TIMEFRAME)
            if df.empty:
                results.append({"symbol": symbol, "error": "no candle data returned"})
                continue
            signal = compute_scalp_signal(df)
            if "error" in signal:
                results.append({"symbol": symbol, "error": signal["error"]})
                continue
            signal["symbol"] = symbol
            results.append(signal)
        except Exception as exc:  # noqa: BLE001 - keep scanning the rest of the watchlist
            log.warning("Scalp scan failed for %s: %s", symbol, exc)
            results.append({"symbol": symbol, "error": str(exc)})
    return results


def _apply_index_bias(results, index_direction):
    """Attaches index_agrees to every non-error row - None index_direction
    (no opinion available this cycle) is left as None too, same
    "no opinion, not a mismatch" convention background._apply_index_filter
    uses for the main screener. Purely informational here - never revokes
    signal_confirmed, unlike the main screener's opt-in strict gate."""
    for r in results:
        if r.get("error"):
            continue
        r["index_agrees"] = None if index_direction is None else (r.get("direction") == index_direction)


# --------------------------------------------------------------------------
# Background loop - independent thread, own state, own persistence file.
# Mirrors background.py's _run_loop shape (never let the thread die, log
# and retry instead) but simpler: no OI, no alerts, no rescan-event (there's
# no settings page for scalp params yet to need a wake-immediately trigger).
# --------------------------------------------------------------------------

_scalp_lock = threading.Lock()
_scalp_state = {
    "results": [],
    "last_scan": None,
    "last_error": None,
    "index_direction": None,
    "index_close": None,
    "index_chg_pct": None,
}


def _load_persisted_scalp_state():
    if not os.path.exists(SCALP_RESULTS_FILE):
        return
    try:
        with open(SCALP_RESULTS_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict) and "results" in saved:
            with _scalp_lock:
                _scalp_state["results"] = saved.get("results", [])
                _scalp_state["last_scan"] = saved.get("last_scan")
                _scalp_state["index_direction"] = saved.get("index_direction")
                _scalp_state["index_close"] = saved.get("index_close")
                _scalp_state["index_chg_pct"] = saved.get("index_chg_pct")
                _scalp_state["last_error"] = None
    except (json.JSONDecodeError, OSError):
        pass


def _save_persisted_scalp_state():
    with _scalp_lock:
        snapshot = {
            "results": _scalp_state["results"],
            "last_scan": _scalp_state["last_scan"],
            "index_direction": _scalp_state["index_direction"],
            "index_close": _scalp_state["index_close"],
            "index_chg_pct": _scalp_state["index_chg_pct"],
        }
    try:
        with open(SCALP_RESULTS_FILE, "w") as f:
            json.dump(snapshot, f, default=str)
    except Exception:  # noqa: BLE001 - persistence must never crash the scan loop
        log.exception("Failed to persist scalp results")


_load_persisted_scalp_state()


def get_scalp_state():
    with _scalp_lock:
        return dict(_scalp_state)


def _run_scalp_loop():
    while True:
        try:
            kite = kite_auth.get_kite_client()
            if kite is not None and is_market_open():
                try:
                    results = scan_scalp_watchlist(kite)
                    index_direction, index_close, index_chg_pct = fetch_index_direction(kite, SCALP_TIMEFRAME)
                    _apply_index_bias(results, index_direction)
                    with _scalp_lock:
                        _scalp_state["results"] = results
                        _scalp_state["index_direction"] = index_direction
                        _scalp_state["index_close"] = index_close
                        _scalp_state["index_chg_pct"] = index_chg_pct
                        _scalp_state["last_scan"] = now_ist().isoformat(timespec="seconds")
                        _scalp_state["last_error"] = None
                    _save_persisted_scalp_state()
                except Exception as exc:  # noqa: BLE001
                    log.exception("Scalp scan failed")
                    with _scalp_lock:
                        _scalp_state["last_error"] = str(exc)
                time.sleep(SCALP_SCAN_INTERVAL_SECONDS)
            else:
                time.sleep(30)
        except Exception:  # noqa: BLE001 - never let this thread die
            log.exception("Scalp scan loop hit an unexpected error - retrying")
            with _scalp_lock:
                _scalp_state["last_error"] = "Scalp loop hit an unexpected error - see server logs."
            time.sleep(30)


def start_scalp_scanner():
    thread = threading.Thread(target=_run_scalp_loop, daemon=True)
    thread.start()
