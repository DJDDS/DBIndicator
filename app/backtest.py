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
from pathlib import Path
import threading
import time

import numpy as np
import pandas as pd

from . import config
from . import early_signal, early_research, v6_edge, v8_dual
from .config import settings, PARAM_WEIGHTS_FILE, WATCHLIST_TIMEFRAME
from .indicators import (
    compute_series, compute_avwap_series, session_vwap_series, BIG_CANDLE_LOOKBACK,
    _OPENING_WINDOW_TIMEFRAMES,
    _compute_adx, _classify_regime, _in_opening_window, _in_4hour_warmup, _HTF_RESAMPLE,
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
    "15minute": 90, "60minute": 90, "4hour": 180, "day": 365, "week": 900,
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

def _fetch_history(token, timeframe, days, kite):
    """Returns a DataFrame of open/high/low/close/volume candles for one
    symbol - just the one Kite API call needed (no futures/OI lookup,
    since none of the selectable parameters use OI)."""
    to_date = now_ist()
    from_date = to_date - dt.timedelta(days=days)
    interval = "60minute" if timeframe == "4hour" else timeframe

    data = kite.historical_data(token, from_date, to_date, interval)
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df = df.rename(columns={"date": "timestamp"}).set_index("timestamp")

    if timeframe == "4hour":
        df = df.resample("4h", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()

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
    total_drag = max(0.0, float(cost_pct)) + 2 * max(0.0, float(slippage_pct))

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
    drag = float(cost_pct) + 2.0 * float(slippage_pct)

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

    rank_specs = [
        ("v8_tod_rvol_percentile", lambda f: f.get("tod_rvol"), False),
        ("v8_opening_rvol_percentile", lambda f: f.get("opening_rvol"), False),
        ("v8_range_shock_percentile", lambda f: f.get("bar_range_atr"), False),
        ("v8_gap_shock_percentile", lambda f: pd.to_numeric(f.get("gap_atr"), errors="coerce").abs(), False),
        ("v8_turnover_percentile", lambda f: f.get("turnover_notional"), False),
        ("v8_oi_strength_percentile", lambda f: pd.to_numeric(f.get("oi_chg_60m_pct"), errors="coerce").abs(), False),
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
)


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
                                progress_cb=None, stage_cb=None, universe_is_full_fno=False,
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
    run_dir = (Path(resume_run_dir) if resume_run_dir is not None else
        _early_research_run_dir(
            symbols=symbols, timeframe=timeframe, days=days, holdout_pct=holdout_pct,
            cost_pct=cost_pct, slippage_pct=slippage_pct, research_mode=research_mode,
        )) if fast_v8 else None
    completed_shards = _completed_research_symbol_shards(run_dir) if run_dir is not None else {}
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
    window_start = (now_ist() - dt.timedelta(days=days)).replace(tzinfo=None).isoformat()
    for i, symbol in enumerate(symbols):
        if fast_v8 and symbol in completed_shards:
            if progress_cb:
                progress_cb(i + 1, len(symbols), f"{symbol} · resumed")
            continue
        if progress_cb:
            progress_cb(i, len(symbols), symbol)
        token = instruments.get(symbol)
        if not token:
            notes[symbol] = "symbol not found on NSE"
            if fast_v8:
                path = _write_research_symbol_shard(run_dir, i, symbol, compact_frame=None, replay=None, note=notes[symbol])
                completed_shards[symbol] = path
            continue
        try:
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
                sector_rank_series=sector_rank_series, futures_df=futures_df)
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
                path = _write_research_symbol_shard(
                    run_dir, i, symbol, compact_frame=compact_v8, replay=replay, note=None
                )
                completed_shards[symbol] = path
                # The shard owns the compact feature/replay payload now; keep the
                # 211-stock fetch stage essentially constant-memory.
                del replay, compact_v8, feat, df, execution_df, oi, futures_df
                if (i + 1) % 10 == 0:
                    gc.collect()
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

    if fast_v8:
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

    if stage_cb:
        stage_cb(2, 4, "Building cross-sectional ranks", 72)
    if fast_v8:
        _attach_v8_full_universe_scores_from_shards(replays, usable_shards, stage_cb=stage_cb)
    else:
        _attach_v8_full_universe_scores(replays, v8_feature_frames)
    # Cross-sectional ranks are now attached to the compact event rows; release
    # the full-universe feature/index history before Stage 3 aggregation.
    if fast_v8:
        v8_feature_frames.clear()
        turnover_series.clear()
        sector_history.clear()
        sector_ret_parts.clear()
        sector_rank_frame = None
        index_df = None
        gc.collect()

    if progress_cb:
        progress_cb(len(symbols), len(symbols), None)
    run_context = {
        "setup_timeframe": timeframe,
        "execution_timeframe": execution_timeframe,
        "days": days,
        "cost_pct": float(cost_pct),
        "slippage_pct": float(slippage_pct),
        "universe_is_full_fno": bool(universe_is_full_fno),
        "fast_v8": bool(fast_v8),
        "research_mode": research_mode or ("v9_fast" if fast_v8 else "legacy"),
    }
    if stage_cb:
        stage_cb(3, 4, (
            "Running frozen Bear FSB final test" if research_mode == "v91_bear_final"
            else ("Validating V9.1 goal-focused models" if research_mode == "v91_fast" else "Validating V9 professional playbooks")
        ), 86)
    if fast_v8:
        research = early_research.aggregate_v8_research_fast(
            replays, holdout_pct=holdout_pct, run_context=run_context
        )
    else:
        research = early_research.aggregate_research(
            replays, holdout_pct=holdout_pct, ref_horizon=3, horizons=horizons,
            run_context=run_context)
    if stage_cb:
        stage_cb(4, 4, "Preparing report", 98)
    symbols_completed_count = len(replays)
    if fast_v8:
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
        "fast_v8": bool(fast_v8),
        "research_notes": [
            "Historical OI uses Kite's available futures-history series; live ranking aggregates near/next/far expiries, so rollover-era historical OI is an approximation rather than a reconstructed three-expiry book.",
            ("4-hour setups are formed only from completed 4H candles and execute on the first "
             "available 15-minute bar; 15-minute setups retain next-bar execution."),
            "Higher-timeframe context is replayed using only fully closed buckets to avoid look-ahead.",
            "Sector context is replayed when the stock has a mapped NSE sector index and that index history is available.",
        ],
        "generated_at": now_ist().isoformat(timespec="seconds"),
    }


