"""
The Scanner logic: RSI(9) vs its smoothing line, MACD vs its signal
line, 9 EMA vs the Bollinger Band middle line, and the "N of 3"
confluence rule - same definitions used in the Pine Script / LipiScript
versions and the earlier backtest, so results here should line up with
what you've already seen on your charts.
"""
import datetime as dt

import numpy as np
import pandas as pd

from .config import settings


def rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int, slow: int, signal: int):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def macd_params_for_timeframe(timeframe: str):
    """Mirrors the auto-preset logic from the Pine Script version:
    15-min -> 3,8,9 | 30-min -> 8,16,9 | anything else (incl. 4-hour,
    60-min, day, week) -> custom/default, since the original notes only
    specified presets for 15/30-min."""
    preset = settings.MACD_PRESET.lower()
    if preset == "15min":
        return 3, 8, 9
    if preset == "30min":
        return 8, 16, 9
    if preset == "custom":
        return settings.MACD_CUSTOM_FAST, settings.MACD_CUSTOM_SLOW, settings.MACD_CUSTOM_SIGNAL
    # auto
    if timeframe == "15minute":
        return 3, 8, 9
    if timeframe == "30minute":
        return 8, 16, 9
    return settings.MACD_CUSTOM_FAST, settings.MACD_CUSTOM_SLOW, settings.MACD_CUSTOM_SIGNAL


def _cross_up(a, b):
    return (a.shift(1) <= b.shift(1)) & (a > b)


def _cross_down(a, b):
    return (a.shift(1) >= b.shift(1)) & (a < b)


_INTRADAY_TIMEFRAMES = ("3minute", "15minute", "60minute", "4hour")

# Timeframes whose OWN candle size is smaller than OPENING_WINDOW_MINUTES,
# i.e. a candle labeled 9:15 genuinely IS (part of) the noisy opening
# window itself. Deliberately separate from _INTRADAY_TIMEFRAMES above:
# a 4-hour bar labeled 9:15 spans the ENTIRE 9:15-13:15 block, not just
# the first 15 minutes, so applying this check to it would wrongly
# exclude every stock's current 4-hour bar for the first four hours of
# every trading day - that was a real bug, not a hypothetical one.
# "3minute" was added alongside scalper.py, which runs its own signal
# engine on this timeframe - it's smaller than the opening window too.
_OPENING_WINDOW_TIMEFRAMES = ("3minute", "15minute")

RSI_OVERBOUGHT = 65   # backtest.py's separate rsi_threshold param - Bullish side
RSI_OVERSOLD = 35     # (Bearish side). Not part of the 4-parameter screener below.

# Regime classification (Trending/Ranging/Transitional) via ADX - see
# _compute_adx/_classify_regime below. These threshold levels are the
# standard, widely-used Wilder ADX bands, so they're kept as fixed
# constants rather than yet more tunable settings; the one knob you
# actually want to turn (how much stricter volume confirmation should
# be in a Ranging regime) is settings.RANGING_VOL_MULTIPLIER instead.
ADX_TRENDING_THRESHOLD = 25
ADX_RANGING_THRESHOLD = 18

# Chaikin Money Flow - a bounded (-1..+1), DIRECTIONAL read of recent
# volume, unlike Relative Volume above (vol_confirmed/vol_multiple - pure
# magnitude, no "up" or "down" of its own). See _compute_cmf below and
# PARAMETER_ANALYSIS_2.md Finding #2: a huge volume spike on a red candle
# scores identically to one on a green candle under Relative Volume alone;
# CMF is what lets the screener tell "volume spiked and it was net buying"
# apart from "volume spiked, but it was actually distribution". Standard
# 20-period lookback, kept as a fixed constant rather than yet another
# tunable Settings field - same reasoning as the ADX thresholds just
# above, it's the conventional length for this indicator rather than
# something that benefits from per-user tuning the way RSI/EMA/BB do.
CMF_LENGTH = 20

# Candlestick pattern recognition - a THIRD, independent read on top of
# RSI/MACD/EMA-BB (which are all smoothed derivatives of price - see
# NEXT_HORIZON_RESEARCH.md's redundancy note) and CMF (a volume read):
# this reads the raw multi-bar SHAPE of price action instead, which is
# genuinely different information, not another oscillator. How many
# bars BEFORE the pattern to look back at (excluding the pattern's own
# bars) to decide whether it formed after an up-move or a down-move -
# needed because a hammer-shaped candle is bullish (a rejected sell-off)
# after a downtrend but bearish ("Hanging Man", a rejected rally) after
# an uptrend - same candle shape, opposite meaning depending on context.
CANDLE_TREND_LOOKBACK = 5

# How many bars back to look for the most recent qualifying "big candle"
# (range-expansion bar, see _compute_big_candle below) when deciding
# big_candle_recent_*/big_candle_continuation in compute_signal - a fixed
# constant, not user-tunable, same reasoning as CANDLE_TREND_LOOKBACK above.
BIG_CANDLE_LOOKBACK = 15

OPENING_WINDOW_MINUTES = 15  # signals formed in the first N minutes after the 9:15 IST open
                              # are excluded from signal_confirmed/in_opening_window below -
                              # these candles are usually the most gap-driven and noisy of the day


# Bars per trading session, used to scale the ATR floor across timeframes -
# see effective_min_atr_pct below.
_BARS_PER_SESSION = {"3minute": 125.0, "15minute": 25.0, "60minute": 6.25,
                      "4hour": 1.5625, "day": 1.0, "week": 1.0}


def effective_min_atr_pct(timeframe: str) -> float:
    """settings.MIN_ATR_PCT is expressed as a DAILY figure (the watchlist runs
    on daily bars, and "ATR is 1.2% of price" is only a meaningful sentence
    once you say over what period). Applying that same number to a 15-minute
    bar is a category error: a 15-minute bar's true range is a fraction of a
    day's, so essentially nothing intraday could ever clear a daily floor -
    the gate would silently exclude the entire universe rather than filter it.

    Volatility scales with the SQUARE ROOT of time, so the floor is divided by
    sqrt(bars per session): a 1.2% daily floor becomes ~0.24% on 15-minute
    bars and ~0.96% on 4-hour ones. That is the standard scaling assumption
    and an approximation - real intraday volatility is not uniform across the
    session (opens and closes are livelier than midday) - but it is far closer
    to right than reusing the daily number unchanged, and it keeps ONE tunable
    rather than one per timeframe."""
    bars = _BARS_PER_SESSION.get(timeframe, 1.0)
    return settings.MIN_ATR_PCT / (bars ** 0.5)


def _in_opening_window(ts, timeframe: str) -> bool:
    """True if this candle's timestamp falls within the first
    OPENING_WINDOW_MINUTES minutes after the 9:15 IST market open. Only
    meaningful for a timeframe whose own candle size is smaller than
    that window (see _OPENING_WINDOW_TIMEFRAMES) - a daily/weekly/4-hour
    candle spans a whole session (or more), so this never applies to
    those regardless of what its bin label happens to be."""
    if timeframe not in _OPENING_WINDOW_TIMEFRAMES:
        return False
    market_open = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    return market_open <= ts < market_open + dt.timedelta(minutes=OPENING_WINDOW_MINUTES)


