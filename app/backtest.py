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
import json
import logging
import threading
import time

import numpy as np
import pandas as pd

from .config import settings, PARAM_WEIGHTS_FILE
from .indicators import (
    compute_series, RSI_OVERBOUGHT, RSI_OVERSOLD,
    _compute_adx, _classify_regime, _in_opening_window, _in_4hour_warmup, _HTF_RESAMPLE,
)
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

DEFAULT_HORIZONS = (5, 10, 20)
WARMUP_DAYS = 20          # extra calendar days fetched before the requested
                           # window purely so indicators are warmed up -
                           # trades are never counted in this stretch.
MAX_BACKTEST_DAYS = 90    # sane upper bound - Kite's own historical-data API
                           # limits how many days you can pull in one request
                           # for intraday intervals anyway (a too-long window
                           # just shows up as a per-symbol fetch error below,
                           # it won't crash the run).
_RATE_LIMIT_PAUSE = 0.35  # ~3 req/sec, matching Kite's historical-data rate limit
MAX_TRADES_RETURNED = 500  # cap on the trade-by-trade list sent to the browser
                            # (see run_backtest) - summary stats always use every trade


# The full menu of selectable backtest parameters - shown as checkboxes on
# the backtest page (web.py passes PARAM_DEFS straight to the template so
# labels stay in one place). Add a new one here and in _param_bull_bear().
PARAM_DEFS = [
    {"id": "rsi_cross", "label": "RSI Cross (vs its smoothing line)"},
    {"id": "macd_cross", "label": "MACD Cross (vs signal line)"},
    {"id": "ema_bb_cross", "label": "EMA9 vs Bollinger Mid Cross"},
    {"id": "rsi_threshold", "label": f"RSI > {RSI_OVERBOUGHT} (Bearish: RSI < {RSI_OVERSOLD})"},
    {"id": "rel_volume", "label": "Relative Volume above your configured threshold (20-bar avg, Settings page) - confirmation only, combine with a directional parameter"},
    {"id": "cmf_flow", "label": "Chaikin Money Flow sign (directional volume - Bullish if recent volume skewed toward up-closes, Bearish if down-closes; distinct from the magnitude-only Relative Volume above - see PARAMETER_ANALYSIS_2.md Finding #2)"},
    {"id": "candle_pattern", "label": "Candlestick pattern (Engulfing / Hammer-family / Morning-Evening Star - reads the raw shape of recent price action, not a smoothed derivative like the others above - see NEXT_HORIZON_RESEARCH.md)"},
]
PARAM_IDS = [p["id"] for p in PARAM_DEFS]
DEFAULT_PARAMS = ("rsi_cross", "macd_cross", "ema_bb_cross")  # the original 3-indicator rule
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
        "id": "require_volume_flow",
        "label": "Volume-flow agreement (Chaikin Money Flow sign must match your signal's direction, matches "
                  "live's optional REQUIRE_VOLUME_FLOW_AGREEMENT gate)",
    },
    {
        "id": "require_candle_pattern",
        "label": "Candlestick-pattern agreement (most recent Engulfing/Hammer-family/Morning-Evening Star "
                  "pattern must match your signal's direction, matches live's optional "
                  "REQUIRE_CANDLE_PATTERN_AGREEMENT gate)",
    },
]
FILTER_IDS = [f["id"] for f in FILTER_DEFS]


