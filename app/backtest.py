"""
Historical backtest for a customizable RSI/MACD/EMA-BB/volume signal.
Rather than a single fixed 3-indicator rule, you choose which of the
available PARAM_DEFS to combine (checkboxes on the backtest page) and
how many of your chosen ones must agree at once (the "required" count) -
so you can test your own combination, not just the dashboard's default.

Available parameters (see PARAM_DEFS below):
  - RSI Cross / MACD Cross / EMA-BB Cross: the original 3 crossover
    events - each fires only on the exact bar the cross happens (see
    indicators.py's rsi_up/rsi_dn etc.), same events that trigger a
    Telegram alert (alerts.py keys off fresh_signal, not a lingering
    "still aligned" state - see the git history on this file for why
    that distinction matters: a majority-state check is always true
    with 3 indicators and silently produces zero backtest trades).
  - RSI Threshold: RSI > 65 for Bullish, RSI < 35 for Bearish - a
    momentum/extremity state rather than a crossover, so it can span
    many consecutive bars.
  - Relative Volume: today's bar's volume vs its own 20-bar average,
    > 1.2x - a conviction/confirmation state, applied the same way to
    both directions.

A signal fires the moment at least `required` of your chosen parameters
agree on the same direction at once; a rising-edge check still turns
that into discrete entries (so a state that stays true for a stretch of
bars only counts as one trade, not one per bar).

Reuses indicators.compute_series() directly rather than reimplementing
the indicator math in a separate form, so results here stay faithful
to what the live dashboard is actually computing.

Three optional FILTER_DEFS checkboxes (require_htf, require_regime_volume,
exclude_opening_window - see _signal_series) let you additionally replay
the same GATES the live screener applies on top of its 4 parameters
(higher-timeframe trend agreement, the ADX-regime-scaled Relative Volume
bar, and the opening-window/4-hour-warmup suppression - see
indicators.compute_signal). All three default OFF, so existing behaviour
and every prior backtest result is unchanged unless you opt in - turning
all three on is what makes a backtest run genuinely comparable to what
"Confirmed" means live, closing the gap this module's docstring used to
just warn about. See _htf_direction_series/_regime_volume_hot_series/
_opening_window_mask below for the vectorized, no-lookahead replay of
each one.

Runs as its own background thread (like the main scanner) rather than
inside a request handler - fetching + replaying a whole watchlist can
take a while, well past what a web request should block on. Poll
get_backtest_state() from the dashboard to show progress.
"""
import datetime as dt
import gc
import hashlib
import json
import pickle
import logging
import os
import shutil
from pathlib import Path
import threading
import time

import numpy as np
import pandas as pd

from . import config
from . import costs, early_signal, early_research, v6_edge, v8_dual, research_runtime, v94_magnitude, v95_daily_evidence, v953_contract_structure, v96_trial17, v97_trial19, nse_futures_history, nse_cash_history, nse_mwpl, nse_earnings_history, nse_market_regime
from .config import settings, PARAM_WEIGHTS_FILE, WATCHLIST_TIMEFRAME
from .indicators import (
    compute_series, compute_avwap_series, session_vwap_series, BIG_CANDLE_LOOKBACK,
    _OPENING_WINDOW_TIMEFRAMES,
    _compute_adx, _classify_regime, _in_opening_window, _in_4hour_warmup, _HTF_RESAMPLE,
    effective_min_atr_pct,
)
from . import scanner as scanner_mod
from .scanner import _load_instrument_map, _load_index_token, now_ist

# Index symbols selectable as "also backtest" checkboxes on the Backtest
# page (web.py's /api/backtest/start and /api/weights/start both accept
# these via the index_symbol form field, in addition to your normal
# WATCHLIST) - resolved via scanner._load_index_token rather than the
# equity instrument map, since neither trades as a normal NSE equity
# (NIFTY 50 is an NSE index, SENSEX a BSE index - see
# scanner.INDEX_EXCHANGES). Note neither has a real traded "volume" the
# way a stock does (Kite returns 0), so the "rel_volume" backtest
# parameter never fires for these - that's expected, not a bug.
INDEX_SYMBOLS = ["NIFTY 50", "SENSEX"]

log = logging.getLogger(__name__)

DEFAULT_HORIZONS = (1, 2, 3, 5, 10)


def research_promotable(stats, min_trades=60, min_profit_factor=1.10):
    """Whether an untouched holdout result is strong enough for live use.

    This is intentionally simple and conservative: positive net expectancy,
    profit factor above 1.10 and enough trades.  Win rate alone is not a
    promotion criterion.
    """
    if not stats:
        return False
    return bool(
        (stats.get("trade_count") or 0) >= min_trades
        and (stats.get("avg_return_pct") is not None and stats.get("avg_return_pct") > 0)
        and (stats.get("profit_factor") is not None and stats.get("profit_factor") > min_profit_factor)
    )
WARMUP_DAYS = 20          # extra calendar days fetched before the requested
                           # window purely so indicators are warmed up -
                           # trades are never counted in this stretch.
# "Days" means CALENDAR days, so the same number buys wildly different
# amounts of DATA depending on the timeframe: 30 days is ~750 bars of
# 15-minute candles but only ~21 trading bars of daily ones - well under the
# ~40 the indicators need to warm up. That mismatch silently skipped every
# symbol ("not enough historical candles returned") and made the whole
# backtest look broken while reporting no error at all.
#
# So the bounds are per-timeframe. The upper bounds respect what Kite will
# serve (intraday history is limited; daily/weekly goes back years, and
# _fetch_historical_chunked splits any range into safe per-request chunks).
# The lower bounds are the point below which a run cannot produce a single
# valid bar, and are ENFORCED with a clear error rather than a silent zero.
MAX_BACKTEST_DAYS_BY_TF = {
    "15minute": 365, "60minute": 365, "4hour": 730, "day": 1095, "week": 1825,
}
MIN_BACKTEST_DAYS_BY_TF = {
    "15minute": 5, "60minute": 10, "4hour": 30, "day": 120, "week": 540,
}
DEFAULT_BACKTEST_DAYS_BY_TF = {
    "15minute": 180, "60minute": 90, "4hour": 180, "day": 365, "week": 900,
}
MAX_BACKTEST_DAYS = 1095  # absolute ceiling; the per-timeframe cap above is what actually applies


def backtest_day_bounds(timeframe):
    """(min, max, default) calendar days for a timeframe - drives both the
    server-side clamp and the form's own min/max/value attributes, so the
    page can never offer a number the engine will reject."""
    return (MIN_BACKTEST_DAYS_BY_TF.get(timeframe, 5),
            MAX_BACKTEST_DAYS_BY_TF.get(timeframe, 90),
            DEFAULT_BACKTEST_DAYS_BY_TF.get(timeframe, 30))
_RATE_LIMIT_PAUSE = 0.35  # ~3 req/sec, matching Kite's historical-data rate limit
MAX_TRADES_RETURNED = 500  # cap on the trade-by-trade list sent to the browser
                            # (see run_backtest) - summary stats always use every trade


# Round-trip cost defaults, from Zerodha's own published charges (see
# NEXT_HORIZON_RESEARCH.md Finding 2). Stock FUTURES run roughly
# 0.06-0.10% of notional per round trip; OPTIONS run 0.6-1%+ because STT
# and exchange charges are levied on PREMIUM rather than underlying
# notional, a structurally much smaller base. The futures figure is the
# default here because that is what this screener's own signals are sized
# against; an options strategy should be tested with a far higher number.
DEFAULT_COST_PCT = 0.08        # round-trip brokerage + STT + exchange + GST, % of notional
DEFAULT_SLIPPAGE_PCT = 0.05    # per side; doubled below, since you cross the spread twice



# The full menu of selectable backtest parameters - shown as checkboxes on
# the backtest page (web.py passes PARAM_DEFS straight to the template so
# labels stay in one place). Add a new one here and in _param_bull_bear().
PARAM_DEFS = [
    {"id": "rsi_cross", "label": "RSI Cross (vs its smoothing line)"},
    {"id": "macd_cross", "label": "MACD Cross (vs signal line)"},
    {"id": "rel_volume", "label": "Relative Volume above your configured threshold (20-bar avg, Settings page) - confirmation only, combine with a directional parameter"},
    {"id": "cmf_flow", "label": "Chaikin Money Flow sign (directional volume - Bullish if recent volume skewed toward up-closes, Bearish if down-closes; distinct from the magnitude-only Relative Volume above - see PARAMETER_ANALYSIS_2.md Finding #2)"},
    {"id": "candle_pattern", "label": "Candlestick pattern (Engulfing / Hammer-family / Morning-Evening Star - reads the raw shape of recent price action, not a smoothed derivative like the others above - see NEXT_HORIZON_RESEARCH.md)"},
    {"id": "big_candle_pattern", "label": "Big candle / range expansion (a bar whose own range is a real multiple of its ATR AND closes near its own high/low - an ANTICIPATORY read, not a smoothed derivative like RSI/MACD/EMA-BB above - see indicators._compute_big_candle)"},
    {"id": "strong_close", "label": "Strong close in range (close in the extreme top/bottom % of the bar's own high-low range, regardless of range size - the BTST 'closed with conviction' read)"},
]
PARAM_IDS = [p["id"] for p in PARAM_DEFS]
DEFAULT_PARAMS = ("rsi_cross", "macd_cross", "cmf_flow")  # the live screener's 3 directional votes
DEFAULT_REQUIRED = 2

# Optional GATES (not votes - see _signal_series) that replay the same
# checks indicators.compute_signal applies live on top of its 4-parameter
# vote, before it counts a bar as "Confirmed". Shown as a separate row of
# checkboxes on the backtest page, distinct from PARAM_DEFS above: these
# don't add to bull_count/bear_count, they can only SUPPRESS a signal your
# chosen parameters already agree on. All default OFF so nothing here
# changes any existing backtest/weight-run result unless you opt in.
FILTER_DEFS = [
    {
        "id": "require_oi_agreement",
        "label": "OI positioning agreement (an unusual OI move must form a fresh buildup in the "
                  "signal's direction - THE gate the live shortlist runs on, and the one this file "
                  "could not replay until now)",
    },
    {
        "id": "require_htf",
        "label": "Higher-timeframe trend agreement (matches the live screener's HTF filter)",
    },
    {
        "id": "require_regime_volume",
        "label": "Regime-adaptive Relative Volume threshold (stricter bar when ADX reads Ranging, matches live) "
                  "- only changes anything if Relative Volume is also selected above",
    },
    {
        "id": "exclude_opening_window",
        "label": "Exclude the opening-window / 4-hour warm-up (first 15 min after 9:15, or first 30 min of "
                  "each 4-hour block, matches live)",
    },
    {
        "id": "require_candle_pattern",
        "label": "Candlestick-pattern agreement (most recent Engulfing/Hammer-family/Morning-Evening Star "
                  "pattern must match your signal's direction, matches live's optional "
                  "REQUIRE_CANDLE_PATTERN_AGREEMENT gate)",
    },
    {
        "id": "require_macd_hist",
        "label": "MACD histogram momentum agreement (histogram must be growing in your signal's direction - "
                  "momentum accelerating, not fading - matches live's optional REQUIRE_MACD_HIST_AGREEMENT gate)",
    },
    {
        "id": "require_big_candle",
        "label": "Big-candle agreement (the most recent range-expansion big candle within the last 15 bars must "
                  "match your signal's direction, matches live's optional REQUIRE_BIG_CANDLE_AGREEMENT gate)",
    },
    {
        "id": "require_strong_close",
        "label": "Strong-close agreement (the bar's close must land in the extreme top/bottom of its own range, "
                  "in your signal's direction, matches live's optional REQUIRE_STRONG_CLOSE_AGREEMENT gate)",
    },
    {
        "id": "require_entry_location",
        "label": "Entry-location filter (skip bars where price is already more than your configured ATR multiple "
                  "past its own VWAP - i.e. the move is being chased rather than caught early - matches live's "
                  "optional REQUIRE_ENTRY_LOCATION_AGREEMENT gate)",
    },
    {
        "id": "require_atr_floor",
        "label": "Minimum-ATR volatility floor (skip bars where the stock's ATR as a % of price is below your "
                  "configured floor - too quiet to plausibly deliver a big move - matches live's optional "
                  "REQUIRE_ATR_FLOOR gate)",
    },
]
# NOTE on parity coverage: every LIVE gate is now replayable here EXCEPT
# three, each for a structural reason rather than an oversight.
# REQUIRE_INDEX_AGREEMENT and REQUIRE_SECTOR_AGREEMENT would each need a
# second instrument's full history fetched and replayed per symbol (NIFTY
# 50, or that symbol's own sectoral index) - doable, but it multiplies
# every backtest's Kite API cost, so it's deliberately deferred rather
# than silently approximated. REQUIRE_BREADTH_AGREEMENT is watchlist-
# scoped and cross-sectional (it depends on what every OTHER symbol was
# doing on that same bar), which this per-symbol replay architecture
# can't express at all without restructuring the whole run. And
# REQUIRE_DELIVERY_AGREEMENT can never be backtested: NSE publishes
# delivery data only for recent sessions, with no historical archive
# reachable from here, so there is no past value to replay.
FILTER_IDS = [f["id"] for f in FILTER_DEFS]


# --------------------------------------------------------------------------
# Background job plumbing (mirrors background.py's pattern: a lock-guarded
# state dict + a daemon thread, polled from the dashboard)
# --------------------------------------------------------------------------

_bt_lock = threading.Lock()
_bt_state = {
    "status": "idle",  # idle | running | done | error
    "progress": {"done": 0, "total": 0, "symbol": None},
    "params": None,
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}


def get_backtest_state() -> dict:
    with _bt_lock:
        return dict(_bt_state, progress=dict(_bt_state["progress"]))


def _progress_cb(done, total, symbol):
    with _bt_lock:
        _bt_state["progress"] = {"done": done, "total": total, "symbol": symbol}


def start_backtest(kite, symbols=None, timeframe=None, days=30, horizons=DEFAULT_HORIZONS,
                    params=DEFAULT_PARAMS, required=DEFAULT_REQUIRED,
                    require_htf=False, require_regime_volume=False, exclude_opening_window=False,
                    require_candle_pattern=False,
                    require_macd_hist=False, require_big_candle=False,
                    require_strong_close=False, require_entry_location=False,
                    require_atr_floor=False, require_oi_agreement=False,
                    cost_pct=DEFAULT_COST_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT,
                    holdout_pct=0.0) -> dict:
    """Kicks off a backtest run in a background thread. Returns
    {"started": True} or {"started": False, "reason": ...} if one is
    already running - only one backtest runs at a time. The five
    require_htf/require_regime_volume/exclude_opening_window/require_
    volume_flow/require_candle_pattern flags are the FILTER_DEFS gates
    above - all default False, matching every prior caller/test that
    only ever passed params/required."""
    with _bt_lock:
        if _bt_state["status"] == "running":
            return {"started": False, "reason": "A backtest is already running."}
        symbols = list(symbols or settings.WATCHLIST)
        timeframe = timeframe or WATCHLIST_TIMEFRAME
        params = tuple(params)
        _bt_state["status"] = "running"
        _bt_state["progress"] = {"done": 0, "total": len(symbols), "symbol": None}
        _bt_state["params"] = {
            "timeframe": timeframe, "days": days, "horizons": list(horizons),
            "params": list(params), "required": required,
            "require_htf": bool(require_htf), "require_regime_volume": bool(require_regime_volume),
            "exclude_opening_window": bool(exclude_opening_window),
            "require_candle_pattern": bool(require_candle_pattern),
            "require_macd_hist": bool(require_macd_hist),
            "require_big_candle": bool(require_big_candle),
            "require_strong_close": bool(require_strong_close),
            "require_entry_location": bool(require_entry_location),
            "require_atr_floor": bool(require_atr_floor),
            "require_oi_agreement": bool(require_oi_agreement),
        }
        _bt_state["result"] = None
        _bt_state["error"] = None
        _bt_state["started_at"] = now_ist().isoformat(timespec="seconds")
        _bt_state["finished_at"] = None

    thread = threading.Thread(
        target=_run_backtest_job,
        args=(kite, symbols, timeframe, days, horizons, params, required,
              require_htf, require_regime_volume, exclude_opening_window,
              require_candle_pattern, require_macd_hist, require_big_candle,
              require_strong_close, require_entry_location, require_atr_floor,
              require_oi_agreement,
              cost_pct, slippage_pct, holdout_pct),
        daemon=True,
    )
    thread.start()
    return {"started": True}


def _run_backtest_job(kite, symbols, timeframe, days, horizons, params, required,
                       require_htf=False, require_regime_volume=False, exclude_opening_window=False,
                       require_candle_pattern=False,
                    require_macd_hist=False, require_big_candle=False,
                    require_strong_close=False, require_entry_location=False,
                    require_atr_floor=False, require_oi_agreement=False,
                    cost_pct=DEFAULT_COST_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT,
                    holdout_pct=0.0):
    try:
        result = run_backtest(
            kite, symbols, timeframe=timeframe, days=days, horizons=horizons,
            params=params, required=required, progress_cb=_progress_cb,
            cost_pct=cost_pct, slippage_pct=slippage_pct, holdout_pct=holdout_pct,
            require_htf=require_htf, require_regime_volume=require_regime_volume,
            exclude_opening_window=exclude_opening_window, require_candle_pattern=require_candle_pattern,
            require_macd_hist=require_macd_hist, require_big_candle=require_big_candle,
            require_strong_close=require_strong_close,
            require_entry_location=require_entry_location,
            require_atr_floor=require_atr_floor,
            require_oi_agreement=require_oi_agreement,
        )
        with _bt_lock:
            _bt_state["status"] = "done"
            _bt_state["result"] = result
    except Exception as exc:  # noqa: BLE001 - a failed backtest must never crash the app
        log.exception("Backtest run failed")
        with _bt_lock:
            _bt_state["status"] = "error"
            _bt_state["error"] = str(exc)
    finally:
        with _bt_lock:
            _bt_state["finished_at"] = now_ist().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Historical data fetching
# --------------------------------------------------------------------------

_INTRADAY_INTERVAL_MINUTES = {
    "minute": 1, "3minute": 3, "5minute": 5, "10minute": 10,
    "15minute": 15, "30minute": 30, "60minute": 60, "4hour": 240,
}


def _drop_incomplete_intraday_bars(df, interval, now=None):
    """Keep only candles whose full interval had elapsed at research time.

    Kite can return the currently-forming intraday candle. Research must never
    score that partial bar because its OHLC/volume are not yet knowable live at
    candle close. The index may be tz-aware or naive; ``now_ist`` is normally
    naive IST, so normalize the comparison shape without changing wall time.
    """
    if df is None or df.empty:
        return df
    minutes = _INTRADAY_INTERVAL_MINUTES.get(str(interval))
    if not minutes:
        return df
    idx = pd.DatetimeIndex(df.index)
    clock = pd.Timestamp(now if now is not None else now_ist())
    if idx.tz is not None and clock.tzinfo is None:
        clock = clock.tz_localize(idx.tz)
    elif idx.tz is None and clock.tzinfo is not None:
        clock = clock.tz_localize(None)
    completed_at = idx + pd.Timedelta(minutes=minutes)
    if str(interval) == "4hour":
        # NSE cash/F&O session ends at 15:30, so the final 13:15 bucket is a
        # legitimate session-closed 4H context candle even though the exchange
        # day has only 6h15m. During the session it remains incomplete.
        session_close = idx.normalize() + pd.Timedelta(hours=15, minutes=30)
        completed_at = pd.DatetimeIndex([min(a, b) for a, b in zip(completed_at, session_close)])
    completed = completed_at <= clock
    return df.loc[completed].copy()


def _history_coverage_summary(price_df, oi_series, requested_days):
    """Transparent price/OI measurement coverage for one research symbol."""
    price_idx = pd.DatetimeIndex([] if price_df is None else price_df.index)
    oi = pd.Series(dtype="float64") if oi_series is None else pd.Series(oi_series).dropna()
    price_bars = int(len(price_idx))
    oi_bars = int(len(oi))
    return {
        "requested_days": int(requested_days),
        "price_bars": price_bars,
        "price_first_timestamp": price_idx[0].isoformat() if price_bars else None,
        "price_last_timestamp": price_idx[-1].isoformat() if price_bars else None,
        "oi_bars": oi_bars,
        "oi_first_timestamp": pd.Timestamp(oi.index[0]).isoformat() if oi_bars else None,
        "oi_last_timestamp": pd.Timestamp(oi.index[-1]).isoformat() if oi_bars else None,
        "oi_bar_coverage_pct": round((oi_bars / price_bars * 100.0), 1) if price_bars else 0.0,
        "oi_available": bool(oi_bars),
    }


def _aggregate_history_coverage(rows, *, timeframe, requested_days):
    rows = list(rows or [])
    price_bars = sum(int(r.get("price_bars") or 0) for r in rows)
    oi_bars = sum(int(r.get("oi_bars") or 0) for r in rows)
    valid_oi = sum(1 for r in rows if r.get("oi_available"))
    price_first = [r.get("price_first_timestamp") for r in rows if r.get("price_first_timestamp")]
    price_last = [r.get("price_last_timestamp") for r in rows if r.get("price_last_timestamp")]
    oi_first = [r.get("oi_first_timestamp") for r in rows if r.get("oi_first_timestamp")]
    oi_last = [r.get("oi_last_timestamp") for r in rows if r.get("oi_last_timestamp")]
    return {
        "timeframe": timeframe,
        "requested_days": int(requested_days),
        "symbols_measured": len(rows),
        "symbols_with_oi": valid_oi,
        "symbols_without_oi": max(0, len(rows) - valid_oi),
        "price_bars": price_bars,
        "oi_bars": oi_bars,
        "oi_bar_coverage_pct": round((oi_bars / price_bars * 100.0), 1) if price_bars else 0.0,
        "price_first_timestamp": min(price_first) if price_first else None,
        "price_last_timestamp": max(price_last) if price_last else None,
        "oi_first_timestamp": min(oi_first) if oi_first else None,
        "oi_last_timestamp": max(oi_last) if oi_last else None,
        "note": (
            "Intraday OI uses the currently live near-expiry futures token; expired-contract "
            "15-minute OI is not reconstructed. Missing history remains missing and lowers coverage."
        ),
    }


def _daily_oi_coverage_summary(daily_oi_map, symbols):
    rows = []
    for symbol in symbols or []:
        ser = (daily_oi_map or {}).get(symbol)
        if ser is None:
            continue
        s = pd.Series(ser).dropna()
        if s.empty:
            continue
        rows.append((symbol, s))
    first = [pd.Timestamp(s.index[0]).isoformat() for _, s in rows]
    last = [pd.Timestamp(s.index[-1]).isoformat() for _, s in rows]
    return {
        "symbols_measured": len(list(symbols or [])),
        "symbols_with_daily_oi": len(rows),
        "daily_oi_observations": int(sum(len(s) for _, s in rows)),
        "first_timestamp": min(first) if first else None,
        "last_timestamp": max(last) if last else None,
        "source": "Kite daily futures OI with continuous=True; mapped point-in-time only after each completed session",
        "lookahead_guard": "same-day morning bars can see only the previous completed daily OI observation",
    }


def _merge_daily_oi_coverage(rows, symbols_measured):
    rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    with_oi = sum(1 for r in rows if int(r.get("symbols_with_daily_oi") or 0) > 0)
    observations = sum(int(r.get("daily_oi_observations") or 0) for r in rows)
    first = [r.get("first_timestamp") for r in rows if r.get("first_timestamp")]
    last = [r.get("last_timestamp") for r in rows if r.get("last_timestamp")]
    return {
        "symbols_measured": int(symbols_measured or 0),
        "symbols_with_daily_oi": int(with_oi),
        "daily_oi_observations": int(observations),
        "first_timestamp": min(first) if first else None,
        "last_timestamp": max(last) if last else None,
        "source": "Kite daily futures OI with continuous=True; fetched per symbol and mapped point-in-time only after each completed session",
        "lookahead_guard": "same-day morning bars can see only the previous completed daily OI observation",
    }