# A 4-hour candle is always labeled by its BUCKET's start (9:15 or 13:15,
# see fetch_candles' resample origin/offset) for its ENTIRE real four-hour
# span - so _in_opening_window's candle-label check above can't be reused
# for it: `market_open <= ts < market_open + N minutes` would stay True
# for the whole 9:15-13:15 block (ts never advances past "9:15" as a
# label until the next bucket starts), wrongly suppressing signal_confirmed
# for four hours instead of just the first few minutes. That's exactly why
# "4hour" was deliberately left out of _OPENING_WINDOW_TIMEFRAMES.
# _in_4hour_warmup below checks REAL wall-clock time against the block
# boundary instead of the candle's label, so it only ever suppresses the
# first FOUR_HOUR_WARMUP_MINUTES of actual elapsed time - safe to combine
# with the label-based check above without that over-suppression bug.
FOUR_HOUR_WARMUP_MINUTES = 30
_FOUR_HOUR_BLOCK_STARTS = (dt.time(9, 15), dt.time(13, 15))  # matches fetch_candles' 4h resample origin


def _in_4hour_warmup(now, timeframe: str) -> bool:
    """True if `now` (real current IST time, passed in by the live caller
    - None for anything that isn't live-scanning, e.g. a backtest replay,
    which never sets this and so never triggers it) falls within the
    first FOUR_HOUR_WARMUP_MINUTES of the four-hour block currently
    forming. Not enough real 60-minute sub-bars have accumulated into a
    freshly-started 4-hour bucket yet for its indicators to be reliable -
    this is what was actually behind "4-hour scan results aren't right
    right after market open," not noise in the traditional
    gap-driven-open sense _in_opening_window guards against."""
    if timeframe != "4hour" or now is None:
        return False
    for block_start_t in _FOUR_HOUR_BLOCK_STARTS:
        block_start = now.replace(hour=block_start_t.hour, minute=block_start_t.minute, second=0, microsecond=0)
        if block_start <= now < block_start + dt.timedelta(minutes=FOUR_HOUR_WARMUP_MINUTES):
            return True
    return False

# Resample spec for each timeframe's "higher timeframe" trend read, used by
# _higher_timeframe_direction below - reuses whatever candles were already
# fetched for the main scan (no extra Kite API call, so no added rate-limit
# risk) resampled into a coarser bucket, and "label" picks which MACD
# preset compute_series uses for that resampled bucket (see
# macd_params_for_timeframe - "day"/"week" both fall through to the
# custom/default preset, same as "4hour" always has).
#
# 60minute and 4hour both use a daily HTF opinion rather than weekly -
# 4hour's own lookback (120 days) only yields ~17 resampled WEEKLY bars,
# short of the ~22 compute_series needs to warm up (BB_LENGTH+2), so a
# weekly HTF read would silently come back "no opinion" almost always.
# Daily easily clears that bar for both.
_HTF_RESAMPLE = {
    "15minute": {"rule": "4h", "kwargs": {"origin": "start_day", "offset": "9h15min"}, "label": "4hour"},
    "60minute": {"rule": "1D", "kwargs": {}, "label": "day"},
    "4hour": {"rule": "1D", "kwargs": {}, "label": "day"},
    # Daily's own higher timeframe is weekly. Without this entry a daily
    # scan had NO higher-timeframe check at all - htf_direction came back
    # None, htf_agrees was always True, and the gate silently did nothing
    # on the very timeframe the watchlist now runs on. scanner._lookback_days
    # pulls 400 calendar days for "day", which resamples to ~57 weekly bars -
    # comfortably past compute_series' ~37-bar warm-up requirement.
    "day": {"rule": "1W", "kwargs": {}, "label": "week"},
}


def _higher_timeframe_direction(df: pd.DataFrame, timeframe: str):
    """A lightweight higher-timeframe trend read: resamples the SAME
    candles already fetched for `timeframe` into a coarser bucket (4-hour
    for a 15-min scan) and reports which way that bucket's own 3-indicator
    majority currently leans - "Bullish", "Bearish", or None if this
    timeframe has no higher-timeframe entry above (not applicable - the
    caller should treat that as "no opinion, don't filter on this") or
    there isn't enough resampled history yet to compute the indicators."""
    spec = _HTF_RESAMPLE.get(timeframe)
    if spec is None or df.empty:
        return None
    htf_df = df.resample(spec["rule"], **spec["kwargs"]).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    htf_series = compute_series(htf_df, spec.get("label", "4hour"))
    if "error" in htf_series:
        return None
    i = len(htf_df) - 1
    # Same three directional votes the main signal uses - RSI, MACD and
    # Chaikin Money Flow. CMF replaced the old EMA9-vs-Bollinger-mid vote
    # (see compute_signal): that was a third transform of the same close
    # series, so it added little independent information, while CMF is
    # derived from volume and genuinely is a separate read.
    htf_cmf = htf_series["cmf"].iloc[i]
    align_count = (
        int(htf_series["rsi_line"].iloc[i] > htf_series["rsi_smooth"].iloc[i])
        + int(htf_series["macd_line"].iloc[i] > htf_series["signal_line"].iloc[i])
        + int(pd.notna(htf_cmf) and htf_cmf > 0)
    )
    return "Bullish" if align_count >= 2 else "Bearish"


