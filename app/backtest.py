"""
Historical backtest for the core RSI + MACD + EMA/Bollinger confluence
signal - the same 3-indicator rule the live dashboard uses to decide
when a stock "has a signal" (Quick Settings' Required dropdown: 2-of-3
or 3-of-3 strict). Instead of guessing a win rate, this replays that
exact rule bar by bar over real historical price data pulled from your
own Kite connection, and reports what actually would have happened -
win rate, average return, and worst drawdown per trade, at whichever
holding-period horizons you pick, with Bullish and Bearish setups
broken out separately since a strategy's edge (or lack of one) often
differs by direction.

Deliberately does NOT layer on Open Interest structure, OI
acceleration, volume, or VWAP - this tests the pure 3-parameter
confluence signal on its own, not the broader "High-Conviction"
composite filter shown on the dashboard.

Reuses indicators.compute_series() directly rather than reimplementing
the indicator math in a separate form, so results here stay faithful
to what the live dashboard is actually computing.

Runs as its own background thread (like the main scanner) rather than
inside a request handler - fetching + replaying a whole watchlist can
take a while, well past what a web request should block on. Poll
get_backtest_state() from the dashboard to show progress.
"""
import datetime as dt
import logging
import threading
import time

import numpy as np
import pandas as pd

from .config import settings
from .indicators import compute_series
from .scanner import _load_instrument_map, now_ist

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

def _fetch_history(token, timeframe, days, kite):
    """Returns a DataFrame of open/high/low/close/volume candles for one
    symbol - just the one Kite API call needed for the pure 3-indicator
    signal (no futures/OI lookup, unlike the old High-Conviction
    backtest, since this rule doesn't use OI at all)."""
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
# Vectorized replay of the 3-indicator confluence signal over a symbol's
# full history
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
    """Entry = the bar where alignment first reaches settings.MIN_REQUIRED
    (a rising edge - so a setup that persists for many bars only counts
    as one trade, not one per bar), exactly matching the live dashboard's
    own definition of "a stock has a signal" (Quick Settings' Required
    dropdown). No OI, volume or VWAP involved - purely the 3 indicators."""
    series = compute_series(df, timeframe)
    if "error" in series:
        return []

    aligned, direction = _direction_series(series)
    has_signal = aligned >= settings.MIN_REQUIRED
    entries = has_signal & ~has_signal.shift(1).fillna(False)

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
    and poorly the other."""
    bullish = [t for t in trades if t["direction"] == "Bullish"]
    bearish = [t for t in trades if t["direction"] == "Bearish"]
    return {
        "all": _summarize_group(trades, horizons),
        "bullish": _summarize_group(bullish, horizons),
        "bearish": _summarize_group(bearish, horizons),
    }


def run_backtest(kite, symbols, timeframe="15minute", days=30, horizons=DEFAULT_HORIZONS, progress_cb=None) -> dict:
    days = min(int(days or 30), MAX_BACKTEST_DAYS)
    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0})) or DEFAULT_HORIZONS

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
            symbol_trades = _replay_symbol(df, symbol, timeframe, window_start.replace(tzinfo=None), horizons)
        except Exception as exc:  # noqa: BLE001
            log.exception("Backtest replay failed for %s", symbol)
            symbol_notes[symbol] = f"replay failed: {exc}"
            continue
        trades.extend(symbol_trades)

    if progress_cb:
        progress_cb(len(symbols), len(symbols), None)

    trades.sort(key=lambda t: t["entry_time"])

    # Without OI/volume filtering the signal fires far more often -
    # a loose 2-of-3 Required setting over a big watchlist can produce
    # thousands of trades. Win-rate/return stats below are computed from
    # the FULL trade list either way; only the trade-by-trade list sent
    # to the browser for display is capped, so the page stays responsive
    # and the response doesn't balloon into megabytes.
    summary = _summarize(trades, horizons)
    total_trade_count = len(trades)
    display_trades = trades[-MAX_TRADES_RETURNED:]

    return {
        "timeframe": timeframe,
        "days_requested": days,
        "horizons": list(horizons),
        "min_required": settings.MIN_REQUIRED,
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