# Transaction-cost presets for the backtest UI (NEXT_HORIZON_RESEARCH.md
# Finding 2). Figures are round-trip cost_pct (brokerage + STT + exchange
# + SEBI + stamp + GST, charged once per completed trade) and per-leg
# slippage_pct (applied twice inside _compute_trade - once per fill),
# both expressed as a % of notional/premium. Stock-futures numbers come
# from Zerodha's published F&O charge structure applied to a liquid vs.
# thin order book; options numbers reflect that STT/exchange charges on
# options are levied on PREMIUM, not notional, which is structurally why
# they run roughly 6-10x higher as a percentage even though the rupee
# brokerage itself is flat. These are deliberately approximate, editable
# starting points, not a claim of precision - the whole point of Finding
# 2 is that even a rough cost haircut is far more honest than the
# previous default of assuming costs are zero.
COST_PRESETS = [
    {"id": "none", "label": "None (raw price return, prior behaviour)", "cost_pct": 0.0, "slippage_pct": 0.0},
    {"id": "futures_liquid", "label": "Stock futures - liquid (e.g. Nifty50 names)", "cost_pct": 0.08, "slippage_pct": 0.05},
    {"id": "futures_illiquid", "label": "Stock futures - less liquid / mid-cap", "cost_pct": 0.10, "slippage_pct": 0.15},
    {"id": "options", "label": "Options (cost as % of premium, not notional)", "cost_pct": 0.8, "slippage_pct": 0.5},
]


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
                    require_volume_flow=False, require_candle_pattern=False,
                    cost_pct=0.0, slippage_pct=0.0, holdout_pct=0.0) -> dict:
    """Kicks off a backtest run in a background thread. Returns
    {"started": True} or {"started": False, "reason": ...} if one is
    already running - only one backtest runs at a time. The five
    require_htf/require_regime_volume/exclude_opening_window/require_
    volume_flow/require_candle_pattern flags are the FILTER_DEFS gates
    above - all default False, matching every prior caller/test that
    only ever passed params/required. cost_pct/slippage_pct/holdout_pct
    (NEXT_HORIZON_RESEARCH.md Finding 2) all default to 0.0 - raw,
    un-costed, no holdout split - for the same reason."""
    with _bt_lock:
        if _bt_state["status"] == "running":
            return {"started": False, "reason": "A backtest is already running."}
        symbols = list(symbols or settings.WATCHLIST)
        timeframe = timeframe or settings.TIMEFRAME
        params = tuple(params)
        _bt_state["status"] = "running"
        _bt_state["progress"] = {"done": 0, "total": len(symbols), "symbol": None}
        _bt_state["params"] = {
            "timeframe": timeframe, "days": days, "horizons": list(horizons),
            "params": list(params), "required": required,
            "require_htf": bool(require_htf), "require_regime_volume": bool(require_regime_volume),
            "exclude_opening_window": bool(exclude_opening_window), "require_volume_flow": bool(require_volume_flow),
            "require_candle_pattern": bool(require_candle_pattern),
            "cost_pct": cost_pct, "slippage_pct": slippage_pct, "holdout_pct": holdout_pct,
        }
        _bt_state["result"] = None
        _bt_state["error"] = None
        _bt_state["started_at"] = now_ist().isoformat(timespec="seconds")
        _bt_state["finished_at"] = None

    thread = threading.Thread(
        target=_run_backtest_job,
        args=(kite, symbols, timeframe, days, horizons, params, required,
              require_htf, require_regime_volume, exclude_opening_window, require_volume_flow,
              require_candle_pattern, cost_pct, slippage_pct, holdout_pct),
        daemon=True,
    )
    thread.start()
    return {"started": True}


def _run_backtest_job(kite, symbols, timeframe, days, horizons, params, required,
                       require_htf=False, require_regime_volume=False, exclude_opening_window=False,
                       require_volume_flow=False, require_candle_pattern=False,
                       cost_pct=0.0, slippage_pct=0.0, holdout_pct=0.0):
    try:
        result = run_backtest(
            kite, symbols, timeframe=timeframe, days=days, horizons=horizons,
            params=params, required=required, progress_cb=_progress_cb,
            require_htf=require_htf, require_regime_volume=require_regime_volume,
            exclude_opening_window=exclude_opening_window, require_volume_flow=require_volume_flow,
            require_candle_pattern=require_candle_pattern,
            cost_pct=cost_pct, slippage_pct=slippage_pct, holdout_pct=holdout_pct,
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
    cross happens); rsi_threshold and rel_volume are states that can
    stay true for a stretch of consecutive bars.

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
    if param_id == "ema_bb_cross":
        return series["ema_up"], series["ema_dn"]
    if param_id == "rsi_threshold":
        rsi_line = series["rsi_line"]
        return rsi_line > RSI_OVERBOUGHT, rsi_line < RSI_OVERSOLD
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
    raise ValueError(f"unknown backtest parameter: {param_id}")