def _compute_adx(df: pd.DataFrame, length: int) -> pd.Series:
    """Wilder's Average Directional Index - a 0-100 read of how strongly
    a market is TRENDING, independent of direction (a strong downtrend
    scores just as high as a strong uptrend). Used only for regime
    classification below (Trending/Ranging/Transitional), not as a
    screener parameter in its own right."""
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, min_periods=length, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, min_periods=length, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def compute_atr(df: pd.DataFrame, length: int) -> pd.Series:
    """Wilder's Average True Range - a volatility read used by scalper.py
    to size its suggested (informational-only) stop-loss/target off each
    stock's own recent bar-to-bar range rather than a flat percentage.
    Deliberately a standalone function with its own small True-Range
    calc, NOT a refactor of the (already deployed, already tested)
    True-Range logic inside _compute_adx above - the few duplicated
    lines are worth it to keep this isolated from regime detection, so
    a change here can never risk regressing the regime/volume-threshold
    feature."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def _classify_regime(adx_value):
    """None means "not enough history yet to judge" - treated the same
    as a Transitional regime by callers (i.e. no volume-threshold
    adjustment), never as an error."""
    if adx_value is None or pd.isna(adx_value):
        return None
    if adx_value >= ADX_TRENDING_THRESHOLD:
        return "Trending"
    if adx_value <= ADX_RANGING_THRESHOLD:
        return "Ranging"
    return "Transitional"


def _compute_cmf(df: pd.DataFrame, length: int) -> pd.Series:
    """Chaikin Money Flow: weights each bar's volume by where its close
    landed within that bar's own high-low range (Money Flow Multiplier -
    +1 means it closed at the high, -1 at the low, 0 for a flat/doji bar
    where high==low), sums that weighted volume ("Money Flow Volume")
    over `length` bars, and normalizes by total volume over the same
    window. Result is bounded roughly -1..+1: positive means recent
    volume has skewed toward up-closes (real buying pressure), negative
    means down-closes (distribution) - see CMF_LENGTH above."""
    high, low, close, volume = df["high"], df["low"], df["close"], df["volume"]
    mfm = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    mfm = mfm.fillna(0.0)  # a flat/doji bar contributes zero directional weight, not NaN
    mfv = mfm * volume
    vol_sum = volume.rolling(length, min_periods=5).sum()
    return mfv.rolling(length, min_periods=5).sum() / vol_sum.replace(0, np.nan)


def _compute_candle_pattern(df: pd.DataFrame):
    """Vectorized, no-lookahead candlestick pattern recognition. Every
    boolean below is computed ONLY from bar i and bars strictly BEFORE
    it (via .shift()) - never a future bar - so a pattern "confirmed" at
    bar i's close is exactly what a live scan would have seen at that
    same moment, same convention as every other series in this module.

    Detects three families, in ascending bar-count (and, for same-bar
    conflicts, roughly ascending reliability - a 2-3 bar pattern is a
    stronger tell than a single-candle shape alone):
      - Engulfing (2 bars): current candle's real body fully engulfs the
        prior candle's opposite-colored body - a sharp reversal in
        sentiment within a single bar.
      - Hammer-family (1 bar, context-dependent): a small body with one
        long shadow and one short one. The SAME shape reads as bullish
        (Hammer/Inverted Hammer - a rejected move against the prior
        trend) after a down-move, or bearish (Hanging Man/Shooting Star)
        after an up-move - see CANDLE_TREND_LOOKBACK above.
      - Morning/Evening Star (3 bars): a strong trend candle, a small
        "star" body, then a strong opposite-direction candle closing
        back past the midpoint of the first candle's body - a classic
        3-bar exhaustion-and-reversal shape.

    Returns (direction, name): two object Series aligned to df's index.
    direction is "Bullish"/"Bearish"/None (None when no pattern fired,
    OR when a bullish and a bearish pattern both fired on the very same
    bar - genuinely conflicting signals, treated as "no opinion" rather
    than arbitrarily picking one). name is a human-readable label for
    display (e.g. "Bullish Engulfing"), or None alongside a None
    direction. When more than one same-direction pattern fires on one
    bar, engulfing wins the display label over star, which wins over the
    hammer-family, reflecting that ordering's typical reliability -
    direction itself doesn't depend on this priority since same-
    direction patterns never conflict."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = c - o
    body_abs = body.abs()
    rng = (h - l).replace(0, np.nan)
    upper_shadow = h - np.maximum(o, c)
    lower_shadow = np.minimum(o, c) - l

    lb = CANDLE_TREND_LOOKBACK
    # Prior-trend context uses only bars BEFORE the pattern's own bar(s) -
    # close.shift(1) (the bar right before the pattern candle) vs.
    # close.shift(1 + lb) (lb bars before that) - so the pattern candle's
    # own move never contaminates the "what was the trend leading in"
    # read.
    prior_close = c.shift(1)
    prior_trend_up = prior_close > c.shift(1 + lb)
    prior_trend_down = prior_close < c.shift(1 + lb)

    # --- Engulfing (2 bars: this bar + the one before it) ---
    prev_o, prev_c = o.shift(1), c.shift(1)
    bullish_engulfing = (prev_c < prev_o) & (c > o) & (o <= prev_c) & (c >= prev_o)
    bearish_engulfing = (prev_c > prev_o) & (c < o) & (o >= prev_c) & (c <= prev_o)

    # --- Hammer-family shape (1 bar): small body, one long shadow, one
    # short one. Same shape test for both hammer-type (long lower shadow)
    # and shooting-star-type (long upper shadow); prior_trend_up/down
    # decides which label/direction it gets.
    small_body = body_abs <= 0.35 * rng
    hammer_shape = small_body & (lower_shadow >= 2 * body_abs) & (upper_shadow <= 0.25 * rng)
    inverted_shape = small_body & (upper_shadow >= 2 * body_abs) & (lower_shadow <= 0.25 * rng)
    hammer = hammer_shape & prior_trend_down          # bullish - rejected sell-off
    hanging_man = hammer_shape & prior_trend_up        # bearish - rejected rally, same shape
    inverted_hammer = inverted_shape & prior_trend_down  # bullish
    shooting_star = inverted_shape & prior_trend_up      # bearish

    # --- Morning/Evening Star (3 bars: this bar + the 2 before it) ---
    b1_o, b1_c = o.shift(2), c.shift(2)
    star_body = body_abs.shift(1)
    b1_body = (b1_o - b1_c).abs()
    star_is_small = star_body <= 0.5 * b1_body.replace(0, np.nan)
    b1_mid = (b1_o + b1_c) / 2
    morning_star = (b1_c < b1_o) & star_is_small & (c > o) & (c > b1_mid)
    evening_star = (b1_c > b1_o) & star_is_small & (c < o) & (c < b1_mid)

    bullish_any = bullish_engulfing | hammer | inverted_hammer | morning_star
    bearish_any = bearish_engulfing | hanging_man | shooting_star | evening_star
    conflict = bullish_any & bearish_any  # both fired on the same bar - no clear opinion

    direction = pd.Series(
        np.where(conflict, None, np.where(bullish_any, "Bullish", np.where(bearish_any, "Bearish", None))),
        index=df.index, dtype=object,
    )

    name = pd.Series(None, index=df.index, dtype=object)
    # Assign in ascending priority so the LAST assignment (highest
    # priority - engulfing) wins wherever multiple same-direction
    # patterns overlap on one bar. Never overwrites a conflict bar since
    # those are already forced to direction=None above and excluded here
    # by construction (a conflict bar has both a bullish_any and
    # bearish_any pattern true, but name assignment below is keyed off
    # the same masks, so a conflicted bar picks up a name from whichever
    # runs last - harmless since direction=None is what callers actually
    # branch on for display/scoring; name is cosmetic only).
    name[hammer] = "Hammer"
    name[hanging_man] = "Hanging Man"
    name[inverted_hammer] = "Inverted Hammer"
    name[shooting_star] = "Shooting Star"
    name[morning_star] = "Morning Star"
    name[evening_star] = "Evening Star"
    name[bullish_engulfing] = "Bullish Engulfing"
    name[bearish_engulfing] = "Bearish Engulfing"
    name[direction.isna()] = None

    return direction, name


def _compute_big_candle(df: pd.DataFrame, atr_series: pd.Series, atr_multiplier: float, strong_close_threshold_pct: float):
    """Vectorized "range expansion" / big-candle read - alongside
    _compute_candle_pattern above, this is one of the app's genuinely
    ANTICIPATORY reads rather than a confirmatory one: RSI/MACD/EMA-BB/
    CMF are all smoothed derivatives that tell you a move is already
    under way. A bar counts as a "big candle" when its own true range is
    at least atr_multiplier x that bar's OWN ATR (a real range EXPANSION,
    not just an average day) AND its close sits in the extreme top or
    bottom strong_close_threshold_pct% of its own high-low range (real
    directional conviction, not just a wide, indecisive bar with no clear
    winner - that combination gets no opinion at all, see below).

    Returns (direction, level, close_position) - three Series aligned to
    df's index:
      - direction: "Bullish"/"Bearish"/None per bar (None for a normal
        bar, OR a wide-but-indecisive one that closed mid-range).
      - level: that bar's own high (Bullish) or low (Bearish), NaN
        otherwise - the price a LATER bar would need to clear to count as
        continuation (see compute_signal's big_candle_recent_*/
        big_candle_continuation, which look this series up over a short
        trailing window rather than just the latest bar).
      - close_position: 0-1 for EVERY bar regardless of whether it was a
        big candle (0 = closed at the low, 1 = closed at the high; NaN
        for a doji where high == low) - reused by compute_signal's
        strong_close_agrees, a separate BTST-oriented "closed strong in
        its own direction" read that doesn't require range expansion at
        all, just an extreme close."""
    high, low, close = df["high"], df["low"], df["close"]
    rng = high - low
    close_position = (close - low) / rng.replace(0, np.nan)
    range_expansion = atr_series.notna() & (atr_series > 0) & (rng >= atr_multiplier * atr_series)
    hi_cut = strong_close_threshold_pct / 100.0
    lo_cut = 1 - hi_cut
    bullish = range_expansion & close_position.notna() & (close_position >= hi_cut)
    bearish = range_expansion & close_position.notna() & (close_position <= lo_cut)
    direction = pd.Series(
        np.where(bullish, "Bullish", np.where(bearish, "Bearish", None)),
        index=df.index, dtype=object,
    )
    level = pd.Series(np.where(bullish, high, np.where(bearish, low, np.nan)), index=df.index)
    return direction, level, close_position