def _fetch_history(token, timeframe, days, kite):
    """Return completed historical candles using the same safe chunking as live scans."""
    to_date = now_ist()
    from_date = to_date - dt.timedelta(days=days)
    interval = "60minute" if timeframe == "4hour" else timeframe

    data = scanner_mod._fetch_historical_chunked(
        kite, token, from_date, to_date, interval, oi=False, continuous=False
    )
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df = df.rename(columns={"date": "timestamp"}).set_index("timestamp")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = _drop_incomplete_intraday_bars(df, interval, now=to_date)

    if timeframe == "4hour":
        df = df.resample("4h", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        df = _drop_incomplete_intraday_bars(df, "4hour", now=to_date)

    return df


# --------------------------------------------------------------------------
# Vectorized replay of the chosen parameter combination over a symbol's
# full history
# --------------------------------------------------------------------------

def _param_bull_bear(series: dict, param_id: str, rel_volume_hot: pd.Series = None):
    """Returns (bullish_bool_series, bearish_bool_series) for one
    selectable parameter, aligned to series' index. The *_cross
    parameters are single-bar pulses (true only on the exact bar the
    cross happens); rel_volume/cmf_flow/candle_pattern/big_candle_pattern/
    strong_close are states that can stay true for a stretch of
    consecutive bars.

    rel_volume_hot, when given (see _signal_series' require_regime_volume
    handling), REPLACES the plain flat-threshold "hot" read with the
    ADX-regime-scaled one from _regime_volume_hot_series - this is how
    the regime-adaptive volume threshold is wired in: it only ever
    changes what "hot" means for the rel_volume parameter's OWN vote, it
    never adds a separate mandatory gate of its own, mirroring how ADX
    regime only ever scales indicators.compute_signal's existing 4th
    parameter live, rather than acting as a 5th vote."""
    if param_id == "rsi_cross":
        return series["rsi_up"], series["rsi_dn"]
    if param_id == "macd_cross":
        return series["macd_up"], series["macd_dn"]
    if param_id == "rel_volume":
        if rel_volume_hot is not None:
            is_hot = rel_volume_hot
        else:
            volume = series["df"]["volume"]
            vol_avg = series["vol_avg"]
            rel_vol = volume / vol_avg.replace(0, np.nan)
            is_hot = rel_vol.notna() & (rel_vol > settings.REL_VOLUME_THRESHOLD)
        return is_hot, is_hot.copy()  # same condition confirms either direction
    if param_id == "cmf_flow":
        # Directional, unlike rel_volume above - genuinely has its own
        # bull/bear read (Chaikin Money Flow's sign), reused straight
        # from compute_series (see indicators._compute_cmf) rather than
        # recomputed here, same "reuse compute_series" convention as
        # every other parameter in this function.
        cmf = series["cmf"]
        return cmf > 0, cmf < 0
    if param_id == "candle_pattern":
        # Directional, like cmf_flow above - reused straight from
        # compute_series' own "candle_direction" column (see
        # indicators._compute_candle_pattern) rather than recomputed
        # here, same "reuse compute_series" convention as every other
        # parameter in this function.
        cd = series["candle_direction"]
        return cd == "Bullish", cd == "Bearish"
    if param_id == "big_candle_pattern":
        # Directional, like candle_pattern above - reused straight from
        # compute_series' own "big_candle_direction" column (see
        # indicators._compute_big_candle) rather than recomputed here,
        # same "reuse compute_series" convention as every other
        # parameter in this function. Genuinely different information
        # from candle_pattern: this is a SINGLE-bar range-expansion +
        # extreme-close read (ATR-relative), not a multi-bar shape.
        bcd = series["big_candle_direction"]
        return bcd == "Bullish", bcd == "Bearish"
    if param_id == "strong_close":
        # Directional, like big_candle_pattern above - reused from
        # compute_series' own "close_position" column (0-1, see
        # indicators._compute_big_candle), tested against the SAME
        # settings.STRONG_CLOSE_THRESHOLD_PCT the live dashboard's
        # strong_close_agrees field uses - but WITHOUT big_candle_
        # pattern's range-expansion requirement, so this fires on any
        # extreme close, wide bar or not.
        close_position = series["close_position"]
        hi_cut = settings.STRONG_CLOSE_THRESHOLD_PCT / 100.0
        lo_cut = 1 - hi_cut
        return close_position >= hi_cut, close_position <= lo_cut
    raise ValueError(f"unknown backtest parameter: {param_id}")


def _candle_pattern_agree_series(series: dict, direction: pd.Series) -> pd.Series:
    """Vectorized replay of indicators.compute_signal's candle_agrees -
    does the most recent candlestick pattern's direction (see
    indicators._compute_candle_pattern, reused from compute_series' own
    "candle_direction" column) agree with each bar's own chosen
    direction? None (no pattern fired, or a bullish and bearish pattern
    both fired on the same bar) is treated as agreeing - same "None
    means agree" convention used by _volume_flow_agree_series/require_
    htf above, so a bar with no candle-pattern opinion is never silently
    suppressed."""
    cd = series["candle_direction"]
    return cd.isna() | (cd == direction)


def _macd_hist_agree_series(series: dict, direction: pd.Series) -> pd.Series:
    """Vectorized replay of indicators.compute_signal's macd_hist_agrees -
    is the MACD histogram GROWING in each bar's own chosen direction
    (momentum accelerating) rather than shrinking against it? Reads the
    histogram's own slope (this bar vs. the previous one), deliberately
    NOT "hist > 0", which would be identical to the macd_line/signal_line
    check already available as the macd_cross parameter. The first bar
    (no previous bar to compare against) is treated as agreeing - same
    "None means agree" convention used throughout."""
    hist = series["macd_hist"]
    rising = hist > hist.shift(1)
    no_opinion = hist.isna() | hist.shift(1).isna()
    agrees = pd.Series(np.where(direction == "Bullish", rising, ~rising), index=hist.index)
    return agrees | no_opinion


def _big_candle_agree_series(series: dict, direction: pd.Series) -> pd.Series:
    """Vectorized replay of indicators.compute_signal's big_candle_agrees -
    does the most recent qualifying range-expansion "big candle" within
    BIG_CANDLE_LOOKBACK bars agree with each bar's own chosen direction?
    Uses a forward-fill limited to that lookback window, which is exactly
    the vectorized equivalent of compute_signal's own backward search over
    the same window (and is no-lookahead by construction - ffill only ever
    carries PAST values forward). A bar with no qualifying big candle
    anywhere in its lookback reads as agreeing, same convention as every
    other agree-series here."""
    recent_dir = series["big_candle_direction"].ffill(limit=BIG_CANDLE_LOOKBACK)
    return recent_dir.isna() | (recent_dir == direction)


def _strong_close_agree_series(series: dict, direction: pd.Series) -> pd.Series:
    """Vectorized replay of indicators.compute_signal's strong_close_agrees -
    did each bar's own close land in the extreme top/bottom
    settings.STRONG_CLOSE_THRESHOLD_PCT% of its own high-low range, in
    that bar's chosen direction? A doji bar (high == low, close_position
    NaN) is treated as agreeing rather than silently suppressed."""
    close_position = series["close_position"]
    hi_cut = settings.STRONG_CLOSE_THRESHOLD_PCT / 100.0
    lo_cut = 1 - hi_cut
    agrees = pd.Series(
        np.where(direction == "Bullish", close_position >= hi_cut, close_position <= lo_cut),
        index=close_position.index,
    )
    return agrees | close_position.isna()


def _entry_location_agree_series(series: dict, direction: pd.Series, timeframe: str) -> pd.Series:
    """Vectorized replay of indicators.compute_signal's
    entry_location_agrees - is price already more than settings.
    MAX_ENTRY_EXTENSION_ATR ATRs past its own VWAP, in each bar's own
    chosen direction (i.e. the move is being chased rather than caught
    early)? Mirrors live's VWAP-with-AVWAP-fallback: session VWAP where
    the timeframe has one (intraday), the anchored VWAP otherwise, so
    day/week backtests aren't silently ungated. Bars with no usable
    VWAP/ATR yet read as agreeing."""
    df = series["df"]
    vwap_series = session_vwap_series(df, timeframe)
    if vwap_series.isna().all():
        vwap_series = compute_avwap_series(series)
    atr = series["atr"]
    usable = vwap_series.notna() & atr.notna() & (atr > 0)
    raw_distance = (df["close"] - vwap_series) / atr.replace(0, np.nan)
    signed = pd.Series(np.where(direction == "Bullish", raw_distance, -raw_distance), index=df.index)
    extended = usable & (signed > settings.MAX_ENTRY_EXTENSION_ATR)
    return ~extended


def _atr_floor_agree_series(series: dict) -> pd.Series:
    """Vectorized replay of indicators.compute_signal's atr_floor_agrees -
    is this stock's ATR, as a % of its own price, at or above settings.
    MIN_ATR_PCT on each bar (i.e. is it moving enough to plausibly deliver
    a real move at all)? Directionless by nature, so unlike the other
    agree-series here it takes no `direction` argument. Bars with no ATR
    yet read as agreeing."""
    df = series["df"]
    atr = series["atr"]
    atr_pct = atr / df["close"].replace(0, np.nan) * 100
    return atr_pct.isna() | (atr_pct >= settings.MIN_ATR_PCT)


def _regime_volume_hot_series(series: dict) -> pd.Series:
    """Vectorized replay of indicators.compute_signal's regime-adaptive
    Relative Volume bar: ADX classifies EVERY bar's own Trending/Ranging/
    Transitional regime (_compute_adx/_classify_regime, same as live),
    and only a Ranging bar gets REL_VOLUME_THRESHOLD scaled up by
    RANGING_VOL_MULTIPLIER - Trending/Transitional/unknown-regime bars
    use your configured threshold unchanged, exactly like
    compute_signal's `vol_threshold_multiplier = RANGING_VOL_MULTIPLIER
    if regime == "Ranging" else 1.0`. Only meaningful if "rel_volume" is
    one of your chosen PARAM_DEFS for this run - see _param_bull_bear."""
    df = series["df"]
    adx_series = _compute_adx(df, settings.ADX_LENGTH)
    regime = adx_series.apply(_classify_regime)
    multiplier = regime.map({"Ranging": settings.RANGING_VOL_MULTIPLIER}).fillna(1.0)
    effective_threshold = settings.REL_VOLUME_THRESHOLD * multiplier
    rel_vol = df["volume"] / series["vol_avg"].replace(0, np.nan)
    return rel_vol.notna() & (rel_vol > effective_threshold)


def _htf_direction_series(df: pd.DataFrame, timeframe: str) -> pd.Series:
    """Vectorized, no-lookahead replay of indicators._higher_timeframe_
    direction across a WHOLE backtest window (live only ever reads the
    single latest bucket - a backtest replay needs a direction opinion at
    EVERY bar, which is a materially different problem, not just a loop
    over the live function).

    Live safely reads the CURRENTLY-forming HTF bucket, because it's only
    ever built from candles that have actually arrived by "now" - there's
    no future data in it to leak. Naively resampling the FULL history
    here and reading each bucket's fully-closed OHLC would NOT be safe
    the same way: an early bar inside a still-forming 4-hour bucket would
    be reading that bucket's EVENTUAL close, i.e. its own future - real
    lookahead bias. To avoid that, every bar here only ever sees its most
    recently FULLY CLOSED HTF bucket's direction (shift by one bucket,
    then merge_asof direction="backward" to align back onto the
    fine-grained index) - a conservative, explicitly one-bucket-stale
    choice, not a literal replay of live's read.

    Returns an object Series aligned to df's index: "Bullish"/"Bearish",
    or None where this timeframe has no HTF entry (see _HTF_RESAMPLE) or
    the owning bucket hadn't finished warming up its own indicators yet."""
    spec = _HTF_RESAMPLE.get(timeframe)
    if spec is None or df.empty:
        return pd.Series(None, index=df.index, dtype=object)

    htf_df = df.resample(spec["rule"], **spec["kwargs"]).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    htf_series = compute_series(htf_df, spec.get("label", "4hour"))
    if "error" in htf_series:
        return pd.Series(None, index=df.index, dtype=object)

    # Only trust a bucket once ALL of its own indicators have actually
    # warmed up - compute_series' own len() guard only protects the
    # LATEST bar (all live ever reads), not every earlier bar in this
    # backtest's full htf_df, so without this an early, still-warming
    # bucket's NaN indicator values would compare as False across the
    # board (NaN > x is always False, never NaN, in pandas) and get
    # mislabeled "Bearish" by construction instead of "no opinion yet".
    warm = (
        htf_series["rsi_smooth"].notna() & htf_series["macd_line"].notna()
        & htf_series["signal_line"].notna() & htf_series["cmf"].notna()
    )
    # CMF, not EMA9-vs-Bollinger-mid: must match indicators.
    # _higher_timeframe_direction, or a backtest with require_htf on would
    # replay a DIFFERENT higher-timeframe rule than the live screener uses.
    align_count = (
        (htf_series["rsi_line"] > htf_series["rsi_smooth"]).astype(int)
        + (htf_series["macd_line"] > htf_series["signal_line"]).astype(int)
        + (htf_series["cmf"] > 0).astype(int)
    )
    bucket_direction = pd.Series(np.where(align_count >= 2, "Bullish", "Bearish"), index=htf_df.index, dtype=object)
    bucket_direction = bucket_direction.where(warm, other=None)
    # Shift by ONE full bucket so a bar can only ever see the last FULLY
    # CLOSED bucket's direction, never its own still-forming one.
    bucket_direction_prior = bucket_direction.shift(1)

    fine = pd.DataFrame({"ts": df.index}).sort_values("ts")
    htf_lookup = pd.DataFrame({"ts": bucket_direction_prior.index, "htf_dir": bucket_direction_prior.values}).sort_values("ts")
    merged = pd.merge_asof(fine, htf_lookup, on="ts", direction="backward")
    return pd.Series(merged["htf_dir"].values, index=df.index)


def _opening_window_mask(df: pd.DataFrame, timeframe: str) -> pd.Series:
    """Vectorized replay of indicators._in_opening_window OR'd with
    _in_4hour_warmup, across every bar in df - each bar's OWN timestamp
    stands in for "the current wall-clock time as of that bar" (a
    backtest replay has no separate real "now" the way a live scan
    does), which is exactly the historically-correct read: it reproduces
    what a live scan running at that exact past moment would have seen."""
    return pd.Series(
        [bool(_in_opening_window(ts, timeframe) or _in_4hour_warmup(ts, timeframe)) for ts in df.index],
        index=df.index,
    )



def _record(diag, gate, readable, total):
    """Accumulate how many bars a gate had a REAL reading for.

    Without this, a gate whose underlying reading is unavailable looks
    identical to a gate that was evaluated and changed nothing: both cut 0%
    of trades. Those call for opposite responses - one needs fixing, the
    other needs removing - so the ablation must be able to tell them apart."""
    if diag is None:
        return
    d = diag.setdefault(gate, {"readable": 0, "total": 0})
    d["readable"] += int(readable)
    d["total"] += int(total)


def _record_verdicts(diag, gate, verdict, candidate_mask):
    """Record exact pass/fail/missing counts for a tri-state gate."""
    if diag is None:
        return
    cand = candidate_mask.fillna(False).astype(bool)
    v = verdict.reindex(cand.index)[cand]
    d = diag.setdefault(gate, {"readable": 0, "total": 0})
    d["passed"] = d.get("passed", 0) + int(v.eq(1.0).sum())
    d["failed"] = d.get("failed", 0) + int(v.eq(0.0).sum())
    d["missing"] = d.get("missing", 0) + int(v.isna().sum())

def _signal_series(series: dict, params, required: int, timeframe: str = None,
                    require_htf: bool = False, require_regime_volume: bool = False,
                    exclude_opening_window: bool = False, require_candle_pattern: bool = False, require_macd_hist: bool = False,
                    require_big_candle: bool = False, require_strong_close: bool = False,
                    require_entry_location: bool = False, require_atr_floor: bool = False,
                    require_oi_agreement: bool = False, oi_history=None, oi_intraday=False,
                    diag=None):
    """Combines the chosen parameters bar-by-bar: has_signal is true on
    any bar where at least `required` of them agree on the same
    direction at once. Deliberately NOT the continuous "aligned >=
    min_required" majority state used elsewhere for display (index.html's
    Matching Now list, background.py's positional_qualified) - with only
    the original 3 crossover parameters selected that state is always
    >= 2 by construction (3 things can't split narrower than 2-1), which
    would make has_signal always true and silently produce zero trades.
    Mixing in state-type parameters (rel_volume, cmf_flow, candle_pattern,
    big_candle_pattern, strong_close) is safe here because those are
    genuine, sometimes-false conditions.

    require_htf/require_regime_volume/exclude_opening_window/require_
    volume_flow/require_candle_pattern (all default False) replay the
    same GATES indicators.compute_signal (plus background.py's opt-in
    filters) apply live on top of the parameter vote before calling a
    bar "signal_confirmed" - see _htf_direction_series/_regime_volume_
    hot_series/_opening_window_mask/_volume_flow_agree_series/
    _candle_pattern_agree_series above. Unlike PARAM_DEFS these never
    add to bull_count/bear_count; they can only SUPPRESS a bar your
    chosen parameters already agreed on, exactly like live's
    `signal_confirmed = aligned >= MIN_REQUIRED and htf_agrees and not
    in_opening_window` (further revoked by background.py's
    REQUIRE_INDEX_AGREEMENT/REQUIRE_VOLUME_FLOW_AGREEMENT/
    REQUIRE_CANDLE_PATTERN_AGREEMENT when those are on).
    require_htf/exclude_opening_window need `timeframe` (raises if
    omitted while set)."""
    index = series["rsi_line"].index
    rel_volume_hot = _regime_volume_hot_series(series) if require_regime_volume else None

    bull_count = pd.Series(0, index=index)
    bear_count = pd.Series(0, index=index)
    for param_id in params:
        bull, bear = _param_bull_bear(series, param_id, rel_volume_hot=rel_volume_hot)
        bull_count = bull_count + bull.reindex(index).fillna(False).astype(int)
        bear_count = bear_count + bear.reindex(index).fillna(False).astype(int)

    # Directional dominance, not just threshold-reached: rel_volume's
    # bull/bear flags are literally the same series (it's a confirmation
    # condition with no inherent direction of its own), so bull_count and
    # bear_count can tie at >= required simultaneously - e.g. selecting
    # rel_volume by itself. Without this guard that tie would always get
    # silently labeled "Bullish", inventing a direction that isn't really
    # there. Requiring the OTHER side to stay below `required` means a
    # pure confirmation-only selection correctly produces zero trades
    # instead of mislabeled ones - combine it with at least one directional
    # parameter (a *_cross, cmf_flow, candle_pattern, big_candle_pattern
    # or strong_close) to get real entries.
    is_bull = (bull_count >= required) & (bear_count < required)
    is_bear = (bear_count >= required) & (bull_count < required)
    has_signal = is_bull | is_bear
    direction = pd.Series(np.where(is_bull, "Bullish", "Bearish"), index=index)

    if require_htf:
        if not timeframe:
            raise ValueError("require_htf needs a timeframe")
        htf_dir = _htf_direction_series(series["df"], timeframe).reindex(index)
        htf_agrees = htf_dir.isna() | (htf_dir == direction)
        has_signal = has_signal & htf_agrees

    if exclude_opening_window:
        if not timeframe:
            raise ValueError("exclude_opening_window needs a timeframe")
        in_window = _opening_window_mask(series["df"], timeframe).reindex(index).fillna(False)
        has_signal = has_signal & ~in_window

    if require_oi_agreement:
        # The gate that actually decides the live shortlist. Until now this
        # file could not replay it at all, so the ablation measured the old
        # four-vote screen and said nothing about the engine in production.
        z = _oi_zscore_series(oi_history, index, intraday=oi_intraday)
        oi_ok = _oi_agrees_series(z, series["df"]["close"].reindex(index), direction)
        # TWO different questions, and reporting only the second made a rare
        # gate indistinguishable from a broken one:
        #   data     - did we have an OI baseline for this bar at all?
        #   verdict  - did that baseline produce a decisive agree/disagree?
        # A gate with data on 90% of bars and a verdict on 1% is WORKING and
        # simply strict. A gate with data on 1% is still broken. Both showed
        # "1% read" before.
        _record(diag, "require_oi_agreement",
                oi_ok[has_signal].notna().sum(), has_signal.sum())
        _record(diag, "require_oi_agreement__data",
                z[has_signal].notna().sum(), has_signal.sum())
        _record_verdicts(diag, "require_oi_agreement", oi_ok, has_signal)
        # An OI-required experiment must be evaluated only where OI produced a
        # decisive agreement. Missing/neutral OI is NOT a pass: otherwise a gate
        # with 25% coverage can appear to cut 0% of trades and falsely look useless.
        has_signal = has_signal & oi_ok.eq(1.0)

    if require_candle_pattern:
        candle_agrees = _candle_pattern_agree_series(series, direction).reindex(index).fillna(True)
        has_signal = has_signal & candle_agrees

    if require_macd_hist:
        hist_agrees = _macd_hist_agree_series(series, direction).reindex(index).fillna(True)
        has_signal = has_signal & hist_agrees

    if require_big_candle:
        bc_agrees = _big_candle_agree_series(series, direction).reindex(index).fillna(True)
        has_signal = has_signal & bc_agrees

    if require_strong_close:
        sc_agrees = _strong_close_agree_series(series, direction).reindex(index).fillna(True)
        has_signal = has_signal & sc_agrees

    if require_entry_location:
        if not timeframe:
            raise ValueError("require_entry_location needs a timeframe")
        _el_raw = _entry_location_agree_series(series, direction, timeframe).reindex(index)
        _record(diag, "require_entry_location", _el_raw[has_signal].notna().sum(), has_signal.sum())
        el_agrees = _el_raw.fillna(True)
        has_signal = has_signal & el_agrees

    if require_atr_floor:
        floor_agrees = _atr_floor_agree_series(series).reindex(index).fillna(True)
        has_signal = has_signal & floor_agrees

    return has_signal, direction


def _compute_trade(df: pd.DataFrame, entry_pos: int, direction: str, symbol: str, horizons,
                    cost_pct: float = 0.0, slippage_pct: float = 0.0,
                    stop_price: float = None, target_price: float = None):
    """Entry is executed at the NEXT bar's open after the signal bar
    (never the signal bar's own close, to avoid lookahead bias).
    Returns are computed at each requested horizon (in bars), plus the
    single worst adverse move (drawdown) seen at any point during the
    longest hold - None if there isn't enough remaining data for even
    the shortest horizon.

    cost_pct/slippage_pct implement NEXT_HORIZON_RESEARCH.md Finding 2 -
    the single highest-priority item in that report, and the reason every
    win-rate this module used to print was optimistic. A raw price-move
    backtest silently assumes free, perfectly-filled trades; real F&O
    round trips are not free, and the drag is subtracted from EVERY
    horizon's return here so the reported numbers are net rather than
    gross. Both default to 0.0 so an explicit caller (and every existing
    saved result) keeps the old gross behaviour unless it opts in -
    run_backtest below defaults them to the realistic values above.

    Slippage is counted TWICE (entry and exit) because you cross the
    spread on both sides. mae_pct is deliberately left GROSS - it
    describes raw adverse price action during the hold, which is a
    property of the market rather than of your cost structure, and
    muddying it with fees would make it mean two things at once."""
    if entry_pos + 1 >= len(df):
        return None
    entry_price = float(df["open"].iloc[entry_pos + 1])
    if not entry_price:
        return None
    entry_time = df.index[entry_pos + 1]
    signal_time = df.index[entry_pos]
    sign = 1 if direction == "Bullish" else -1

    # Total round-trip drag applied to every horizon's return below.
    # max(0, ...) on each leg: a negative cost or slippage is nonsense, and
    # left unclamped it would silently ADD return - a backtest that pays you
    # to trade. Clamp rather than raise, so one bad config value can never
    # kill a whole run.
    total_drag = costs.round_trip_drag_pct(cost_pct, slippage_pct)

    # ---- stop / target exits -------------------------------------------
    # The app computes an ATR stop and target for every row, shows them on the
    # dashboard, and sizes the suggested position off the stop - but until this
    # existed neither the backtest nor the journal ever EXITED on them. Every
    # win rate measured "enter, then hold N bars regardless", which is not the
    # trade the app tells you to take: in reality the stop takes you out first.
    # Measuring the wrong strategy is a deeper error than measuring the right
    # one imprecisely, so this runs ahead of the horizon logic below.
    # stop_price/target_price are passed IN rather than derived here, and are
    # computed off the SIGNAL BAR'S CLOSE (see _replay_symbol) - the same basis
    # indicators.compute_signal uses for the levels shown on the dashboard and
    # stored by the journal. Deriving them from entry_price instead (the next
    # bar's open) silently produced different levels in the backtest than the
    # ones you were actually shown, so the two systems disagreed about whether
    # the same trade stopped out. One basis, one answer.
    exit_bar = exit_price_hit = None
    exit_reason = "horizon"
    if stop_price is not None:
        for k in range(1, max(horizons) + 1):
            pos = entry_pos + 1 + k
            if pos >= len(df):
                break
            bar = df.iloc[pos]
            if direction == "Bullish":
                hit_stop = bar["low"] <= stop_price
                hit_target = bar["high"] >= target_price
            else:
                hit_stop = bar["high"] >= stop_price
                hit_target = bar["low"] <= target_price
            # Both inside one bar: assume the STOP filled first. Intrabar
            # order is unknowable from OHLC, and assuming the favourable one
            # would flatter every result - the pessimistic read is the honest
            # default here.
            if hit_stop:
                exit_bar, exit_price_hit, exit_reason = k, stop_price, "stop"
                break
            if hit_target:
                exit_bar, exit_price_hit, exit_reason = k, target_price, "target"
                break

    # Drawdown spans only the bars actually HELD. If the stop took you out on
    # bar 2, whatever the price did on bars 3-20 is not your drawdown - you
    # were flat. journal._resolve_one applies the identical rule, so the two
    # systems report the same MAE for the same trade.
    max_h = exit_bar if exit_bar is not None else max(horizons)
    hold_end = min(entry_pos + 1 + max_h, len(df) - 1)
    hold_slice = df.iloc[entry_pos + 1: hold_end + 1]
    if hold_slice.empty:
        return None
    # Worst adverse move seen at any point during the hold, as a % of
    # entry price - always <= 0 (0 means the trade was never underwater
    # at all, negative means how far underwater it got at worst).
    if direction == "Bullish":
        mae_pct = min(0.0, float((hold_slice["low"].min() - entry_price) / entry_price * 100))
    else:
        mae_pct = min(0.0, float((entry_price - hold_slice["high"].max()) / entry_price * 100))

    returns = {}
    for h in horizons:
        exit_pos = entry_pos + 1 + h
        if exit_pos >= len(df):
            continue
        if exit_bar is not None and exit_bar <= h:
            # Already stopped out (or took target) before this horizon - you
            # were flat, so every longer horizon reports that same realised
            # exit rather than pretending you rode the position on.
            gross = sign * (exit_price_hit - entry_price) / entry_price * 100
        else:
            gross = sign * (float(df["close"].iloc[exit_pos]) - entry_price) / entry_price * 100
        returns[h] = round(gross - total_drag, 3)
    if not returns:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "signal_time": signal_time.isoformat(),
        "entry_time": entry_time.isoformat(),
        "entry_price": round(entry_price, 2),
        "returns_pct": returns,
        "mae_pct": round(mae_pct, 3),
        "cost_drag_pct": round(total_drag, 4),
        "exit_reason": exit_reason,
        "exit_bar": exit_bar,
        "stop_price": round(stop_price, 2) if stop_price is not None else None,
        "target_price": round(target_price, 2) if target_price is not None else None,
    }


# --------------------------------------------------------------------------
# Replaying the early-signal engine over history.
#
# Until now this file could not see app/early_signal.py at all, which meant
# the gate ablation measured the OLD four-vote architecture and said nothing
# about the OI engine that actually decides what appears on the dashboard.
# Running it and calling the result validation would have been measuring the
# wrong strategy carefully.
#
# Two pieces are needed, and they have different cost profiles:
#
#   * The OI z-score is needed on EVERY bar (the gate has to be applied
#     bar-by-bar like every other gate), so it is computed as a rolling
#     series rather than by calling early_signal.oi_zscore 250 times.
#   * The full 5-component score is only needed at SIGNAL bars, which are
#     sparse, so that stays a plain loop over the handful of entries.
# --------------------------------------------------------------------------

def _fetch_near_futures_history_for_research(kite, symbol, timeframe, days=None):
    """Current near-expiry futures price history for V6 basis research.

    Coverage is intentionally partial around expiry rolls: Kite exposes the
    currently live contract token, not a reconstructed historical near-month
    chain. Missing periods stay missing and are reported as such by V6.
    """
    try:
        contracts = scanner_mod._load_fut_contracts_map(kite).get(symbol, [])
    except Exception:  # noqa: BLE001 - basis is optional research context
        contracts = []
    if not contracts:
        return None
    token = contracts[0].get("instrument_token")
    if not token:
        return None
    fetch_days = (int(days) + WARMUP_DAYS) if days is not None else WARMUP_DAYS + 30
    try:
        return _fetch_history(token, timeframe, fetch_days, kite)
    except Exception as exc:  # noqa: BLE001
        log.debug("Near-futures basis history unavailable for %s: %s", symbol, exc)
        return None


def _fetch_oi_history_for_backtest(kite, symbol, timeframe, days=None):
    """Daily/intraday OI series for one symbol, for replaying the OI gate.

    Deliberately goes through scanner.fetch_oi_history so the backtest reads
    EXACTLY the series the live engine reads - same interval, same
    continuous flag, same resample. A backtest that built its own OI series
    a slightly different way would be measuring a different strategy and
    would never announce it."""
    # NOT throttle=0. That was a real bug: it removed the rate limiting on a
    # path that then fetched OI for every symbol in the universe, so Kite
    # started refusing calls, the baseline came back empty, and - because a
    # missing baseline used to be silently treated as a pass, so the OI gate
    # could report "no effect" even when it was mostly unmeasured.
    # A gate that never fired reported as a gate that did nothing.
    days_override = (int(days) + WARMUP_DAYS) if days is not None else None
    hist = scanner_mod.fetch_oi_history(
        kite, [symbol], timeframe=timeframe, days_override=days_override)
    return hist.get(symbol)


def _oi_zscore_series(oi_series, price_index, intraday=False):
    """Per-bar OI z-score, aligned to the price bars.

    Returns a float Series on price_index, NaN wherever there is no usable
    baseline. NaN means "unknown", never "normal" - the caller must not read
    a missing baseline as an absence of unusual activity.

    The rolling mean and standard deviation are SHIFTED by one so the bar
    being scored never contributes to the distribution it is measured
    against. Without the shift a large move inflates its own sigma and
    shrinks its own z-score, which biases the measurement hardest on exactly
    the events the engine exists to catch - and in a backtest that is
    lookahead, because a live scan cannot know the current bar when it
    builds the baseline."""
    if oi_series is None or len(oi_series) < early_signal.MIN_BASELINE_OBS + 2:
        return pd.Series(np.nan, index=price_index)

    oi = pd.Series(oi_series).dropna()
    oi = oi[oi > 0]
    if len(oi) < early_signal.MIN_BASELINE_OBS + 2:
        return pd.Series(np.nan, index=price_index)

    changes = oi.pct_change() * 100.0
    if intraday and isinstance(oi.index, pd.DatetimeIndex):
        # Same overnight exclusion the live engine applies - see
        # early_signal._pct_changes for why leaving those in makes the
        # sigma so wide that real intraday builds stop registering.
        same_session = pd.Series(oi.index.normalize()).diff().eq(pd.Timedelta(0))
        changes = changes.where(pd.Series(same_session.values, index=oi.index))

    window = early_signal.INTRADAY_BASELINE_OBS if intraday else early_signal.BASELINE_DAYS
    valid = changes.dropna()
    mu = valid.rolling(window, min_periods=early_signal.MIN_BASELINE_OBS).mean().shift(1)
    sd = valid.rolling(window, min_periods=early_signal.MIN_BASELINE_OBS).std(ddof=1).shift(1)
    z = (valid - mu) / sd.where(sd > 1e-6)
    z = z.replace([np.inf, -np.inf], np.nan)

    # Align onto the price bars. reindex + ffill because a price bar can
    # exist where an OI bar does not (a futures contract that did not trade
    # that interval); the most recent known OI reading is the honest value
    # to carry forward, and the limit stops a long data gap being presented
    # as a current reading.
    return z.reindex(price_index, method="ffill", limit=2)


def _oi_agrees_series(z_series, close, direction, price_threshold=0.3):
    """Per-bar "does OI positioning back this bar's direction".

    Mirrors early_signal.classify_oi_structure + FRESH_POSITIONING exactly:
    an unusual OI move forming a fresh BUILDUP quadrant. Covering and
    unwinding are position-closing flow and do not count as agreement, same
    as live.

    Returns True / False / NaN. NaN means the OI gate is not decisively
    measurable on that bar. When OI agreement is explicitly required, NaN is
    excluded rather than silently counted as a pass; diagnostics separately
    report how often that happened."""
    price_chg = close.pct_change() * 100.0
    unusual = z_series.abs() >= early_signal.OI_Z_THRESHOLD
    oi_up = z_series > 0
    price_up = price_chg > price_threshold
    price_down = price_chg < -price_threshold

    long_buildup = unusual & price_up & oi_up
    short_buildup = unusual & price_down & oi_up
    wants_bull = direction.reindex(z_series.index) == "Bullish"

    agrees = (wants_bull & long_buildup) | (~wants_bull & short_buildup)
    disagrees = (wants_bull & short_buildup) | (~wants_bull & long_buildup)

    out = pd.Series(np.nan, index=z_series.index, dtype="float64")
    out[agrees] = 1.0
    out[disagrees] = 0.0
    return out

def _early_score_at(series, df, pos, direction, z_series=None):
    """The live engine's score for one historical bar.

    Feeds early_signal.early_signal_score the components this replay
    actually has. Relative strength is absent (the backtest replays one
    symbol at a time, with no index series alongside), so that axis is
    UNMEASURED rather than assumed neutral - which lowers coverage exactly
    as it would live. Scores from here are therefore slightly conservative
    versus the dashboard's, and comparable to each other, which is what
    matters for ranking bands against outcomes."""
    try:
        i = int(pos)
        close = df["close"]
        vol = df["volume"] if "volume" in df.columns else None
        rvol = None
        if vol is not None and i >= 20:
            avg = float(vol.iloc[max(0, i - 20):i].mean())
            if avg > 0:
                rvol = round(float(vol.iloc[i]) / avg, 2)
        rvol_accel, vol_rising = (early_signal.rvol_acceleration(vol.iloc[: i + 1])
                                  if vol is not None else (None, None))
        hi, lo = float(df["high"].iloc[i]), float(df["low"].iloc[i])
        close_pos = None
        if hi > lo:
            close_pos = round((float(close.iloc[i]) - lo) / (hi - lo) * 100.0, 1)

        oi_z = None
        if z_series is not None and i < len(z_series):
            v = z_series.iloc[i]
            oi_z = None if pd.isna(v) else round(float(v), 2)
        structure = None
        if oi_z is not None and i >= 1:
            prev = float(close.iloc[i - 1])
            if prev > 0:
                structure = early_signal.classify_oi_structure(
                    (float(close.iloc[i]) / prev - 1.0) * 100.0,
                    1.0 if oi_z > 0 else -1.0, oi_z=oi_z)

        rsi_line, rsi_smooth = series.get("rsi_line"), series.get("rsi_smooth")
        macd_line, signal_line = series.get("macd_line"), series.get("signal_line")
        bull = direction == "Bullish"
        rsi_above = macd_agrees = rsi_cross = None
        if rsi_line is not None and rsi_smooth is not None and i < len(rsi_line):
            above = bool(rsi_line.iloc[i] > rsi_smooth.iloc[i])
            rsi_above = above if bull else (not above)
            if i >= 1:
                prev_above = bool(rsi_line.iloc[i - 1] > rsi_smooth.iloc[i - 1])
                rsi_cross = (above and not prev_above) if bull else ((not above) and prev_above)
        if macd_line is not None and signal_line is not None and i < len(macd_line):
            mabove = bool(macd_line.iloc[i] > signal_line.iloc[i])
            macd_agrees = mabove if bull else (not mabove)

        return early_signal.early_signal_score(
            direction, oi_z=oi_z, oi_structure=structure,
            rvol=rvol, rvol_accel=rvol_accel, vol_rising=vol_rising,
            rsi_cross=rsi_cross, rsi_above=rsi_above, macd_agrees=macd_agrees,
            close_pos=close_pos,
        )
    except Exception:  # noqa: BLE001 - a score we cannot compute is None, never a guess
        return None


def _replay_symbol(df: pd.DataFrame, symbol: str, timeframe: str, window_start, horizons, params, required,
                    cost_pct=0.0, slippage_pct=0.0,  # net-of-cost returns - see _compute_trade
                    require_htf=False, require_regime_volume=False, exclude_opening_window=False,
                    require_candle_pattern=False,
                    require_macd_hist=False, require_big_candle=False,
                    require_strong_close=False, require_entry_location=False,
                    require_atr_floor=False,
                    require_oi_agreement=False, oi_history=None, oi_intraday=False,
                    diag=None):
    """Entry = the bar where your chosen parameter combination first
    reaches `required` agreement (see _signal_series), de-duped via a
    rising edge so a signal that stays true for a stretch of bars only
    counts once."""
    series = compute_series(df, timeframe)
    if "error" in series:
        return []

    # One z-score pass for the whole symbol, reused at every signal bar.
    _oi_z_cached = (_oi_zscore_series(oi_history, df.index, intraday=oi_intraday)
                    if oi_history is not None else None)

    has_signal, direction = _signal_series(
        series, params, required, timeframe=timeframe,
        require_htf=require_htf, require_regime_volume=require_regime_volume,
        require_oi_agreement=require_oi_agreement, oi_history=oi_history,
        oi_intraday=oi_intraday, diag=diag,
        exclude_opening_window=exclude_opening_window,
        require_candle_pattern=require_candle_pattern,
            require_macd_hist=require_macd_hist, require_big_candle=require_big_candle,
            require_strong_close=require_strong_close,
            require_entry_location=require_entry_location,
            require_atr_floor=require_atr_floor,
    )
    # shift(..., fill_value=False) instead of shift(1).fillna(False): a
    # plain shift(1) on a bool Series introduces a leading NaN, which
    # upcasts the whole series to object dtype - then fillna(False) has to
    # downcast it back to bool, which newer pandas now warns about
    # (FutureWarning: Downcasting object dtype arrays on .fillna...).
    # Passing fill_value explicitly keeps the Series bool-dtype the entire
    # time, so there's nothing to downcast and no warning is ever raised.
    entries = has_signal & ~has_signal.shift(1, fill_value=False)

    # Always recorded on every trade (regardless of whether "rel_volume"
    # is one of your chosen params for THIS run) so compute_param_weights
    # below can measure volume's own historical contribution - was the
    # signal bar's volume already hot at the moment of entry, or not -
    # without needing a separate dedicated backtest run just for that.
    rel_vol = series["df"]["volume"] / series["vol_avg"].replace(0, np.nan)
    vol_hot = rel_vol.notna() & (rel_vol > settings.REL_VOLUME_THRESHOLD)

    trades = []
    for pos in np.flatnonzero(entries.to_numpy()):
        ts = df.index[pos]
        if ts.to_pydatetime().replace(tzinfo=None) < window_start:
            continue  # inside the warm-up buffer, not the requested window
        # Reproduce exactly the stop/target indicators.compute_signal would
        # have displayed on this bar: signal-bar close +/- multiplier x ATR,
        # rounded the same way, so a replayed trade exits on the same levels a
        # live one would.
        stop_price = target_price = None
        atr_series = series.get("atr")
        if atr_series is not None:
            atr_v = atr_series.iloc[pos]
            if pd.notna(atr_v) and atr_v > 0:
                ref_close = float(df["close"].iloc[pos])
                d = direction.iloc[pos]
                atr_v = round(float(atr_v), 2)
                if d == "Bullish":
                    stop_price = round(ref_close - settings.ATR_STOP_MULTIPLIER * atr_v, 2)
                    target_price = round(ref_close + settings.ATR_TARGET_MULTIPLIER * atr_v, 2)
                else:
                    stop_price = round(ref_close + settings.ATR_STOP_MULTIPLIER * atr_v, 2)
                    target_price = round(ref_close - settings.ATR_TARGET_MULTIPLIER * atr_v, 2)
        _dir = direction.iloc[pos]
        _score = _early_score_at(series, df, pos, _dir, _oi_z_cached)
        trade = _compute_trade(df, pos, direction.iloc[pos], symbol, horizons,
                                cost_pct=cost_pct, slippage_pct=slippage_pct,
                                stop_price=stop_price, target_price=target_price)
        if trade:
            trade["vol_confirmed_at_entry"] = bool(vol_hot.iloc[pos])
            # Carry the engine's own score onto the trade so outcomes can be
            # reported BY BAND. Without this, summarize_by_band finds no
            # scored trades and silently returns empty buckets - the table
            # renders as "no data" rather than as an error, which is the
            # worst kind of failure: the feature looks like it ran.
            trade["early_score"] = _score["score"] if _score else None
            trade["early_coverage"] = _score["coverage"] if _score else None
            trades.append(trade)
    return trades


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------