def _volume_flow_agree_series(series: dict, direction: pd.Series) -> pd.Series:
    """Vectorized replay of indicators.compute_signal's vol_flow_agrees -
    does Chaikin Money Flow's sign (see indicators._compute_cmf, reused
    from compute_series' own "cmf" column) agree with each bar's own
    chosen direction? NaN or exactly-0.0 CMF (not enough history yet, or
    a genuinely flat read) is treated as agreeing - same "None means
    agree" convention used by _htf_direction_series/require_htf above,
    so a bar with no CMF opinion yet is never silently suppressed."""
    cmf = series["cmf"]
    flow_dir = pd.Series(np.where(cmf > 0, "Bullish", np.where(cmf < 0, "Bearish", None)), index=cmf.index, dtype=object)
    return flow_dir.isna() | (flow_dir == direction)


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
        & htf_series["signal_line"].notna() & htf_series["ema9"].notna() & htf_series["bb_mid"].notna()
    )
    align_count = (
        (htf_series["rsi_line"] > htf_series["rsi_smooth"]).astype(int)
        + (htf_series["macd_line"] > htf_series["signal_line"]).astype(int)
        + (htf_series["ema9"] > htf_series["bb_mid"]).astype(int)
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


def _signal_series(series: dict, params, required: int, timeframe: str = None,
                    require_htf: bool = False, require_regime_volume: bool = False,
                    exclude_opening_window: bool = False, require_volume_flow: bool = False,
                    require_candle_pattern: bool = False):
    """Combines the chosen parameters bar-by-bar: has_signal is true on
    any bar where at least `required` of them agree on the same
    direction at once. Deliberately NOT the continuous "aligned >=
    min_required" majority state used elsewhere for display (index.html's
    Matching Now list, background.py's positional_qualified) - with only
    the original 3 crossover parameters selected that state is always
    >= 2 by construction (3 things can't split narrower than 2-1), which
    would make has_signal always true and silently produce zero trades.
    Mixing in threshold-type parameters (rsi_threshold, rel_volume) is
    safe here because those are genuine, sometimes-false conditions.

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
    # parameter (a *_cross or rsi_threshold) to get real entries.
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

    if require_volume_flow:
        flow_agrees = _volume_flow_agree_series(series, direction).reindex(index).fillna(True)
        has_signal = has_signal & flow_agrees

    if require_candle_pattern:
        candle_agrees = _candle_pattern_agree_series(series, direction).reindex(index).fillna(True)
        has_signal = has_signal & candle_agrees

    return has_signal, direction


def _compute_trade(df: pd.DataFrame, entry_pos: int, direction: str, symbol: str, horizons,
                    cost_pct: float = 0.0, slippage_pct: float = 0.0):
    """Entry is executed at the NEXT bar's open after the signal bar
    (never the signal bar's own close, to avoid lookahead bias).
    Returns are computed at each requested horizon (in bars), plus the
    single worst adverse move (drawdown) seen at any point during the
    longest hold - None if there isn't enough remaining data for even
    the shortest horizon.

    cost_pct/slippage_pct (both default 0.0 - raw price return, exactly
    the prior behaviour) let a run report returns NET of realistic
    Zerodha F&O transaction costs and execution slippage, per
    NEXT_HORIZON_RESEARCH.md Finding 2: without this, a backtest showing
    a small average edge can be entirely illusory once real costs are
    applied - options round-trip costs alone run roughly 0.6-1%+ of
    premium versus roughly 0.06-0.10% of notional for stock futures, a
    gap this app's own default parameters can't see without opting in
    here. Both are expressed as a flat, direction-agnostic percentage
    subtracted from the raw return: cost_pct is the round-trip
    brokerage/STT/exchange/SEBI/stamp/GST hit (a single flat deduction,
    since it's charged once per completed round trip regardless of which
    way you traded), and slippage_pct is a PER-LEG execution-slippage
    assumption applied twice (once for the entry fill, once for the exit
    fill) since both legs are exposed to it independently - deliberately
    a simple, symmetric, easy-to-verify model rather than adjusting the
    entry/exit price directionally, matching how the underlying cost
    research itself is expressed (a flat round-trip % of notional/
    premium, not a directional fill simulation). Only returns_pct is
    cost-adjusted - mae_pct (used for stop-sizing) is deliberately left
    on raw, un-costed price action, since it represents the worst
    UNREALIZED intrabar excursion a stop would have needed to survive,
    a different concept from realized, cost-adjusted P&L at exit."""
    if entry_pos + 1 >= len(df):
        return None
    entry_price = float(df["open"].iloc[entry_pos + 1])
    if not entry_price:
        return None
    entry_time = df.index[entry_pos + 1]
    signal_time = df.index[entry_pos]
    sign = 1 if direction == "Bullish" else -1

    max_h = max(horizons)
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

    total_cost_pct = max(0.0, cost_pct) + 2 * max(0.0, slippage_pct)
    returns = {}
    for h in horizons:
        exit_pos = entry_pos + 1 + h
        if exit_pos >= len(df):
            continue
        exit_price = float(df["close"].iloc[exit_pos])
        raw_return_pct = sign * (exit_price - entry_price) / entry_price * 100
        returns[h] = round(raw_return_pct - total_cost_pct, 3)
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
    }


def _replay_symbol(df: pd.DataFrame, symbol: str, timeframe: str, window_start, horizons, params, required,
                    require_htf=False, require_regime_volume=False, exclude_opening_window=False,
                    require_volume_flow=False, require_candle_pattern=False, cost_pct=0.0, slippage_pct=0.0):
    """Entry = the bar where your chosen parameter combination first
    reaches `required` agreement (see _signal_series), de-duped via a
    rising edge so a signal that stays true for a stretch of bars only
    counts once."""
    series = compute_series(df, timeframe)
    if "error" in series:
        return []

    has_signal, direction = _signal_series(
        series, params, required, timeframe=timeframe,
        require_htf=require_htf, require_regime_volume=require_regime_volume,
        exclude_opening_window=exclude_opening_window, require_volume_flow=require_volume_flow,
        require_candle_pattern=require_candle_pattern,
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
        trade = _compute_trade(df, pos, direction.iloc[pos], symbol, horizons,
                                cost_pct=cost_pct, slippage_pct=slippage_pct)
        if trade:
            trade["vol_confirmed_at_entry"] = bool(vol_hot.iloc[pos])
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
        out[str(h)] = {
            "trade_count": len(rets),
            "win_rate_pct": round(len(wins) / len(rets) * 100, 1),
            "avg_return_pct": round(sum(rets) / len(rets), 3),
            "best_return_pct": round(max(rets), 3),
            "worst_return_pct": round(min(rets), 3),
        }
    maes = [t["mae_pct"] for t in trades]
    out["overall"] = {
        "total_trades": len(trades),
        "avg_drawdown_pct": round(sum(maes) / len(maes), 3) if maes else None,
        "worst_drawdown_pct": round(min(maes), 3) if maes else None,
    }
    return out


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


def _summarize_by_split(trades, horizons, split_iso: str):
    """Splits an already-computed trade list into train (entry_time <
    split_iso) and holdout (entry_time >= split_iso) groups, each
    summarized the same way _summarize does the pooled set - lets a
    SINGLE backtest run (one fetch, one replay) also answer "does this
    configuration's performance hold up on data it wasn't looked at
    while choosing parameters", per NEXT_HORIZON_RESEARCH.md Finding 2,
    without a second fetch/replay pass. entry_time strings are ISO 8601
    from the same tz-naive index format throughout this module, so a
    plain string comparison against split_iso is a correct chronological
    comparison - no need to re-parse to datetime."""
    train = [t for t in trades if t["entry_time"] < split_iso]
    holdout = [t for t in trades if t["entry_time"] >= split_iso]
    return {
        "split_at": split_iso,
        "train": _summarize(train, horizons),
        "holdout": _summarize(holdout, horizons),
    }


def run_backtest(kite, symbols, timeframe="15minute", days=30, horizons=DEFAULT_HORIZONS,
                  params=DEFAULT_PARAMS, required=DEFAULT_REQUIRED, progress_cb=None,
                  require_htf=False, require_regime_volume=False, exclude_opening_window=False,
                  require_volume_flow=False, require_candle_pattern=False,
                  cost_pct=0.0, slippage_pct=0.0, holdout_pct=0.0) -> dict:
    days = min(int(days or 30), MAX_BACKTEST_DAYS)
    require_htf = bool(require_htf)
    require_regime_volume = bool(require_regime_volume)
    exclude_opening_window = bool(exclude_opening_window)
    require_volume_flow = bool(require_volume_flow)
    require_candle_pattern = bool(require_candle_pattern)
    try:
        cost_pct = max(0.0, float(cost_pct or 0.0))
    except (TypeError, ValueError):
        cost_pct = 0.0
    try:
        slippage_pct = max(0.0, float(slippage_pct or 0.0))
    except (TypeError, ValueError):
        slippage_pct = 0.0
    try:
        holdout_pct = min(90.0, max(0.0, float(holdout_pct or 0.0)))
    except (TypeError, ValueError):
        holdout_pct = 0.0
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

    for idx, symbol in enumerate(symbols):
        if progress_cb:
            progress_cb(idx, len(symbols), symbol)
        token = instruments.get(symbol)
        if not token and symbol in INDEX_SYMBOLS:
            token = _load_index_token(kite, symbol)
        if not token:
            symbol_notes[symbol] = "symbol not found on NSE"
            continue
        try:
            df = _fetch_history(token, timeframe, fetch_days, kite)
        except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the whole backtest
            symbol_notes[symbol] = f"history fetch failed: {exc}"
            time.sleep(_RATE_LIMIT_PAUSE)
            continue
        time.sleep(_RATE_LIMIT_PAUSE)

        if df is None or df.empty or len(df) < max(settings.BB_LENGTH, 35) + 5:
            symbol_notes[symbol] = "not enough historical candles returned"
            continue

        try:
            symbol_trades = _replay_symbol(
                df, symbol, timeframe, window_start.replace(tzinfo=None), horizons, params, required,
                require_htf=require_htf, require_regime_volume=require_regime_volume,
                exclude_opening_window=exclude_opening_window, require_volume_flow=require_volume_flow,
                require_candle_pattern=require_candle_pattern,
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
    total_trade_count = len(trades)
    display_trades = trades[-MAX_TRADES_RETURNED:]

    # Train/holdout out-of-sample split (NEXT_HORIZON_RESEARCH.md Finding
    # 2): holdout_pct=0 (the default) leaves train_holdout as None and
    # changes nothing else - every prior caller/test that never passed
    # this gets byte-identical results. When > 0, the LAST holdout_pct%
    # of the requested window (by calendar time, not trade count - a
    # date-based split, never a shuffle, since order matters for a
    # legitimate out-of-sample test) is held out; split_summary reports
    # both halves so you can see at a glance whether performance
    # actually survives on data this exact parameter choice never saw.
    train_holdout = None
    if holdout_pct > 0:
        split_dt = window_start + (to_date - window_start) * (1 - holdout_pct / 100)
        split_iso = split_dt.replace(tzinfo=None).isoformat(timespec="seconds")
        train_holdout = _summarize_by_split(trades, horizons, split_iso)

    return {
        "timeframe": timeframe,
        "days_requested": days,
        "horizons": list(horizons),
        "params": list(params),
        "required": required,
        "require_htf": require_htf,
        "require_regime_volume": require_regime_volume,
        "exclude_opening_window": exclude_opening_window,
        "require_volume_flow": require_volume_flow,
        "require_candle_pattern": require_candle_pattern,
        "cost_pct": cost_pct,
        "slippage_pct": slippage_pct,
        "holdout_pct": holdout_pct,
        "train_holdout": train_holdout,
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_end": to_date.isoformat(timespec="seconds"),
        "symbols_scanned": len(symbols),
        "symbols_with_trades": len({t["symbol"] for t in trades}),
        "symbols_skipped": symbol_notes,
        "trades": display_trades,
        "total_trade_count": total_trade_count,
        "summary": summary,
        "generated_at": to_date.isoformat(timespec="seconds"),
    }


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
    ("ema_bb_cross", "EMA9 vs Bollinger Mid Cross (solo)"),
    ("rel_volume", "Relative Volume confirmation lift (2-of-3 baseline)"),
]


def compute_param_weights(kite, symbols=None, timeframe=None, days=30, ref_horizon=10, progress_cb=None):
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
    timeframe = timeframe or settings.TIMEFRAME
    ref_horizon = int(ref_horizon)
    horizons = tuple(sorted({5, 10, 20, ref_horizon}))

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
        params=("rsi_cross", "macd_cross", "ema_bb_cross"), required=2,
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


def start_weight_computation(kite, symbols=None, timeframe=None, days=30, ref_horizon=10) -> dict:
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
        timeframe = timeframe or settings.TIMEFRAME
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
