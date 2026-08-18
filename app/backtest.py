"""
Historical backtest for the "High-Conviction Setups" composite rule
(see background.py's _apply_oi_screener_fields). Instead of guessing a
win rate, this replays the exact same rule - strict 3-of-3 confluence,
OI structure, an accelerating OI break signal, volume >=1.5x average,
and price vs VWAP - bar by bar over real historical candle + Open
Interest data pulled from your own Kite connection, and reports what
actually would have happened.

Reuses indicators.compute_series() and scanner.classify_oi_trend() /
classify_oi_structure() directly rather than reimplementing the logic
in a separate form, so results here stay faithful to what the live
dashboard is actually doing.

Runs as its own background thread (like the main scanner) rather than
inside a request handler - fetching + replaying a whole watchlist can
easily take a minute or two, well past what a web request should block
on. Poll get_backtest_state() from the dashboard to show progress.

Known limitation, surfaced in the report: Kite's instrument list only
ever contains CURRENTLY ACTIVE futures contracts, so historical Open
Interest is only available back to whenever the current near-month
contract started trading - realistically ~30-45 days, even if you ask
for a longer window. Price/indicator history has no such cap, but any
rule component that depends on OI (structure, break signal) simply
won't fire before that point.
"""
import datetime as dt
import logging
import threading
import time

import numpy as np
import pandas as pd

from .config import settings
from .indicators import compute_series
from .scanner import (
    _NON_STOCK_FNO_NAMES,
    _load_instrument_map,
    classify_oi_trend,
    now_ist,
)

log = logging.getLogger(__name__)

_INTRADAY_TIMEFRAMES = ("15minute", "4hour")
DEFAULT_HORIZONS = (5, 10, 20)
WARMUP_DAYS = 20          # extra calendar days fetched before the requested
                           # window purely so indicators are warmed up -
                           # trades are never counted in this stretch.
MAX_BACKTEST_DAYS = 90    # sane upper bound; OI depth caps real coverage well below this anyway
_RATE_LIMIT_PAUSE = 0.35  # ~3 req/sec, matching Kite's historical-data rate limit

_fut_token_cache = {"date": None, "map": {}}


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


def start_backtest(kite, symbols=None, timeframe=None, days=30, horizons=DEFAULT_HORIZONS) -> dict:
    """Kicks off a backtest run in a background thread. Returns
    {"started": True} or {"started": False, "reason": ...} if one is
    already running - only one backtest runs at a time."""
    with _bt_lock:
        if _bt_state["status"] == "running":
            return {"started": False, "reason": "A backtest is already running."}
        symbols = list(symbols or settings.WATCHLIST)
        timeframe = timeframe or settings.TIMEFRAME
        _bt_state["status"] = "running"
        _bt_state["progress"] = {"done": 0, "total": len(symbols), "symbol": None}
        _bt_state["params"] = {"timeframe": timeframe, "days": days, "horizons": list(horizons)}
        _bt_state["result"] = None
        _bt_state["error"] = None
        _bt_state["started_at"] = now_ist().isoformat(timespec="seconds")
        _bt_state["finished_at"] = None

    thread = threading.Thread(
        target=_run_backtest_job, args=(kite, symbols, timeframe, days, horizons), daemon=True
    )
    thread.start()
    return {"started": True}


def _run_backtest_job(kite, symbols, timeframe, days, horizons):
    try:
        result = run_backtest(kite, symbols, timeframe=timeframe, days=days, horizons=horizons, progress_cb=_progress_cb)
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

def _load_fut_token_map(kite) -> dict:
    """Like scanner._load_current_fut_map, but also keeps the futures
    contract's instrument_token (needed for historical_data(oi=True) -
    the live scanner only ever needs the tradingsymbol for quote())."""
    today = dt.date.today()
    if _fut_token_cache["date"] == today.isoformat() and _fut_token_cache["map"]:
        return _fut_token_cache["map"]
    instruments = kite.instruments("NFO")
    nearest = {}
    for row in instruments:
        if row.get("instrument_type") != "FUT":
            continue
        name = (row.get("name") or "").strip()
        expiry = row.get("expiry")
        if not name or name.upper() in _NON_STOCK_FNO_NAMES:
            continue
        if not expiry or expiry < today:
            continue
        current = nearest.get(name)
        if current is None or expiry < current[0]:
            nearest[name] = (expiry, row["tradingsymbol"], row["instrument_token"])
    fut_map = {
        name: {"tradingsymbol": symbol, "token": token, "expiry": expiry}
        for name, (expiry, symbol, token) in nearest.items()
    }
    if fut_map:
        _fut_token_cache["date"] = today.isoformat()
        _fut_token_cache["map"] = fut_map
    return fut_map