def _summarize_group(trades, horizons):
    """Win-rate/avg-return/best/worst per horizon, plus overall trade
    count and drawdown, for one group of trades (all, or just one
    direction)."""
    out = {}
    for h in horizons:
        rets = [t["returns_pct"][h] for t in trades if h in t["returns_pct"]]
        if not rets:
            out[str(h)] = {"trade_count": 0}
            continue
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        avg_winner = (sum(wins) / len(wins)) if wins else None
        avg_loser = (sum(losses) / len(losses)) if losses else None
        payoff = (avg_winner / abs(avg_loser)) if avg_winner is not None and avg_loser not in (None, 0) else None
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
        out[str(h)] = {
            "trade_count": len(rets),
            "win_rate_pct": round(len(wins) / len(rets) * 100, 1),
            "avg_return_pct": round(sum(rets) / len(rets), 3),
            "median_return_pct": round(float(np.median(rets)), 3),
            "avg_winner_pct": round(avg_winner, 3) if avg_winner is not None else None,
            "avg_loser_pct": round(avg_loser, 3) if avg_loser is not None else None,
            "payoff_ratio": round(payoff, 2) if payoff is not None else None,
            "profit_factor": round(profit_factor, 2) if profit_factor is not None and np.isfinite(profit_factor) else profit_factor,
            "best_return_pct": round(max(rets), 3),
            "worst_return_pct": round(min(rets), 3),
        }
    maes = [t["mae_pct"] for t in trades]
    reasons = [t.get("exit_reason") for t in trades]
    out["exits"] = {
        "stop": reasons.count("stop"),
        "target": reasons.count("target"),
        "horizon": reasons.count("horizon"),
    }
    out["overall"] = {
        "total_trades": len(trades),
        "avg_drawdown_pct": round(sum(maes) / len(maes), 3) if maes else None,
        "worst_drawdown_pct": round(min(maes), 3) if maes else None,
    }
    return out


def _summarize_by_split(trades, horizons, split_at):
    """Splits `trades` at an ISO timestamp and summarizes each side
    separately - NEXT_HORIZON_RESEARCH.md Finding 2's second half, the
    overfitting discipline.

    The trap it addresses: the natural workflow (run a backtest, tweak a
    parameter, run it again on the same window, keep what scored best) is
    mechanically a search over parameter space, and a search over ENOUGH
    configurations will produce a good-looking winner from pure noise even
    when no real edge exists - the "Deflated Sharpe Ratio" problem. The
    fix needs no new infrastructure, only that the number you finally
    believe was measured on data you never tuned against.

    So: tune freely against `train`, then look at `holdout` exactly ONCE
    and accept whatever it says. A holdout you re-check after every tweak
    has quietly become training data and tells you nothing.

    Entry times are compared as ISO strings, which sorts identically to
    real datetime ordering for a fixed format. Strictly-before goes to
    train; at-or-after goes to holdout."""
    train = [t for t in trades if t.get("entry_time", "") < split_at]
    holdout = [t for t in trades if t.get("entry_time", "") >= split_at]
    return {
        "split_at": split_at,
        "train": _summarize(train, horizons),
        "holdout": _summarize(holdout, horizons),
    }


def _summarize(trades, horizons):
    """Splits results into All / Bullish-only / Bearish-only - a
    strategy's real edge (or lack of one) often differs by direction,
    and pooling them together can hide that a rule works well one way
    and poorly the other. Also splits by whether Relative Volume was
    already hot (see _replay_symbol's vol_confirmed_at_entry) at the
    exact moment of entry, regardless of whether "rel_volume" was one
    of the parameters you actually selected for this run - this is
    what compute_param_weights below reads to measure volume's own
    historical contribution (see its "lift" note)."""
    bullish = [t for t in trades if t["direction"] == "Bullish"]
    bearish = [t for t in trades if t["direction"] == "Bearish"]
    vol_confirmed = [t for t in trades if t.get("vol_confirmed_at_entry")]
    vol_not_confirmed = [t for t in trades if not t.get("vol_confirmed_at_entry")]
    return {
        "all": _summarize_group(trades, horizons),
        "bullish": _summarize_group(bullish, horizons),
        "bearish": _summarize_group(bearish, horizons),
        "vol_confirmed": _summarize_group(vol_confirmed, horizons),
        "vol_not_confirmed": _summarize_group(vol_not_confirmed, horizons),
    }


# --------------------------------------------------------------------------
# The overnight (BTST/STBT) test.
#
# Nothing else in this file can measure a BTST trade, and that is not a
# tuning gap - it is a modelling one. Two mismatches, either of which alone
# makes the number meaningless:
#
#   * HORIZON. DEFAULT_HORIZONS is (5, 10, 20) BARS. On daily candles that
#     is five to twenty trading days. A BTST idea is ONE bar. Every win rate
#     this app has ever printed for a BTST pick was really a two-to-four
#     week swing trade wearing its name.
#
#   * ENTRY. _compute_trade enters at the NEXT bar's open, which is correct
#     for avoiding lookahead on an ordinary signal. But a BTST signal is
#     generated AT the close and acted on AT that close - entering the next
#     open means you have already missed the overnight move you were trying
#     to capture, and are measuring tomorrow's day-trade instead.
#
# So this models it properly: enter at the SIGNAL BAR's close, exit at the
# next bar's open and again at the next bar's close. That is the actual
# trade, and it is the only way to find out whether the premise holds.
#
# There is a real reason to doubt that premise. The gate selects for a stock
# closing at the extreme top of its range on an up day - the single most
# extended point of the session. Short-horizon REVERSAL is among the more
# robust findings in equity microstructure: momentum tends to work over
# months, while over one to five days strong recent returns tend to give
# back. If that dominates here, "buy what closed strongest, hold overnight"
# is not a weak edge, it is the wrong sign - and no amount of tightening the
# other checks would fix it. This function is how we find out instead of
# arguing about it.
# --------------------------------------------------------------------------

def overnight_outcomes(df, direction_series, signal_mask, cost_pct=0.0, slippage_pct=0.0):
    """Enter at the signal bar's CLOSE; exit next open and next close.

    Returns per-exit stats plus the raw per-trade returns, net of costs."""
    o, c = df["open"], df["close"]
    nxt_o, nxt_c = o.shift(-1), c.shift(-1)
    drag = costs.round_trip_drag_pct(cost_pct, slippage_pct)

    long_mask = direction_series.reindex(df.index) == "Bullish"
    sign = pd.Series(np.where(long_mask, 1.0, -1.0), index=df.index)

    to_open = ((nxt_o / c - 1.0) * 100.0) * sign - drag
    to_close = ((nxt_c / c - 1.0) * 100.0) * sign - drag

    live = signal_mask.reindex(df.index).fillna(False) & nxt_c.notna()
    out = {}
    for label, ser in (("next_open", to_open), ("next_close", to_close)):
        vals = ser[live].dropna()
        if vals.empty:
            out[label] = {"trade_count": 0}
            continue
        out[label] = {
            "trade_count": int(len(vals)),
            "win_rate_pct": round(float((vals > 0).mean() * 100.0), 1),
            "avg_return_pct": round(float(vals.mean()), 3),
            "median_return_pct": round(float(vals.median()), 3),
            "best_pct": round(float(vals.max()), 2),
            "worst_pct": round(float(vals.min()), 2),
        }
    return out


def compare_overnight_outcomes(df, direction_series, signal_mask, cost_pct=0.0, slippage_pct=0.0):
    """Compare the same overnight setup in its original direction and reversed.

    This prevents a losing continuation premise from being endlessly tuned with
    extra filters when the data is actually signalling short-horizon mean reversion.
    Both legs use identical signals, prices and costs; only the trade sign changes.
    """
    continuation = overnight_outcomes(
        df, direction_series, signal_mask, cost_pct=cost_pct, slippage_pct=slippage_pct)
    reversed_direction = direction_series.map({"Bullish": "Bearish", "Bearish": "Bullish"})
    reversal = overnight_outcomes(
        df, reversed_direction, signal_mask, cost_pct=cost_pct, slippage_pct=slippage_pct)
    return {"continuation": continuation, "reversal": reversal}


_on_state = {"status": "idle", "result": None, "error": None, "progress": None}
_on_lock = threading.Lock()


def start_overnight_backtest(kite, symbols, **kw):
    with _on_lock:
        if _on_state["status"] == "running":
            return {"started": False, "reason": "an overnight test is already running"}
        _on_state.update(status="running", result=None, error=None, progress=None)

    def _job():
        try:
            def _cb(i, total, sym):
                with _on_lock:
                    _on_state["progress"] = {"done": i, "total": total, "symbol": sym}
            res = run_overnight_backtest(kite, symbols, progress_cb=_cb, **kw)
            with _on_lock:
                _on_state.update(status="done", result=res)
        except Exception as exc:  # noqa: BLE001
            log.exception("Overnight backtest failed")
            with _on_lock:
                _on_state.update(status="error", error=str(exc))

    threading.Thread(target=_job, daemon=True).start()
    return {"started": True}


def get_overnight_state():
    with _on_lock:
        return dict(_on_state)


def run_overnight_backtest(kite, symbols, timeframe="day", days=365,
                            strong_close_pct=None, require_up_day=True,
                            cost_pct=DEFAULT_COST_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT,
                            progress_cb=None):
    """Does the BTST/STBT premise hold at all?

    Replays the panel's two HARD gates - closed decisively inside its own
    range, and the day itself went that way - then measures the actual
    overnight trade. Deliberately does NOT apply the soft checks: the
    question here is whether the core premise has an edge before any
    refinement, because refining a setup whose sign is wrong only produces
    a smaller loss more confidently."""
    threshold = float(strong_close_pct if strong_close_pct is not None
                      else settings.STRONG_CLOSE_THRESHOLD_PCT)
    instruments = _load_instrument_map(kite)
    per_symbol = {}
    agg = {
        "continuation": {"next_open": [], "next_close": []},
        "reversal": {"next_open": [], "next_close": []},
    }
    notes = {}

    for idx, symbol in enumerate(symbols):
        if progress_cb:
            progress_cb(idx, len(symbols), symbol)
        token = instruments.get(symbol)
        if not token:
            notes[symbol] = "symbol not found on NSE"
            continue
        try:
            df = _fetch_history(token, timeframe, days, kite)
        except Exception as exc:  # noqa: BLE001
            notes[symbol] = f"history fetch failed: {exc}"
            time.sleep(_RATE_LIMIT_PAUSE)
            continue
        time.sleep(_RATE_LIMIT_PAUSE)
        if df is None or df.empty or len(df) < 30:
            notes[symbol] = "not enough candles"
            continue

        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        rng = (h - l).replace(0, np.nan)
        close_pos = (c - l) / rng * 100.0

        up_day = (c > o) & (c > c.shift(1))
        down_day = (c < o) & (c < c.shift(1))
        bull = close_pos >= threshold
        bear = close_pos <= (100.0 - threshold)
        if require_up_day:
            bull, bear = bull & up_day, bear & down_day

        sig = bull | bear
        direction = pd.Series(np.where(bull, "Bullish", "Bearish"), index=df.index)
        res = compare_overnight_outcomes(df, direction, sig, cost_pct, slippage_pct)
        per_symbol[symbol] = res
        for strategy, exits in agg.items():
            for k in exits:
                st = res.get(strategy, {}).get(k, {})
                if st.get("trade_count"):
                    exits[k].append((st["trade_count"], st["win_rate_pct"], st["avg_return_pct"]))

    summary = {}
    for strategy, exits in agg.items():
        summary[strategy] = {}
        for k, rows in exits.items():
            n = sum(r[0] for r in rows)
            if not n:
                summary[strategy][k] = {"trade_count": 0}
                continue
            summary[strategy][k] = {
                "trade_count": n,
                # Weighted by trade count so a symbol with 3 signals cannot
                # swing the headline as hard as one with 60.
                "win_rate_pct": round(sum(r[0] * r[1] for r in rows) / n, 1),
                "avg_return_pct": round(sum(r[0] * r[2] for r in rows) / n, 3),
                "symbols": len(rows),
            }
    return {
        "summary": summary,
        "per_symbol": per_symbol,
        "symbols_skipped": notes,
        "strong_close_pct": threshold,
        "require_up_day": bool(require_up_day),
        "cost_pct": float(cost_pct),
        "slippage_pct": float(slippage_pct),
        "timeframe": timeframe,
        "days": int(days),
        "computed_at": now_ist().isoformat(timespec="seconds"),
    }


def summarize_vs_target(trades, df_lookup=None):
    """Does the screen hit the target written down in config.py?

    Every other number this file produces answers a question nobody asked:
    "what is the average return after exactly N bars." That is not how the
    trade is taken. config.SUCCESS_* states the real one - reach
    +SUCCESS_TARGET_ATR before -SUCCESS_STOP_ATR, within SUCCESS_HORIZON_BARS
    - and this reports against it, verdict included, so the answer is a pass
    or a fail rather than a number to interpret.

    Uses the stop/target exits _compute_trade already walks bar by bar, so a
    trade that hit its stop first is a loss even if it later recovered."""
    resolved = [t for t in trades if t.get("exit_reason") in ("stop", "target")]
    horizon_only = [t for t in trades if t.get("exit_reason") == "horizon"]
    hits = sum(1 for t in resolved if t.get("exit_reason") == "target")
    n = len(resolved)

    target = config.SUCCESS_MIN_WIN_RATE
    breakeven = config.SUCCESS_STOP_ATR / (config.SUCCESS_TARGET_ATR + config.SUCCESS_STOP_ATR) * 100.0
    rate = round(100.0 * hits / n, 1) if n else None
    enough = n >= config.SUCCESS_MIN_SAMPLE

    if not n:
        verdict = "no trades resolved to a stop or target - nothing to judge"
    elif not enough:
        verdict = (f"only {n} resolved trades, below the {config.SUCCESS_MIN_SAMPLE} "
                   f"this target requires - the interval is too wide to act on")
    elif rate >= target:
        verdict = f"MEETS the target ({rate}% vs {target}% needed)"
    elif rate >= breakeven:
        verdict = (f"above breakeven ({breakeven:.1f}%) but BELOW the {target}% target "
                   f"- an edge too thin to pay for mistakes")
    else:
        verdict = (f"FAILS - {rate}% is below the {breakeven:.1f}% needed just to break "
                   f"even at {config.SUCCESS_TARGET_ATR}:{config.SUCCESS_STOP_ATR}")

    return {
        "target_atr": config.SUCCESS_TARGET_ATR,
        "stop_atr": config.SUCCESS_STOP_ATR,
        "horizon_bars": config.SUCCESS_HORIZON_BARS,
        "required_win_rate": target,
        "breakeven_win_rate": round(breakeven, 1),
        "min_sample": config.SUCCESS_MIN_SAMPLE,
        "resolved_count": n,
        "unresolved_at_horizon": len(horizon_only),
        "hit_rate_pct": rate,
        "sample_sufficient": enough,
        "verdict": verdict,
    }


def summarize_by_band(trades, horizons):
    """Outcomes grouped by the engine's own score band.

    This is the number the score bands assert and nothing has ever checked:
    do higher-scoring setups actually win more often? If the bands are real,
    win rate rises monotonically across them. If it is flat, the score is
    ranking noise and the bands are decoration - which is a finding worth
    having, not a failure.

    Every row carries its trade_count, because a 71% win rate on 7 trades is
    not evidence and must not be presented beside a rate built on 200."""
    # Ranges stated EXPLICITLY rather than derived from the next entry.
    # Deriving them looked tidy and produced overlapping buckets - the
    # 65-band swallowed the 75-band's trades and every rate below the top
    # band was a blend, which would have made the score look flatter than
    # it is. Explicit bounds cannot drift.
    bands = [(85, 101, "broad"), (75, 85, "clear"), (65, 75, "narrow"), (0, 65, "below floor")]
    out = []
    for lo, hi, label in bands:
        group = [t for t in trades
                 if t.get("early_score") is not None and lo <= t["early_score"] < hi]
        if not group:
            out.append({"band": label, "min_score": lo, "max_score": hi - 1,
                        "trade_count": 0, "by_horizon": {}})
            continue
        by_h = {}
        for h in horizons:
            key = f"return_{h}_pct"
            rets = [t[key] for t in group if t.get(key) is not None]
            if not rets:
                continue
            wins = sum(1 for r in rets if r > 0)
            by_h[str(h)] = {
                "trade_count": len(rets),
                "win_rate_pct": round(100.0 * wins / len(rets), 1),
                "avg_return_pct": round(sum(rets) / len(rets), 3),
            }
        out.append({
            "band": label, "min_score": lo, "max_score": hi - 1,
            "trade_count": len(group),
            "avg_score": round(sum(t["early_score"] for t in group) / len(group), 1),
            "by_horizon": by_h,
        })
    return out


