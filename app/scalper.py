"""
NIFTY 50 Scalping Screener - a separate, faster signal engine from the
main swing-style dashboard (indicators.py/background.py), purpose-built
for fast intraday NIFTY 50 INDEX scalping (e.g. trading NIFTY futures or
ATM options off this signal) - NOT a per-stock screener. Earlier builds
of this scanned the 50 individual NIFTY 50 constituent stocks instead;
that was a misread of the actual ask and has been removed - this now
watches exactly ONE instrument.

Runs on its own 3-minute timeframe and its own background loop
(SCALP_SCAN_INTERVAL_SECONDS, much faster than the main scanner's
configurable interval) - deliberately independent of settings.TIMEFRAME/
settings.WATCHLIST so it never interferes with, or gets reconfigured by,
changes made to the main screener on the Settings page.

The OHLCV feed comes from the nearest-expiry NIFTY 50 INDEX FUTURES
contract (scanner._load_nifty_future), not the raw index. Kite reports
0 volume on raw index historical data (confirmed earlier in this app's
own backtest work - a solo Relative Volume backtest on "NIFTY 50" always
produces zero trades), which would permanently zero out this engine's
Relative Volume parameter if it read the index directly. The futures
contract is a real, tradeable instrument with genuine volume - and
realistically what you'd actually place a NIFTY 50 scalp trade through
anyway.

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

Same opening-window convention as the main screener's 15-minute
timeframe (indicators.OPENING_WINDOW_MINUTES): signal_confirmed is held
False for the first 15 minutes after the 9:15 IST open (until 9:30) -
the most gap-driven, unreliable minutes of the day - even though the
other fields still populate normally underneath.
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
from .indicators import rsi, compute_atr, session_vwap_series, _cross_up, _cross_down, _in_opening_window
from .scanner import fetch_candles, is_market_open, now_ist, _load_nifty_future

log = logging.getLogger(__name__)

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

# Higher-timeframe trend read for the scalp signal, mirroring
# indicators._higher_timeframe_direction's approach for the main screener
# (see indicators._HTF_RESAMPLE) - resamples the SAME 3-minute NIFTY
# futures candles already fetched into a coarser 15-minute bucket and
# reads which way ITS OWN fast/slow EMA leans. This is a genuinely
# different read from the other 3 scalp votes: EMA(5/13) on the 3-minute
# chart can flip several times an hour, and session VWAP resets every
# morning - neither tells you whether the last 15-30 minutes have
# actually been trending up or down. No extra Kite call (same candles,
# just resampled).
SCALP_HTF_RESAMPLE_RULE = "15min"


def _scalp_htf_direction(df: pd.DataFrame):
    """Returns "Bullish"/"Bearish", or None if there isn't enough
    resampled 15-minute history yet (early in the session) - treated as
    "no opinion" by the caller, same convention as indicators.py's
    htf_agrees, so it never blocks every trade before the day's history
    has built up."""
    htf_df = df.resample(SCALP_HTF_RESAMPLE_RULE).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    warmup = SLOW_EMA_LENGTH + 5
    if len(htf_df) < warmup:
        return None
    close = htf_df["close"]
    ema_fast = close.ewm(span=FAST_EMA_LENGTH, adjust=False).mean()
    ema_slow = close.ewm(span=SLOW_EMA_LENGTH, adjust=False).mean()
    return "Bullish" if ema_fast.iloc[-1] > ema_slow.iloc[-1] else "Bearish"


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
    """df must have open/high/low/close/volume columns, oldest row first
    (NIFTY futures candles - see module docstring). Returns the latest
    bar's scalp state, or {"error": ...} if there isn't enough history /
    VWAP hasn't started accumulating yet (only possible in the first
    sliver of a session before any volume has printed - vwap_series is
    NaN there).

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

    # Same opening-window convention as the main screener's 15-minute
    # timeframe (indicators.OPENING_WINDOW_MINUTES=15, i.e. held back
    # until 9:30) - the first few minutes after the open are usually the
    # most gap-driven and unreliable of the day. Only signal_confirmed is
    # suppressed; every other field below still reflects the real
    # current reading.
    in_opening_window = _in_opening_window(df.index[i], SCALP_TIMEFRAME)

    # Higher-timeframe (15-min) trend agreement - a genuine 5th check,
    # kept separate from confirmed_count/MIN_REQUIRED_SCALP (same
    # pattern as indicators.py's htf_agrees for the main screener): it
    # gates the trade call but isn't just another vote toward the 3-of-4
    # count, since "which way the last 15-30 minutes have trended" is a
    # meaningfully different question from "do this instant's 3-minute
    # indicators agree with each other". None (not enough resampled
    # history yet) is always treated as agreeing, so it never blocks
    # trades early in the session before 15-min history has built up.
    htf_direction = _scalp_htf_direction(df)
    htf_agrees = True if htf_direction is None else (htf_direction == direction)

    signal_confirmed = confirmed_count >= MIN_REQUIRED_SCALP and not in_opening_window and htf_agrees

    # Trade-actionable framing, on top of the raw direction/confirmed
    # fields above: rather than making the caller translate "Bullish +
    # confirmed" into "go long" themselves, decide the actual instruction
    # here. WAIT (not BUY/SELL) whenever signal_confirmed is False, with a
    # short reason so the page can explain *why* there's no trade right
    # now instead of just showing a blank/muted state.
    if signal_confirmed:
        trade_action = "BUY" if direction == "Bullish" else "SELL"
        trade_reason = None
    else:
        trade_action = "WAIT"
        if in_opening_window:
            trade_reason = "Opening-window warm-up (first 15 min of the session)"
        elif not htf_agrees:
            trade_reason = "Against the 15-min trend (currently %s)" % htf_direction
        else:
            trade_reason = "Only %d of 4 aligned (need %d)" % (confirmed_count, MIN_REQUIRED_SCALP)
    trade_label = (trade_action + " NIFTY FUT") if trade_action != "WAIT" else "NO TRADE - WAIT"

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
        "in_opening_window": in_opening_window,
        "htf_direction": htf_direction,
        "htf_agrees": htf_agrees,
        "signal_confirmed": signal_confirmed,
        "trade_action": trade_action,
        "trade_label": trade_label,
        "trade_reason": trade_reason,
        "atr": round(float(atr_val), 2) if pd.notna(atr_val) else None,
        "stop": stop,
        "target": target,
        "risk_reward": round(ATR_TARGET_MULT / ATR_STOP_MULT, 2) if stop is not None else None,
        "timestamp": df.index[i].isoformat(),
    }