_EARLY_RESEARCH_STATE_PATH = Path(
    os.environ.get("EARLY_RESEARCH_STATE_PATH", "/tmp/dbindicator-early-research-state.json")
)
_EARLY_RESEARCH_WORK_ROOT = Path(
    os.environ.get("EARLY_RESEARCH_WORK_ROOT", "/tmp/dbindicator-early-research-work")
)
_RESEARCH_RESUME_SCHEMA = "v91-resume-shards-1"


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


def _research_symbol_shard_path(run_dir, index, symbol):
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(symbol))
    return Path(run_dir) / f"{int(index):04d}-{safe}.pkl"


def _write_research_symbol_shard(run_dir, index, symbol, *, compact_frame, replay, note):
    """Atomically persist one completed symbol so a worker restart can resume."""
    path = _research_symbol_shard_path(run_dir, index, symbol)
    payload = {
        "symbol": str(symbol),
        "compact_frame": compact_frame,
        "replay": replay,
        "note": note,
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


def _default_early_research_state():
    return {
        "status": "idle",
        "progress": {"done": 0, "total": 0, "symbol": None, "stage": None, "stage_index": 0, "stage_total": 4, "overall_pct": 0},
        "result": None, "error": None, "started_at": None, "finished_at": None,
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
        base["error"] = "Research job interrupted by server restart before completion. Run it again to resume from the saved symbol batches."
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
        return dict(_early_research_state, progress=dict(_early_research_state["progress"]))


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
        _early_research_state.update({
            "status": "running", "progress": {"done": 0, "total": len(symbols), "symbol": None, "stage": "Fetching F&O history", "stage_index": 1, "stage_total": 4, "overall_pct": 1},
            "result": None, "error": None, "started_at": now_ist().isoformat(timespec="seconds"),
            "finished_at": None, "params": {"timeframe": timeframe, "days": days, "fast_v8": bool(fast_v8), "research_mode": research_mode},
        })
    _persist_early_research_state()

    def _progress(done, total, symbol):
        with _early_research_lock:
            pct = 1 if not total else max(1, min(70, round((done / total) * 70)))
            _early_research_state["progress"] = {
                "done": done, "total": total, "symbol": symbol,
                "stage": "Fetching F&O history", "stage_index": 1, "stage_total": 4,
                "overall_pct": pct,
            }
        # Checkpoint periodically; do not turn every Kite symbol into a disk fsync.
        if done == 0 or done == total or done % 5 == 0:
            _persist_early_research_state()

    def _stage(stage_index, stage_total, stage, overall_pct):
        with _early_research_lock:
            current = _early_research_state.get("progress") or {}
            _early_research_state["progress"] = {
                "done": current.get("done", len(symbols)), "total": current.get("total", len(symbols)),
                "symbol": None, "stage": stage, "stage_index": stage_index,
                "stage_total": stage_total, "overall_pct": overall_pct,
            }
        _persist_early_research_state()

    def _job():
        try:
            result = run_early_movement_research(
                kite, symbols=symbols, timeframe=timeframe, days=days, holdout_pct=holdout_pct,
                cost_pct=cost_pct, slippage_pct=slippage_pct, progress_cb=_progress, stage_cb=_stage,
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