def session_vwap(df: pd.DataFrame, timeframe: str):
    """Volume-weighted average price for just today's candles (resets
    every session, like a broker terminal's VWAP) - not meaningful on
    day/week bars, so returns None for those. None also comes back if
    there's no volume recorded yet for today (e.g. right at open)."""
    if timeframe not in _INTRADAY_TIMEFRAMES or df.empty:
        return None
    last_date = df.index[-1].date()
    session = df[df.index.map(lambda ts: ts.date()) == last_date]
    total_vol = session["volume"].sum()
    if session.empty or not total_vol:
        return None
    typical = (session["high"] + session["low"] + session["close"]) / 3
    return float((typical * session["volume"]).sum() / total_vol)


def session_vwap_series(df: pd.DataFrame, timeframe: str) -> pd.Series:
    """Same session-anchored VWAP as session_vwap above, but as a full
    running series aligned to df's whole index (cumulative within each
    calendar day, resetting at the next session's first bar) rather than
    just the latest bar's value - needed so scalper.py can detect the
    actual BAR a stock's price crosses its own running VWAP, not just
    whether it's currently above/below it. Returns an all-NaN series for
    non-intraday timeframes or an empty df, same convention as
    session_vwap's None."""
    if timeframe not in _INTRADAY_TIMEFRAMES or df.empty:
        return pd.Series(np.nan, index=df.index)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical * df["volume"]
    day = df.index.map(lambda ts: ts.date())
    cum_tp_vol = tp_vol.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def _current_trend_anchor_pos(align_count: pd.Series):
    """Integer position of the bar where the current confluence majority
    (align_count >= 2 -> Bullish, else Bearish - same rule compute_signal
    uses for `direction`) most recently FLIPPED, i.e. where the current
    trend leg began. Falls back to the first bar with a valid (non-NaN)
    align_count reading if no flip is found anywhere in the fetched
    window (the whole available history has been one continuous
    direction) - never None as long as at least one bar is valid, so the
    anchored VWAP below always has somewhere to start from. Returns None
    only if align_count has no valid readings at all (not enough history
    yet - same "not enough candles" case compute_series already guards
    against, kept here too since this can be called standalone via
    compute_avwap_series)."""
    valid = align_count.notna()
    if not valid.any():
        return None
    valid_positions = np.flatnonzero(valid.values)
    first_valid_pos = int(valid_positions[0])
    bull = (align_count >= 2).values
    anchor_pos = first_valid_pos
    for k in range(len(bull) - 1, first_valid_pos, -1):
        if bull[k] != bull[k - 1]:
            anchor_pos = k
            break
    return anchor_pos


def anchored_vwap_series(df: pd.DataFrame, anchor_pos) -> pd.Series:
    """Volume-weighted average price computed from `anchor_pos` onward
    only (not session-reset like session_vwap_series above) - "average
    price paid since [anchor]", NaN before the anchor bar itself. Used
    with anchor_pos = _current_trend_anchor_pos's result to get a VWAP
    anchored to the start of the stock's CURRENT confluence trend leg
    (see compute_avwap_series/compute_signal's avwap field) - genuinely
    different from session VWAP (resets every day regardless of trend)
    and meaningful on every timeframe, not just intraday ones."""
    out = pd.Series(np.nan, index=df.index)
    if anchor_pos is None or df.empty:
        return out
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical * df["volume"]
    seg_cum_tp_vol = tp_vol.iloc[anchor_pos:].cumsum()
    seg_cum_vol = df["volume"].iloc[anchor_pos:].cumsum()
    out.iloc[anchor_pos:] = (seg_cum_tp_vol / seg_cum_vol.replace(0, np.nan)).values
    return out


def _full_align_count_series(series: dict) -> pd.Series:
    """The same 0-3 "how many of RSI/MACD/EMA-BB currently agree" count
    compute_signal computes for just the LAST bar (align_count there),
    but as a full series across every bar - needed for
    _current_trend_anchor_pos's flip search. NaN comparisons (e.g.
    rsi_line vs rsi_smooth during RSI's own warm-up window) evaluate to
    plain False under ">" rather than propagating NaN, which would
    otherwise silently mislabel not-yet-warmed-up bars as a real
    "Bearish" reading - `valid_row` masks those bars back to NaN so
    _current_trend_anchor_pos's first-valid-position logic (and its flip
    search, which only ever looks at positions >= that) never sees a
    fabricated reading."""
    valid_row = (
        series["rsi_line"].notna() & series["rsi_smooth"].notna()
        & series["macd_line"].notna() & series["signal_line"].notna()
        & series["cmf"].notna()
    )
    raw_count = (
        (series["rsi_line"] > series["rsi_smooth"]).astype(int)
        + (series["macd_line"] > series["signal_line"]).astype(int)
        + (series["cmf"] > 0).astype(int)
    )
    return raw_count.where(valid_row)


def compute_avwap_series(series: dict) -> pd.Series:
    """Given a compute_series() result dict, returns the anchored-VWAP
    series - factored out so /api/chart can plot the exact same line the
    dashboard's AVWAP badge uses (see compute_signal) without duplicating
    the RSI/MACD/EMA-BB alignment math here a second time."""
    anchor_pos = _current_trend_anchor_pos(_full_align_count_series(series))
    return anchored_vwap_series(series["df"], anchor_pos)


