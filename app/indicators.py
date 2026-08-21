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

OPENING_WINDOW_MINUTES = 15  # signals formed in the first N minutes after the 9:15 IST open
                              # are excluded from signal_confirmed/in_opening_window below -
                              # these candles are usually the most gap-driven and noisy of the day


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
    align_count = (
        int(htf_series["rsi_line"].iloc[i] > htf_series["rsi_smooth"].iloc[i])
        + int(htf_series["macd_line"].iloc[i] > htf_series["signal_line"].iloc[i])
        + int(htf_series["ema9"].iloc[i] > htf_series["bb_mid"].iloc[i])
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

    ema9 = close.ewm(span=settings.EMA_LENGTH, adjust=False).mean()
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

    rsi_up, rsi_dn = _cross_up(rsi_line, rsi_smooth), _cross_down(rsi_line, rsi_smooth)
    macd_up, macd_dn = _cross_up(macd_line, signal_line), _cross_down(macd_line, signal_line)
    ema_up, ema_dn = _cross_up(ema9, bb_mid), _cross_down(ema9, bb_mid)

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
        "rsi_up": rsi_up, "rsi_dn": rsi_dn,
        "macd_up": macd_up, "macd_dn": macd_dn,
        "ema_up": ema_up, "ema_dn": ema_dn,
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
    ema9, bb_mid = series["ema9"], series["bb_mid"]
    fast, slow, sig = series["macd_params"]

    i = len(df) - 1  # last closed candle
    bull_count = int(series["rsi_up"].iloc[i]) + int(series["macd_up"].iloc[i]) + int(series["ema_up"].iloc[i])
    bear_count = int(series["rsi_dn"].iloc[i]) + int(series["macd_dn"].iloc[i]) + int(series["ema_dn"].iloc[i])

    # fresh_signal only tracks the 3 CROSSOVER-capable indicators (RSI,
    # MACD, EMA/BB) - Relative Volume is a continuous magnitude state,
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

    align_count = (
        int(rsi_line.iloc[i] > rsi_smooth.iloc[i])
        + int(macd_line.iloc[i] > signal_line.iloc[i])
        + int(ema9.iloc[i] > bb_mid.iloc[i])
    )

    vwap = session_vwap(df, timeframe)
    vs_vwap = None
    if vwap:
        vs_vwap = "Above" if close.iloc[i] > vwap else "Below"

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
    vol_flow_agrees = True if vol_flow_direction is None else (vol_flow_direction == direction)

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

    return {
        "close": round(float(close.iloc[i]), 2),
        "rsi": round(float(rsi_line.iloc[i]), 1),
        "rsi_state": "Bullish" if rsi_line.iloc[i] > rsi_smooth.iloc[i] else "Bearish",
        "macd_params": f"{fast},{slow},{sig}",
        "macd_state": "Bullish" if macd_line.iloc[i] > signal_line.iloc[i] else "Bearish",
        "ema_bb_state": "Bullish" if ema9.iloc[i] > bb_mid.iloc[i] else "Bearish",
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
        "breakout_state": breakout_state,
        "vol_multiple": vol_multiple,
        "volume": volume,
        "vol_confirmed": vol_confirmed,
        "cmf": cmf_value,
        "vol_flow_direction": vol_flow_direction,
        "vol_flow_agrees": vol_flow_agrees,
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
    }
