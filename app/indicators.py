"""
The Scanner logic: RSI(9) vs its smoothing line, MACD vs its signal
line, 9 EMA vs the Bollinger Band middle line, and the "N of 3"
confluence rule - same definitions used in the Pine Script / LipiScript
versions and the earlier backtest, so results here should line up with
what you've already seen on your charts.
"""
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


_INTRADAY_TIMEFRAMES = ("15minute", "30minute", "60minute", "4hour")


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
        "rsi_up": rsi_up, "rsi_dn": rsi_dn,
        "macd_up": macd_up, "macd_dn": macd_dn,
        "ema_up": ema_up, "ema_dn": ema_dn,
        "macd_params": (fast, slow, sig),
    }


def compute_signal(df: pd.DataFrame, timeframe: str) -> dict:
    """df must have a 'close' column, oldest row first. Returns the
    latest confluence state - current bullish/bearish alignment count,
    whether a fresh signal fired on the most recently CLOSED candle,
    and the individual indicator values for display."""
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

    fresh_signal = None
    if bull_count >= settings.MIN_REQUIRED:
        fresh_signal = "Bullish"
    elif bear_count >= settings.MIN_REQUIRED:
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

    return {
        "close": round(float(close.iloc[i]), 2),
        "rsi": round(float(rsi_line.iloc[i]), 1),
        "rsi_state": "Bullish" if rsi_line.iloc[i] > rsi_smooth.iloc[i] else "Bearish",
        "macd_params": f"{fast},{slow},{sig}",
        "macd_state": "Bullish" if macd_line.iloc[i] > signal_line.iloc[i] else "Bearish",
        "ema_bb_state": "Bullish" if ema9.iloc[i] > bb_mid.iloc[i] else "Bearish",
        "aligned": max(align_count, 3 - align_count),
        # Which way the *majority* of the 3 indicators currently point,
        # regardless of whether today's candle is the exact one where
        # that alignment first formed (fresh_signal below is that
        # narrower, crossover-only flag). With 3 indicators there's
        # never a tie, so align_count >= 2 always means bullish majority.
        "direction": "Bullish" if align_count >= 2 else "Bearish",
        "fresh_signal": fresh_signal,
        "timestamp": df.index[i].isoformat(),
        "vwap": round(vwap, 2) if vwap else None,
        "vs_vwap": vs_vwap,
        "breakout_state": breakout_state,
        "vol_multiple": vol_multiple,
    }