def scan_nifty_scalp(kite) -> dict:
    """Resolves the current NIFTY future and returns its scalp signal, or
    {"error": ...} if the contract/candles aren't available yet. Single
    dict, not a list - there's exactly one instrument to watch."""
    token, fut_symbol = _load_nifty_future(kite)
    if not token:
        return {"error": "could not resolve current NIFTY futures contract"}
    df = fetch_candles(kite, token, SCALP_TIMEFRAME)
    if df.empty:
        return {"error": "no candle data returned"}
    signal = compute_scalp_signal(df)
    signal["future_symbol"] = fut_symbol
    return signal


# --------------------------------------------------------------------------
# Background loop - independent thread, own state, own persistence file.
# Mirrors background.py's _run_loop shape (never let the thread die, log
# and retry instead) but simpler: single instrument, no OI, no alerts, no
# rescan-event (there's no settings page for scalp params yet to need a
# wake-immediately trigger).
# --------------------------------------------------------------------------

_scalp_lock = threading.Lock()
_scalp_state = {
    "signal": None,
    "last_scan": None,
    "last_error": None,
}


def _load_persisted_scalp_state():
    if not os.path.exists(SCALP_RESULTS_FILE):
        return
    try:
        with open(SCALP_RESULTS_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict) and "signal" in saved:
            with _scalp_lock:
                _scalp_state["signal"] = saved.get("signal")
                _scalp_state["last_scan"] = saved.get("last_scan")
                _scalp_state["last_error"] = None
    except (json.JSONDecodeError, OSError):
        pass


def _save_persisted_scalp_state():
    with _scalp_lock:
        snapshot = {
            "signal": _scalp_state["signal"],
            "last_scan": _scalp_state["last_scan"],
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
                    signal = scan_nifty_scalp(kite)
                    with _scalp_lock:
                        _scalp_state["signal"] = signal
                        _scalp_state["last_scan"] = now_ist().isoformat(timespec="seconds")
                        _scalp_state["last_error"] = signal.get("error")
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