def run_backtest(kite, symbols, timeframe="15minute", days=30, horizons=DEFAULT_HORIZONS,
                  params=DEFAULT_PARAMS, required=DEFAULT_REQUIRED, progress_cb=None,
                  require_htf=False, require_regime_volume=False, exclude_opening_window=False,
                  require_candle_pattern=False,
                    require_macd_hist=False, require_big_candle=False,
                    require_strong_close=False, require_entry_location=False,
                    require_atr_floor=False,
                    cost_pct=DEFAULT_COST_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT,
                    holdout_pct=0.0,
                 require_oi_agreement=False,
                 history_cache=None, oi_history_cache=None) -> dict:
    lo, hi, _default = backtest_day_bounds(timeframe)
    days = int(days or _default)
    if days < lo:
        # Loud, not silent: below this the fetch cannot return enough bars for
        # the indicators to warm up, so every symbol would be skipped and the
        # run would report zero trades with no visible reason.
        raise ValueError(
            f"{days} days is too short for {timeframe} candles - that's fewer bars than the "
            f"indicators need to warm up, so every symbol would be skipped. Use at least {lo} days."
        )
    days = min(days, hi)
    # Clamp at the entry point too, not just inside _compute_trade, so the
    # values ECHOED back in the result (and shown on the Backtest page) are
    # the ones actually applied - reporting cost_pct=-5 while silently
    # having used 0 would be its own kind of dishonest number.
    cost_pct = max(0.0, float(cost_pct or 0.0))
    slippage_pct = max(0.0, float(slippage_pct or 0.0))
    # Capped at 90, not 100: a 100% holdout leaves nothing to tune against,
    # which defeats the purpose of splitting at all.
    holdout_pct = min(max(0.0, float(holdout_pct or 0.0)), 90.0)
    require_htf = bool(require_htf)
    require_regime_volume = bool(require_regime_volume)
    exclude_opening_window = bool(exclude_opening_window)
    require_candle_pattern = bool(require_candle_pattern)
    require_macd_hist = bool(require_macd_hist)
    require_big_candle = bool(require_big_candle)
    require_strong_close = bool(require_strong_close)
    require_entry_location = bool(require_entry_location)
    require_atr_floor = bool(require_atr_floor)
    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0})) or DEFAULT_HORIZONS

    params = tuple(p for p in (params or ()) if p in PARAM_IDS) or DEFAULT_PARAMS
    try:
        required = int(required)
    except (TypeError, ValueError):
        required = DEFAULT_REQUIRED
    required = max(1, min(required, len(params)))

    instruments = _load_instrument_map(kite)
    to_date = now_ist()
    window_start = to_date - dt.timedelta(days=days)
    fetch_days = days + WARMUP_DAYS

    trades = []
    symbol_notes = {}
    gate_diag = {}

    for idx, symbol in enumerate(symbols):
        if progress_cb:
            progress_cb(idx, len(symbols), symbol)
        token = instruments.get(symbol)
        if not token and symbol in INDEX_SYMBOLS:
            token = _load_index_token(kite, symbol)
        if not token:
            symbol_notes[symbol] = "symbol not found on NSE"
            continue
        history_key = (symbol, timeframe, fetch_days)
        cached_price = history_cache is not None and history_key in history_cache
        if cached_price:
            df = history_cache[history_key]
        else:
            try:
                df = _fetch_history(token, timeframe, fetch_days, kite)
            except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the whole backtest
                symbol_notes[symbol] = f"history fetch failed: {exc}"
                time.sleep(_RATE_LIMIT_PAUSE)
                continue
            if history_cache is not None:
                history_cache[history_key] = df
            time.sleep(_RATE_LIMIT_PAUSE)

        if df is None or df.empty or len(df) < max(settings.BB_LENGTH, 35) + 5:
            symbol_notes[symbol] = "not enough historical candles returned"
            continue

        # OI history for this symbol, only when the gate is actually on -
        # it is an extra API call per symbol and there is no reason to pay
        # it for a run that will not consult it.
        oi_hist = None
        if require_oi_agreement:
            oi_key = (symbol, timeframe, days)
            cached_oi = oi_history_cache is not None and oi_key in oi_history_cache
            if cached_oi:
                oi_hist = oi_history_cache[oi_key]
            else:
                try:
                    # NOT `... or None`. That idiom is fine for a dict or a list
                    # and raises ValueError on a pandas Series.
                    fetched = _fetch_oi_history_for_backtest(kite, symbol, timeframe, days=days)
                    if fetched is not None and len(fetched):
                        oi_hist = fetched
                except Exception as exc:  # noqa: BLE001 - a missing baseline must not kill the symbol
                    log.warning("Backtest OI history failed for %s: %s", symbol, exc)
                if oi_history_cache is not None:
                    # Cache None too, so an unavailable symbol is not retried for
                    # every gate in the same research sweep.
                    oi_history_cache[oi_key] = oi_hist
                time.sleep(_RATE_LIMIT_PAUSE)
            if oi_hist is None:
                symbol_notes[symbol] = "no OI history for the OI gate"

        try:
            symbol_trades = _replay_symbol(
                df, symbol, timeframe, window_start.replace(tzinfo=None), horizons, params, required,
                require_oi_agreement=require_oi_agreement, oi_history=oi_hist,
                oi_intraday=scanner_mod.oi_is_intraday(timeframe), diag=gate_diag,
                require_htf=require_htf, require_regime_volume=require_regime_volume,
                exclude_opening_window=exclude_opening_window,     require_candle_pattern=require_candle_pattern,
            require_macd_hist=require_macd_hist, require_big_candle=require_big_candle,
            require_strong_close=require_strong_close,
            require_entry_location=require_entry_location,
            require_atr_floor=require_atr_floor,
                cost_pct=cost_pct, slippage_pct=slippage_pct,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Backtest replay failed for %s", symbol)
            symbol_notes[symbol] = f"replay failed: {exc}"
            continue
        trades.extend(symbol_trades)

    if progress_cb:
        progress_cb(len(symbols), len(symbols), None)

    trades.sort(key=lambda t: t["entry_time"])

    # A loose combination (few required out of many chosen) fires far
    # more often - can produce thousands of trades over a big watchlist.
    # Win-rate/return stats below are computed from the FULL trade list
    # either way; only the trade-by-trade list sent to the browser for
    # display is capped, so the page stays responsive and the response
    # doesn't balloon into megabytes.
    summary = _summarize(trades, horizons)

    # Train/holdout split (research Finding 2). holdout_pct is a share of
    # the requested WINDOW, not of the trade count - splitting by trade
    # count would let a burst of correlated signals on one day land on
    # both sides of the boundary, which is exactly the leakage the split
    # exists to prevent. 0 (default) means no split at all.
    train_holdout = None
    if holdout_pct and holdout_pct > 0:
        span = (to_date - window_start).total_seconds()
        split_dt = window_start + dt.timedelta(seconds=span * (1 - float(holdout_pct) / 100.0))
        train_holdout = _summarize_by_split(
            trades, horizons, split_dt.replace(tzinfo=None).isoformat(timespec="seconds"))
    total_trade_count = len(trades)
    display_trades = trades[-MAX_TRADES_RETURNED:]

    return {
        "timeframe": timeframe,
        "days_requested": days,
        "horizons": list(horizons),
        "params": list(params),
        "required": required,
        "require_htf": require_htf,
        "require_regime_volume": require_regime_volume,
        "exclude_opening_window": exclude_opening_window,
        "require_candle_pattern": require_candle_pattern,
        "require_macd_hist": require_macd_hist,
        "require_big_candle": require_big_candle,
        "require_strong_close": require_strong_close,
        "require_entry_location": require_entry_location,
        "require_atr_floor": require_atr_floor,
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_end": to_date.isoformat(timespec="seconds"),
        "symbols_scanned": len(symbols),
        "symbols_with_trades": len({t["symbol"] for t in trades}),
        "symbols_skipped": symbol_notes,
        "trades": display_trades,
        "total_trade_count": total_trade_count,
        "summary": summary,
        "by_band": summarize_by_band(trades, horizons),
        "vs_target": summarize_vs_target(trades),
        "gate_diagnostics": gate_diag,
        "cost_pct": float(cost_pct),
        "slippage_pct": float(slippage_pct),
        "holdout_pct": float(holdout_pct),
        "train_holdout": train_holdout,
        "generated_at": to_date.isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# F&O Early Movement research - live-engine parity, not legacy vote counts.
# --------------------------------------------------------------------------

def _trim_replay_to_window(replay, window_start):
    """Trim every research event family to the requested non-warmup window."""
    out = dict(replay or {})
    for key in ("energy_events", "baseline_energy_events", "ignition_events",
                "best_entry_events", "swing_events", "recent_range_confirmation_events", "v9_playbook_events"):
        rows = list(out.get(key) or [])
        out[key] = [e for e in rows
                    if (e.get("signal_time") or e.get("entry_time") or "") >= window_start]
    return out



def _attach_v8_full_universe_scores(replays, feature_frames):
    """Attach point-in-time V8 percentiles using every researched F&O stock.

    The helper intentionally constructs one cross-sectional frame at a time so a
    180/365-day full-universe run does not retain a stack of large rank matrices
    in memory. Breakout strength is ranked among contemporaneous breakout events;
    participation, relative performance and OI magnitude are ranked against the
    full researched universe at the same timestamp.
    """
    frames = {s: f for s, f in (feature_frames or {}).items() if f is not None and not f.empty}
    if not frames:
        return replays

    event_refs = []
    seen_event_ids = set()
    families = ("ignition_events", "best_entry_events", "swing_events", "recent_range_confirmation_events", "v9_playbook_events")
    for replay in replays or []:
        for family in families:
            for event in replay.get(family) or []:
                marker = id(event)
                if marker in seen_event_ids:
                    continue
                seen_event_ids.add(marker)
                event_refs.append(event)
    if not event_refs:
        return replays

    def _norm_ts(frame, raw):
        try:
            ts = pd.Timestamp(raw)
            if frame.index.tz is None and ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            elif frame.index.tz is not None and ts.tzinfo is None:
                ts = ts.tz_localize(frame.index.tz)
            return ts
        except Exception:
            return None

    def _attach_rank(output_key, extractor, *, inverse_for_bear=False):
        parts = {}
        for symbol, frame in frames.items():
            try:
                ser = pd.to_numeric(extractor(frame), errors="coerce")
                ser.name = symbol
                parts[symbol] = ser
            except Exception:
                continue
        if not parts:
            return
        raw = pd.concat(parts, axis=1).sort_index()
        bull_rank = raw.rank(axis=1, pct=True, method="average") * 100.0
        bear_rank = (-raw).rank(axis=1, pct=True, method="average") * 100.0 if inverse_for_bear else None
        for event in event_refs:
            symbol = event.get("symbol")
            if symbol not in bull_rank.columns:
                continue
            ts = _norm_ts(bull_rank, event.get("signal_time"))
            if ts is None or ts not in bull_rank.index:
                continue
            use = bear_rank if inverse_for_bear and event.get("direction") == "Bearish" else bull_rank
            value = use.at[ts, symbol]
            if pd.notna(value):
                event[output_key] = round(float(value), 2)
        del raw, bull_rank, bear_rank

    _attach_rank("v8_tod_rvol_percentile", lambda f: f.get("tod_rvol"))
    _attach_rank("v8_opening_rvol_percentile", lambda f: f.get("opening_rvol"))
    _attach_rank("v8_range_shock_percentile", lambda f: f.get("bar_range_atr"))
    _attach_rank("v8_gap_shock_percentile", lambda f: pd.to_numeric(f.get("gap_atr"), errors="coerce").abs())
    _attach_rank("v8_turnover_percentile", lambda f: f.get("turnover_notional"))
    _attach_rank("v8_oi_strength_percentile", lambda f: pd.to_numeric(f.get("oi_chg_60m_pct"), errors="coerce").abs())

    def _relative(frame):
        cols = []
        for col in ("rs_pct", "stock_sector_lead_pct"):
            if col in frame:
                cols.append(pd.to_numeric(frame[col], errors="coerce"))
        if not cols:
            return pd.Series(np.nan, index=frame.index)
        return pd.concat(cols, axis=1).median(axis=1, skipna=True)
    _attach_rank("v8_relative_percentile", _relative, inverse_for_bear=True)

    # Breakout strength belongs to the candidate set, not to quiet stocks with
    # no escaped level. Rank it only among simultaneous directional events.
    by_time = {}
    for event in event_refs:
        if event.get("breakout_source") != "Recent Range":
            continue
        by_time.setdefault(event.get("signal_time"), []).append(event)
    for group in by_time.values():
        vals = [e.get("breakout_extension_atr") for e in group]
        ranks = v8_dual.percentile_rank(vals)
        for event, rank in zip(group, ranks):
            event["v8_breakout_strength_percentile"] = rank

    for event in event_refs:
        scored = v8_dual.score_preranked_row(event)
        for key, value in scored.items():
            if key.startswith("v8_") or key.startswith("v81_"):
                event[key] = value
    return replays


def _attach_v8_full_universe_scores_from_shards(replays, shard_map, stage_cb=None):
    """Attach V8/V9 cross-sectional ranks without retaining 211 feature frames in RAM.

    Only one feature-wide matrix is materialized at a time. Compact per-symbol
    frames remain on disk, which sharply lowers the peak memory immediately after
    the historical fetch stage and makes the job restart-resumable.
    """
    shard_map = dict(shard_map or {})
    if not shard_map:
        return replays

    event_refs = []
    seen_event_ids = set()
    families = ("ignition_events", "best_entry_events", "swing_events", "recent_range_confirmation_events", "v9_playbook_events")
    for replay in replays or []:
        for family in families:
            for event in replay.get(family) or []:
                marker = id(event)
                if marker in seen_event_ids:
                    continue
                seen_event_ids.add(marker)
                event_refs.append(event)
    if not event_refs:
        return replays

    def _norm_ts(frame, raw):
        try:
            ts = pd.Timestamp(raw)
            if frame.index.tz is None and ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            elif frame.index.tz is not None and ts.tzinfo is None:
                ts = ts.tz_localize(frame.index.tz)
            return ts
        except Exception:
            return None

    def _column(frame, name, *, absolute=False):
        if name not in frame:
            return pd.Series(np.nan, index=frame.index, dtype="float32")
        ser = pd.to_numeric(frame[name], errors="coerce")
        return ser.abs() if absolute else ser

    rank_specs = [
        ("v8_tod_rvol_percentile", lambda f: _column(f, "tod_rvol"), False),
        ("v8_opening_rvol_percentile", lambda f: _column(f, "opening_rvol"), False),
        ("v8_range_shock_percentile", lambda f: _column(f, "bar_range_atr"), False),
        ("v8_gap_shock_percentile", lambda f: _column(f, "gap_atr", absolute=True), False),
        ("v8_turnover_percentile", lambda f: _column(f, "turnover_notional"), False),
        ("v8_oi_strength_percentile", lambda f: _column(f, "oi_chg_60m_pct", absolute=True), False),
    ]

    def _relative(frame):
        cols = []
        for col in ("rs_pct", "stock_sector_lead_pct"):
            if col in frame:
                cols.append(pd.to_numeric(frame[col], errors="coerce"))
        if not cols:
            return pd.Series(np.nan, index=frame.index)
        return pd.concat(cols, axis=1).median(axis=1, skipna=True)

    rank_specs.append(("v8_relative_percentile", _relative, True))

    for pos, (output_key, extractor, inverse_for_bear) in enumerate(rank_specs, start=1):
        if stage_cb:
            stage_cb(2, 4, f"Building cross-sectional ranks ({pos}/{len(rank_specs)})", 71 + round((pos / len(rank_specs)) * 13))
        parts = {}
        for symbol, path in shard_map.items():
            try:
                payload = _load_research_symbol_shard(path)
                frame = payload.get("compact_frame")
                if frame is None or frame.empty:
                    continue
                ser = pd.to_numeric(extractor(frame), errors="coerce")
                ser.name = symbol
                parts[symbol] = ser
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read V9 rank shard for %s: %s", symbol, exc)
        if not parts:
            continue
        raw = pd.concat(parts, axis=1).sort_index()
        bull_rank = raw.rank(axis=1, pct=True, method="average") * 100.0
        bear_rank = (-raw).rank(axis=1, pct=True, method="average") * 100.0 if inverse_for_bear else None
        for event in event_refs:
            symbol = event.get("symbol")
            if symbol not in bull_rank.columns:
                continue
            ts = _norm_ts(bull_rank, event.get("signal_time"))
            if ts is None or ts not in bull_rank.index:
                continue
            use = bear_rank if inverse_for_bear and event.get("direction") == "Bearish" else bull_rank
            value = use.at[ts, symbol]
            if pd.notna(value):
                event[output_key] = round(float(value), 2)
        del parts, raw, bull_rank, bear_rank
        gc.collect()

    by_time = {}
    for event in event_refs:
        if event.get("breakout_source") != "Recent Range":
            continue
        by_time.setdefault(event.get("signal_time"), []).append(event)
    for group in by_time.values():
        vals = [e.get("breakout_extension_atr") for e in group]
        ranks = v8_dual.percentile_rank(vals)
        for event, rank in zip(group, ranks):
            event["v8_breakout_strength_percentile"] = rank

    for event in event_refs:
        scored = v8_dual.score_preranked_row(event)
        for key, value in scored.items():
            if key.startswith("v8_") or key.startswith("v81_"):
                event[key] = value
    return replays


_V8_COMPACT_FEATURE_COLUMNS = (
    "tod_rvol", "opening_rvol", "bar_range_atr", "gap_atr",
    "turnover_notional", "oi_chg_60m_pct", "rs_pct", "stock_sector_lead_pct",
    "daily_oi_z_pti", "daily_oi_chg_pct_pti",
)

_V91_COMPACT_EVENT_KEYS = (
    "symbol", "signal_time", "entry_time", "timestamp", "direction", "v8_direction",
    "v91_accumulation_probe", "v91_accumulation_seed_direction", "v92_accumulation_seed",
    "fresh_breakout", "breakout_source", "breakout_direction", "breakout_extension_atr",
    "entry_is_extended", "compression_score",
    "atr_pct",
    "price_chg_60m_pct", "price_move_60m_atr", "oi_z",
    "oi_chg_60m_pct", "oi_chg_30m_pct", "oi_acceleration", "basis_acceleration",
    "daily_oi_z_pti", "daily_oi_chg_pct_pti",
    "future_price_chg_60m_pct", "future_oi_chg_60m_pct",
    "vwap_side_agrees", "bull_vwap_available", "bull_above_vwap", "vwap_distance_atr",
    "tod_rvol", "opening_rvol", "bar_range_atr", "gap_atr",
    "turnover_notional", "rs_pct", "rs_acceleration", "stock_sector_lead_pct", "stock_index_lead_pct",
    "market_regime", "index_ret_8_pct", "index_vol_20bar_pct", "sector_rank_percentile", "basis_pct",
    "close_position_pct", "high", "low", "close",
    "intraday_returns", "swing_returns", "mfe_atr", "mae_atr",
    "v93_event_type", "movement_outcomes",
    "v93_silent_oi_lead", "v93_silent_oi_lead_bars", "v93_silent_compression_score",
    "v93_absolute_regime_aligned", "v93_trial13_candidate",
)


def _compact_v91_events(replay):
    """Extract only rows needed by V9.1 Bull Accumulation / frozen Bear FSB.

    V9.1 deliberately ignores the retired V9 playbooks.  Keeping only scalar
    evidence plus the small return/excursion dictionaries prevents each symbol
    checkpoint from carrying the full replay object into Stage 2.
    """
    out = []
    for raw in (replay or {}).get("v9_playbook_events") or []:
        keep = False
        if raw.get("v93_event_type") is not None or raw.get("v93_trial13_candidate") is True:
            keep = True
        elif raw.get("v92_accumulation_seed") is True:
            # Diagnostic V9.2 seed must survive even when a later Bull gate fails;
            # otherwise the funnel cannot identify the population bottleneck.
            keep = True
        elif raw.get("v91_accumulation_probe") is True:
            keep = True
            try:
                basis = raw.get("basis_acceleration")
                if basis is not None and np.isfinite(float(basis)) and float(basis) < -0.02:
                    keep = False
                cp = raw.get("close_position_pct")
                if cp is not None and np.isfinite(float(cp)) and float(cp) < 60.0:
                    keep = False
            except (TypeError, ValueError):
                pass
        elif raw.get("direction") == "Bearish" and raw.get("fresh_breakout") is True:
            # Keep the broad fresh-short seed here and apply the frozen Bear FSB
            # thresholds exactly once at the freeze boundary in v91_goal.
            # Duplicating extension/basis/CLV filters in the compactor makes the
            # research sample depend on an upstream implementation detail.
            try:
                price_60 = float(raw.get("price_chg_60m_pct"))
                oi_60 = float(raw.get("oi_chg_60m_pct"))
                keep = bool(np.isfinite(price_60) and np.isfinite(oi_60) and price_60 < 0 and oi_60 > 0)
            except (TypeError, ValueError):
                keep = False
        if not keep:
            continue
        row = {key: raw.get(key) for key in _V91_COMPACT_EVENT_KEYS if key in raw}
        out.append(row)
    return out


def _v91_confirmation_summary(replay):
    """Compact the audit counters V9.1 still exposes without retaining events."""
    return early_research.confirmation_diagnostics((replay or {}).get("ignition_events") or [])


def _merge_v91_confirmation_summaries(summaries):
    merged = {}
    for summary in summaries or []:
        for key, value in (summary or {}).items():
            if isinstance(value, (int, np.integer)):
                merged[key] = int(merged.get(key, 0)) + int(value)
    return merged


def _compact_v8_feature_frame(frame):
    """Keep only cross-sectional V9 inputs in float32 to cap full-universe RAM."""
    if frame is None or frame.empty:
        return pd.DataFrame(index=getattr(frame, "index", None))
    cols = [c for c in _V8_COMPACT_FEATURE_COLUMNS if c in frame.columns]
    if not cols:
        return pd.DataFrame(index=frame.index)
    compact = frame.loc[:, cols].apply(pd.to_numeric, errors="coerce")
    return compact.astype(np.float32, copy=False)


def run_early_movement_research(kite, symbols=None, timeframe="15minute", days=30, holdout_pct=30.0,
                                cost_pct=DEFAULT_COST_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT,
                                progress_cb=None, stage_cb=None, input_progress_cb=None, universe_is_full_fno=False,
                                fast_v8=False, research_mode=None, resume_run_dir=None) -> dict:
    """Replay the primary V6 research on a real 15m or 4H setup timeframe.

    15-minute setups execute on the next 15-minute bar as before. 4-hour
    setups are built from completed 4H candles but are executed and evaluated
    on a separate 15-minute stream, so a 4H signal cannot accidentally enter
    at the next 4H candle or peek inside an unfinished setup candle.
    """
    if timeframe not in ("15minute", "4hour"):
        raise ValueError("Primary Stock-in-Play research supports only 15minute or 4hour setup timeframes")
    execution_timeframe = "15minute"
    lo, hi, default = backtest_day_bounds(timeframe)
    days = max(lo, min(int(days or default), hi))
    symbols = list(symbols or settings.WATCHLIST)
    horizons = DEFAULT_HORIZONS
    streaming_v91 = bool(fast_v8 and research_mode in ("v91_fast", "v91_bear_final", "v93_lab"))
    run_dir = (Path(resume_run_dir) if resume_run_dir is not None else
        _early_research_run_dir(
            symbols=symbols, timeframe=timeframe, days=days, holdout_pct=holdout_pct,
            cost_pct=cost_pct, slippage_pct=slippage_pct, research_mode=research_mode,
        )) if fast_v8 else None
    completed_shards = _completed_research_symbol_shards(run_dir) if run_dir is not None else {}
    # V9.3 fetches daily continuous OI *inside* each symbol batch.  The old
    # full-universe pre-sweep meant a Railway restart at symbol 190 lost the
    # whole 210-symbol daily-OI pass before a single resumable symbol shard had
    # been written.  Per-symbol acquisition makes every completed symbol a
    # durable unit of work and keeps the research stage essentially constant-memory.
    daily_oi_coverage = {}
    daily_oi_coverage_rows = []
    if streaming_v91 and run_dir is not None and _v91_ranked_events_path(run_dir).exists():
        try:
            ranked_v91_payload = _load_v91_ranked_events_checkpoint(_v91_ranked_events_path(run_dir))
            if progress_cb:
                progress_cb(len(symbols), len(symbols), None)
            if stage_cb:
                stage_cb(2, 4, "Stage 2 checkpoint available — loading ranked events", 84)
            run_context = {
                "setup_timeframe": timeframe,
                "execution_timeframe": execution_timeframe,
                "days": days,
                "cost_pct": float(cost_pct),
                "slippage_pct": float(slippage_pct),
                "universe_is_full_fno": bool(universe_is_full_fno),
                "fast_v8": True,
                "research_mode": research_mode,
                "history_coverage": dict(ranked_v91_payload.get("history_coverage") or {}),
                "daily_oi_coverage": dict(ranked_v91_payload.get("daily_oi_coverage") or {}),
                "effective_atr_floor_pct": effective_min_atr_pct(timeframe),
            }
            if stage_cb:
                if research_mode == "v91_bear_final":
                    stage_cb(3, 4, "Running frozen Bear FSB final test", 86)
                elif research_mode == "v93_lab":
                    stage_cb(3, 4, "Running V9.4 Measurement Repair + Trial 13 resolution + Trial 14", 86)
                else:
                    stage_cb(3, 4, "Validating V9.2 goal-focused models", 86)
            research = early_research.aggregate_v91_compact_events(
                ranked_v91_payload.get("events") or [],
                ranked_v91_payload.get("confirmation") or {},
                holdout_pct=holdout_pct,
                run_context=run_context,
                stage3_progress_cb=(
                    (lambda message, pct: stage_cb(3, 4, message, pct)) if stage_cb else None
                ),
            )
            if stage_cb:
                stage_cb(4, 4, "Preparing report", 98)
            return {
                "timeframe": timeframe,
                "setup_timeframe": timeframe,
                "execution_timeframe": execution_timeframe,
                "days": days,
                "symbols_scanned": len(symbols),
                "symbols_completed": int(ranked_v91_payload.get("symbols_completed") or len(completed_shards)),
                "symbols_skipped": dict(ranked_v91_payload.get("notes") or {}),
                "cost_pct": float(cost_pct),
                "slippage_pct": float(slippage_pct),
                "research": research,
                "history_coverage": dict(ranked_v91_payload.get("history_coverage") or {}),
                "fast_v8": True,
                "research_notes": [
                    "Historical OI uses Kite's available futures-history series; live ranking aggregates near/next/far expiries, so rollover-era historical OI is an approximation rather than a reconstructed three-expiry book.",
                    "15-minute setups retain next-bar execution; V9.3 streaming reuses the same point-in-time event logic.",
                    "V9.3 separately maps daily continuous-futures OI only after the completed session close; same-day morning bars cannot see that day's daily OI.",
                    "Stage 2 converts each heavy symbol shard once into a lean rank-only checkpoint, streams one cross-sectional rank at a time, and checkpoints after input preparation plus every completed rank.",
                    "A Stage-2 rank-progress checkpoint resumes after the last completed rank; a completed ranked-events checkpoint resumes directly at validation after a worker restart.",
                    "Historical membership uses the current NSE stock-F&O universe replayed backward; point-in-time F&O membership is not available in the present data source, so survivorship bias is explicitly disclosed rather than hidden.",
                    "V9.3 daily continuous-OI features are point-in-time and swing-oriented; intraday current-contract OI remains partial and is never backfilled from invented expired-contract 15-minute data.",
                ],
                "generated_at": now_ist().isoformat(timespec="seconds"),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("V9.1 Stage-2 checkpoint unusable; rebuilding from symbol shards: %s", exc)
            _v91_ranked_events_path(run_dir).unlink(missing_ok=True)
    instruments = _load_instrument_map(kite)
    index_token = _load_index_token(kite, "NIFTY 50")
    index_df = None
    try:
        if index_token:
            index_df = _fetch_history(index_token, timeframe, days + WARMUP_DAYS, kite)
    except Exception as exc:  # noqa: BLE001
        log.warning("Early research NIFTY history unavailable: %s", exc)

    replays = []
    notes = {}
    # V6 sector leadership is cross-sectional. Fetch each needed sector once,
    # then rank sector returns point-in-time across the available sector set.
    sector_history = {}
    sectors_needed = sorted({scanner_mod.SYMBOL_SECTOR_MAP.get(s) for s in symbols
                             if scanner_mod.SYMBOL_SECTOR_MAP.get(s)})
    for sector_symbol in sectors_needed:
        try:
            sector_token = _load_index_token(kite, sector_symbol)
            sector_history[sector_symbol] = (
                _fetch_history(sector_token, timeframe, days + WARMUP_DAYS, kite)
                if sector_token else None
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Early research sector history unavailable for %s: %s", sector_symbol, exc)
            sector_history[sector_symbol] = None
    sector_ret_parts = {}
    for sector_symbol, sdf in sector_history.items():
        if sdf is not None and not sdf.empty and "close" in sdf:
            sector_ret_parts[sector_symbol] = pd.to_numeric(sdf["close"], errors="coerce").pct_change(8) * 100.0
    sector_rank_frame = None
    if sector_ret_parts:
        sector_ret_frame = pd.concat(sector_ret_parts, axis=1).sort_index()
        sector_rank_frame = sector_ret_frame.rank(axis=1, pct=True, method="average") * 100.0

    turnover_series = {}
    v8_feature_frames = {}
    history_coverage_rows = []
    window_start = (now_ist() - dt.timedelta(days=days)).replace(tzinfo=None).isoformat()
    for i, symbol in enumerate(symbols):
        if fast_v8 and symbol in completed_shards:
            if progress_cb:
                progress_cb(i + 1, len(symbols), f"{symbol} · resumed")
            continue
        if progress_cb:
            progress_cb(i, len(symbols), f"{symbol} · daily OI + price/OI")
        token = instruments.get(symbol)
        if not token:
            notes[symbol] = "symbol not found on NSE"
            if fast_v8:
                path = _write_research_symbol_shard(run_dir, i, symbol, compact_frame=None, replay=None, note=notes[symbol])
                completed_shards[symbol] = path
            continue
        try:
            daily_oi_series = None
            daily_cov = None
            if research_mode == "v93_lab":
                try:
                    one_daily = scanner_mod.fetch_oi_history(
                    kite, [symbol], timeframe="day", days_override=days + WARMUP_DAYS
                    )
                    daily_oi_series = one_daily.get(symbol)
                    daily_cov = _daily_oi_coverage_summary(
                        ({symbol: daily_oi_series} if daily_oi_series is not None else {}), [symbol]
                    )
                    daily_oi_coverage_rows.append(daily_cov)
                except Exception as daily_exc:  # disclosed research input, never fabricated
                    log.debug("V9.4 daily continuous OI unavailable for %s: %s", symbol, daily_exc)
                    daily_oi_series = None
                    daily_cov = _daily_oi_coverage_summary({}, [symbol])
                    daily_oi_coverage_rows.append(daily_cov)
            df = _fetch_history(token, timeframe, days + WARMUP_DAYS, kite)
            if df is None or df.empty:
                notes[symbol] = "no price history"
                if fast_v8:
                    path = _write_research_symbol_shard(run_dir, i, symbol, compact_frame=None, replay=None, note=notes[symbol])
                    completed_shards[symbol] = path
                continue
            execution_df = df
            if timeframe == "4hour":
                execution_df = _fetch_history(
                    token, execution_timeframe, days + WARMUP_DAYS, kite
                )
                if execution_df is None or execution_df.empty:
                    notes[symbol] = "no 15-minute execution history for 4-hour setup"
                    if fast_v8:
                        path = _write_research_symbol_shard(run_dir, i, symbol, compact_frame=None, replay=None, note=notes[symbol])
                        completed_shards[symbol] = path
                    continue
            oi = _fetch_oi_history_for_backtest(kite, symbol, timeframe, days=days)
            cov = None
            try:
                window_floor = pd.Timestamp(window_start)
                price_cov = df.loc[pd.DatetimeIndex(df.index) >= early_research._align_timestamp_to_index(window_floor, df.index)]
                if oi is not None and len(oi):
                    oi_floor = early_research._align_timestamp_to_index(window_floor, oi.index)
                    oi_cov = pd.Series(oi).loc[pd.DatetimeIndex(oi.index) >= oi_floor]
                else:
                    oi_cov = oi
                cov = _history_coverage_summary(price_cov, oi_cov, requested_days=days)
                cov["symbol"] = symbol
                cov["timeframe"] = timeframe
                history_coverage_rows.append(cov)
            except Exception as coverage_exc:  # coverage is diagnostic, never a run blocker
                log.debug("Coverage diagnostic failed for %s: %s", symbol, coverage_exc)
            sector_symbol = scanner_mod.SYMBOL_SECTOR_MAP.get(symbol)
            sector_df = sector_history.get(sector_symbol) if sector_symbol else None
            sector_rank_series = None
            if sector_symbol and sector_rank_frame is not None and sector_symbol in sector_rank_frame.columns:
                sector_rank_series = sector_rank_frame[sector_symbol]
            futures_df = _fetch_near_futures_history_for_research(
                kite, symbol, timeframe, days=days
            )
            feat = early_research.build_feature_frame(
                df, timeframe, oi_series=oi, index_df=index_df, sector_df=sector_df,
                sector_rank_series=sector_rank_series, futures_df=futures_df,
                daily_oi_series=daily_oi_series)
            if feat.empty:
                notes[symbol] = "not enough history for early-movement features"
                if fast_v8:
                    path = _write_research_symbol_shard(run_dir, i, symbol, compact_frame=None, replay=None, note=notes[symbol])
                    completed_shards[symbol] = path
                continue
            if not fast_v8 and "turnover_notional" in feat.columns:
                turnover_series[symbol] = pd.to_numeric(feat["turnover_notional"], errors="coerce")
            compact_v8 = _compact_v8_feature_frame(feat)
            if not fast_v8 and not compact_v8.empty:
                v8_feature_frames[symbol] = compact_v8
            # Preserve ATR for Energy Building's directionless expansion target.
            series = compute_series(df, timeframe)
            if "error" not in series:
                feat["atr"] = series["atr"]
            replay = early_research.replay_feature_frame(
                df, feat, symbol, horizons=horizons,
                cost_pct=cost_pct, slippage_pct=slippage_pct,
                execution_df=(execution_df if timeframe == "4hour" else None),
                setup_timeframe=timeframe, fast_v8=fast_v8,
            )
            replay = _trim_replay_to_window(replay, window_start)
            if fast_v8:
                v91_events = _compact_v91_events(replay) if streaming_v91 else None
                v91_confirmation = _v91_confirmation_summary(replay) if streaming_v91 else None
                path = _write_research_symbol_shard(
                    run_dir, i, symbol, compact_frame=compact_v8,
                    replay=(None if streaming_v91 else replay), note=None,
                    v91_events=v91_events, v91_confirmation=v91_confirmation,
                    history_coverage=cov,
                    daily_oi_coverage=daily_cov,
                )
                completed_shards[symbol] = path
                # The shard owns the compact feature/replay payload now; keep the
                # 211-stock fetch stage essentially constant-memory.
                del replay, compact_v8, feat, df, execution_df, oi, futures_df, v91_events, v91_confirmation, daily_oi_series
                # Pandas/NumPy may leave freed arenas mapped. Trim after every
                # completed symbol so a 210-stock Railway sweep does not grow
                # toward the worker/container memory ceiling across Stage 1.
                research_runtime.release_memory_pressure()
            else:
                replays.append(replay)
        except Exception as exc:  # noqa: BLE001
            log.exception("Early movement research failed for %s", symbol)
            notes[symbol] = str(exc)
            if fast_v8:
                try:
                    path = _write_research_symbol_shard(run_dir, i, symbol, compact_frame=None, replay=None, note=notes[symbol])
                    completed_shards[symbol] = path
                except Exception as shard_exc:  # noqa: BLE001
                    log.warning("Could not checkpoint failed symbol %s: %s", symbol, shard_exc)
        time.sleep(_RATE_LIMIT_PAUSE)

    # V9.4 persists one tiny point-in-time daily-OI snapshot for the live
    # magnitude shadow.  The live scanner reads this compact cache instead of
    # reopening 210 research shards or launching another historical OI sweep.
    if research_mode == "v93_lab" and fast_v8 and completed_shards:
        try:
            v94_magnitude.persist_daily_oi_snapshot_from_shards(completed_shards)
        except Exception as exc:  # research cache is useful but never run-blocking
            log.warning("Could not persist V9.4 daily-OI live snapshot: %s", exc)

    ranked_v91_payload = None
    if streaming_v91:
        # Stage 1 is complete.  Drop index/sector history before Stage 2 so the
        # cross-sectional ranking matrices do not overlap with the fetch-stage
        # memory footprint on constrained Railway workers.
        v8_feature_frames.clear()
        turnover_series.clear()
        sector_history.clear()
        sector_ret_parts.clear()
        sector_rank_frame = None
        index_df = None
        gc.collect()
        completed_shards = _completed_research_symbol_shards(run_dir)
        checkpoint_path = _build_v91_ranked_events_checkpoint(run_dir, completed_shards, stage_cb=stage_cb)
        ranked_v91_payload = _load_v91_ranked_events_checkpoint(checkpoint_path)
        notes.update(ranked_v91_payload.get("notes") or {})
        daily_oi_coverage = dict(ranked_v91_payload.get("daily_oi_coverage") or {})
        usable_shards = {}
    elif fast_v8:
        completed_shards = _completed_research_symbol_shards(run_dir)
        replays, shard_notes, usable_shards = _load_research_replays_from_shards(completed_shards, symbols)
        notes.update(shard_notes)
    else:
        usable_shards = {}

    # Replace the per-stock historical turnover percentile with the actual
    # point-in-time cross-sectional percentile across the researched F&O
    # universe. This is the closest historical analogue to live Stock-in-Play
    # ranking and avoids calling a stock "high turnover" merely because it is
    # active relative to itself.
    if not fast_v8:
        if turnover_series:
            turnover_frame = pd.concat(turnover_series, axis=1).sort_index()
            turnover_rank_frame = turnover_frame.rank(axis=1, pct=True, method="average") * 100.0
            for replay in replays:
                for family in ("ignition_events", "best_entry_events", "swing_events", "recent_range_confirmation_events"):
                    for event in replay.get(family) or []:
                        try:
                            ts = pd.Timestamp(event.get("signal_time"))
                            sym = event.get("symbol")
                            if sym not in turnover_rank_frame.columns:
                                continue
                            # Match timezone shape of the research frame.
                            if turnover_rank_frame.index.tz is None and ts.tzinfo is not None:
                                ts = ts.tz_localize(None)
                            elif turnover_rank_frame.index.tz is not None and ts.tzinfo is None:
                                ts = ts.tz_localize(turnover_rank_frame.index.tz)
                            if ts not in turnover_rank_frame.index:
                                continue
                            rank = turnover_rank_frame.at[ts, sym]
                            if pd.notna(rank):
                                event["turnover_percentile"] = round(float(rank), 2)
                                event["catalyst_score"] = v6_edge.catalyst_proxy_score(
                                    gap_atr=event.get("gap_atr"), opening_rvol=event.get("opening_rvol"),
                                    tod_rvol=event.get("tod_rvol"), bar_range_atr=event.get("bar_range_atr"),
                                    turnover_percentile=event.get("turnover_percentile"),
                                )
                        except Exception:
                            continue

    if stage_cb and not streaming_v91:
        stage_cb(2, 4, "Building cross-sectional ranks", 72)
    if streaming_v91:
        pass
    elif fast_v8:
        _attach_v8_full_universe_scores_from_shards(replays, usable_shards, stage_cb=stage_cb)
    else:
        _attach_v8_full_universe_scores(replays, v8_feature_frames)
    # Cross-sectional ranks are now attached to the compact event rows; release
    # the full-universe feature/index history before Stage 3 aggregation.
    if fast_v8 and not streaming_v91:
        v8_feature_frames.clear()
        turnover_series.clear()
        sector_history.clear()
        sector_ret_parts.clear()
        sector_rank_frame = None
        index_df = None
        gc.collect()

    if progress_cb:
        progress_cb(len(symbols), len(symbols), None)
    history_coverage = _aggregate_history_coverage(
        history_coverage_rows, timeframe=timeframe, requested_days=days
    )
    if research_mode == "v93_lab" and not daily_oi_coverage:
        daily_oi_coverage = _merge_daily_oi_coverage(daily_oi_coverage_rows, len(symbols))
    run_context = {
        "setup_timeframe": timeframe,
        "execution_timeframe": execution_timeframe,
        "days": days,
        "cost_pct": float(cost_pct),
        "slippage_pct": float(slippage_pct),
        "universe_is_full_fno": bool(universe_is_full_fno),
        "fast_v8": bool(fast_v8),
        "research_mode": research_mode or ("v9_fast" if fast_v8 else "legacy"),
        "history_coverage": history_coverage,
        "daily_oi_coverage": daily_oi_coverage,
        "effective_atr_floor_pct": effective_min_atr_pct(timeframe),
    }
    if stage_cb:
        if research_mode == "v91_bear_final":
            stage_cb(3, 4, "Running frozen Bear FSB final test", 86)
        elif research_mode == "v93_lab":
            stage_cb(3, 4, "Running V9.4 Measurement Repair + Trial 13 resolution + Trial 14", 86)
        elif research_mode == "v91_fast":
            stage_cb(3, 4, "Validating V9.2 goal-focused models", 86)
        else:
            stage_cb(3, 4, "Validating V9 professional playbooks", 86)
    if streaming_v91:
        research = early_research.aggregate_v91_compact_events(
            ranked_v91_payload.get("events") or [],
            ranked_v91_payload.get("confirmation") or {},
            holdout_pct=holdout_pct,
            run_context=run_context,
            stage3_progress_cb=(
                (lambda message, pct: stage_cb(3, 4, message, pct)) if stage_cb else None
            ),
        )
    elif fast_v8:
        research = early_research.aggregate_v8_research_fast(
            replays, holdout_pct=holdout_pct, run_context=run_context
        )
    else:
        research = early_research.aggregate_research(
            replays, holdout_pct=holdout_pct, ref_horizon=3, horizons=horizons,
            run_context=run_context)
    if stage_cb:
        stage_cb(4, 4, "Preparing report", 98)
    symbols_completed_count = len(completed_shards) if streaming_v91 else len(replays)
    if streaming_v91:
        if ranked_v91_payload is not None:
            ranked_v91_payload.clear()
        gc.collect()
    elif fast_v8:
        replays.clear()
        gc.collect()
    return {
        "timeframe": timeframe,
        "setup_timeframe": timeframe,
        "execution_timeframe": execution_timeframe,
        "days": days,
        "symbols_scanned": len(symbols),
        "symbols_completed": symbols_completed_count,
        "symbols_skipped": notes,
        "cost_pct": float(cost_pct),
        "slippage_pct": float(slippage_pct),
        "research": research,
        "history_coverage": history_coverage,
        "fast_v8": bool(fast_v8),
        "research_notes": [
            "Historical OI uses Kite's available futures-history series; live ranking aggregates near/next/far expiries, so rollover-era historical OI is an approximation rather than a reconstructed three-expiry book.",
            ("4-hour setups are formed only from completed 4H candles and execute on the first "
             "available 15-minute bar; 15-minute setups retain next-bar execution."),
            "Higher-timeframe context is replayed using only fully closed buckets to avoid look-ahead.",
            "Sector context is replayed when the stock has a mapped NSE sector index and that index history is available.",
            "Historical membership uses the current NSE stock-F&O universe replayed backward; point-in-time F&O membership is not available in the present data source, so survivorship bias is explicitly disclosed rather than hidden.",
        ],
        "generated_at": now_ist().isoformat(timespec="seconds"),
    }


_RESEARCH_STATE_DIR = Path(os.environ.get("RESEARCH_STATE_DIR", ".dbindicator-research"))
_EARLY_RESEARCH_STATE_PATH = Path(
    os.environ.get("EARLY_RESEARCH_STATE_PATH", str(_RESEARCH_STATE_DIR / "early-research-state.json"))
)
_EARLY_RESEARCH_WORK_ROOT = Path(
    os.environ.get("EARLY_RESEARCH_WORK_ROOT", str(_RESEARCH_STATE_DIR / "work"))
)
_V95_DAILY_STATE_PATH = Path(
    os.environ.get("V95_DAILY_STATE_PATH", str(_RESEARCH_STATE_DIR / "v95-daily-state.json"))
)
_V95_DAILY_WORK_ROOT = Path(
    os.environ.get("V95_DAILY_WORK_ROOT", str(_RESEARCH_STATE_DIR / "v95-daily-work"))
)
_V96_STATE_PATH = Path(
    os.environ.get("V96_STATE_PATH", str(_RESEARCH_STATE_DIR / "v96-trial17-state.json"))
)
_V96_WORK_ROOT = Path(
    os.environ.get("V96_WORK_ROOT", str(_RESEARCH_STATE_DIR / "v96-trial17-work"))
)
_V97_STATE_PATH = Path(
    os.environ.get("V97_STATE_PATH", str(_RESEARCH_STATE_DIR / "v97-trial19-state.json"))
)
_V97_WORK_ROOT = Path(
    os.environ.get("V97_WORK_ROOT", str(_RESEARCH_STATE_DIR / "v97-trial19-work"))
)
_RESEARCH_RESUME_SCHEMA = "v934-resume-shards-1"
_V95_RUN_SCHEMA = "v952-nse-daily-evidence-run-1"
_V96_RUN_SCHEMA = "v960-trial17-independent-total-oi-run-1"
_V97_RUN_SCHEMA = "v972-trial19-confound-integrity-run-1"


def _early_research_run_dir(*, symbols, timeframe, days, holdout_pct, cost_pct, slippage_pct, research_mode):
    """Return a deterministic same-day work directory for resumable research shards.

    The date is part of the key because the historical window moves each day. A process
    restart on the same trading day resumes the exact same 211-stock job; a later-day
    run starts fresh instead of silently reusing stale history.
    """
    payload = {
        "schema": _RESEARCH_RESUME_SCHEMA,
        "day": now_ist().date().isoformat(),
        "symbols": list(symbols or []),
        "timeframe": str(timeframe),
        "days": int(days),
        "holdout_pct": float(holdout_pct),
        "cost_pct": float(cost_pct),
        "slippage_pct": float(slippage_pct),
        "research_mode": str(research_mode or "legacy"),
        "feature_revision": "v940-measurement-repair-1" if str(research_mode or "") == "v93_lab" else "legacy",
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    run_dir = Path(_EARLY_RESEARCH_WORK_ROOT) / digest
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = run_dir / "meta.json"
    if not meta.exists():
        tmp = run_dir / "meta.json.tmp"
        tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, meta)
    return run_dir


def _v95_daily_run_dir(*, symbols, days):
    payload = {
        "schema": _V95_RUN_SCHEMA,
        "day": now_ist().date().isoformat(),
        "symbols": list(symbols or []),
        "days": int(days),
        "build": v95_daily_evidence.BUILD_ID,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    run_dir = Path(_V95_DAILY_WORK_ROOT) / digest
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = run_dir / "meta.json"
    if not meta.exists():
        tmp = run_dir / "meta.json.tmp"
        tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, meta)
    return run_dir



def _v96_run_dir(*, symbols):
    payload = {
        "schema": _V96_RUN_SCHEMA,
        "symbols": list(symbols or []),
        "build": v96_trial17.BUILD_ID,
        "window": [str(v96_trial17.INDEPENDENT_START.date()), str(v96_trial17.INDEPENDENT_END.date())],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    run_dir = Path(_V96_WORK_ROOT) / digest
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = run_dir / "meta.json"
    if not meta.exists():
        tmp = run_dir / "meta.json.tmp"
        tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, meta)
    return run_dir

def _v97_run_dir(*, symbols):
    payload = {
        "schema": _V97_RUN_SCHEMA,
        "symbols": list(symbols or []),
        "build": v97_trial19.BUILD_ID,
        "window": [str(v97_trial19.INDEPENDENT_START.date()), str(v97_trial19.INDEPENDENT_END.date())],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    run_dir = Path(_V97_WORK_ROOT) / digest
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = run_dir / "meta.json"
    if not meta.exists():
        tmp = run_dir / "meta.json.tmp"
        tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, meta)
    return run_dir


def _research_symbol_shard_path(run_dir, index, symbol):
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(symbol))
    return Path(run_dir) / f"{int(index):04d}-{safe}.pkl"


def _write_research_symbol_shard(run_dir, index, symbol, *, compact_frame, replay, note,
                                 v91_events=None, v91_confirmation=None, history_coverage=None,
                                 daily_oi_coverage=None):
    """Atomically persist one completed symbol so a worker restart can resume."""
    path = _research_symbol_shard_path(run_dir, index, symbol)
    payload = {
        "symbol": str(symbol),
        "compact_frame": compact_frame,
        "replay": replay,
        "note": note,
        "v91_events": v91_events,
        "v91_confirmation": v91_confirmation,
        "history_coverage": history_coverage,
        "daily_oi_coverage": daily_oi_coverage,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def _load_research_symbol_shard(path):
    with Path(path).open("rb") as fh:
        return pickle.load(fh)


def _completed_research_symbol_shards(run_dir):
    out = {}
    for path in sorted(Path(run_dir).glob("*.pkl")):
        try:
            payload = _load_research_symbol_shard(path)
            symbol = str(payload.get("symbol") or "")
            if symbol:
                out[symbol] = path
        except Exception as exc:  # noqa: BLE001
            log.warning("Ignoring unreadable research shard %s: %s", path, exc)
    return out


def _research_resume_summary(run_dir, total_symbols):
    if run_dir is None:
        return None
    saved = len(_completed_research_symbol_shards(run_dir))
    ranked = _v91_ranked_events_path(run_dir).exists()
    if saved <= 0 and not ranked:
        return None
    text = f"{saved}/{int(total_symbols or 0)} symbols saved"
    if ranked:
        text += " · Stage 2 checkpoint available"
    return text


def _load_research_replays_from_shards(shard_map, symbols):
    replays = []
    notes = {}
    usable = {}
    for symbol in symbols:
        path = shard_map.get(symbol)
        if not path:
            continue
        payload = _load_research_symbol_shard(path)
        note = payload.get("note")
        replay = payload.get("replay")
        compact = payload.get("compact_frame")
        if note:
            notes[symbol] = str(note)
        if replay:
            replays.append(replay)
        if compact is not None and getattr(compact, "empty", True) is False:
            usable[symbol] = path
    return replays, notes, usable


_V91_RANKED_EVENTS_SCHEMA = "v930-ranked-events-1"


def _v91_ranked_events_path(run_dir):
    return Path(run_dir) / "ranked-events.pkl"


def _atomic_pickle(path, payload):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def _load_v91_ranked_events_checkpoint(path):
    with Path(path).open("rb") as fh:
        payload = pickle.load(fh)
    if payload.get("schema") != _V91_RANKED_EVENTS_SCHEMA:
        raise ValueError("incompatible V9.1 ranked-events checkpoint")
    return payload


def _v91_rank_progress_path(run_dir):
    return Path(run_dir) / "rank-progress.pkl"


_V91_RANK_PROGRESS_SCHEMA = "v930-rank-progress-1"


def _v91_rank_feature_dir(run_dir):
    path = Path(run_dir) / "rank-features"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _v91_rank_feature_path(run_dir, symbol):
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(symbol))
    return _v91_rank_feature_dir(run_dir) / f"{safe}.pkl"


def _write_v91_rank_feature_shard(run_dir, symbol, compact_frame):
    """Persist only the compact Stage-2 feature frame for one symbol.

    Stage-1 shards also carry event dictionaries and coverage metadata. Reopening
    those heavy payloads during every cross-sectional rank caused large transient
    allocations on Railway.  The lean rank shard keeps Stage 2 disk-streamable.
    """
    path = _v91_rank_feature_path(run_dir, symbol)
    return _atomic_pickle(path, {"symbol": str(symbol), "compact_frame": compact_frame})


def _load_v91_rank_feature_shard(path):
    with Path(path).open("rb") as fh:
        payload = pickle.load(fh)
    return payload.get("compact_frame")


def _save_v91_rank_progress(run_dir, *, rows, confirmations, notes, completed_rank_keys, symbols_completed,
                            inputs_prepared=False, history_coverage_rows=None, daily_oi_coverage_rows=None):
    """Persist Stage-2 progress after input compaction and each completed rank.

    The checkpoint contains compact candidate rows plus audit summaries. Heavy
    Stage-1 symbol shards are converted once into rank-only shards and never need
    to be kept together in RAM. A worker restart after input preparation can
    therefore resume Stage 2 without touching the heavy symbol payloads again.
    """
    return _atomic_pickle(_v91_rank_progress_path(run_dir), {
        "schema": _V91_RANK_PROGRESS_SCHEMA,
        "events": rows,
        "confirmation": _merge_v91_confirmation_summaries(confirmations),
        "notes": notes,
        "completed_rank_keys": list(completed_rank_keys or []),
        "symbols_completed": int(symbols_completed or 0),
        "inputs_prepared": bool(inputs_prepared),
        "history_coverage_rows": list(history_coverage_rows or []),
        "daily_oi_coverage_rows": list(daily_oi_coverage_rows or []),
    })


def _save_v91_input_progress(run_dir, *, rows, confirmations, notes, symbols_completed,
                             history_coverage_rows=None, daily_oi_coverage_rows=None, completed_rank_keys=None):
    """Persist the lean-input preparation boundary separately from rank checkpoints."""
    return _atomic_pickle(_v91_rank_progress_path(run_dir), {
        "schema": _V91_RANK_PROGRESS_SCHEMA,
        "events": rows,
        "confirmation": _merge_v91_confirmation_summaries(confirmations),
        "notes": notes,
        "completed_rank_keys": list(completed_rank_keys or []),
        "symbols_completed": int(symbols_completed or 0),
        "inputs_prepared": True,
        "history_coverage_rows": list(history_coverage_rows or []),
        "daily_oi_coverage_rows": list(daily_oi_coverage_rows or []),
    })


def _load_v91_rank_progress(run_dir):
    path = _v91_rank_progress_path(run_dir)
    if not path.exists():
        return None
    with path.open("rb") as fh:
        payload = pickle.load(fh)
    if payload.get("schema") != _V91_RANK_PROGRESS_SCHEMA:
        raise ValueError("incompatible V9.1 Stage-2 rank progress checkpoint")
    return payload


def _build_v91_ranked_events_checkpoint(run_dir, shard_map, stage_cb=None):
    """Build/reuse the compact Stage-2 checkpoint with bounded memory.

    Reliability contract:
      * each heavy Stage-1 symbol shard is deserialized at most once;
      * compact rank-only shards are persisted before ranking begins;
      * one cross-sectional rank is built at a time from those lean shards;
      * the entire universe of feature frames is never retained in RAM;
      * input preparation and every completed rank are resumable checkpoints;
      * progress identifies loading/ranking/attachment work and elapsed time.
    """
    run_dir = Path(run_dir)
    path = _v91_ranked_events_path(run_dir)
    if path.exists():
        try:
            _load_v91_ranked_events_checkpoint(path)
            if stage_cb:
                stage_cb(2, 4, "Stage 2 checkpoint available — loading ranked events", 84)
            return path
        except Exception as exc:  # noqa: BLE001
            log.warning("Ignoring unreadable V9.1 ranked checkpoint %s: %s", path, exc)
            path.unlink(missing_ok=True)

    started = time.monotonic()

    def elapsed():
        return f"{max(0.0, time.monotonic() - started):.1f}s"

    progress = None
    try:
        progress = _load_v91_rank_progress(run_dir)
    except Exception as exc:  # noqa: BLE001
        log.warning("Ignoring unreadable Stage-2 rank progress in %s: %s", run_dir, exc)
        _v91_rank_progress_path(run_dir).unlink(missing_ok=True)

    rows = list((progress or {}).get("events") or [])
    confirmations = []
    if progress and progress.get("confirmation"):
        confirmations = [progress.get("confirmation") or {}]
    notes = dict((progress or {}).get("notes") or {})
    completed_rank_keys = list((progress or {}).get("completed_rank_keys") or [])
    history_coverage_rows = list((progress or {}).get("history_coverage_rows") or [])
    daily_oi_coverage_rows = list((progress or {}).get("daily_oi_coverage_rows") or [])
    inputs_prepared = bool((progress or {}).get("inputs_prepared"))
    already_loaded_events = bool(progress)

    items = list(dict(shard_map or {}).items())
    total_symbols = len(items)

    if stage_cb and completed_rank_keys:
        stage_cb(2, 4, f"Resuming Stage 2 after {len(completed_rank_keys)}/7 ranks · elapsed {elapsed()}", 71)

    if not inputs_prepared:
        # Convert heavy Stage-1 payloads into lean feature-only shards one symbol
        # at a time. This is the only Stage-2 pass that deserializes event-rich
        # Stage-1 checkpoints. Nothing from the full universe is retained in RAM.
        history_coverage_rows = []
        daily_oi_coverage_rows = []
        for idx, (symbol, shard_path) in enumerate(items, start=1):
            if stage_cb and (idx == 1 or idx % 10 == 0 or idx == total_symbols):
                stage_cb(2, 4, f"Loading Stage-2 inputs · preparing memory-safe {idx}/{total_symbols} · elapsed {elapsed()}", 71)
            payload = _load_research_symbol_shard(shard_path)
            if payload.get("note") and symbol not in notes:
                notes[symbol] = str(payload["note"])
            if payload.get("history_coverage"):
                history_coverage_rows.append(dict(payload["history_coverage"]))
            if payload.get("daily_oi_coverage"):
                daily_oi_coverage_rows.append(dict(payload["daily_oi_coverage"]))
            if not already_loaded_events:
                shard_events = payload.get("v91_events")
                shard_confirmation = payload.get("v91_confirmation")
                if shard_events is None and payload.get("replay") is not None:
                    shard_events = _compact_v91_events(payload.get("replay"))
                if shard_confirmation is None and payload.get("replay") is not None:
                    shard_confirmation = _v91_confirmation_summary(payload.get("replay"))
                rows.extend(shard_events or [])
                confirmations.append(shard_confirmation or {})
            compact = payload.get("compact_frame")
            if compact is not None and getattr(compact, "empty", True) is False:
                _write_v91_rank_feature_shard(run_dir, symbol, compact)
            del payload, compact
            if idx % 10 == 0:
                gc.collect()

        inputs_prepared = True
        _save_v91_input_progress(
            run_dir, rows=rows, confirmations=confirmations, notes=notes,
            symbols_completed=total_symbols, completed_rank_keys=completed_rank_keys,
            history_coverage_rows=history_coverage_rows,
            daily_oi_coverage_rows=daily_oi_coverage_rows,
        )
        gc.collect()
        if stage_cb:
            stage_cb(2, 4, f"Memory-safe Stage-2 inputs ready · {total_symbols} symbols · elapsed {elapsed()}", 71)
    elif stage_cb:
        stage_cb(2, 4, f"Resuming from memory-safe Stage-2 input checkpoint · elapsed {elapsed()}", 71)

    def _norm_ts(frame, raw):
        try:
            ts = pd.Timestamp(raw)
            if frame.index.tz is None and ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            elif frame.index.tz is not None and ts.tzinfo is None:
                ts = ts.tz_localize(frame.index.tz)
            return ts
        except Exception:
            return None

    def _column(frame, name, *, absolute=False):
        if name not in frame:
            return pd.Series(np.nan, index=frame.index, dtype="float32")
        ser = pd.to_numeric(frame[name], errors="coerce")
        return ser.abs() if absolute else ser

    def _relative(frame):
        cols = []
        for col in ("rs_pct", "stock_sector_lead_pct"):
            if col in frame:
                cols.append(pd.to_numeric(frame[col], errors="coerce"))
        if not cols:
            return pd.Series(np.nan, index=frame.index, dtype="float32")
        return pd.concat(cols, axis=1).median(axis=1, skipna=True)

    rank_specs = [
        ("v8_tod_rvol_percentile", "TOD RVOL", lambda f: _column(f, "tod_rvol"), False),
        ("v8_opening_rvol_percentile", "opening RVOL", lambda f: _column(f, "opening_rvol"), False),
        ("v8_range_shock_percentile", "range shock", lambda f: _column(f, "bar_range_atr"), False),
        ("v8_gap_shock_percentile", "gap shock", lambda f: _column(f, "gap_atr", absolute=True), False),
        ("v8_turnover_percentile", "turnover", lambda f: _column(f, "turnover_notional"), False),
        ("v8_oi_strength_percentile", "OI strength", lambda f: _column(f, "oi_chg_60m_pct", absolute=True), False),
        ("v8_relative_percentile", "relative strength", _relative, True),
    ]

    for pos, (output_key, label, extractor, inverse_for_bear) in enumerate(rank_specs, start=1):
        if output_key in completed_rank_keys:
            continue
        parts = []
        if stage_cb:
            stage_cb(2, 4, f"Rank {pos}/7 {label} · streaming lean inputs · elapsed {elapsed()}", 71 + round((pos / len(rank_specs)) * 13))
        for idx, (symbol, _heavy_path) in enumerate(items, start=1):
            feature_path = _v91_rank_feature_path(run_dir, symbol)
            if not feature_path.exists():
                continue
            frame = _load_v91_rank_feature_shard(feature_path)
            if frame is None or getattr(frame, "empty", True):
                continue
            extracted = extractor(frame)
            if isinstance(extracted, pd.Series):
                ser = pd.to_numeric(extracted, errors="coerce")
            elif extracted is None or (isinstance(extracted, float) and np.isnan(extracted)):
                ser = pd.Series(np.nan, index=frame.index, dtype="float32")
            else:
                ser = pd.to_numeric(pd.Series(extracted, index=frame.index), errors="coerce")
            ser = ser.astype("float32", copy=False)
            ser.name = symbol
            parts.append(ser)
            del frame, extracted, ser
            if stage_cb and (idx == 1 or idx % 25 == 0 or idx == total_symbols):
                stage_cb(2, 4, f"Rank {pos}/7 {label} · loaded {idx}/{total_symbols} symbols · elapsed {elapsed()}", 71 + round((pos / len(rank_specs)) * 13))

        if not parts:
            completed_rank_keys.append(output_key)
            _save_v91_rank_progress(
                run_dir, rows=rows, confirmations=confirmations, notes=notes,
                completed_rank_keys=completed_rank_keys, symbols_completed=total_symbols,
                inputs_prepared=True, history_coverage_rows=history_coverage_rows,
                daily_oi_coverage_rows=daily_oi_coverage_rows,
            )
            continue

        if stage_cb:
            stage_cb(2, 4, f"Rank {pos}/7 {label} · ranking {len(parts)} symbols · elapsed {elapsed()}", 71 + round((pos / len(rank_specs)) * 13))
        raw = pd.concat(parts, axis=1).sort_index()
        del parts
        bull_rank = raw.rank(axis=1, pct=True, method="average") * 100.0
        bear_rank = (-raw).rank(axis=1, pct=True, method="average") * 100.0 if inverse_for_bear else None
        del raw

        if stage_cb:
            stage_cb(2, 4, f"Rank {pos}/7 {label} · attaching {len(rows)} events · elapsed {elapsed()}", 71 + round((pos / len(rank_specs)) * 13))
        for event in rows:
            symbol = event.get("symbol")
            if symbol not in bull_rank.columns:
                continue
            ts = _norm_ts(bull_rank, event.get("signal_time"))
            if ts is None or ts not in bull_rank.index:
                continue
            use = bear_rank if inverse_for_bear and event.get("direction") == "Bearish" else bull_rank
            value = use.at[ts, symbol]
            if pd.notna(value):
                event[output_key] = round(float(value), 2)

        completed_rank_keys.append(output_key)
        _save_v91_rank_progress(
            run_dir, rows=rows, confirmations=confirmations, notes=notes,
            completed_rank_keys=completed_rank_keys, symbols_completed=total_symbols,
            inputs_prepared=True, history_coverage_rows=history_coverage_rows,
            daily_oi_coverage_rows=daily_oi_coverage_rows,
        )
        del bull_rank, bear_rank
        research_runtime.release_memory_pressure()

    if stage_cb:
        stage_cb(2, 4, f"Finalizing breakout-strength ranks · elapsed {elapsed()}", 84)
    by_time = {}
    for event in rows:
        if event.get("breakout_source") != "Recent Range":
            continue
        by_time.setdefault(event.get("signal_time"), []).append(event)
    for group in by_time.values():
        ranks = v8_dual.percentile_rank([e.get("breakout_extension_atr") for e in group])
        for event, rank in zip(group, ranks):
            event["v8_breakout_strength_percentile"] = rank
    del by_time

    total_rows = len(rows)
    if stage_cb:
        stage_cb(2, 4, f"Scoring ranked candidates (0/{total_rows}) · elapsed {elapsed()}", 85)
    next_quarter = 1
    for pos, event in enumerate(rows, start=1):
        v8_dual.score_goal_preranked_row_inplace(event)
        if stage_cb and total_rows:
            quarter = min(4, (pos * 4) // total_rows)
            if quarter >= next_quarter or pos == total_rows:
                pct = min(100, round((pos / total_rows) * 100))
                stage_cb(2, 4, f"Scoring ranked candidates ({pct}% · {pos}/{total_rows}) · elapsed {elapsed()}", 85)
                next_quarter = quarter + 1

    if stage_cb:
        stage_cb(2, 4, "Writing Stage-2 checkpoint", 85)
    if history_coverage_rows:
        requested_days_cov = max(int(r.get("requested_days") or 0) for r in history_coverage_rows)
        timeframe_cov = next((str(r.get("timeframe")) for r in history_coverage_rows if r.get("timeframe")), "15minute")
        checkpoint_history_coverage = _aggregate_history_coverage(
            history_coverage_rows, timeframe=timeframe_cov, requested_days=requested_days_cov
        )
    else:
        checkpoint_history_coverage = {}
    final_path = _atomic_pickle(path, {
        "schema": _V91_RANKED_EVENTS_SCHEMA,
        "events": rows,
        "confirmation": _merge_v91_confirmation_summaries(confirmations),
        "notes": notes,
        "symbols_completed": len(shard_map or {}),
        "history_coverage": checkpoint_history_coverage,
        "daily_oi_coverage": _merge_daily_oi_coverage(daily_oi_coverage_rows, total_symbols),
    })
    _v91_rank_progress_path(run_dir).unlink(missing_ok=True)
    return final_path



_V95_RESUME_SCHEMA = "v952-nse-daily-evidence-shard-1"


def _v95_symbol_shard_path(run_dir, index, symbol):
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(symbol))
    return Path(run_dir) / f"{int(index):04d}-{safe}.pkl"


def _load_v95_symbol_shard(path, symbol):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            payload = pickle.load(fh)
        if not isinstance(payload, dict) or payload.get("schema") != _V95_RESUME_SCHEMA:
            return None
        if payload.get("symbol") != symbol:
            return None
        frame = payload.get("frame")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return None
        return frame
    except Exception as exc:  # noqa: BLE001
        log.warning("Ignoring invalid V9.5 symbol checkpoint %s: %s", path, exc)
        return None


def _save_v95_symbol_shard(path, symbol, frame):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump({"schema": _V95_RESUME_SCHEMA, "symbol": symbol, "frame": frame}, fh, protocol=pickle.HIGHEST_PROTOCOL)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def run_v95_daily_oi_evidence(kite, symbols=None, days=1095, progress_cb=None, integrity_data=None,
                               resume_run_dir=None, stage_cb=None) -> dict:
    """Run the isolated V9.5 daily-OI evidence lab on official NSE OI history.

    Historical stock-futures OI comes from NSE's own daily F&O bhavcopies
    (legacy + UDiFF).  The frozen Trial-15 primary series is the *near-month*
    share-equivalent OI reconstructed from those contract files; next/far and
    total OI remain diagnostics.  Kite is retained for daily cash prices only.

    MWPL/ban data is loaded only for the already-declared validation dates and
    is never requested for the locked final 20%.  Any missing load-bearing
    integrity source fails closed rather than falling back to fabricated data.
    """
    symbols = [str(s).strip().upper() for s in (symbols or settings.WATCHLIST)]
    days = max(1095, min(int(days or 1095), 3650))
    integrity_data = dict(integrity_data or {})
    explicit_membership = integrity_data.get("membership_by_symbol") or {}
    explicit_mwpl = integrity_data.get("mwpl_by_symbol") or {}
    explicit_ban = integrity_data.get("ban_by_symbol") or {}
    explicit_lot = integrity_data.get("lot_size_by_symbol") or {}
    explicit_expiry = integrity_data.get("expiry_by_symbol") or {}
    atm_iv_map = integrity_data.get("atm_iv_by_symbol") or {}

    cash_tokens = _load_instrument_map(kite)
    to_date = now_ist()
    research_start = (to_date - dt.timedelta(days=days)).date()
    # Warmup is outside the measured window and exists only for expected-OI,
    # 60-day shock z, 20-day realised vol and 14-day ATR features.
    fetch_start = to_date - dt.timedelta(days=days + 150)
    archive_days = pd.bdate_range(pd.Timestamp(fetch_start.date()), pd.Timestamp(to_date.date()))

    if stage_cb:
        stage_cb(1, 4, "Loading official NSE stock-futures OI archive", 3)

    supplied_histories = integrity_data.get("nse_history_by_symbol")
    if supplied_histories:
        nse_histories = dict(supplied_histories)
        if "_meta" not in nse_histories:
            nse_histories["_meta"] = dict(integrity_data.get("nse_history_meta") or {})
    else:
        archive_client = nse_futures_history.NSEFuturesArchiveClient(
            cache_dir=_RESEARCH_STATE_DIR / "nse-fo-bhavcopy"
        )

        last_archive_progress = {"done": -1}

        def _archive_progress(done, total_days, label):
            # Persisting UI state on every one of ~800 dates creates avoidable
            # disk churn.  Report at coarse milestones while the client still
            # caches every successfully downloaded archive independently.
            if stage_cb and (done == 0 or done == total_days or done - last_archive_progress["done"] >= 20):
                pct = 3 + round((done / max(total_days, 1)) * 27)
                stage_cb(1, 4, f"NSE F&O archive {done}/{total_days} · {label}", min(30, pct))
                last_archive_progress["done"] = done

        nse_histories = nse_futures_history.build_symbol_histories(
            archive_days, symbols, archive_client, progress_cb=_archive_progress, discover_historical=True
        )

    nse_meta = dict(nse_histories.get("_meta") or {})
    nse_date_coverage = float(nse_meta.get("date_coverage") or 0.0)
    nse_coverage_ok = bool(nse_date_coverage >= 0.95)
    discovered_symbols = sorted(k for k in nse_histories if k != "_meta")
    # The research population uses the historical FUTSTK union, not today's
    # F&O membership replayed backward.  A symbol still needs an NSE cash
    # token so its outcomes can be measured honestly.
    research_symbols = sorted(set(discovered_symbols) | set(symbols))
    priceable_discovered = [s for s in discovered_symbols if cash_tokens.get(s)]
    historical_price_coverage = float(len(priceable_discovered) / len(discovered_symbols)) if discovered_symbols else 0.0
    historical_membership_ok = bool(nse_coverage_ok and discovered_symbols and historical_price_coverage >= 0.95)

    # NSE contract presence gives point-in-time eligibility; UDiFF lot
    # quantity plus legacy share-equivalent OPEN_INT makes the near-month
    # primary OI comparable across lot revisions.
    controls = {
        "historical_membership_available": historical_membership_ok,
        "mwpl_available": bool(explicit_mwpl) and bool(explicit_ban),
        "lot_size_normalization_available": bool(nse_coverage_ok),
        "atm_iv_available": bool(atm_iv_map),
        "independent_history_guard_required": True,
    }

    frames = {}
    notes = {}
    coverage = []
    total = len(research_symbols)
    resumed_symbol_shards = 0
    run_dir = Path(resume_run_dir) if resume_run_dir is not None else None
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)

    if stage_cb:
        stage_cb(2, 4, "Building daily cash + NSE near-month OI evidence frames", 31)
    if progress_cb:
        progress_cb(0, total, None)

    for i, symbol in enumerate(research_symbols, start=1):
        if progress_cb:
            progress_cb(i - 1, total, symbol)
        shard_path = _v95_symbol_shard_path(run_dir, i - 1, symbol) if run_dir is not None else None
        cached_frame = _load_v95_symbol_shard(shard_path, symbol) if shard_path is not None else None
        if cached_frame is not None:
            frame = cached_frame
            frames[symbol] = frame
            resumed_symbol_shards += 1
            coverage.append({
                "symbol": symbol,
                "rows": int(len(frame)),
                "first": str(frame.index.min().date()),
                "last": str(frame.index.max().date()),
                "derived_expiry_calendar": False,
                "nse_near_oi_rows": int(pd.to_numeric(frame.get("nse_near_oi"), errors="coerce").notna().sum()) if "nse_near_oi" in frame else 0,
                "resumed": True,
            })
            research_runtime.release_memory_pressure()
            if progress_cb:
                progress_cb(i, total, symbol)
            continue

        cash_token = cash_tokens.get(symbol)
        hist = nse_histories.get(symbol) or {}
        near_oi = hist.get("near_oi")
        if not cash_token:
            notes[symbol] = "Missing NSE cash token; cash history not fabricated."
            if progress_cb:
                progress_cb(i, total, symbol)
            continue
        if not isinstance(near_oi, pd.Series) or near_oi.dropna().empty:
            notes[symbol] = "Official NSE near-month futures OI history unavailable; Kite OI fallback disabled."
            if progress_cb:
                progress_cb(i, total, symbol)
            continue

        try:
            price_rows = scanner_mod._fetch_historical_chunked(
                kite, cash_token, fetch_start, to_date, "day", oi=False, continuous=False
            )
            price = pd.DataFrame(price_rows)
            if price.empty:
                raise ValueError("daily cash price history unavailable")
            price = price.rename(columns={"date": "timestamp"}).set_index("timestamp")
            price = price[~price.index.duplicated(keep="last")].sort_index()

            oi = pd.to_numeric(pd.Series(near_oi).copy(), errors="coerce")
            oi.index = pd.to_datetime(oi.index).normalize()
            oi = oi.dropna()
            oi = oi[~oi.index.duplicated(keep="last")].sort_index()

            expiry_series = explicit_expiry.get(symbol) if symbol in explicit_expiry else hist.get("near_expiry")
            mwpl_series = explicit_mwpl.get(symbol)
            ban_series = explicit_ban.get(symbol)
            frame = v95_daily_evidence.build_symbol_daily_frame(
                price, oi,
                expiry_dates=expiry_series,
                ban_series=ban_series,
                mwpl_series=mwpl_series,
            )

            # Membership from an official daily bhavcopy is point-in-time and
            # must never be forward-filled through a day on which the contract
            # is absent.  Explicit caller data may override it for audits.
            membership = explicit_membership.get(symbol) if symbol in explicit_membership else hist.get("membership")
            if membership is not None and not frame.empty:
                m = pd.Series(membership).copy()
                m.index = pd.to_datetime(m.index).normalize()
                m = m.reindex(frame.index).astype("boolean").fillna(False).astype(bool)
                frame["eligible"] = frame["eligible"] & m
                frame["fno_member_pti"] = m
            else:
                frame["fno_member_pti"] = False
                frame["eligible"] = False

            # Preserve the contract structure for diagnostics while keeping
            # the frozen Trial-15 feature on near-month OI only.
            for col, key in (
                ("nse_total_oi", "total_oi"),
                ("nse_near_oi", "near_oi"),
                ("nse_next_oi", "next_oi"),
                ("nse_far_oi", "far_oi"),
                ("nse_lot_size", "lot_size"),
                ("nse_near_dte", "near_dte"),
            ):
                source = hist.get(key)
                if isinstance(source, pd.Series):
                    ser = pd.to_numeric(source.copy(), errors="coerce")
                    ser.index = pd.to_datetime(ser.index).normalize()
                    frame[col] = ser.reindex(frame.index)

            iv = atm_iv_map.get(symbol)
            if iv is not None and not frame.empty:
                ivs = pd.Series(iv).copy()
                ivs.index = pd.to_datetime(ivs.index).normalize()
                frame["atm_iv_pct_pti"] = pd.to_numeric(ivs.reindex(frame.index), errors="coerce")

            frame = frame[frame.index.date >= research_start]
            if frame.empty:
                raise ValueError("no eligible daily rows inside requested research window")
            frames[symbol] = frame
            coverage.append({
                "symbol": symbol,
                "rows": int(len(frame)),
                "first": str(frame.index.min().date()),
                "last": str(frame.index.max().date()),
                "derived_expiry_calendar": False,
                "nse_near_oi_rows": int(frame["nse_near_oi"].notna().sum()) if "nse_near_oi" in frame else int(oi.notna().sum()),
                "membership_days": int(frame["fno_member_pti"].sum()),
                "resumed": False,
            })
            if shard_path is not None:
                _save_v95_symbol_shard(shard_path, symbol, frame)
        except Exception as exc:  # noqa: BLE001
            log.exception("V9.5 NSE daily evidence fetch failed for %s", symbol)
            notes[symbol] = str(exc)
        finally:
            research_runtime.release_memory_pressure()
            if progress_cb:
                progress_cb(i, total, symbol)

    # Load MWPL/ban only for the pre-declared validation dates.  The locked
    # final 20% is intentionally not touched by this integrity download.
    mwpl_result = None
    if frames and not controls["mwpl_available"]:
        if stage_cb:
            stage_cb(3, 4, "Loading validation-only NSE MWPL / ban controls", 82)
        _, validation_dates, _ = v95_daily_evidence._partition_dates(frames)
        try:
            mwpl_client = nse_mwpl.NSEHistoricalReportClient(
                cache_dir=_RESEARCH_STATE_DIR / "nse-mwpl"
            )
            mwpl_result = nse_mwpl.build_validation_mwpl_controls(
                validation_dates=sorted(validation_dates),
                symbols=list(frames),
                client=mwpl_client,
                min_date_coverage=0.95,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("V9.5 MWPL validation control load failed")
            mwpl_result = {
                "available": False,
                "reason": f"MWPL_LOAD_ERROR:{exc}",
                "date_coverage": 0.0,
                "mwpl_by_symbol": {}, "ban_by_symbol": {},
                "source": "NSE_F&O_COMBINED_OPEN_INTEREST", "errors": {"load": str(exc)},
            }
        controls["mwpl_available"] = bool(mwpl_result.get("available"))
        if controls["mwpl_available"]:
            for symbol, frame in frames.items():
                mws = (mwpl_result.get("mwpl_by_symbol") or {}).get(symbol)
                bans = (mwpl_result.get("ban_by_symbol") or {}).get(symbol)
                if isinstance(mws, pd.Series):
                    s = pd.to_numeric(mws.copy(), errors="coerce")
                    s.index = pd.to_datetime(s.index).normalize()
                    frame["mwpl_pct"] = s.reindex(frame.index)
                if isinstance(bans, pd.Series):
                    s = pd.Series(bans).copy()
                    s.index = pd.to_datetime(s.index).normalize()
                    # Missing/non-validation dates stay False only because
                    # evaluate_trial15 selects validation rows; the values are
                    # not interpreted outside that partition.
                    frame["ban_flag"] = s.reindex(frame.index).fillna(False).astype(bool)
    elif controls["mwpl_available"]:
        mwpl_result = {
            "available": True, "reason": "APPLIED_EXPLICIT_INPUT", "date_coverage": 1.0,
            "source": "EXPLICIT_POINT_IN_TIME_INPUT", "errors": {},
        }

    if stage_cb:
        stage_cb(4, 4, "Evaluating frozen Trial 15 evidence gates", 92)
    research_runtime.release_memory_pressure()
    research = v95_daily_evidence.evaluate_trial15(frames, controls=controls) if frames else {
        "build": v95_daily_evidence.BUILD_ID,
        "trial15": v95_daily_evidence.trial15_spec(),
        "trial16": v95_daily_evidence.trial16_spec(),
        "status": "INCONCLUSIVE_NO_DATA", "primary_pass": False,
        "controls": {
            "realized_vol_control": "UNAVAILABLE",
            "atm_iv_control": "APPLIED" if controls["atm_iv_available"] else "UNAVAILABLE_NOT_FABRICATED",
            "mwpl_control": "APPLIED" if controls["mwpl_available"] else "UNAVAILABLE",
            "historical_membership": "APPLIED" if controls["historical_membership_available"] else "CURRENT_UNIVERSE_REPLAY_SURVIVORSHIP_BIAS",
            "lot_size_normalization": "APPLIED" if controls["lot_size_normalization_available"] else "UNAVAILABLE_DISCLOSED",
            "v94_discovery_overlap_guard": "UNAVAILABLE_NO_DATA",
        },
        "final_test": {"locked": True, "rows_locked": 0},
        "research_only": True,
    }

    contract_structure_research = v953_contract_structure.evaluate_contract_structure(frames) if frames else {
        "build": v95_daily_evidence.BUILD_ID, "status": "NO_DATA", "research_only": True,
        "trial_number": None, "final_20_locked": True, "features": {},
    }

    # Archive completeness is an integrity gate of the data layer, not a new
    # research threshold.  A weak archive can never turn Trial 15 into PASS.
    if frames and not nse_coverage_ok:
        research["primary_pass"] = False
        reasons = list(research.get("inconclusive_reasons") or [])
        if "NSE_HISTORY_COVERAGE" not in reasons:
            reasons.append("NSE_HISTORY_COVERAGE")
        research["inconclusive_reasons"] = reasons
        # Data-quality gaps block a potential PASS but cannot hide an already
        # demonstrated efficacy failure.
        if not str(research.get("status") or "").startswith("FAIL_"):
            research["status"] = "INCONCLUSIVE_NSE_HISTORY_COVERAGE"

    structure_available = bool(frames) and all(
        isinstance(nse_histories.get(s), dict)
        and isinstance(nse_histories[s].get("near_oi"), pd.Series)
        and isinstance(nse_histories[s].get("next_oi"), pd.Series)
        and isinstance(nse_histories[s].get("far_oi"), pd.Series)
        for s in frames
    )
    return {
        "build": v95_daily_evidence.BUILD_ID,
        "days": days,
        "timeframe": "day",
        "symbols_scanned": len(research_symbols),
        "current_symbols_requested": len(symbols),
        "symbols_completed": len(frames),
        "symbols_skipped": notes,
        "coverage": coverage,
        "integrity": {
            **controls,
            "intraday_pipeline_used": False,
            "historical_oi_source": nse_meta.get("source") or "NSE_OFFICIAL_FO_BHAVCOPY",
            "nse_oi_date_coverage": nse_date_coverage,
            "nse_oi_coverage_ok": nse_coverage_ok,
            "nse_archive_dates_requested": int(nse_meta.get("dates_requested") or 0),
            "nse_archive_dates_loaded": int(nse_meta.get("dates_loaded") or 0),
            "nse_archive_errors": dict(nse_meta.get("errors") or {}),
            "historical_symbols_discovered": int(nse_meta.get("historical_symbols_discovered") or len(discovered_symbols)),
            "historical_membership_price_coverage": historical_price_coverage,
            "current_universe_replay": not historical_membership_ok,
            "membership_basis": "NSE_POINT_IN_TIME_HISTORICAL_FUTSTK_UNION" if historical_membership_ok else "INCOMPLETE_HISTORICAL_FUTSTK_UNION",
            "expiry_calendar": "NSE_ACTUAL_CONTRACT_EXPIRIES",
            "oi_normalization": "NSE_OPEN_INTEREST_QUANTITY_NORMALIZED_TO_SHARE_EQUIVALENT",
            "nse_oi_structure": {
                "near_next_far_available": structure_available,
                "primary_series": "near_oi_share_equivalent",
                "diagnostic_series": ["total_oi_share_equivalent", "next_oi_share_equivalent", "far_oi_share_equivalent"],
            },
            "mwpl_date_coverage": float((mwpl_result or {}).get("date_coverage") or 0.0),
            "mwpl_reason": (mwpl_result or {}).get("reason") or ("APPLIED_EXPLICIT_INPUT" if controls["mwpl_available"] else "UNAVAILABLE"),
            "mwpl_source": (mwpl_result or {}).get("source") or "UNAVAILABLE",
            "mwpl_errors": dict((mwpl_result or {}).get("errors") or {}),
            "atm_iv_source": "EXPLICIT_POINT_IN_TIME_INPUT" if atm_iv_map else "UNAVAILABLE_NOT_FABRICATED",
            "resumed_symbol_shards": int(resumed_symbol_shards),
        },
        "research": research,
        "contract_structure_research": contract_structure_research,
        "research_only": True,
    }


def run_v96_trial17(kite, symbols=None, progress_cb=None, integrity_data=None,
                     resume_run_dir=None, stage_cb=None) -> dict:
    """Run frozen Trial 17 on older, non-overlapping official NSE history.

    Evidence dates are fixed in :mod:`app.v96_trial17`; warm-up data is fetched
    only before that window so the total-OI z-score and volatility controls are
    point-in-time. No V9.5/Trial-15 evaluator or locked final partition is read.
    """
    symbols = [str(x).strip().upper() for x in (symbols or settings.WATCHLIST)]
    integrity_data = dict(integrity_data or {})
    warmup_start = v96_trial17.INDEPENDENT_START - pd.Timedelta(days=150)
    archive_end = v96_trial17.INDEPENDENT_END
    cash_end = archive_end + pd.Timedelta(days=7)
    archive_days = pd.bdate_range(warmup_start, archive_end)

    if stage_cb:
        stage_cb(1, 4, "Loading official NSE Trial-17 independent-history archive", 4)

    supplied_histories = integrity_data.get("nse_history_by_symbol")
    if supplied_histories:
        histories = dict(supplied_histories)
        histories.setdefault("_meta", dict(integrity_data.get("nse_history_meta") or {}))
    else:
        client = nse_futures_history.NSEFuturesArchiveClient(cache_dir=_RESEARCH_STATE_DIR / "nse-fo-bhavcopy")
        last = {"done": -1}
        def _archive_progress(done, total, label):
            if stage_cb and (done == 0 or done == total or done - last["done"] >= 20):
                stage_cb(1, 4, f"NSE Trial17 archive {done}/{total} · {label}", min(28, 4 + round((done/max(total,1))*24)))
                last["done"] = done
        histories = nse_futures_history.build_symbol_histories(
            archive_days, symbols, client, progress_cb=_archive_progress, discover_historical=True
        )

    meta = dict(histories.get("_meta") or {})
    date_coverage = float(meta.get("date_coverage") or 0.0)
    archive_ok = bool(date_coverage >= 0.95)
    discovered = sorted(k for k in histories if k != "_meta")
    research_symbols = discovered or symbols
    membership_ok = bool(archive_ok and discovered)

    cash_days = pd.bdate_range(warmup_start, cash_end)
    supplied_cash = integrity_data.get("nse_cash_by_symbol")
    if supplied_cash:
        cash_histories = dict(supplied_cash)
        cash_histories.setdefault("_meta", dict(integrity_data.get("nse_cash_meta") or {}))
    else:
        cash_client = nse_cash_history.NSECashArchiveClient(cache_dir=_RESEARCH_STATE_DIR / "nse-cm-bhavcopy")
        cash_last = {"done": -1}
        def _cash_progress(done, total, label):
            if stage_cb and (done == 0 or done == total or done - cash_last["done"] >= 20):
                stage_cb(2, 4, f"NSE Trial17 cash archive {done}/{total} · {label}", min(48, 29 + round((done/max(total,1))*19)))
                cash_last["done"] = done
        cash_histories = nse_cash_history.build_symbol_price_histories(
            cash_days, research_symbols, cash_client, progress_cb=_cash_progress
        )

    cash_meta = dict(cash_histories.get("_meta") or {})
    cash_date_coverage = float(cash_meta.get("date_coverage") or 0.0)
    cash_archive_ok = bool(cash_date_coverage >= 0.95)
    member_points = 0
    priced_member_points = 0
    for symbol in research_symbols:
        hist = histories.get(symbol) or {}
        membership = hist.get("membership")
        price = cash_histories.get(symbol)
        if not isinstance(membership, pd.Series) or not isinstance(price, pd.DataFrame):
            continue
        m = membership.copy(); m.index = pd.to_datetime(m.index).normalize()
        m = m[(m.index >= v96_trial17.INDEPENDENT_START) & (m.index <= v96_trial17.INDEPENDENT_END)].fillna(False).astype(bool)
        closes = pd.to_numeric(price.get("close"), errors="coerce")
        closes.index = pd.to_datetime(closes.index).normalize()
        closes = closes.reindex(m.index)
        member_points += int(m.sum())
        priced_member_points += int((m & closes.notna()).sum())
    membership_price_coverage = float(priced_member_points / member_points) if member_points else 0.0
    historical_cash_ok = bool(cash_archive_ok and member_points > 0 and membership_price_coverage >= 0.95)

    controls = {
        "historical_membership_available": membership_ok,
        "historical_cash_price_available": historical_cash_ok,
        "lot_size_normalization_available": archive_ok,
        "mwpl_available": False,
    }

    frames = {}
    notes = {}
    coverage = []
    total = len(research_symbols)
    resumed = 0
    run_dir = Path(resume_run_dir) if resume_run_dir is not None else None
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
    if stage_cb:
        stage_cb(2, 4, "Building Trial-17 cash + total-OI frames", 30)

    for i, symbol in enumerate(research_symbols, start=1):
        if progress_cb:
            progress_cb(i-1, total, symbol)
        shard = _v95_symbol_shard_path(run_dir, i-1, symbol) if run_dir is not None else None
        cached = _load_v95_symbol_shard(shard, symbol) if shard is not None else None
        if cached is not None:
            frames[symbol] = cached
            resumed += 1
            if progress_cb:
                progress_cb(i, total, symbol)
            continue
        hist = histories.get(symbol) or {}
        near_oi = hist.get("near_oi")
        total_oi = hist.get("total_oi")
        price = cash_histories.get(symbol)
        if not isinstance(price, pd.DataFrame) or price.empty:
            notes[symbol] = "Official NSE historical cash OHLC unavailable; not replaced by current-universe Kite token."
            if progress_cb: progress_cb(i, total, symbol)
            continue
        if not isinstance(total_oi, pd.Series) or total_oi.dropna().empty or not isinstance(near_oi, pd.Series):
            notes[symbol] = "Official NSE total/near FUTSTK OI history unavailable."
            if progress_cb: progress_cb(i, total, symbol)
            continue
        try:
            price = price.copy()
            price.index = pd.to_datetime(price.index).tz_localize(None).normalize()
            price = price[~price.index.duplicated(keep="last")].sort_index()
            price = price.dropna(subset=["open", "high", "low", "close"])
            frame = v95_daily_evidence.build_symbol_daily_frame(
                price, near_oi, expiry_dates=hist.get("near_expiry")
            )
            membership = hist.get("membership")
            if isinstance(membership, pd.Series):
                m = membership.copy(); m.index = pd.to_datetime(m.index).normalize()
                m = m.reindex(frame.index).astype("boolean").fillna(False).astype(bool)
                frame["fno_member_pti"] = m
                frame["eligible"] = frame["eligible"] & m
            else:
                frame["fno_member_pti"] = False
                frame["eligible"] = False
            for col, key in (("nse_total_oi","total_oi"),("nse_near_oi","near_oi"),("nse_next_oi","next_oi"),("nse_far_oi","far_oi"),("nse_near_dte","near_dte"),("nse_lot_size","lot_size")):
                source = hist.get(key)
                if isinstance(source, pd.Series):
                    ser = pd.to_numeric(source.copy(), errors="coerce")
                    ser.index = pd.to_datetime(ser.index).normalize()
                    frame[col] = ser.reindex(frame.index)
            frames[symbol] = frame
            coverage.append({"symbol":symbol,"rows":int(len(frame)),"membership_days":int(frame["fno_member_pti"].sum())})
            if shard is not None:
                _save_v95_symbol_shard(shard, symbol, frame)
        except Exception as exc:  # noqa: BLE001
            log.exception("V9.6 Trial17 frame failed for %s", symbol)
            notes[symbol] = str(exc)
        finally:
            research_runtime.release_memory_pressure()
            if progress_cb: progress_cb(i, total, symbol)

    # Load MWPL only for Trial-17 evidence dates; no later V9.5 discovery/final dates are requested.
    mwpl_result = None
    if frames:
        if stage_cb:
            stage_cb(3, 4, "Loading Trial-17 NSE MWPL / ban controls", 82)
        trial_dates = sorted({pd.Timestamp(d).normalize() for f in frames.values() for d in f.index
                              if v96_trial17.INDEPENDENT_START <= pd.Timestamp(d).tz_localize(None).normalize() <= v96_trial17.INDEPENDENT_END})
        try:
            mwpl_client = nse_mwpl.NSEHistoricalReportClient(cache_dir=_RESEARCH_STATE_DIR / "nse-mwpl")
            mwpl_result = nse_mwpl.build_validation_mwpl_controls(
                validation_dates=trial_dates, symbols=list(frames), client=mwpl_client, min_date_coverage=0.95
            )
        except Exception as exc:  # noqa: BLE001
            mwpl_result = {"available":False,"reason":f"MWPL_LOAD_ERROR:{exc}","date_coverage":0.0,
                           "mwpl_by_symbol":{},"ban_by_symbol":{},"source":"NSE_F&O_COMBINED_OPEN_INTEREST","errors":{"load":str(exc)}}
        controls["mwpl_available"] = bool(mwpl_result.get("available"))
        if controls["mwpl_available"]:
            for symbol, frame in frames.items():
                mws=(mwpl_result.get("mwpl_by_symbol") or {}).get(symbol)
                bans=(mwpl_result.get("ban_by_symbol") or {}).get(symbol)
                if isinstance(mws,pd.Series):
                    x=pd.to_numeric(mws.copy(), errors="coerce"); x.index=pd.to_datetime(x.index).normalize(); frame["mwpl_pct"]=x.reindex(frame.index)
                if isinstance(bans,pd.Series):
                    x=bans.copy(); x.index=pd.to_datetime(x.index).normalize(); frame["ban_flag"]=x.reindex(frame.index).fillna(False).astype(bool)

    if stage_cb:
        stage_cb(4, 4, "Evaluating frozen Trial 17 independent evidence gates", 94)
    research = v96_trial17.evaluate_trial17(frames, controls=controls) if frames else v96_trial17.evaluate_trial17({}, controls=controls)
    if frames and not archive_ok and not str(research.get("status") or "").startswith("FAIL_"):
        research["status"] = "INCONCLUSIVE_NSE_HISTORY_COVERAGE"
        research["primary_pass"] = False

    # V9.6.2 promotion controls are separate from frozen Trial 17. They may
    # unlock eligibility for a future Trial 18 preregistration, but never
    # rewrite the Trial-17 result or activate production.
    supplied_earnings = integrity_data.get("earnings_map")
    supplied_regime = integrity_data.get("market_regime")
    event_symbols = list(research.get("event_symbols") or [])
    if supplied_earnings is not None:
        earnings_map = dict(supplied_earnings)
    elif event_symbols and str(research.get("status") or "") == "PASS_INDEPENDENT_VALIDATION":
        if stage_cb:
            stage_cb(4, 4, "Loading NSE financial-result calendar for promotion control", 95)
        earnings_client = nse_earnings_history.NSEEarningsHistoryClient(cache_dir=_RESEARCH_STATE_DIR / "nse-earnings")
        last = {"done": -1}
        def _earnings_progress(done, total, symbol):
            if stage_cb and (done == 0 or done == total or done - last["done"] >= 10):
                stage_cb(4, 4, f"NSE earnings calendar {done}/{total} · {symbol}", min(97, 95 + round((done/max(total,1))*2)))
                last["done"] = done
        earnings_map = nse_earnings_history.build_earnings_map(
            event_symbols,
            v96_trial17.INDEPENDENT_START - pd.tseries.offsets.BDay(7),
            v96_trial17.INDEPENDENT_END + pd.tseries.offsets.BDay(7),
            earnings_client, progress_cb=_earnings_progress,
        )
    else:
        earnings_map = {"_meta": {"loaded_symbols": [], "symbol_coverage": 0.0, "source": "NOT_LOADED_TRIAL17_NOT_PASSED"}}

    if supplied_regime is not None:
        market_regime = pd.DataFrame(supplied_regime).copy()
    elif event_symbols and str(research.get("status") or "") == "PASS_INDEPENDENT_VALIDATION":
        if stage_cb:
            stage_cb(4, 4, "Loading India VIX + NIFTY regime controls", 98)
        regime_client = nse_market_regime.NSEMarketRegimeClient(cache_dir=_RESEARCH_STATE_DIR / "nse-market-regime")
        market_regime = regime_client.fetch(v96_trial17.INDEPENDENT_START - pd.Timedelta(days=45), v96_trial17.INDEPENDENT_END)
    else:
        market_regime = pd.DataFrame()

    promotion = v96_trial17.evaluate_promotion_controls(
        frames, frozen_result=research, controls=controls, earnings_map=earnings_map,
        market_regime=market_regime,
    ) if frames else {"status": "INCONCLUSIVE_NO_DATA", "trial18_eligible": False, "research_only": True}

    return {
        "build": v96_trial17.BUILD_ID,
        "symbols_scanned": len(research_symbols),
        "symbols_completed": len(frames),
        "symbols_skipped": notes,
        "coverage": coverage,
        "integrity": {
            **controls,
            "historical_oi_primary": "NSE_TOTAL_FUTSTK_OI_SHARE_EQUIVALENT",
            "nse_oi_date_coverage": date_coverage,
            "nse_oi_coverage_ok": archive_ok,
            "historical_symbols_discovered": len(discovered),
            "historical_membership_available": membership_ok,
            "historical_cash_price_available": historical_cash_ok,
            "historical_membership_price_coverage": membership_price_coverage,
            "historical_cash_date_coverage": cash_date_coverage,
            "historical_cash_source": cash_meta.get("source") or "NSE_OFFICIAL_CM_LEGACY_BHAVCOPY",
            "independent_window": {"start":str(v96_trial17.INDEPENDENT_START.date()),"end":str(v96_trial17.INDEPENDENT_END.date())},
            "mwpl_date_coverage": float((mwpl_result or {}).get("date_coverage") or 0.0),
            "mwpl_reason": (mwpl_result or {}).get("reason") or "UNAVAILABLE",
            "resumed_symbol_shards": resumed,
            "prior_locked_finals_read": False,
        },
        "research": research,
        "promotion_controls": promotion,
        "promotion_status": promotion.get("status"),
        "trial18_eligible": bool(promotion.get("trial18_eligible")),
        "research_only": True,
    }

def _v97_recent_mwpl_incomplete_result(mwpl_result):
    """Return only scalar diagnostics when recent-window MWPL is incomplete.

    The raw MWPL payload contains pandas Series for per-symbol limits/ban
    flags and must never be embedded in durable/UI research state.
    """
    mwpl_result = dict(mwpl_result or {})
    def _num(key):
        try:
            return float(mwpl_result.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return {
        "status": "INCONCLUSIVE_RECENT_MWPL",
        "non_load_bearing": False,
        "reason": str(mwpl_result.get("reason") or "MWPL_UNAVAILABLE"),
        "mwpl_date_coverage": _num("date_coverage"),
        "mwpl_month_coverage": _num("month_coverage"),
        "mwpl_observation_coverage": _num("observation_coverage"),
        "secban_risk_date_coverage": _num("secban_risk_date_coverage"),
        "mwpl_source": str(mwpl_result.get("source") or "UNAVAILABLE"),
    }


def _build_v97_recent_mwpl_bound(*, symbols, stage_cb=None):
    """Bound ban/MWPL sensitivity on the 2021-2023 independent window.

    This never invokes the closed Trial-17 evaluator. It rebuilds only the
    already-frozen extreme-OI event rows from official cached NSE daily data.
    """
    start = v96_trial17.INDEPENDENT_START
    end = v96_trial17.INDEPENDENT_END
    warmup = start - pd.Timedelta(days=180)
    days = pd.bdate_range(warmup, end)
    try:
        if stage_cb: stage_cb(4,4,"Bounding MWPL/ban impact on 2021-2023 window",96)
        fo_client = nse_futures_history.NSEFuturesArchiveClient(cache_dir=_RESEARCH_STATE_DIR/"nse-fo-bhavcopy")
        histories = nse_futures_history.build_symbol_histories(days, symbols, fo_client, discover_historical=True)
        discovered = sorted(k for k in histories if k != "_meta")
        if not discovered:
            return {"status":"INCONCLUSIVE_RECENT_HISTORY","non_load_bearing":False,"reason":"NO_HISTORICAL_SYMBOLS"}
        cash_client = nse_cash_history.NSECashArchiveClient(cache_dir=_RESEARCH_STATE_DIR/"nse-cm-bhavcopy")
        cash = nse_cash_history.build_symbol_price_histories(days, discovered, cash_client)
        prepared = []; total_oi_by_symbol = {}; raw_frames = {}
        for symbol in discovered:
            hist = histories.get(symbol) or {}; price = cash.get(symbol)
            total_oi = hist.get("total_oi"); near_oi = hist.get("near_oi")
            if not isinstance(price,pd.DataFrame) or price.empty or not isinstance(total_oi,pd.Series) or not isinstance(near_oi,pd.Series):
                continue
            price = price.copy(); price.index = pd.to_datetime(price.index).tz_localize(None).normalize(); price = price[~price.index.duplicated(keep="last")].sort_index()
            try:
                frame = v95_daily_evidence.build_symbol_daily_frame(price, near_oi, expiry_dates=hist.get("near_expiry"))
            except Exception:
                continue
            membership = hist.get("membership")
            if isinstance(membership,pd.Series):
                m=membership.copy(); m.index=pd.to_datetime(m.index).normalize(); m=m.reindex(frame.index).astype("boolean").fillna(False).astype(bool); frame["fno_member_pti"]=m; frame["eligible"]=frame["eligible"]&m
            else:
                frame["fno_member_pti"]=False; frame["eligible"]=False
            for col,key in (("nse_total_oi","total_oi"),("nse_near_oi","near_oi"),("nse_next_oi","next_oi"),("nse_far_oi","far_oi"),("nse_near_dte","near_dte")):
                source=hist.get(key)
                if isinstance(source,pd.Series):
                    ser=pd.to_numeric(source.copy(),errors="coerce"); ser.index=pd.to_datetime(ser.index).normalize(); frame[col]=ser.reindex(frame.index)
            raw_frames[symbol]=frame
            ser=pd.to_numeric(total_oi.copy(),errors="coerce"); ser.index=pd.to_datetime(ser.index).normalize(); total_oi_by_symbol[symbol]=ser
        if not raw_frames:
            return {"status":"INCONCLUSIVE_RECENT_HISTORY","non_load_bearing":False,"reason":"NO_USABLE_FRAMES"}
        trial_dates = pd.bdate_range(start, end)
        mwpl_client = nse_mwpl.NSEHistoricalReportClient(cache_dir=_RESEARCH_STATE_DIR/"nse-mwpl")
        mw = nse_mwpl.build_monthly_mwpl_controls(validation_dates=trial_dates, symbols=list(raw_frames), total_oi_by_symbol=total_oi_by_symbol, client=mwpl_client, min_date_coverage=0.95)
        if not mw.get("available"):
            return _v97_recent_mwpl_incomplete_result(mw)
        for symbol,frame in raw_frames.items():
            x=v953_contract_structure.build_contract_structure_frame(frame).copy()
            x["date"]=pd.DatetimeIndex(x.index).tz_localize(None).normalize(); x=x[(x["date"]>=start)&(x["date"]<=end)].copy()
            if x.empty: continue
            x["symbol"]=symbol
            eligible=x.get("eligible",True)
            if not isinstance(eligible,pd.Series): eligible=pd.Series(bool(eligible),index=x.index)
            if "fno_member_pti" in x: eligible=eligible.fillna(False).astype(bool)&x["fno_member_pti"].fillna(False).astype(bool)
            x["trial19_eligible"]=eligible.fillna(False).astype(bool)
            x["extreme_oi_event"]=(pd.to_numeric(x.get("total_z"),errors="coerce")>=v97_trial19.TOTAL_OI_Z_MIN).fillna(False).astype(bool)
            x["dte_bucket"]=v97_trial19._dte_bucket(x)
            mws=(mw.get("mwpl_by_symbol") or {}).get(symbol); bans=(mw.get("ban_by_symbol") or {}).get(symbol)
            if isinstance(mws,pd.Series):
                q=pd.to_numeric(mws.copy(),errors="coerce"); q.index=pd.to_datetime(q.index).normalize(); x["mwpl_pct"]=q.reindex(pd.to_datetime(x["date"]).dt.normalize()).to_numpy()
            if isinstance(bans,pd.Series):
                q=bans.copy(); q.index=pd.to_datetime(q.index).normalize(); x["ban_flag"]=q.reindex(pd.to_datetime(x["date"]).dt.normalize()).fillna(False).to_numpy(dtype=bool)
            prepared.append(x.reset_index(drop=True))
        if not prepared:
            return {"status":"INCONCLUSIVE_RECENT_HISTORY","non_load_bearing":False,"reason":"NO_PREPARED_ROWS"}
        result=v97_trial19.evaluate_mwpl_bound(pd.concat(prepared,ignore_index=True),bootstrap_reps=300)
        result["window"]={"start":str(start.date()),"end":str(end.date())}; result["mwpl_source"]=mw.get("source"); result["mwpl_date_coverage"]=mw.get("date_coverage")
        return result
    except Exception as exc:
        log.exception("V9.7.2 recent MWPL bound failed")
        return {"status":"INCONCLUSIVE_RECENT_MWPL_BOUND_ERROR","non_load_bearing":False,"reason":str(exc)}


def run_v97_trial19(kite, symbols=None, progress_cb=None, integrity_data=None,
                     resume_run_dir=None, stage_cb=None) -> dict:
    """Run frozen nonlinear Trial 19 on official NSE 2018-2021 evidence."""
    symbols=[str(x).strip().upper() for x in (symbols or settings.WATCHLIST)]
    integrity_data=dict(integrity_data or {})
    warmup_start=v97_trial19.INDEPENDENT_START-pd.Timedelta(days=180)
    archive_end=v97_trial19.INDEPENDENT_END
    cash_end=archive_end+pd.Timedelta(days=7)
    archive_days=pd.bdate_range(warmup_start,archive_end)
    if stage_cb: stage_cb(1,4,"Loading official NSE Trial-19 third-window archive",4)

    supplied_histories=integrity_data.get("nse_history_by_symbol")
    if supplied_histories:
        histories=dict(supplied_histories); histories.setdefault("_meta",dict(integrity_data.get("nse_history_meta") or {}))
    else:
        client=nse_futures_history.NSEFuturesArchiveClient(cache_dir=_RESEARCH_STATE_DIR/"nse-fo-bhavcopy")
        last={"done":-1}
        def _archive_progress(done,total,label):
            if stage_cb and (done==0 or done==total or done-last["done"]>=20):
                stage_cb(1,4,f"NSE Trial19 archive {done}/{total} · {label}",min(28,4+round((done/max(total,1))*24))); last["done"]=done
        histories=nse_futures_history.build_symbol_histories(archive_days,symbols,client,progress_cb=_archive_progress,discover_historical=True)
    meta=dict(histories.get("_meta") or {}); date_coverage=float(meta.get("date_coverage") or 0.0); archive_ok=bool(date_coverage>=0.95)
    discovered=sorted(k for k in histories if k!="_meta"); research_symbols=discovered or symbols; membership_ok=bool(archive_ok and discovered)

    cash_days=pd.bdate_range(warmup_start,cash_end)
    supplied_cash=integrity_data.get("nse_cash_by_symbol")
    if supplied_cash:
        cash_histories=dict(supplied_cash); cash_histories.setdefault("_meta",dict(integrity_data.get("nse_cash_meta") or {}))
    else:
        cash_client=nse_cash_history.NSECashArchiveClient(cache_dir=_RESEARCH_STATE_DIR/"nse-cm-bhavcopy")
        last={"done":-1}
        def _cash_progress(done,total,label):
            if stage_cb and (done==0 or done==total or done-last["done"]>=20):
                stage_cb(2,4,f"NSE Trial19 cash archive {done}/{total} · {label}",min(48,29+round((done/max(total,1))*19))); last["done"]=done
        cash_histories=nse_cash_history.build_symbol_price_histories(cash_days,research_symbols,cash_client,progress_cb=_cash_progress)
    cash_meta=dict(cash_histories.get("_meta") or {}); cash_date_coverage=float(cash_meta.get("date_coverage") or 0.0); cash_archive_ok=bool(cash_date_coverage>=0.95)
    member_points=0; priced_member_points=0
    for symbol in research_symbols:
        hist=histories.get(symbol) or {}; membership=hist.get("membership"); price=cash_histories.get(symbol)
        if not isinstance(membership,pd.Series) or not isinstance(price,pd.DataFrame): continue
        m=membership.copy(); m.index=pd.to_datetime(m.index).normalize(); m=m[(m.index>=v97_trial19.INDEPENDENT_START)&(m.index<=v97_trial19.INDEPENDENT_END)].fillna(False).astype(bool)
        closes=pd.to_numeric(price.get("close"),errors="coerce"); closes.index=pd.to_datetime(closes.index).normalize(); closes=closes.reindex(m.index)
        member_points+=int(m.sum()); priced_member_points+=int((m&closes.notna()).sum())
    membership_price_coverage=float(priced_member_points/member_points) if member_points else 0.0
    historical_cash_ok=bool(cash_archive_ok and member_points>0 and membership_price_coverage>=0.95)
    controls={"historical_membership_available":membership_ok,"historical_cash_price_available":historical_cash_ok,"lot_size_normalization_available":archive_ok,"mwpl_available":False}

    frames={}; notes={}; coverage=[]; total=len(research_symbols); resumed=0
    run_dir=Path(resume_run_dir) if resume_run_dir is not None else None
    if run_dir is not None: run_dir.mkdir(parents=True,exist_ok=True)
    if stage_cb: stage_cb(2,4,"Building Trial-19 cash + total-OI frames",30)
    for i,symbol in enumerate(research_symbols,start=1):
        if progress_cb: progress_cb(i-1,total,symbol)
        shard=_v95_symbol_shard_path(run_dir,i-1,symbol) if run_dir is not None else None
        cached=_load_v95_symbol_shard(shard,symbol) if shard is not None else None
        if cached is not None:
            frames[symbol]=cached; resumed+=1
            if progress_cb: progress_cb(i,total,symbol)
            continue
        hist=histories.get(symbol) or {}; near_oi=hist.get("near_oi"); total_oi=hist.get("total_oi"); price=cash_histories.get(symbol)
        if not isinstance(price,pd.DataFrame) or price.empty:
            notes[symbol]="Official NSE historical cash OHLC unavailable."; 
            if progress_cb: progress_cb(i,total,symbol)
            continue
        if not isinstance(total_oi,pd.Series) or total_oi.dropna().empty or not isinstance(near_oi,pd.Series):
            notes[symbol]="Official NSE total/near FUTSTK OI history unavailable."; 
            if progress_cb: progress_cb(i,total,symbol)
            continue
        try:
            price=price.copy(); price.index=pd.to_datetime(price.index).tz_localize(None).normalize(); price=price[~price.index.duplicated(keep="last")].sort_index(); price=price.dropna(subset=["open","high","low","close"])
            frame=v95_daily_evidence.build_symbol_daily_frame(price,near_oi,expiry_dates=hist.get("near_expiry"))
            membership=hist.get("membership")
            if isinstance(membership,pd.Series):
                m=membership.copy(); m.index=pd.to_datetime(m.index).normalize(); m=m.reindex(frame.index).astype("boolean").fillna(False).astype(bool); frame["fno_member_pti"]=m; frame["eligible"]=frame["eligible"]&m
            else:
                frame["fno_member_pti"]=False; frame["eligible"]=False
            for col,key in (("nse_total_oi","total_oi"),("nse_near_oi","near_oi"),("nse_next_oi","next_oi"),("nse_far_oi","far_oi"),("nse_near_dte","near_dte"),("nse_lot_size","lot_size")):
                source=hist.get(key)
                if isinstance(source,pd.Series):
                    ser=pd.to_numeric(source.copy(),errors="coerce"); ser.index=pd.to_datetime(ser.index).normalize(); frame[col]=ser.reindex(frame.index)
            frames[symbol]=frame; coverage.append({"symbol":symbol,"rows":int(len(frame)),"membership_days":int(frame["fno_member_pti"].sum())})
            if shard is not None: _save_v95_symbol_shard(shard,symbol,frame)
        except Exception as exc:
            log.exception("V9.7.2 Trial19 frame failed for %s",symbol); notes[symbol]=str(exc)
        finally:
            research_runtime.release_memory_pressure()
            if progress_cb: progress_cb(i,total,symbol)

    mwpl_result=None
    if frames:
        if stage_cb: stage_cb(3,4,"Loading Trial-19 NSE monthly MWPL + targeted ban controls",82)
        trial_dates=sorted({pd.Timestamp(d).normalize() for f in frames.values() for d in f.index if v97_trial19.INDEPENDENT_START<=pd.Timestamp(d).tz_localize(None).normalize()<=v97_trial19.INDEPENDENT_END})
        try:
            mwpl_client=nse_mwpl.NSEHistoricalReportClient(cache_dir=_RESEARCH_STATE_DIR/"nse-mwpl")
            total_oi_by_symbol={}
            for symbol, frame in frames.items():
                if "nse_total_oi" in frame:
                    ser=pd.to_numeric(frame["nse_total_oi"],errors="coerce").copy()
                    ser.index=pd.to_datetime(ser.index).normalize()
                    total_oi_by_symbol[str(symbol).upper()]=ser
            last_mwpl={"done":-1}
            def _mwpl_progress(done,total,label):
                if stage_cb and (done==0 or done==total or done-last_mwpl["done"]>=1):
                    pct=min(93,82+round((done/max(total,1))*11))
                    stage_cb(3,4,f"Trial-19 MWPL months {done}/{total} · {label}",pct)
                    last_mwpl["done"]=done
            mwpl_result=nse_mwpl.build_monthly_mwpl_controls(
                validation_dates=trial_dates,symbols=list(frames),total_oi_by_symbol=total_oi_by_symbol,
                client=mwpl_client,min_date_coverage=0.95,progress_cb=_mwpl_progress,
            )
        except Exception as exc:
            mwpl_result={"available":False,"reason":f"MWPL_LOAD_ERROR:{exc}","date_coverage":0.0,"month_coverage":0.0,"observation_coverage":0.0,"mwpl_by_symbol":{},"ban_by_symbol":{},"source":"NSE_MONTHLY_MWPL_PLUS_RECONSTRUCTED_TOTAL_FUTSTK_OI","errors":{"load":str(exc)}}
        controls["mwpl_available"]=bool(mwpl_result.get("available"))
        if controls["mwpl_available"]:
            for symbol,frame in frames.items():
                mws=(mwpl_result.get("mwpl_by_symbol") or {}).get(symbol); bans=(mwpl_result.get("ban_by_symbol") or {}).get(symbol)
                if isinstance(mws,pd.Series):
                    x=pd.to_numeric(mws.copy(),errors="coerce"); x.index=pd.to_datetime(x.index).normalize(); frame["mwpl_pct"]=x.reindex(frame.index)
                if isinstance(bans,pd.Series):
                    x=bans.copy(); x.index=pd.to_datetime(x.index).normalize(); frame["ban_flag"]=x.reindex(frame.index).fillna(False).astype(bool)

    if stage_cb: stage_cb(4,4,"Evaluating frozen Trial 19 nonlinear evidence gates",94)
    research=v97_trial19.evaluate_trial19(frames,controls=controls) if frames else v97_trial19.evaluate_trial19({},controls=controls)
    if frames and not archive_ok and not str(research.get("status") or "").startswith("FAIL_"):
        research["status"]="INCONCLUSIVE_NSE_HISTORY_COVERAGE"; research["primary_pass"]=False

    # V9.7.2 confound controls run after frozen efficacy, not after MWPL.
    volatility_control=v97_trial19.evaluate_volatility_confound(frames,frozen_result=research) if frames else {"status":"INCONCLUSIVE_NO_DATA","pass":False,"research_only":True}
    supplied_earnings=integrity_data.get("earnings_map"); event_symbols=list(research.get("event_symbols") or [])
    if supplied_earnings is not None:
        earnings_map=dict(supplied_earnings)
    elif event_symbols and v97_trial19.trial19_efficacy_passed(research):
        if stage_cb: stage_cb(4,4,"Loading NSE board/result calendar for Trial-19 confound",97)
        earnings_client=nse_earnings_history.NSEEarningsHistoryClient(cache_dir=_RESEARCH_STATE_DIR/"nse-earnings")
        earnings_map=nse_earnings_history.build_earnings_map(event_symbols,v97_trial19.INDEPENDENT_START-pd.tseries.offsets.BDay(7),v97_trial19.INDEPENDENT_END+pd.tseries.offsets.BDay(7),earnings_client)
    else:
        earnings_map={"_meta":{"loaded_symbols":[],"symbol_coverage":0.0,"source":"NOT_LOADED_EFFICACY_FAILED"}}
    earnings_control=v97_trial19.evaluate_earnings_promotion(frames,frozen_result=research,earnings_map=earnings_map) if frames else {"status":"INCONCLUSIVE_NO_DATA","trial18_eligible":False,"confound_pass":False,"research_only":True}
    supplied_bound=integrity_data.get("recent_mwpl_bound")
    if controls.get("mwpl_available"):
        recent_bound={"status":"NOT_NEEDED_HISTORICAL_MWPL_APPLIED","non_load_bearing":False}
    elif supplied_bound is not None:
        recent_bound=dict(supplied_bound)
    elif v97_trial19.trial19_efficacy_passed(research):
        recent_bound=_build_v97_recent_mwpl_bound(symbols=symbols,stage_cb=stage_cb)
    else:
        recent_bound={"status":"LOCKED_EFFICACY_FAILED","non_load_bearing":False}
    eligibility=v97_trial19.evaluate_trial18_eligibility(frozen_result=research,volatility_control=volatility_control,earnings_control=earnings_control,integrity_controls=controls,recent_mwpl_bound=recent_bound)
    return {
        "build":v97_trial19.BUILD_ID,"symbols_scanned":len(research_symbols),"symbols_completed":len(frames),"symbols_skipped":notes,"coverage":coverage,
        "integrity":{**controls,"historical_oi_primary":"NSE_TOTAL_FUTSTK_OI_SHARE_EQUIVALENT","nse_oi_date_coverage":date_coverage,"nse_oi_coverage_ok":archive_ok,"historical_symbols_discovered":len(discovered),"historical_membership_available":membership_ok,"historical_cash_price_available":historical_cash_ok,"historical_membership_price_coverage":membership_price_coverage,"historical_cash_date_coverage":cash_date_coverage,"historical_cash_source":cash_meta.get("source") or "NSE_OFFICIAL_CM_LEGACY_BHAVCOPY","independent_window":{"start":str(v97_trial19.INDEPENDENT_START.date()),"end":str(v97_trial19.INDEPENDENT_END.date())},"mwpl_date_coverage":float((mwpl_result or {}).get("date_coverage") or 0.0),"mwpl_month_coverage":float((mwpl_result or {}).get("month_coverage") or 0.0),"mwpl_observation_coverage":float((mwpl_result or {}).get("observation_coverage") or 0.0),"mwpl_source":(mwpl_result or {}).get("source") or "UNAVAILABLE","secban_risk_date_coverage":float((mwpl_result or {}).get("secban_risk_date_coverage") or 0.0),"mwpl_reason":(mwpl_result or {}).get("reason") or "UNAVAILABLE","resumed_symbol_shards":resumed,"prior_locked_finals_read":False},
        "research":research,
        "confound_controls":{"volatility":volatility_control,"earnings":earnings_control,"recent_mwpl_bound":recent_bound},
        "promotion_controls":eligibility,
        "trial18_eligible":bool(eligibility.get("trial18_eligible")),
        "research_only":True,
    }


def _default_v95_daily_state():
    return {
        "status": "idle",
        "mode": "v95_daily",
        "research_only": True,
        "progress": {"done": 0, "total": 0, "symbol": None, "stage": None, "stage_index": 0, "stage_total": 4, "overall_pct": 0},
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
        "params": {"days": 1095, "resume_run_dir": None},
        "worker": {},
    }


def _atomic_write_v95_daily_state(state):
    path = Path(_V95_DAILY_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, default=_research_json_default, allow_nan=True, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _load_v95_daily_state():
    path = Path(_V95_DAILY_STATE_PATH)
    if not path.exists():
        return _default_v95_daily_state()
    try:
        with path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not restore V9.5 daily state: %s", exc)
        return _default_v95_daily_state()
    base = _default_v95_daily_state()
    if isinstance(state, dict):
        base.update(state)
        if isinstance(state.get("progress"), dict):
            base["progress"].update(state["progress"])
        if isinstance(state.get("params"), dict):
            base["params"].update(state["params"])
    if base.get("status") == "running":
        run_dir_raw = (base.get("params") or {}).get("resume_run_dir")
        run_dir = Path(run_dir_raw) if run_dir_raw else None
        durable = bool(run_dir and run_dir.exists() and any(run_dir.glob("*.pkl")))
        base["status"] = "error"
        base["error"] = (
            "V9.5 daily evidence was interrupted by a worker restart. Durable symbol checkpoints were found; "
            "run the same V9.5 lab again to resume."
            if durable else
            "V9.5 daily evidence was interrupted by a worker restart and no durable symbol checkpoints were found. "
            "Configure RESEARCH_STATE_DIR on a persistent Railway Volume to survive host replacement."
        )
        base["finished_at"] = now_ist().isoformat(timespec="seconds")
        try:
            _atomic_write_v95_daily_state(base)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist interrupted V9.5 state: %s", exc)
    return base


_v95_daily_lock = threading.Lock()
_v95_daily_state = _load_v95_daily_state()


def _persist_v95_daily_state():
    with _v95_daily_lock:
        snapshot = dict(_v95_daily_state)
        snapshot["progress"] = dict(_v95_daily_state.get("progress") or {})
        snapshot["params"] = dict(_v95_daily_state.get("params") or {})
    try:
        _atomic_write_v95_daily_state(snapshot)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist V9.5 daily state: %s", exc)


def get_v95_daily_oi_state():
    with _v95_daily_lock:
        out = dict(_v95_daily_state)
        out["progress"] = dict(_v95_daily_state.get("progress") or {})
        out["params"] = dict(_v95_daily_state.get("params") or {})
    out["worker"] = research_runtime.snapshot()
    return out


def start_v95_daily_oi_evidence(kite, symbols=None, days=1095, integrity_data=None):
    symbols = list(symbols or settings.WATCHLIST)
    days = max(1095, min(int(days or 1095), 3650))
    if not symbols:
        return {"started": False, "reason": "No F&O symbols supplied for V9.5."}
    with _v95_daily_lock:
        if _v95_daily_state.get("status") == "running":
            return {"started": False, "reason": "V9.5 Daily OI Evidence Lab is already running."}
        if research_runtime.is_research_active():
            return {"started": False, "reason": "Another historical research job is already running."}
        run_dir = _v95_daily_run_dir(symbols=symbols, days=days)
        resumed_done = sum(1 for _ in run_dir.glob("*.pkl"))
        _v95_daily_state.update({
            "status": "running",
            "mode": "v95_daily",
            "research_only": True,
            "progress": {
                "done": resumed_done,
                "total": len(symbols),
                "symbol": None,
                "stage": "Loading official NSE 3+ year stock-futures OI archive",
                "stage_index": 1,
                "stage_total": 4,
                "overall_pct": max(1, min(90, round((resumed_done / len(symbols)) * 90))) if symbols else 1,
                "resume_summary": {"completed_symbol_shards": resumed_done, "total_symbols": len(symbols)},
            },
            "result": None,
            "error": None,
            "started_at": now_ist().isoformat(timespec="seconds"),
            "finished_at": None,
            "params": {"days": days, "resume_run_dir": str(run_dir)},
        })
    _persist_v95_daily_state()

    def _progress(done, total, symbol):
        research_runtime.heartbeat(stage="Building V9.5 NSE daily evidence frames", symbol=symbol, done=done, total=total)
        with _v95_daily_lock:
            resume_summary = (_v95_daily_state.get("progress") or {}).get("resume_summary")
            _v95_daily_state["progress"] = {
                "done": int(done), "total": int(total), "symbol": symbol,
                "stage": "Building daily cash + NSE near-month OI evidence frames",
                "stage_index": 2, "stage_total": 4,
                "overall_pct": max(31, min(80, 31 + round((done / total) * 49))) if total else 31,
                "resume_summary": resume_summary,
            }
        if done == 0 or done == total or (done > 0 and done % 5 == 0):
            _persist_v95_daily_state()

    def _stage(stage_index, stage_total, stage, overall_pct):
        research_runtime.heartbeat(stage=stage)
        with _v95_daily_lock:
            current = _v95_daily_state.get("progress") or {}
            _v95_daily_state["progress"] = {
                "done": current.get("done", 0), "total": current.get("total", len(symbols)), "symbol": None,
                "stage": stage, "stage_index": int(stage_index), "stage_total": int(stage_total),
                "overall_pct": int(overall_pct), "resume_summary": current.get("resume_summary"),
            }
        _persist_v95_daily_state()

    def _job():
        try:
            with research_runtime.research_slot():
                research_runtime.heartbeat(stage="V9.5 worker acquired exclusive heavy-work slot")
                result = run_v95_daily_oi_evidence(
                    kite, symbols=symbols, days=days, progress_cb=_progress,
                    integrity_data=integrity_data, resume_run_dir=run_dir, stage_cb=_stage,
                )
            with _v95_daily_lock:
                _v95_daily_state["progress"] = {
                    "done": len(symbols), "total": len(symbols), "symbol": None,
                    "stage": "Complete", "stage_index": 4, "stage_total": 4, "overall_pct": 100,
                    "resume_summary": {"completed_symbol_shards": len(symbols), "total_symbols": len(symbols)},
                }
                _v95_daily_state["result"] = result
                _v95_daily_state["status"] = "done"
                _v95_daily_state["error"] = None
                _v95_daily_state["finished_at"] = now_ist().isoformat(timespec="seconds")
            _persist_v95_daily_state()
            shutil.rmtree(run_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("V9.5 daily evidence run failed")
            with _v95_daily_lock:
                _v95_daily_state["status"] = "error"
                _v95_daily_state["error"] = str(exc)
                _v95_daily_state["finished_at"] = now_ist().isoformat(timespec="seconds")
            _persist_v95_daily_state()
        finally:
            research_runtime.end_research()
            research_runtime.release_memory_pressure()

    research_runtime.begin_research("v95_daily")
    threading.Thread(target=_job, daemon=True).start()
    return {"started": True, "mode": "v95_daily", "resumed_symbol_shards": resumed_done}



def _default_v96_state():
    return {
        "status": "idle", "mode": "v96_trial17", "research_only": True,
        "progress": {"done":0,"total":0,"symbol":None,"stage":None,"stage_index":0,"stage_total":4,"overall_pct":0},
        "result": None, "error": None, "started_at": None, "finished_at": None,
        "params": {"resume_run_dir": None}, "worker": {},
    }


def _atomic_write_v96_state(state):
    path = Path(_V96_STATE_PATH); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, default=_research_json_default, allow_nan=True, separators=(",", ":"))
        fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)


def _load_v96_state():
    path = Path(_V96_STATE_PATH)
    if not path.exists(): return _default_v96_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_v96_state()
    base = _default_v96_state()
    if isinstance(state, dict):
        base.update(state)
        if isinstance(state.get("progress"), dict): base["progress"].update(state["progress"])
        if isinstance(state.get("params"), dict): base["params"].update(state["params"])
    if base.get("status") == "running":
        run_dir_raw=(base.get("params") or {}).get("resume_run_dir")
        run_dir=Path(run_dir_raw) if run_dir_raw else None
        durable=bool(run_dir and run_dir.exists() and any(run_dir.glob("*.pkl")))
        base["status"]="error"
        base["error"]=("V9.6 Trial 17 worker restarted before completion. Durable symbol checkpoints were found; run again to resume." if durable else "V9.6 Trial 17 worker restarted before completion; no durable symbol checkpoint was found.")
    return base


_v96_lock = threading.Lock()
_v96_state = _load_v96_state()


def _persist_v96_state():
    with _v96_lock:
        snapshot=dict(_v96_state); snapshot["progress"]=dict(_v96_state.get("progress") or {}); snapshot["params"]=dict(_v96_state.get("params") or {})
    snapshot["worker"] = research_runtime.snapshot()
    _atomic_write_v96_state(snapshot)


def get_v96_trial17_state():
    with _v96_lock:
        out=dict(_v96_state); out["progress"]=dict(_v96_state.get("progress") or {}); out["params"]=dict(_v96_state.get("params") or {})
    out["worker"] = research_runtime.snapshot()
    return out


def start_v96_trial17(kite, symbols=None, integrity_data=None):
    symbols=[str(s).strip().upper() for s in (symbols or settings.WATCHLIST)]
    with _v96_lock:
        if _v96_state.get("status") == "running":
            return {"started":False,"reason":"V9.6 Trial 17 is already running."}
        if research_runtime.is_research_active():
            return {"started":False,"reason":"Another historical research job is already running."}
        run_dir=_v96_run_dir(symbols=symbols)
        resumed=sum(1 for _ in run_dir.glob("*.pkl"))
        _v96_state.update({
            "status":"running","mode":"v96_trial17","research_only":True,
            "progress":{"done":resumed,"total":len(symbols),"symbol":None,"stage":"Loading official NSE Trial-17 independent-history archive","stage_index":1,"stage_total":4,"overall_pct":max(1,min(80,round(resumed/max(len(symbols),1)*80)))},
            "result":None,"error":None,"started_at":now_ist().isoformat(timespec="seconds"),"finished_at":None,
            "params":{"resume_run_dir":str(run_dir)},
        })
    _persist_v96_state()

    def _progress(done,total,symbol):
        research_runtime.heartbeat(stage="Building V9.6 Trial-17 frames", symbol=symbol, done=done, total=total)
        with _v96_lock:
            cur=_v96_state.get("progress") or {}
            _v96_state["progress"]={"done":int(done),"total":int(total),"symbol":symbol,"stage":"Building Trial-17 cash + total-OI frames","stage_index":2,"stage_total":4,"overall_pct":max(30,min(80,30+round(done/max(total,1)*50)))}
        if done==0 or done==total or (done>0 and done%5==0): _persist_v96_state()

    def _stage(stage_index,stage_total,stage,overall_pct):
        research_runtime.heartbeat(stage=stage)
        with _v96_lock:
            cur=_v96_state.get("progress") or {}
            _v96_state["progress"]={"done":cur.get("done",0),"total":cur.get("total",len(symbols)),"symbol":None,"stage":stage,"stage_index":int(stage_index),"stage_total":int(stage_total),"overall_pct":int(overall_pct)}
        _persist_v96_state()

    def _job():
        try:
            with research_runtime.research_slot():
                result=run_v96_trial17(kite,symbols=symbols,progress_cb=_progress,integrity_data=integrity_data,resume_run_dir=run_dir,stage_cb=_stage)
            with _v96_lock:
                _v96_state["progress"]={"done":result.get("symbols_scanned",len(symbols)),"total":result.get("symbols_scanned",len(symbols)),"symbol":None,"stage":"Complete","stage_index":4,"stage_total":4,"overall_pct":100}
                _v96_state["result"]=result; _v96_state["status"]="done"; _v96_state["error"]=None; _v96_state["finished_at"]=now_ist().isoformat(timespec="seconds")
            _persist_v96_state(); shutil.rmtree(run_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("V9.6 Trial17 run failed")
            with _v96_lock:
                _v96_state["status"]="error"; _v96_state["error"]=str(exc); _v96_state["finished_at"]=now_ist().isoformat(timespec="seconds")
            _persist_v96_state()
        finally:
            research_runtime.end_research(); research_runtime.release_memory_pressure()

    research_runtime.begin_research("v96_trial17")
    threading.Thread(target=_job, daemon=True).start()
    return {"started":True,"mode":"v96_trial17","resumed_symbol_shards":resumed}

def _default_v97_state():
    return {"status":"idle","mode":"v97_trial19","research_only":True,"progress":{"done":0,"total":0,"symbol":None,"stage":None,"stage_index":0,"stage_total":4,"overall_pct":0},"result":None,"error":None,"started_at":None,"finished_at":None,"params":{"resume_run_dir":None},"worker":{}}


def _research_state_json_safe(value):
    """Recursively convert research state to JSON-safe builtins.

    This is a last-resort persistence/render boundary. Research evaluators
    should still return compact scalar diagnostics rather than raw frames.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (pd.Timestamp, dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Series):
        return [_research_state_json_safe(v) for v in value.tolist()]
    if isinstance(value, (pd.Index, np.ndarray)):
        return [_research_state_json_safe(v) for v in value.tolist()]
    if isinstance(value, pd.DataFrame):
        return [_research_state_json_safe(row) for row in value.to_dict(orient="records")]
    if isinstance(value, dict):
        return {str(k): _research_state_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_research_state_json_safe(v) for v in value]
    try:
        json.dumps(value, allow_nan=True)
        return value
    except (TypeError, ValueError):
        return str(value)


def _atomic_write_v97_state(state):
    path=Path(_V97_STATE_PATH); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    safe_state = _research_state_json_safe(state)
    with tmp.open("w",encoding="utf-8") as fh:
        json.dump(safe_state,fh,allow_nan=True,separators=(",",":")); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp,path)


def _load_v97_state():
    path=Path(_V97_STATE_PATH)
    if not path.exists(): return _default_v97_state()
    try: state=json.loads(path.read_text(encoding="utf-8"))
    except Exception: return _default_v97_state()
    base=_default_v97_state()
    if isinstance(state,dict):
        base.update(state)
        if isinstance(state.get("progress"),dict): base["progress"].update(state["progress"])
        if isinstance(state.get("params"),dict): base["params"].update(state["params"])
    if base.get("status")=="running":
        run_dir_raw=(base.get("params") or {}).get("resume_run_dir"); run_dir=Path(run_dir_raw) if run_dir_raw else None; durable=bool(run_dir and run_dir.exists() and any(run_dir.glob("*.pkl")))
        base["status"]="error"; base["error"]=("V9.7.2 Trial 19 worker restarted before completion. Durable checkpoints were found; run again to resume." if durable else "V9.7 Trial 19 worker restarted before completion; no durable checkpoint was found.")
    return base


_v97_lock=threading.Lock(); _v97_state=_load_v97_state()


def _persist_v97_state():
    with _v97_lock:
        snap=dict(_v97_state); snap["progress"]=dict(_v97_state.get("progress") or {}); snap["params"]=dict(_v97_state.get("params") or {})
    snap["worker"]=research_runtime.snapshot(); _atomic_write_v97_state(snap)


def get_v97_trial19_state():
    with _v97_lock:
        out=dict(_v97_state); out["progress"]=dict(_v97_state.get("progress") or {}); out["params"]=dict(_v97_state.get("params") or {})
    out["worker"]=research_runtime.snapshot()
    return _research_state_json_safe(out)


def start_v97_trial19(kite,symbols=None,integrity_data=None):
    symbols=[str(s).strip().upper() for s in (symbols or settings.WATCHLIST)]
    with _v97_lock:
        if _v97_state.get("status")=="running": return {"started":False,"reason":"V9.7.2 Trial 19 is already running."}
        if research_runtime.is_research_active(): return {"started":False,"reason":"Another historical research job is already running."}
        run_dir=_v97_run_dir(symbols=symbols); resumed=sum(1 for _ in run_dir.glob("*.pkl"))
        _v97_state.update({"status":"running","mode":"v97_trial19","research_only":True,"progress":{"done":resumed,"total":len(symbols),"symbol":None,"stage":"Loading official NSE Trial-19 third-window archive","stage_index":1,"stage_total":4,"overall_pct":max(1,min(80,round(resumed/max(len(symbols),1)*80)))},"result":None,"error":None,"started_at":now_ist().isoformat(timespec="seconds"),"finished_at":None,"params":{"resume_run_dir":str(run_dir)}})
    _persist_v97_state()
    def _progress(done,total,symbol):
        research_runtime.heartbeat(stage="Building V9.7.2 Trial-19 frames",symbol=symbol,done=done,total=total)
        with _v97_lock: _v97_state["progress"]={"done":int(done),"total":int(total),"symbol":symbol,"stage":"Building Trial-19 cash + total-OI frames","stage_index":2,"stage_total":4,"overall_pct":max(30,min(80,30+round(done/max(total,1)*50)))}
        if done==0 or done==total or (done>0 and done%5==0): _persist_v97_state()
    def _stage(stage_index,stage_total,stage,overall_pct):
        research_runtime.heartbeat(stage=stage)
        with _v97_lock:
            cur=_v97_state.get("progress") or {}; _v97_state["progress"]={"done":cur.get("done",0),"total":cur.get("total",len(symbols)),"symbol":None,"stage":stage,"stage_index":int(stage_index),"stage_total":int(stage_total),"overall_pct":int(overall_pct)}
        _persist_v97_state()
    def _job():
        try:
            with research_runtime.research_slot(): result=run_v97_trial19(kite,symbols=symbols,progress_cb=_progress,integrity_data=integrity_data,resume_run_dir=run_dir,stage_cb=_stage)
            with _v97_lock:
                _v97_state["progress"]={"done":result.get("symbols_scanned",len(symbols)),"total":result.get("symbols_scanned",len(symbols)),"symbol":None,"stage":"Complete","stage_index":4,"stage_total":4,"overall_pct":100}; _v97_state["result"]=result; _v97_state["status"]="done"; _v97_state["error"]=None; _v97_state["finished_at"]=now_ist().isoformat(timespec="seconds")
            _persist_v97_state(); shutil.rmtree(run_dir,ignore_errors=True)
        except Exception as exc:
            log.exception("V9.7.2 Trial19 run failed")
            with _v97_lock: _v97_state["status"]="error"; _v97_state["error"]=str(exc); _v97_state["finished_at"]=now_ist().isoformat(timespec="seconds")
            _persist_v97_state()
        finally: research_runtime.end_research(); research_runtime.release_memory_pressure()
    research_runtime.begin_research("v97_trial19"); threading.Thread(target=_job,daemon=True).start(); return {"started":True,"mode":"v97_trial19","resumed_symbol_shards":resumed}


def _default_early_research_state():
    return {
        "status": "idle",
        "progress": {"done": 0, "total": 0, "symbol": None, "stage": None, "stage_index": 0, "stage_total": 4, "overall_pct": 0},
        "result": None, "error": None, "started_at": None, "finished_at": None,
        "worker": {},
    }


def _research_json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _atomic_write_early_research_state(state):
    """Stream a complete checkpoint to a temp file then atomically replace it."""
    path = Path(_EARLY_RESEARCH_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, default=_research_json_default, allow_nan=True, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _load_early_research_state():
    path = Path(_EARLY_RESEARCH_STATE_PATH)
    if not path.exists():
        return _default_early_research_state()
    try:
        with path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not restore early research checkpoint: %s", exc)
        return _default_early_research_state()
    base = _default_early_research_state()
    if isinstance(state, dict):
        base.update(state)
        if isinstance(state.get("progress"), dict):
            base["progress"].update(state["progress"])
    if base.get("status") == "running":
        base["status"] = "error"
        run_dir_raw = ((base.get("params") or {}).get("resume_run_dir"))
        run_dir = Path(run_dir_raw) if run_dir_raw else None
        durable = bool(
            run_dir and run_dir.exists() and (
                _v91_ranked_events_path(run_dir).exists()
                or _v91_rank_progress_path(run_dir).exists()
                or any(run_dir.glob("*.pkl"))
            )
        )
        if durable:
            base["error"] = (
                "Research job was interrupted by a worker restart before completion. Durable checkpoint files were found; "
                "run the same lab again and it will attempt to resume from the saved work."
            )
        else:
            base["error"] = (
                "Research job was interrupted by a worker restart before completion and no durable checkpoint was found. "
                "Start a fresh run. Configure RESEARCH_STATE_DIR on a persistent Railway Volume to survive host replacement."
            )
        base["finished_at"] = now_ist().isoformat(timespec="seconds")
        try:
            _atomic_write_early_research_state(base)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist interrupted research state: %s", exc)
    return base


_early_research_lock = threading.Lock()
_early_research_state = _load_early_research_state()


def _persist_early_research_state():
    with _early_research_lock:
        snapshot = dict(_early_research_state)
        snapshot["progress"] = dict(_early_research_state.get("progress") or {})
    try:
        _atomic_write_early_research_state(snapshot)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist early research checkpoint: %s", exc)


def get_early_research_state():
    with _early_research_lock:
        out = dict(_early_research_state, progress=dict(_early_research_state["progress"]))
    out["worker"] = research_runtime.snapshot()
    return out


def start_early_movement_research(kite, symbols=None, timeframe="15minute", days=30, holdout_pct=30.0,
                                  cost_pct=DEFAULT_COST_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT,
                                  universe_is_full_fno=False, fast_v8=False, research_mode=None):
    with _early_research_lock:
        if _early_research_state["status"] == "running":
            return {"started": False, "reason": "Early Movement Research is already running."}
        symbols = list(symbols or settings.WATCHLIST)
        job_run_dir = (
            _early_research_run_dir(
                symbols=symbols, timeframe=timeframe, days=days, holdout_pct=holdout_pct,
                cost_pct=cost_pct, slippage_pct=slippage_pct, research_mode=research_mode,
            ) if fast_v8 else None
        )
        resume_summary = _research_resume_summary(job_run_dir, len(symbols)) if job_run_dir is not None else None
        resumed_done = len(_completed_research_symbol_shards(job_run_dir)) if job_run_dir is not None else 0
        _early_research_state.update({
            "status": "running", "progress": {
                "done": resumed_done, "total": len(symbols), "symbol": None,
                "stage": "Fetching F&O history", "stage_index": 1, "stage_total": 4,
                "overall_pct": max(1, min(70, round((resumed_done / len(symbols)) * 70))) if symbols else 1,
                "resume_summary": resume_summary,
            },
            "result": None, "error": None, "started_at": now_ist().isoformat(timespec="seconds"),
            "finished_at": None, "params": {"timeframe": timeframe, "days": days, "fast_v8": bool(fast_v8), "research_mode": research_mode,
                                                   "resume_run_dir": (str(job_run_dir) if job_run_dir is not None else None)},
        })
    _persist_early_research_state()

    def _progress(done, total, symbol):
        research_runtime.heartbeat(stage="Fetching F&O history", symbol=symbol, done=done, total=total)
        with _early_research_lock:
            if research_mode == "v93_lab":
                pct = 8 if not total else max(8, min(70, 8 + round((done / total) * 62)))
            else:
                pct = 1 if not total else max(1, min(70, round((done / total) * 70)))
            current_resume = (_early_research_state.get("progress") or {}).get("resume_summary")
            _early_research_state["progress"] = {
                "done": done, "total": total, "symbol": symbol,
                "stage": "Fetching F&O history", "stage_index": 1, "stage_total": 4,
                "overall_pct": pct,
                "resume_summary": current_resume,
            }
        # Checkpoint periodically; do not turn every Kite symbol into a disk fsync.
        if done == 0 or done == total or done % 5 == 0:
            _persist_early_research_state()

    def _input_progress(done, total, symbol):
        if research_mode != "v93_lab":
            return
        research_runtime.heartbeat(stage="Fetching per-symbol daily OI", symbol=symbol, done=done, total=total)
        with _early_research_lock:
            pct = 1 if not total else max(1, min(8, 1 + round((done / total) * 7)))
            current_resume = (_early_research_state.get("progress") or {}).get("resume_summary")
            _early_research_state["progress"] = {
                "done": done, "total": total, "symbol": symbol,
                "stage": "Loading point-in-time daily continuous OI baseline",
                "stage_index": 1, "stage_total": 4, "overall_pct": pct,
                "resume_summary": current_resume,
            }
        # Update memory on every heartbeat so polling shows the active symbol,
        # but persist only every five completed symbols to avoid hot-loop fsync I/O.
        if done == 0 or done == total or (done > 0 and done % 5 == 0):
            _persist_early_research_state()

    def _stage(stage_index, stage_total, stage, overall_pct):
        research_runtime.heartbeat(stage=stage)
        with _early_research_lock:
            current = _early_research_state.get("progress") or {}
            _early_research_state["progress"] = {
                "done": current.get("done", len(symbols)), "total": current.get("total", len(symbols)),
                "symbol": None, "stage": stage, "stage_index": stage_index,
                "stage_total": stage_total, "overall_pct": overall_pct,
                "resume_summary": current.get("resume_summary"),
            }
        _persist_early_research_state()

    def _job():
        try:
            with research_runtime.research_slot():
                research_runtime.heartbeat(stage="Research worker acquired exclusive heavy-work slot")
                result = run_early_movement_research(
                    kite, symbols=symbols, timeframe=timeframe, days=days, holdout_pct=holdout_pct,
                    cost_pct=cost_pct, slippage_pct=slippage_pct, progress_cb=_progress, stage_cb=_stage,
                    input_progress_cb=_input_progress if research_mode == "v93_lab" else None,
                    universe_is_full_fno=universe_is_full_fno, fast_v8=fast_v8, research_mode=research_mode,
                    resume_run_dir=job_run_dir)
            with _early_research_lock:
                _early_research_state["progress"] = {"done": len(symbols), "total": len(symbols), "symbol": None, "stage": "Complete", "stage_index": 4, "stage_total": 4, "overall_pct": 100}
                _early_research_state["result"] = result
                _early_research_state["status"] = "done"
                _early_research_state["error"] = None
                _early_research_state["finished_at"] = now_ist().isoformat(timespec="seconds")
            # Atomic checkpoint contains both result and done status; a restart
            # can therefore recover the last completed report without ambiguity.
            _persist_early_research_state()
            # The durable final report is now in the atomic state checkpoint, so
            # same-day reruns must fetch fresh history rather than reuse stale shards.
            if job_run_dir is not None:
                shutil.rmtree(job_run_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("Early movement research run failed")
            with _early_research_lock:
                _early_research_state["status"] = "error"
                _early_research_state["error"] = str(exc)
                _early_research_state["finished_at"] = now_ist().isoformat(timespec="seconds")
            _persist_early_research_state()
        finally:
            research_runtime.end_research()

    research_runtime.begin_research(research_mode or "early_research")
    threading.Thread(target=_job, daemon=True).start()
    return {"started": True}


# --------------------------------------------------------------------------
# "Auto-Weight Parameters" - backtests each of the live screener's 4
# parameters over your watchlist to derive real, backtest-informed
# weights for background.py's weighted_score (see the Backtest page's
# "Auto-Weight Parameters" panel). A separate feature from the
# free-form backtest above it on the same page - this always tests the
# fixed 4-parameter screener combination, not whatever you've picked in
# the checkboxes.
# --------------------------------------------------------------------------

_WEIGHT_PHASES = [
    ("rsi_cross", "RSI Cross (solo)"),
    ("macd_cross", "MACD Cross (solo)"),
    ("cmf_flow", "Chaikin Money Flow sign (solo)"),
    ("rel_volume", "Relative Volume confirmation lift (2-of-3 baseline)"),
]


def compute_param_weights(kite, symbols=None, timeframe=None, days=30, ref_horizon=3, progress_cb=None):
    """Runs a handful of backtests over your watchlist to measure each of
    the 4 screener parameters' own recent historical predictive power,
    then converts that into normalized weights for background.py's
    weighted_score (a backtest-informed alternative to the plain
    aligned/4 equal-weight count):

      - RSI/MACD/EMA-BB: each backtested SOLO (required=1, that one
        parameter only) - literally "how often has THIS crossover alone
        been followed by a winning move lately".
      - Relative Volume: measured as a confirmation LIFT rather than a
        solo win rate, because on its own it has no direction to test
        (see this module's docstring / _signal_series' tie-break guard -
        a lone rel_volume selection always produces zero trades).
        Instead this runs the 2-of-3 directional baseline (RSI/MACD/
        EMA-BB, required=2) once, then compares the win rate of just the
        trades where volume also happened to be hot at entry
        (_summarize's vol_confirmed split) against the trades where it
        wasn't - the DIFFERENCE is volume's own contribution.

    Win rates are read at `ref_horizon` bars held. Returns weights
    normalized to sum to 1.0, with a floor so a single weak reading
    can't zero a parameter out of the live score entirely. Costs 4 full
    backtest passes over `symbols` (a few minutes for a typical
    watchlist) - this is a manual, on-demand action from the Backtest
    page, never run automatically on every scan cycle.

    IMPORTANT: this measures ONE recent window on YOUR specific
    watchlist, not a permanent verdict on an indicator - re-run it
    periodically rather than treating a single result as final."""
    symbols = list(symbols or settings.WATCHLIST)
    timeframe = timeframe or WATCHLIST_TIMEFRAME
    ref_horizon = int(ref_horizon)
    horizons = tuple(sorted(set(DEFAULT_HORIZONS) | {ref_horizon}))

    def _sub_progress(phase_index, phase_label):
        def _cb(done, total, symbol):
            if progress_cb:
                progress_cb(phase_index, len(_WEIGHT_PHASES), phase_label, done, total, symbol)
        return _cb

    win_rates = {}
    notes = {}

    for phase_index, (param_id, phase_label) in enumerate(_WEIGHT_PHASES[:3]):
        result = run_backtest(
            kite, symbols, timeframe=timeframe, days=days, horizons=horizons,
            params=(param_id,), required=1, progress_cb=_sub_progress(phase_index, phase_label),
        )
        stats = result["summary"]["all"].get(str(ref_horizon), {})
        win_rates[param_id] = stats.get("win_rate_pct")
        notes[param_id] = {"trade_count": stats.get("trade_count", 0), "kind": "solo win rate"}

    vol_phase_index, vol_phase_label = 3, _WEIGHT_PHASES[3][1]
    baseline = run_backtest(
        kite, symbols, timeframe=timeframe, days=days, horizons=horizons,
        params=("rsi_cross", "macd_cross", "cmf_flow"), required=2,
        progress_cb=_sub_progress(vol_phase_index, vol_phase_label),
    )
    vol_stats = baseline["summary"].get("vol_confirmed", {}).get(str(ref_horizon), {})
    novol_stats = baseline["summary"].get("vol_not_confirmed", {}).get(str(ref_horizon), {})
    vol_win_rate = vol_stats.get("win_rate_pct")
    novol_win_rate = novol_stats.get("win_rate_pct")
    win_rates["rel_volume"] = vol_win_rate
    notes["rel_volume"] = {
        "trade_count": vol_stats.get("trade_count", 0),
        "kind": "confirmation lift vs unconfirmed",
        "win_rate_without_volume": novol_win_rate,
        "lift_pct": (
            round(vol_win_rate - novol_win_rate, 1)
            if vol_win_rate is not None and novol_win_rate is not None
            else None
        ),
    }

    # Convert win rates into normalized weights - a parameter with too
    # few trades to judge (None) is treated as neutral (25%-equivalent
    # raw score) rather than silently dropped to zero. Weights are then
    # blended with a uniform floor so no parameter can be normalized away
    # to near-zero: 20% of the weight budget is split equally across all
    # N parameters (a guaranteed 5% each with the current 4 parameters),
    # and the remaining 80% is distributed by relative win rate. This
    # guarantees the FINAL weight for every parameter (not just its raw,
    # pre-normalization score) is at least FLOOR_FRACTION/n, and the
    # weights still sum to exactly 1.0.
    raw = {}
    for pid, wr in win_rates.items():
        raw[pid] = 0.25 if wr is None else max(0.0, wr / 100.0)
    total = sum(raw.values()) or 1.0
    norm = {pid: v / total for pid, v in raw.items()}

    FLOOR_FRACTION = 0.20
    n = len(raw) or 1
    weights = {
        pid: round(FLOOR_FRACTION / n + (1 - FLOOR_FRACTION) * norm[pid], 4)
        for pid in raw
    }

    return {
        "weights": weights,
        "win_rates": win_rates,
        "notes": notes,
        "timeframe": timeframe,
        "days": days,
        "ref_horizon": ref_horizon,
        "symbols_count": len(symbols),
        "computed_at": now_ist().isoformat(timespec="seconds"),
    }


_wt_lock = threading.Lock()
_wt_state = {
    "status": "idle",  # idle | running | done | error
    "progress": {"phase_index": 0, "phase_total": len(_WEIGHT_PHASES), "phase_label": None, "done": 0, "total": 0, "symbol": None},
    "params": None,
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}


def get_weights_state() -> dict:
    with _wt_lock:
        return dict(_wt_state, progress=dict(_wt_state["progress"]))


def _weights_progress_cb(phase_index, phase_total, phase_label, done, total, symbol):
    with _wt_lock:
        _wt_state["progress"] = {
            "phase_index": phase_index, "phase_total": phase_total, "phase_label": phase_label,
            "done": done, "total": total, "symbol": symbol,
        }


def start_weight_computation(kite, symbols=None, timeframe=None, days=30, ref_horizon=3) -> dict:
    """Kicks off an "Auto-Weight Parameters" run in a background thread,
    same pattern as start_backtest above. Only one weight computation
    runs at a time, and not alongside a regular backtest run either -
    both hammer the same Kite historical-data rate limit, so running
    them concurrently would just make each other slower and skew
    progress reporting for no benefit."""
    with _wt_lock, _bt_lock:
        if _wt_state["status"] == "running":
            return {"started": False, "reason": "A weight computation is already running."}
        if _bt_state["status"] == "running":
            return {
                "started": False,
                "reason": "A backtest is already running - wait for it to finish first (both share Kite's rate limit).",
            }
        symbols = list(symbols or settings.WATCHLIST)
        timeframe = timeframe or WATCHLIST_TIMEFRAME
        _wt_state["status"] = "running"
        _wt_state["progress"] = {
            "phase_index": 0, "phase_total": len(_WEIGHT_PHASES), "phase_label": None,
            "done": 0, "total": len(symbols), "symbol": None,
        }
        _wt_state["params"] = {"timeframe": timeframe, "days": days, "ref_horizon": ref_horizon}
        _wt_state["result"] = None
        _wt_state["error"] = None
        _wt_state["started_at"] = now_ist().isoformat(timespec="seconds")
        _wt_state["finished_at"] = None

    thread = threading.Thread(
        target=_run_weights_job, args=(kite, symbols, timeframe, days, ref_horizon), daemon=True
    )
    thread.start()
    return {"started": True}


def _run_weights_job(kite, symbols, timeframe, days, ref_horizon):
    try:
        result = compute_param_weights(
            kite, symbols, timeframe=timeframe, days=days, ref_horizon=ref_horizon,
            progress_cb=_weights_progress_cb,
        )
        try:
            with open(PARAM_WEIGHTS_FILE, "w") as f:
                json.dump(result, f, indent=2)
        except OSError:
            log.exception("Failed to persist param weights to %s", PARAM_WEIGHTS_FILE)
        with _wt_lock:
            _wt_state["status"] = "done"
            _wt_state["result"] = result
    except Exception as exc:  # noqa: BLE001 - a failed weight run must never crash the app
        log.exception("Weight computation failed")
        with _wt_lock:
            _wt_state["status"] = "error"
            _wt_state["error"] = str(exc)
    finally:
        with _wt_lock:
            _wt_state["finished_at"] = now_ist().isoformat(timespec="seconds")


def _load_persisted_weights_state():
    """So a restart doesn't lose the last computed weights from the
    Backtest page's own point of view (background.py separately re-reads
    PARAM_WEIGHTS_FILE directly for live scoring - this is just so the
    UI still shows the last result instead of a blank "idle" state)."""
    try:
        with open(PARAM_WEIGHTS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and "weights" in data:
            with _wt_lock:
                _wt_state["status"] = "done"
                _wt_state["result"] = data
                _wt_state["finished_at"] = data.get("computed_at")
    except (json.JSONDecodeError, OSError):
        pass


_load_persisted_weights_state()


# --------------------------------------------------------------------------
# Gate ablation - "which of these layers actually earns its place?"
#
# The screener has accumulated a lot of optional gates. Each was added for a
# reason that sounded good, and NONE of them has ever been measured against
# real outcomes. That is the single largest gap in this project: the app has
# far more machinery than evidence about any of it, which is exactly what
# NEXT_HORIZON_RESEARCH.md warns produces confident-looking, unvalidated
# systems.
#
# Answering "does gate X help?" by hand means running the backtest twice and
# eyeballing two numbers. For the gates below that is 2^N combinations, so
# in practice nobody ever finds out. This automates the honest, cheap
# version of the question: run a BASELINE with every gate off, then one run
# per gate with ONLY that gate on, and report each gate's delta against the
# shared baseline.
#
# Deliberate limits, stated rather than hidden:
#   - Every gate is measured in isolation AND a small set of targeted pairs
#     (especially OI interactions) is measured. This is intentionally not a
#     brute-force 2^N search, which would invite overfitting.
#   - Fewer trades is not automatically worse. A gate that removes 60% of
#     trades and lifts win rate 3 points may or may not be worth it - that
#     depends on how many opportunities you can actually take. Both numbers
#     are reported side by side rather than collapsed into one score.
#   - Small trade counts make win-rate deltas noisy. trade_count is included
#     on every row precisely so a seductive delta on 11 trades is visible as
#     such rather than read as fact.
# --------------------------------------------------------------------------

def _gate_applicability(gate_id, timeframe, params):
    """Why a gate cannot possibly change anything for this run - or None if
    it can.

    Two of the gates are structurally inert under certain configurations, and
    running them anyway is worse than useless: each burns a full backtest pass
    and then reports a delta of exactly zero, which reads as "this gate does
    not help" when the truth is "this gate was never tested". Saying so
    plainly is the difference between a measurement and a misleading blank."""
    if gate_id == "require_oi_agreement" and timeframe not in scanner_mod.OI_HISTORY_SPEC:
        return (f"no OI baseline is defined for {timeframe} candles - see "
                f"scanner.OI_HISTORY_SPEC. Testing it here would silently measure "
                f"a gate that can never fire.")

    if gate_id == "exclude_opening_window" and timeframe not in _OPENING_WINDOW_TIMEFRAMES:
        return (f"not applicable on {timeframe} candles - the opening-window rule only "
                f"suppresses bars on {'/'.join(_OPENING_WINDOW_TIMEFRAMES)}, so there is "
                f"nothing here for it to exclude")
    if gate_id == "require_regime_volume" and "rel_volume" not in params:
        return ("not applicable - this only rescales the Relative Volume threshold, and "
                "Relative Volume is not one of the parameters being tested in this run")
    return None


ABLATION_GATES = [
    ("require_htf", "Higher-timeframe trend agreement"),
    ("require_regime_volume", "Regime-adaptive volume threshold"),
    ("exclude_opening_window", "Exclude opening window / 4h warm-up"),
    ("require_candle_pattern", "Candlestick-pattern agreement"),
    ("require_macd_hist", "MACD histogram momentum"),
    ("require_big_candle", "Big-candle (range expansion) agreement"),
    ("require_strong_close", "Strong close in range"),
    ("require_entry_location", "Entry location (not chasing)"),
    ("require_atr_floor", "Minimum-ATR floor"),
    # The gate that decides the live shortlist. Its absence from this
    # list meant every ablation run measured the OLD architecture.
    ("require_oi_agreement", "OI positioning agreement"),
]

# Small, targeted interaction set. A full 2^N search would overfit and be
# prohibitively slow; these pairs test the combinations most likely to be
# complementary rather than duplicated information.
ABLATION_PAIRS = [
    (("require_oi_agreement", "require_htf"), "OI + higher-timeframe trend"),
    (("require_oi_agreement", "require_entry_location"), "OI + anti-chase entry location"),
    (("require_oi_agreement", "require_macd_hist"), "OI + MACD momentum slope"),
]


def _gate_fired(diagnostics, gate_id, trades_cut_pct):
    """Did this gate actually get a chance to reject anything?

    Two distinct failures look identical in a results table - both show 0%
    of trades cut:

      * the gate was evaluated on real readings and simply never disagreed
      * the reading it depends on was unavailable, so every row passed
        through untested

    Only the first is a finding. Reporting the second as "no effect" is how
    a gate that never ran gets retired for being useless, or trusted for
    being harmless. Where a gate reports coverage, use it; where it does not
    yet, say so rather than implying it was checked."""
    d = (diagnostics or {}).get(gate_id)
    if not d or not d.get("total"):
        return None, None
    coverage = d["readable"] / d["total"]
    if coverage <= 0.001:
        return False, coverage
    return True, coverage


def _ablation_row(label, gate_id, summary, baseline, ref_horizon, diagnostics=None,
                  diagnostic_gate=None, kind="single", train_holdout=None,
                  baseline_train_holdout=None):

    """One row of the ablation table: this gate's stats at ref_horizon and
    its deltas vs the shared baseline. Deltas are None when either side
    produced no trades at that horizon - a missing number is reported as
    missing rather than silently rendered as 0.0, which would read as
    'this gate changed nothing'."""
    stats = (summary or {}).get("all", {}).get(str(ref_horizon), {}) or {}
    base = (baseline or {}).get("all", {}).get(str(ref_horizon), {}) or {}
    wr, base_wr = stats.get("win_rate_pct"), base.get("win_rate_pct")
    ar, base_ar = stats.get("avg_return_pct"), base.get("avg_return_pct")
    pf = stats.get("profit_factor")
    n, base_n = stats.get("trade_count", 0), base.get("trade_count", 0)

    hold = (((train_holdout or {}).get("holdout") or {}).get("all") or {}).get(str(ref_horizon), {}) or {}
    base_hold = (((baseline_train_holdout or {}).get("holdout") or {}).get("all") or {}).get(str(ref_horizon), {}) or {}
    hold_wr, base_hold_wr = hold.get("win_rate_pct"), base_hold.get("win_rate_pct")
    hold_ar, base_hold_ar = hold.get("avg_return_pct"), base_hold.get("avg_return_pct")

    diag_gate = diagnostic_gate or gate_id
    fired, coverage = _gate_fired(diagnostics, diag_gate, None)
    _, data_cov = _gate_fired(diagnostics, diag_gate + "__data", None)
    diag_counts = (diagnostics or {}).get(diag_gate, {}) or {}
    return {
        "gate": gate_id,
        "kind": kind,
        # Present only for gates that report it. data_coverage answers "did
        # the reading exist"; reading_coverage answers "was it decisive".
        "data_coverage": None if data_cov is None else round(data_cov, 3),
        # None = this gate does not report coverage yet. False = it reports
        # coverage and had none, so its row is not a result.
        "fired": fired,
        "reading_coverage": None if coverage is None else round(coverage, 3),
        "label": label,
        "win_rate_pct": wr,
        "avg_return_pct": ar,
        "profit_factor": pf,
        "trade_count": n,
        "win_rate_delta": round(wr - base_wr, 1) if wr is not None and base_wr is not None else None,
        "avg_return_delta": round(ar - base_ar, 3) if ar is not None and base_ar is not None else None,
        "holdout_win_rate_pct": hold_wr,
        "holdout_avg_return_pct": hold_ar,
        "holdout_profit_factor": hold.get("profit_factor"),
        "holdout_trade_count": hold.get("trade_count", 0),
        "holdout_win_rate_delta": round(hold_wr - base_hold_wr, 1) if hold_wr is not None and base_hold_wr is not None else None,
        "holdout_avg_return_delta": round(hold_ar - base_hold_ar, 3) if hold_ar is not None and base_hold_ar is not None else None,
        "oi_passed": diag_counts.get("passed"),
        "oi_failed": diag_counts.get("failed"),
        "oi_missing": diag_counts.get("missing"),
        "trades_removed": (base_n - n) if base_n and n is not None else None,
        "trades_removed_pct": round((base_n - n) / base_n * 100, 1) if base_n else None,
    }


def run_gate_ablation(kite, symbols=None, timeframe=None, days=30, ref_horizon=3,
                       params=DEFAULT_PARAMS, required=DEFAULT_REQUIRED,
                       cost_pct=DEFAULT_COST_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT,
                       progress_cb=None):
    """Baseline + one run per gate. Returns a table sorted by win-rate
    delta, best first. Costs are applied to every run (including the
    baseline) so the comparison is like-for-like and net of what trading
    actually costs."""
    symbols = list(symbols or settings.WATCHLIST)
    timeframe = timeframe or WATCHLIST_TIMEFRAME
    ref_horizon = int(ref_horizon)
    horizons = tuple(sorted(set(DEFAULT_HORIZONS) | {ref_horizon}))
    _runnable = [g for g, _ in ABLATION_GATES if not _gate_applicability(g, timeframe, params)]
    _runnable_pairs = [pair for pair, _ in ABLATION_PAIRS
                       if not any(_gate_applicability(g, timeframe, params) for g in pair)]
    total_phases = len(_runnable) + len(_runnable_pairs) + 1

    def _sub(idx, label):
        def _cb(done, total, symbol):
            if progress_cb:
                progress_cb(idx, total_phases, label, done, total, symbol)
        return _cb

    price_cache, oi_cache = {}, {}
    common = dict(timeframe=timeframe, days=days, horizons=horizons,
                  params=params, required=required,
                  cost_pct=cost_pct, slippage_pct=slippage_pct,
                  holdout_pct=30.0,
                  history_cache=price_cache, oi_history_cache=oi_cache)

    baseline_result = run_backtest(kite, symbols, progress_cb=_sub(0, "Baseline (all gates off)"), **common)
    baseline = baseline_result["summary"]

    rows, skipped = [], []
    for i, (gate_id, label) in enumerate(ABLATION_GATES, start=1):
        reason = _gate_applicability(gate_id, timeframe, params)
        if reason:
            skipped.append({"gate": gate_id, "label": label, "reason": reason})
            continue
        res = run_backtest(kite, symbols, progress_cb=_sub(i, label), **{**common, gate_id: True})
        rows.append(_ablation_row(
            label, gate_id, res["summary"], baseline, ref_horizon,
            diagnostics=res.get("gate_diagnostics"),
            train_holdout=res.get("train_holdout"),
            baseline_train_holdout=baseline_result.get("train_holdout"),
        ))

    phase_index = 1 + len(_runnable)
    for pair, label in ABLATION_PAIRS:
        reasons = [_gate_applicability(g, timeframe, params) for g in pair]
        if any(reasons):
            skipped.append({"gate": "+".join(pair), "label": label,
                            "reason": "; ".join(r for r in reasons if r)})
            continue
        flags = {g: True for g in pair}
        res = run_backtest(kite, symbols, progress_cb=_sub(phase_index, label),
                           **{**common, **flags})
        diag_gate = "require_oi_agreement" if "require_oi_agreement" in pair else pair[0]
        rows.append(_ablation_row(
            label, "+".join(pair), res["summary"], baseline, ref_horizon,
            diagnostics=res.get("gate_diagnostics"), diagnostic_gate=diag_gate, kind="pair",
            train_holdout=res.get("train_holdout"),
            baseline_train_holdout=baseline_result.get("train_holdout")))
        phase_index += 1

    # Rank by untouched holdout expectancy first, not headline win rate.
    # Win rate can rise while the strategy still loses money; average net
    # return on data that was not used to discover the gate is the more
    # honest primary ranking. Missing holdout results sink to the bottom.
    rows.sort(key=lambda r: (
        r.get("holdout_avg_return_pct") is None,
        -(r.get("holdout_avg_return_pct") or 0),
        -(r.get("holdout_profit_factor") or 0),
    ))

    base_stats = baseline.get("all", {}).get(str(ref_horizon), {}) or {}
    return {
        "baseline": {
            "win_rate_pct": base_stats.get("win_rate_pct"),
            "avg_return_pct": base_stats.get("avg_return_pct"),
            "profit_factor": base_stats.get("profit_factor"),
            "trade_count": base_stats.get("trade_count", 0),
            "holdout": ((((baseline_result.get("train_holdout") or {}).get("holdout") or {}).get("all") or {}).get(str(ref_horizon), {}) or {}),
        },
        "rows": rows,
        # Reported, never silently dropped - a gate that could not be tested is
        # a different thing from a gate that was tested and did nothing.
        "skipped": skipped,
        "timeframe": timeframe,
        "days": days,
        "ref_horizon": ref_horizon,
        "cost_pct": float(cost_pct),
        "slippage_pct": float(slippage_pct),
        "symbols_count": len(symbols),
        "computed_at": now_ist().isoformat(timespec="seconds"),
    }


_ab_lock = threading.Lock()
_ab_state = {
    "status": "idle",
    "progress": {"phase_index": 0, "phase_total": len(ABLATION_GATES) + len(ABLATION_PAIRS) + 1,
                  "phase_label": None, "done": 0, "total": 0, "symbol": None},
    "params": None, "result": None, "error": None,
    "started_at": None, "finished_at": None,
}


def get_ablation_state() -> dict:
    with _ab_lock:
        return dict(_ab_state, progress=dict(_ab_state["progress"]))


def _ablation_progress_cb(phase_index, phase_total, phase_label, done, total, symbol):
    with _ab_lock:
        _ab_state["progress"] = {"phase_index": phase_index, "phase_total": phase_total,
                                  "phase_label": phase_label, "done": done, "total": total, "symbol": symbol}


def start_gate_ablation(kite, symbols=None, timeframe=None, days=30, ref_horizon=3) -> dict:
    """Same one-at-a-time discipline as start_weight_computation: this runs
    N+1 full backtests back to back, so letting it overlap with another run
    would just make both crawl against Kite's shared rate limit."""
    with _ab_lock, _bt_lock, _wt_lock:
        if _ab_state["status"] == "running":
            return {"started": False, "reason": "A gate ablation is already running."}
        if _bt_state["status"] == "running":
            return {"started": False, "reason": "A backtest is already running - wait for it to finish (both share Kite's rate limit)."}
        if _wt_state["status"] == "running":
            return {"started": False, "reason": "A weight computation is already running - wait for it to finish."}
        symbols = list(symbols or settings.WATCHLIST)
        timeframe = timeframe or WATCHLIST_TIMEFRAME
        _ab_state["status"] = "running"
        _ab_state["progress"] = {"phase_index": 0, "phase_total": len(ABLATION_GATES) + len(ABLATION_PAIRS) + 1,
                                  "phase_label": None, "done": 0, "total": len(symbols), "symbol": None}
        _ab_state["params"] = {"timeframe": timeframe, "days": days, "ref_horizon": ref_horizon}
        _ab_state["result"] = None
        _ab_state["error"] = None
        _ab_state["started_at"] = now_ist().isoformat(timespec="seconds")
        _ab_state["finished_at"] = None

    thread = threading.Thread(target=_run_ablation_job,
                               args=(kite, symbols, timeframe, days, ref_horizon), daemon=True)
    thread.start()
    return {"started": True}


def _run_ablation_job(kite, symbols, timeframe, days, ref_horizon):
    try:
        result = run_gate_ablation(kite, symbols, timeframe=timeframe, days=days,
                                    ref_horizon=ref_horizon, progress_cb=_ablation_progress_cb)
        with _ab_lock:
            _ab_state["status"] = "done"
            _ab_state["result"] = result
    except Exception as exc:  # noqa: BLE001 - a failed sweep must never crash the app
        log.exception("Gate ablation failed")
        with _ab_lock:
            _ab_state["status"] = "error"
            _ab_state["error"] = str(exc)
    finally:
        with _ab_lock:
            _ab_state["finished_at"] = now_ist().isoformat(timespec="seconds")