def _fetch_history(kite, symbol, token, fut_info, timeframe, days):
    """Returns (df, oi_earliest_date) for one symbol - df has the usual
    open/high/low/close/volume columns plus an 'oi' column (NaN where
    historical OI wasn't available, e.g. before the current futures
    contract existed). oi_earliest_date is None if no OI data came
    back at all."""
    to_date = now_ist()
    from_date = to_date - dt.timedelta(days=days)
    interval = "60minute" if timeframe == "4hour" else timeframe

    eq_data = kite.historical_data(token, from_date, to_date, interval)
    df = pd.DataFrame(eq_data)
    if df.empty:
        return df, None
    df = df.rename(columns={"date": "timestamp"}).set_index("timestamp")
    df["oi"] = np.nan

    oi_from = None
    if fut_info and fut_info.get("token"):
        try:
            oi_data = kite.historical_data(fut_info["token"], from_date, to_date, interval, oi=True)
            oi_df = pd.DataFrame(oi_data)
            if not oi_df.empty and "oi" in oi_df.columns:
                oi_df = oi_df.rename(columns={"date": "timestamp"}).set_index("timestamp")
                df["oi"] = oi_df["oi"].reindex(df.index)
                oi_from = oi_df.index.min().date()
        except Exception as exc:  # noqa: BLE001 - OI history is a bonus, never fatal to the backtest
            log.warning("Historical OI fetch failed for %s: %s", symbol, exc)

    if timeframe == "4hour":
        df = df.resample("4h", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"}
        ).dropna(subset=["open", "high", "low", "close", "volume"])

    return df, oi_from


# --------------------------------------------------------------------------
# Vectorized replay of the live rule set over a symbol's full history
# --------------------------------------------------------------------------

def _direction_series(series: dict):
    rsi_line, rsi_smooth = series["rsi_line"], series["rsi_smooth"]
    macd_line, signal_line = series["macd_line"], series["signal_line"]
    ema9, bb_mid = series["ema9"], series["bb_mid"]
    align_count = (
        (rsi_line > rsi_smooth).astype(int)
        + (macd_line > signal_line).astype(int)
        + (ema9 > bb_mid).astype(int)
    )
    aligned = pd.concat([align_count, 3 - align_count], axis=1).max(axis=1)
    direction = pd.Series(np.where(align_count >= 2, "Bullish", "Bearish"), index=align_count.index)
    return aligned, direction