def compute_series(df: pd.DataFrame, timeframe: str) -> dict:
    """Computes every indicator series for the full df (used by the
    chart API). Returns a dict of aligned pandas Series/DataFrame plus
    the resolved MACD params, or {"error": ...} if there isn't enough
    history yet."""
    if len(df) < max(settings.BB_LENGTH, 35) + 2:
        return {"error": "not enough candles yet"}

    close = df["close"]

    rsi_line = rsi(close, settings.RSI_LENGTH)
    rsi_smooth = rsi_line.rolling(settings.RSI_SMOOTH_LENGTH).mean()

    fast, slow, sig = macd_params_for_timeframe(timeframe)
    macd_line, signal_line = macd(close, fast, slow, sig)
    macd_hist = macd_line - signal_line

    # EMA9 is now a CHART OVERLAY ONLY - it stopped being a screener vote
    # when CMF replaced the EMA9-vs-Bollinger-mid cross, so its length is no
    # longer a tunable setting. 9 is the conventional value and the one the
    # chart has always drawn.
    ema9 = close.ewm(span=9, adjust=False).mean()
    bb_mid = close.rolling(settings.BB_LENGTH).mean()
    # Same BB_LENGTH you already tune in Quick Settings, extended with a
    # standard 2-std-dev envelope - used only for the OI Screener's
    # Breakout/Breakdown column (see compute_signal below), so that
    # column moves with the same parameter you're already adjusting
    # rather than a separate hardcoded setting.
    bb_std = close.rolling(settings.BB_LENGTH).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    vol_avg = df["volume"].rolling(20, min_periods=5).mean()
    cmf = _compute_cmf(df, CMF_LENGTH)
    candle_direction, candle_pattern_name = _compute_candle_pattern(df)

    # ATR computed once here (rather than separately inside compute_signal,
    # which used to recompute it locally) so _compute_big_candle below and
    # compute_signal's own stop/target/position-size block always read the
    # exact same series - see "atr" in the returned dict.
    atr_series = compute_atr(df, settings.ATR_LENGTH)
    big_candle_direction, big_candle_level, close_position = _compute_big_candle(
        df, atr_series, settings.BIG_CANDLE_ATR_MULTIPLIER, settings.STRONG_CLOSE_THRESHOLD_PCT
    )

    rsi_up, rsi_dn = _cross_up(rsi_line, rsi_smooth), _cross_down(rsi_line, rsi_smooth)
    macd_up, macd_dn = _cross_up(macd_line, signal_line), _cross_down(macd_line, signal_line)

    return {
        "df": df,
        "rsi_line": rsi_line,
        "rsi_smooth": rsi_smooth,
        "macd_line": macd_line,
        "signal_line": signal_line,
        "macd_hist": macd_hist,
        "ema9": ema9,
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "vol_avg": vol_avg,
        "cmf": cmf,
        "candle_direction": candle_direction,
        "candle_pattern_name": candle_pattern_name,
        "atr": atr_series,
        "big_candle_direction": big_candle_direction,
        "big_candle_level": big_candle_level,
        "close_position": close_position,
        "rsi_up": rsi_up, "rsi_dn": rsi_dn,
        "macd_up": macd_up, "macd_dn": macd_dn,
        "macd_params": (fast, slow, sig),
    }