def _walkforward_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative intraday VWAP as of each bar, using only that
    session's bars up to and including the current one - the
    walk-forward equivalent of indicators.session_vwap (which only
    ever computes the CURRENT day's VWAP for live scanning). Using the
    whole day's final VWAP here would leak future data into earlier
    bars, biasing the backtest."""
    dates = df.index.map(lambda ts: ts.date())
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tpv = typical * df["volume"]
    cum_tpv = tpv.groupby(dates).cumsum()
    cum_vol = df["volume"].groupby(dates).cumsum()
    return cum_tpv / cum_vol.replace(0, np.nan)


def _oi_structure_series(price_chg_pct: pd.Series, oi_chg_pct: pd.Series, threshold: float = 0.05) -> pd.Series:
    """Vectorized version of scanner.classify_oi_structure, applied
    across a whole history at once."""
    price_up = price_chg_pct > threshold
    price_down = price_chg_pct < -threshold
    oi_up = oi_chg_pct > threshold
    oi_down = oi_chg_pct < -threshold
    conditions = [price_up & oi_up, price_down & oi_up, price_up & oi_down, price_down & oi_down]
    choices = ["Long Buildup", "Short Buildup", "Short Covering", "Long Unwinding"]
    structure = np.select(conditions, choices, default="Neutral")
    valid = price_chg_pct.notna() & oi_chg_pct.notna()
    return pd.Series(np.where(valid, structure, None), index=price_chg_pct.index, dtype=object)


def _oi_trend_series(oi: pd.Series):
    """Walks the OI series bar by bar, calling the actual live
    scanner.classify_oi_trend() on the growing history each time (same
    function background.py uses scan-to-scan) - here it's bar-to-bar
    instead, which is the closest bar-level equivalent for a replay."""
    labels, unusuals = [], []
    history = []
    for val in oi:
        if pd.isna(val):
            labels.append(None)
            unusuals.append(False)
            continue
        history.append(float(val))
        result = classify_oi_trend(history)
        labels.append(result["label"])
        unusuals.append(result["unusual"])
    return (
        pd.Series(labels, index=oi.index, dtype=object),
        pd.Series(unusuals, index=oi.index),
    )


def _compute_trade(df: pd.DataFrame, entry_pos: int, direction: str, symbol: str, horizons):
    """Entry is executed at the NEXT bar's open after the signal bar
    (never the signal bar's own close, to avoid lookahead bias).
    Returns are computed at each requested horizon (in bars), plus the
    single worst adverse move (drawdown) seen at any point during the
    longest hold - None if there isn't enough remaining data for even
    the shortest horizon."""
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

    returns = {}
    for h in horizons:
        exit_pos = entry_pos + 1 + h
        if exit_pos >= len(df):
            continue
        exit_price = float(df["close"].iloc[exit_pos])
        returns[h] = round(sign * (exit_price - entry_price) / entry_price * 100, 3)
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


def _replay_symbol(df: pd.DataFrame, symbol: str, timeframe: str, window_start, horizons):
    series = compute_series(df, timeframe)
    if "error" in series:
        return []

    aligned, direction = _direction_series(series)
    close = df["close"]
    vol_multiple = df["volume"] / series["vol_avg"]

    dates = df.index.map(lambda ts: ts.date())
    price_baseline = close.groupby(dates).transform("first")
    price_chg_pct = pd.Series(
        np.where(price_baseline != 0, (close - price_baseline) / price_baseline * 100, np.nan),
        index=df.index,
    )

    has_oi = "oi" in df.columns and df["oi"].notna().any()
    if has_oi:
        oi = df["oi"]
        oi_baseline = oi.groupby(dates).transform("first")
        oi_chg_pct = pd.Series(
            np.where((oi_baseline.notna()) & (oi_baseline != 0), (oi - oi_baseline) / oi_baseline * 100, np.nan),
            index=df.index,
        )
        structure = _oi_structure_series(price_chg_pct, oi_chg_pct)
        oi_trend_label, oi_unusual = _oi_trend_series(oi)
    else:
        structure = pd.Series(None, index=df.index, dtype=object)
        oi_trend_label = pd.Series(None, index=df.index, dtype=object)
        oi_unusual = pd.Series(False, index=df.index)

    accel_strong = (oi_trend_label == "Accelerating") | oi_unusual.fillna(False)
    break_signal = pd.Series(None, index=df.index, dtype=object)
    break_signal[(structure == "Long Buildup") & accel_strong] = "Break Up"
    break_signal[(structure == "Short Buildup") & accel_strong] = "Break Down"

    if timeframe in _INTRADAY_TIMEFRAMES:
        vwap = _walkforward_vwap(df)
        vs_vwap = pd.Series(
            np.where(close > vwap, "Above", np.where(close < vwap, "Below", None)), index=df.index, dtype=object
        )
    else:
        vs_vwap = pd.Series(None, index=df.index, dtype=object)

    structure_agrees = (
        ((direction == "Bullish") & (structure == "Long Buildup"))
        | ((direction == "Bearish") & (structure == "Short Buildup"))
    )
    positional_qualified = (aligned >= settings.MIN_REQUIRED) & structure_agrees
    vs_vwap_agrees = (
        ((direction == "Bullish") & (vs_vwap == "Above"))
        | ((direction == "Bearish") & (vs_vwap == "Below"))
    )
    break_agrees = (
        ((direction == "Bullish") & (break_signal == "Break Up"))
        | ((direction == "Bearish") & (break_signal == "Break Down"))
    )
    vol_confirmed = vol_multiple.fillna(0) >= 1.5

    high_conviction = positional_qualified & (aligned == 3) & break_agrees & vol_confirmed & vs_vwap_agrees
    entries = high_conviction & ~high_conviction.shift(1).fillna(False)

    trades = []
    for pos in np.flatnonzero(entries.to_numpy()):
        ts = df.index[pos]
        if ts.to_pydatetime().replace(tzinfo=None) < window_start:
            continue  # inside the warm-up buffer, not the requested window
        trade = _compute_trade(df, pos, direction.iloc[pos], symbol, horizons)
        if trade:
            trades.append(trade)
    return trades


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------

def _summarize(trades, horizons):
    summary = {}
    for h in horizons:
        rets = [t["returns_pct"][h] for t in trades if h in t["returns_pct"]]
        if not rets:
            summary[str(h)] = {"trade_count": 0}
            continue
        wins = [r for r in rets if r > 0]
        summary[str(h)] = {
            "trade_count": len(rets),
            "win_rate_pct": round(len(wins) / len(rets) * 100, 1),
            "avg_return_pct": round(sum(rets) / len(rets), 3),
            "best_return_pct": round(max(rets), 3),
            "worst_return_pct": round(min(rets), 3),
        }
    maes = [t["mae_pct"] for t in trades]
    summary["overall"] = {
        "total_trades": len(trades),
        "bullish_trades": sum(1 for t in trades if t["direction"] == "Bullish"),
        "bearish_trades": sum(1 for t in trades if t["direction"] == "Bearish"),
        "avg_drawdown_pct": round(sum(maes) / len(maes), 3) if maes else None,
        "worst_drawdown_pct": round(min(maes), 3) if maes else None,
    }
    return summary


def run_backtest(kite, symbols, timeframe="15minute", days=30, horizons=DEFAULT_HORIZONS, progress_cb=None) -> dict:
    days = min(int(days or 30), MAX_BACKTEST_DAYS)
    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0})) or DEFAULT_HORIZONS

    instruments = _load_instrument_map(kite)
    fut_map = _load_fut_token_map(kite)
    to_date = now_ist()
    window_start = to_date - dt.timedelta(days=days)
    fetch_days = days + WARMUP_DAYS

    trades = []
    symbol_notes = {}
    oi_earliest = None

    for idx, symbol in enumerate(symbols):
        if progress_cb:
            progress_cb(idx, len(symbols), symbol)
        token = instruments.get(symbol)
        if not token:
            symbol_notes[symbol] = "symbol not found on NSE"
            continue
        try:
            df, oi_from = _fetch_history(kite, symbol, token, fut_map.get(symbol), timeframe, fetch_days)
        except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the whole backtest
            symbol_notes[symbol] = f"history fetch failed: {exc}"
            time.sleep(_RATE_LIMIT_PAUSE)
            continue
        time.sleep(_RATE_LIMIT_PAUSE)

        if df is None or df.empty or len(df) < max(settings.BB_LENGTH, 35) + 5:
            symbol_notes[symbol] = "not enough historical candles returned"
            continue
        if oi_from is not None and (oi_earliest is None or oi_from > oi_earliest):
            oi_earliest = oi_from

        try:
            symbol_trades = _replay_symbol(df, symbol, timeframe, window_start.replace(tzinfo=None), horizons)
        except Exception as exc:  # noqa: BLE001
            log.exception("Backtest replay failed for %s", symbol)
            symbol_notes[symbol] = f"replay failed: {exc}"
            continue
        trades.extend(symbol_trades)

    if progress_cb:
        progress_cb(len(symbols), len(symbols), None)

    trades.sort(key=lambda t: t["entry_time"])

    return {
        "timeframe": timeframe,
        "days_requested": days,
        "horizons": list(horizons),
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_end": to_date.isoformat(timespec="seconds"),
        "oi_history_earliest": oi_earliest.isoformat() if oi_earliest is not None else None,
        "symbols_scanned": len(symbols),
        "symbols_with_trades": len({t["symbol"] for t in trades}),
        "symbols_skipped": symbol_notes,
        "trades": trades,
        "summary": _summarize(trades, horizons),
        "generated_at": to_date.isoformat(timespec="seconds"),
    }