def compute_signal(df: pd.DataFrame, timeframe: str, now=None) -> dict:
    """df must have a 'close' column, oldest row first. Returns the
    latest confluence state - current bullish/bearish alignment count,
    whether a fresh signal fired on the most recently CLOSED candle,
    and the individual indicator values for display.

    `now` is the real current IST time, only meaningful for "4hour" (see
    _in_4hour_warmup) - pass it from a live caller (scanner.py does);
    leave it None for anything replaying historical bars (nothing in
    backtest.py calls this function at all, but the default keeps this
    safe for any future caller that doesn't have a real "now" either)."""
    series = compute_series(df, timeframe)
    if "error" in series:
        return series

    close = series["df"]["close"]
    rsi_line, rsi_smooth = series["rsi_line"], series["rsi_smooth"]
    macd_line, signal_line = series["macd_line"], series["signal_line"]
    fast, slow, sig = series["macd_params"]

    i = len(df) - 1  # last closed candle
    # The third crossover is CMF crossing its own zero line, matching the
    # three DIRECTIONAL votes below. This used to be the EMA9-vs-Bollinger
    # -mid cross; when CMF replaced that as a vote, leaving this on EMA/BB
    # would have meant Telegram alerts firing on a rule the screener no
    # longer uses - a silent divergence between what alerts you and what
    # the dashboard calls a signal.
    cmf_series = series["cmf"]
    cmf_up = _cross_up(cmf_series, pd.Series(0.0, index=cmf_series.index))
    cmf_dn = _cross_down(cmf_series, pd.Series(0.0, index=cmf_series.index))
    bull_count = int(series["rsi_up"].iloc[i]) + int(series["macd_up"].iloc[i]) + int(bool(cmf_up.iloc[i]))
    bear_count = int(series["rsi_dn"].iloc[i]) + int(series["macd_dn"].iloc[i]) + int(bool(cmf_dn.iloc[i]))

    # fresh_signal only tracks the 3 CROSSOVER-capable indicators (RSI,
    # MACD, CMF-vs-zero) - Relative Volume is a continuous magnitude state,
    # not something that "crosses" a line, so it can't itself form a
    # fresh signal the way these 3 can. bull_count/bear_count therefore
    # max out at 3 even though settings.MIN_REQUIRED can now go up to
    # 4-of-4 - min() below means "4-of-4" reads as "all 3 crossover
    # indicators fire at once" for this specific check (the strictest
    # the crossover count can ever be), while `aligned`/signal_confirmed
    # further down still separately require volume too.
    fresh_required = min(settings.MIN_REQUIRED, 3)
    fresh_signal = None
    if bull_count >= fresh_required:
        fresh_signal = "Bullish"
    elif bear_count >= fresh_required:
        fresh_signal = "Bearish"

    # The three DIRECTIONAL votes. Chaikin Money Flow replaced the old
    # EMA9-vs-Bollinger-mid vote here: that was a plain moving-average
    # crossover wearing a Bollinger label (nothing about it read the BANDS
    # at all), and being a third transform of the same close series it
    # carried little information RSI and MACD didn't already have - see
    # NEXT_HORIZON_RESEARCH.md Finding 1 on correlated votes, and
    # PARAMETER_ANALYSIS_2.md Finding #3. CMF is derived from volume
    # instead, so the vote is now 2 price reads + 2 volume reads (CMF
    # directional, Relative Volume magnitude) rather than 3 price + 1
    # volume - genuinely more independent evidence for the same count.
    #
    # Bollinger itself did NOT leave the app: the bands still drive the
    # breakout/breakdown state and the band-WIDTH coiling read, which is
    # what Bollinger Bands are actually built to measure.
    cmf_now = series["cmf"].iloc[i]
    align_count = (
        int(rsi_line.iloc[i] > rsi_smooth.iloc[i])
        + int(macd_line.iloc[i] > signal_line.iloc[i])
        + int(pd.notna(cmf_now) and cmf_now > 0)
    )

    vwap = session_vwap(df, timeframe)
    vs_vwap = None
    if vwap:
        vs_vwap = "Above" if close.iloc[i] > vwap else "Below"

    # Anchored VWAP: average price paid since the CURRENT confluence
    # trend leg began (see _current_trend_anchor_pos/anchored_vwap_series
    # above) - unlike session VWAP above, this doesn't reset daily and is
    # meaningful on every timeframe including day/week. None only when
    # there isn't a single valid (post-warmup) bar to anchor from, which
    # can't actually happen here since compute_series already guaranteed
    # enough history for this same df earlier in this function.
    avwap_anchor_pos = _current_trend_anchor_pos(_full_align_count_series(series))
    avwap_series = anchored_vwap_series(df, avwap_anchor_pos)
    avwap_raw = avwap_series.iloc[i]
    avwap = round(float(avwap_raw), 2) if pd.notna(avwap_raw) else None
    vs_avwap = None
    avwap_anchor_time = None
    if avwap is not None:
        vs_avwap = "Above" if close.iloc[i] > avwap else "Below"
        avwap_anchor_time = df.index[avwap_anchor_pos].isoformat()

    bb_mid = series["bb_mid"]  # still used by the band-width coiling read below
    bb_upper, bb_lower = series["bb_upper"], series["bb_lower"]
    breakout_state = None
    if pd.notna(bb_upper.iloc[i]) and close.iloc[i] > bb_upper.iloc[i]:
        breakout_state = "Breakout"
    elif pd.notna(bb_lower.iloc[i]) and close.iloc[i] < bb_lower.iloc[i]:
        breakout_state = "Breakdown"

    vol_avg = series["vol_avg"].iloc[i]
    latest_vol = df["volume"].iloc[i]
    vol_multiple = round(float(latest_vol / vol_avg), 2) if vol_avg and pd.notna(vol_avg) and vol_avg > 0 else None
    volume = int(latest_vol) if pd.notna(latest_vol) else None

    # Regime-adaptive volume bar: a choppy/Ranging market throws off a
    # lot more low-conviction volume spikes than a Trending one, so a
    # breakout there needs a STRICTER Relative Volume reading to be
    # trusted at the same rate a Trending-regime one would be -
    # settings.RANGING_VOL_MULTIPLIER (>=1.0, default 1.3) scales the
    # bar up only in that regime; Trending/Transitional/unknown all use
    # your configured REL_VOLUME_THRESHOLD unchanged.
    adx_series = _compute_adx(df, settings.ADX_LENGTH)
    adx_raw = adx_series.iloc[i]
    adx_value = round(float(adx_raw), 1) if pd.notna(adx_raw) else None
    regime = _classify_regime(adx_value)
    vol_threshold_multiplier = settings.RANGING_VOL_MULTIPLIER if regime == "Ranging" else 1.0
    effective_vol_threshold = round(settings.REL_VOLUME_THRESHOLD * vol_threshold_multiplier, 3)

    # Direction is decided by the 3 DIRECTIONAL indicators only (RSI,
    # MACD, EMA/BB vs Bollinger mid) - Relative Volume has no "up" or
    # "down" of its own, only a magnitude read, so it can't cast a
    # directional vote. It still counts as a full 4th parameter in
    # `aligned` below though - see vol_confirmed.
    dir_match_count = max(align_count, 3 - align_count)  # how many of the 3 agree with the majority
    direction = "Bullish" if align_count >= 2 else "Bearish"

    # vol_confirmed: today's actual participation, not just the price
    # pattern - a real move is usually backed by above-average volume,
    # so a signal on quiet volume is more likely to be noise. This is
    # the 4th of the 4 screener parameters (RSI/MACD/EMA-BB direction +
    # Relative Volume magnitude), each weighted equally in `aligned`
    # below - not a separate mandatory gate layered on top of them like
    # it used to be.
    vol_confirmed = bool(vol_multiple is not None and vol_multiple >= effective_vol_threshold)

    # vol_flow_direction/vol_flow_agrees: a DIRECTIONAL read of the same
    # volume, via Chaikin Money Flow (see _compute_cmf/CMF_LENGTH above) -
    # separate from vol_confirmed above (magnitude only) and deliberately
    # NOT folded into `aligned` below: doing so would silently change
    # every existing signal's score and invalidate any Auto-Weight
    # Parameters run computed before this existed (see
    # PARAMETER_ANALYSIS_2.md Finding #2). None means "not enough volume
    # history yet to judge" (or CMF read exactly 0.0) - treated as
    # agreeing, same convention as htf_direction=None above, so it never
    # silently blocks anything unless settings.REQUIRE_VOLUME_FLOW_AGREEMENT
    # is explicitly turned on (applied a layer up, in background.py's
    # _apply_volume_flow_filter - mirrors how REQUIRE_INDEX_AGREEMENT is
    # applied, not baked in here).
    cmf_raw = series["cmf"].iloc[i]
    cmf_value = round(float(cmf_raw), 3) if pd.notna(cmf_raw) else None
    vol_flow_direction = None
    if cmf_value:  # None or exactly 0.0 both mean "no opinion"
        vol_flow_direction = "Bullish" if cmf_value > 0 else "Bearish"

    # candle_pattern/candle_direction/candle_agrees: the multi-bar SHAPE
    # of recent price action (see _compute_candle_pattern/
    # CANDLE_TREND_LOOKBACK above) - a genuinely different kind of read
    # from RSI/MACD/EMA-BB (all smoothed derivatives of the same close
    # series) or CMF (a volume read). Same "None means agree" convention
    # as htf_direction/vol_flow_direction above: None (no pattern fired,
    # or a bullish and bearish pattern both fired on this bar) is treated
    # as agreeing, so it never silently blocks anything unless
    # settings.REQUIRE_CANDLE_PATTERN_AGREEMENT is explicitly turned on
    # (applied a layer up, in background.py's _apply_candle_pattern_filter -
    # mirrors _apply_volume_flow_filter, not baked in here).
    candle_direction_val = series["candle_direction"].iloc[i]
    candle_pattern_val = series["candle_pattern_name"].iloc[i]
    candle_agrees = True if candle_direction_val is None else (candle_direction_val == direction)

    # macd_hist_rising/macd_hist_agrees: is the MACD histogram GROWING in
    # the crossover's direction (momentum strengthening), not just which
    # side of zero it's on. Deliberately NOT "hist > 0" - that's
    # mathematically identical to macd_state's own macd_line > signal_line
    # check (hist = macd_line - signal_line by construction, see
    # compute_series), so it would carry zero new information. This is
    # the histogram's own slope instead: for a Bullish row, today's bar
    # taller than yesterday's means the bullish push is accelerating, not
    # just barely holding above the signal line; for Bearish, the mirror
    # (falling histogram = accelerating bearish push). None only when
    # there's no previous bar to compare against (first bar of the
    # fetched window) - treated as agreeing, same "never silently block
    # on missing data" convention as every other *_agrees field above.
    # Same NOT-folded-into-`aligned`-below reasoning as vol_flow_agrees
    # above (PARAMETER_ANALYSIS_2.md Finding #2) - opt-in only, via
    # settings.REQUIRE_MACD_HIST_AGREEMENT applied a layer up in
    # background.py's _apply_macd_hist_filter.
    macd_hist_series = series["macd_hist"]
    hist_now = macd_hist_series.iloc[i]
    hist_prev = macd_hist_series.iloc[i - 1] if i > 0 else None
    macd_hist_value = round(float(hist_now), 3) if pd.notna(hist_now) else None
    macd_hist_rising = None
    if hist_prev is not None and pd.notna(hist_now) and pd.notna(hist_prev):
        macd_hist_rising = bool(hist_now > hist_prev)
    macd_hist_agrees = True if macd_hist_rising is None else (
        macd_hist_rising if direction == "Bullish" else not macd_hist_rising
    )

    # close_position/nr7/bb_width_percentile/vol_contracting/big_candle*:
    # the app's ANTICIPATORY reads (see _compute_big_candle above) -
    # genuinely different in kind from RSI/MACD/EMA-BB/CMF (all
    # confirmatory - they tell you a move already started) and even from
    # candle_agrees (a multi-bar SHAPE read, still after-the-fact). Three
    # independent ideas, grouped here since they all reuse the same
    # close_position/atr series from compute_series:
    #
    #   - vol_contracting/bb_width_percentile/nr7: is this stock currently
    #     COILING - Bollinger Band width near a multi-week low relative to
    #     its own recent history (settings.VOL_CONTRACTION_LOOKBACK bars,
    #     VOL_CONTRACTION_THRESHOLD_PCT percentile), or today's range the
    #     narrowest of the last 7 bars (classic NR7). Tight consolidation
    #     has historically preceded outsized breakouts more often than an
    #     already-wide range does (Minervini's Volatility Contraction
    #     Pattern). No inherent direction of its own (a coiled stock can
    #     break either way), so it never feeds `aligned`/signal_confirmed -
    #     shown as a "worth watching" badge only.
    #   - big_candle/big_candle_direction/big_candle_level (THIS bar) and
    #     big_candle_recent_*/big_candle_continuation (the most recent
    #     qualifying range-expansion bar within BIG_CANDLE_LOOKBACK bars,
    #     which may be this bar itself at bars_ago=0): big_candle_
    #     continuation is only meaningful for a PRIOR bar (bars_ago > 0) -
    #     has price since gone on to actually clear that bar's own
    #     high/low in its own direction, the "does yesterday's big candle
    #     level hold up" read that matters for a BTST/swing continuation
    #     decision. big_candle_agrees follows the same "None means agree"
    #     convention as every other *_agrees field above - opt-in only,
    #     via settings.REQUIRE_BIG_CANDLE_AGREEMENT in background.py's
    #     _apply_big_candle_filter.
    #   - strong_close_agrees: a simpler, BTST-oriented read - did THIS
    #     bar's own close land in the extreme top/bottom
    #     settings.STRONG_CLOSE_THRESHOLD_PCT% of its own high-low range,
    #     in this row's own direction - real buyer/seller conviction into
    #     the close, independent of range expansion (doesn't require an
    #     unusually wide bar, just a decisive close). Opt-in, via
    #     settings.REQUIRE_STRONG_CLOSE_AGREEMENT in background.py's
    #     _apply_strong_close_filter.
    close_position_raw = series["close_position"].iloc[i]
    close_position_pct = round(float(close_position_raw) * 100, 1) if pd.notna(close_position_raw) else None

    bb_width_series = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan)
    bw_now = bb_width_series.iloc[i]
    bb_width_percentile = None
    vol_contracting = False
    if pd.notna(bw_now):
        window = settings.VOL_CONTRACTION_LOOKBACK
        recent = bb_width_series.iloc[max(0, i - window + 1): i + 1].dropna()
        if len(recent) >= 5:
            bb_width_percentile = round(float((recent <= bw_now).mean() * 100), 1)
            vol_contracting = bb_width_percentile <= settings.VOL_CONTRACTION_THRESHOLD_PCT

    range_series = series["df"]["high"] - series["df"]["low"]
    nr7 = bool(i >= 6 and range_series.iloc[i] == range_series.iloc[i - 6: i + 1].min())

    big_candle_dir_series = series["big_candle_direction"]
    big_candle_level_series = series["big_candle_level"]
    big_candle_direction_val = big_candle_dir_series.iloc[i]
    big_candle = big_candle_direction_val is not None
    big_candle_level = round(float(big_candle_level_series.iloc[i]), 2) if big_candle else None

    big_candle_recent_direction = None
    big_candle_recent_level = None
    big_candle_recent_bars_ago = None
    start = max(0, i - BIG_CANDLE_LOOKBACK)
    dir_slice = big_candle_dir_series.iloc[start:i + 1]
    qualifying = dir_slice[dir_slice.notna()]
    if len(qualifying):
        j = df.index.get_loc(qualifying.index[-1])
        big_candle_recent_direction = qualifying.iloc[-1]
        big_candle_recent_level = round(float(big_candle_level_series.iloc[j]), 2)
        big_candle_recent_bars_ago = i - j

    big_candle_continuation = None
    if big_candle_recent_direction is not None and big_candle_recent_bars_ago:
        if big_candle_recent_direction == "Bullish":
            big_candle_continuation = bool(close.iloc[i] > big_candle_recent_level)
        else:
            big_candle_continuation = bool(close.iloc[i] < big_candle_recent_level)

    big_candle_agrees = True if big_candle_recent_direction is None else (big_candle_recent_direction == direction)

    strong_close_agrees = True
    if close_position_pct is not None:
        threshold = settings.STRONG_CLOSE_THRESHOLD_PCT
        if direction == "Bullish":
            strong_close_agrees = close_position_pct >= threshold
        else:
            strong_close_agrees = close_position_pct <= (100 - threshold)

    # ATR resolved here (rather than down in the stop/target block below,
    # where it used to live) because the two entry-quality reads that
    # follow immediately need it too - see entry_extension_atr/atr_pct.
    # Same series either way: computed once in compute_series.
    atr_series = series["atr"]
    atr_raw = atr_series.iloc[i]
    atr_value = round(float(atr_raw), 2) if pd.notna(atr_raw) and atr_raw > 0 else None

    # entry_extension_atr/entry_is_extended/entry_location_agrees: HOW FAR
    # price already is from its own VWAP, measured in ATR units - the
    # "am I early or am I chasing" read (PARAMETER_ANALYSIS_2.md Finding
    # #4: "nothing prices in WHERE a move already is"). A signal that
    # fires when price is already 3 ATR past VWAP is the same "Confirmed"
    # today as one that fires right as the move turns, even though the
    # first is a materially worse entry - this is what lets those two be
    # told apart. Uses session VWAP where available (intraday), falling
    # back to the anchored VWAP (meaningful on day/week bars too, where
    # session VWAP is None by construction) so this isn't silently
    # intraday-only. Signed relative to the row's own direction: a
    # Bullish row extended ABOVE VWAP is "extended" (chasing), while a
    # Bullish row still BELOW its VWAP reads negative (early / pulled
    # back), and vice versa for Bearish - so the same threshold means the
    # same thing in both directions. None whenever there's no usable VWAP
    # or ATR yet; treated as agreeing, same "never block on missing data"
    # convention as every other *_agrees field above.
    entry_extension_atr = None
    entry_is_extended = None
    entry_reference = None
    vwap_ref = vwap if vwap else avwap
    if vwap_ref and atr_value:
        entry_reference = "VWAP" if vwap else "AVWAP"
        raw_distance = (float(close.iloc[i]) - vwap_ref) / atr_value
        signed = raw_distance if direction == "Bullish" else -raw_distance
        entry_extension_atr = round(float(signed), 2)
        entry_is_extended = bool(entry_extension_atr > settings.MAX_ENTRY_EXTENSION_ATR)
    entry_location_agrees = True if entry_is_extended is None else (not entry_is_extended)

    # atr_pct/atr_floor_agrees: is this stock currently moving ENOUGH, in
    # its own percentage terms, to be worth trading at all
    # (PARAMETER_ANALYSIS_2.md Finding #5 - "no volatility floor, in
    # either engine"). ATR as a % of price rather than raw ATR, so the
    # threshold means the same thing on a Rs.150 stock and a Rs.5000 one.
    # In genuinely dead/illiquid stretches any breakout is more likely to
    # be a false start no matter how many parameters agree, simply
    # because there isn't enough real movement backing it - and for a
    # BIG-move hunt specifically, a stock whose own recent range is tiny
    # structurally can't deliver one. Distinct from the ADX regime check
    # (which reads trend STRENGTH, not movement SIZE) and from
    # vol_contracting above (which is about a coiling stock about to
    # expand - deliberately not gated for exactly that reason). None
    # whenever ATR hasn't warmed up; treated as agreeing.
    atr_pct = None
    atr_floor_agrees = True
    if atr_value and close.iloc[i]:
        atr_pct = round(float(atr_value / float(close.iloc[i]) * 100), 3)
        atr_floor_agrees = atr_pct >= effective_min_atr_pct(timeframe)

    # aligned: 0-4, how many of the 4 parameters currently agree with
    # this row's direction (the 3 directional ones, always 2 or 3 of
    # them by construction, plus Relative Volume as an independent 4th).
    # settings.MIN_REQUIRED (2/3/4-of-4) is the bar for "confirmed"
    # below and for what counts as a fresh signal.
    aligned = dir_match_count + int(vol_confirmed)

    # htf_direction/htf_agrees: does the higher timeframe's own trend
    # agree with this candle's direction? None means "not applicable for
    # this timeframe" (see _HTF_RESAMPLE) - treated as agreeing so it
    # never silently filters out timeframes it has no opinion on.
    htf_direction = _higher_timeframe_direction(df, timeframe)
    htf_agrees = True if htf_direction is None else (htf_direction == direction)

    in_opening_window = _in_opening_window(df.index[i], timeframe) or _in_4hour_warmup(now, timeframe)

    # ATR-based risk layer: a suggested stop-loss/target scaled to this
    # stock's OWN recent volatility (Wilder's ATR - see compute_atr above)
    # rather than a flat percentage, so a quiet stock gets a tight
    # stop/target and a volatile one gets a wide one automatically. Pure
    # display information alongside the signal - it never feeds into
    # `aligned`/signal_confirmed and this app never places an order.
    # None whenever there isn't enough history yet for ATR to have
    # warmed up (same convention as vwap/adx above).
    # atr_value already resolved above (moved up so the entry-location /
    # ATR-floor reads could use it too) - this block just turns it into a
    # suggested stop/target.
    stop = target = risk_reward = None
    if atr_value:
        entry = float(close.iloc[i])
        if direction == "Bullish":
            stop = round(entry - settings.ATR_STOP_MULTIPLIER * atr_value, 2)
            target = round(entry + settings.ATR_TARGET_MULTIPLIER * atr_value, 2)
        else:
            stop = round(entry + settings.ATR_STOP_MULTIPLIER * atr_value, 2)
            target = round(entry - settings.ATR_TARGET_MULTIPLIER * atr_value, 2)
        risk_reward = round(settings.ATR_TARGET_MULTIPLIER / settings.ATR_STOP_MULTIPLIER, 2)

    # Position-size suggestion (NEXT_HORIZON_RESEARCH.md Finding 4's
    # fixed-fractional sizing): how many shares to risk exactly
    # settings.RISK_PER_TRADE_PCT of settings.ACCOUNT_CAPITAL if the
    # suggested ATR stop above is hit - risk_amount / per-share risk
    # distance, floored to a whole share. None whenever `stop` itself is
    # None (not enough ATR history yet) or the per-share risk distance
    # rounds to zero (a stop multiplier/ATR so tiny the math is
    # meaningless) - never divides by zero. This is a SUGGESTION for you
    # to size a real order yourself; this app places no orders and has
    # no visibility into your real account, hence ACCOUNT_CAPITAL being
    # a number you tell it (see config.py), not one it reads anywhere.
    position_qty = position_risk_amount = None
    if stop is not None:
        per_share_risk = abs(entry - stop)
        if per_share_risk > 0:
            risk_amount = settings.ACCOUNT_CAPITAL * settings.RISK_PER_TRADE_PCT / 100
            qty = int(risk_amount // per_share_risk)
            if qty > 0:
                position_qty = qty
                position_risk_amount = round(qty * per_share_risk, 2)

    return {
        "close": round(float(close.iloc[i]), 2),
        "rsi": round(float(rsi_line.iloc[i]), 1),
        "rsi_state": "Bullish" if rsi_line.iloc[i] > rsi_smooth.iloc[i] else "Bearish",
        "macd_params": f"{fast},{slow},{sig}",
        "macd_state": "Bullish" if macd_line.iloc[i] > signal_line.iloc[i] else "Bearish",
        "aligned": aligned,
        # Which way the *majority* of the 3 indicators currently point,
        # regardless of whether today's candle is the exact one where
        # that alignment first formed (fresh_signal below is that
        # narrower, crossover-only flag). With 3 indicators there's
        # never a tie, so align_count >= 2 always means bullish majority.
        "direction": direction,
        "fresh_signal": fresh_signal,
        "timestamp": df.index[i].isoformat(),
        "vwap": round(vwap, 2) if vwap else None,
        "vs_vwap": vs_vwap,
        "avwap": avwap,
        "vs_avwap": vs_avwap,
        "avwap_anchor_time": avwap_anchor_time,
        "breakout_state": breakout_state,
        "vol_multiple": vol_multiple,
        "volume": volume,
        "vol_confirmed": vol_confirmed,
        "cmf": cmf_value,
        "vol_flow_direction": vol_flow_direction,
        "candle_pattern": candle_pattern_val,
        "candle_direction": candle_direction_val,
        "candle_agrees": candle_agrees,
        "macd_hist": macd_hist_value,
        "macd_hist_rising": macd_hist_rising,
        "macd_hist_agrees": macd_hist_agrees,
        "close_position_pct": close_position_pct,
        "nr7": nr7,
        "bb_width_percentile": bb_width_percentile,
        "vol_contracting": vol_contracting,
        "big_candle": big_candle,
        "big_candle_direction": big_candle_direction_val,
        "big_candle_level": big_candle_level,
        "big_candle_recent_direction": big_candle_recent_direction,
        "big_candle_recent_level": big_candle_recent_level,
        "big_candle_recent_bars_ago": big_candle_recent_bars_ago,
        "big_candle_continuation": big_candle_continuation,
        "big_candle_agrees": big_candle_agrees,
        "strong_close_agrees": strong_close_agrees,
        "entry_extension_atr": entry_extension_atr,
        "entry_is_extended": entry_is_extended,
        "entry_reference": entry_reference,
        "entry_location_agrees": entry_location_agrees,
        "atr_pct": atr_pct,
        "atr_floor_agrees": atr_floor_agrees,
        "adx": adx_value,
        "regime": regime,
        "vol_threshold_effective": effective_vol_threshold,
        "htf_direction": htf_direction,
        "htf_agrees": htf_agrees,
        "in_opening_window": in_opening_window,
        # A stricter, opt-in read of "this row currently has a signal":
        # the aligned/MIN_REQUIRED state (now out of 4 parameters,
        # Relative Volume included as an equal 4th rather than an
        # always-mandatory add-on - see aligned above) PLUS
        # higher-timeframe agreement (where applicable) PLUS NOT in the
        # noisy opening window - meant to cut down on the false signals
        # a bare majority alignment produces on noisy 15-min candles.
        # Doesn't replace aligned/direction above (still shown
        # everywhere for transparency) - it's an additional, opt-in
        # filter surfaced in the UI and used to gate Telegram alerts.
        "signal_confirmed": bool(
            aligned >= settings.MIN_REQUIRED and htf_agrees and not in_opening_window
        ),
        "atr": atr_value,
        "stop": stop,
        "target": target,
        "risk_reward": risk_reward,
        "position_qty": position_qty,
        "position_risk_amount": position_risk_amount,
    }
