"""
Forward-testing signal journal (NEXT_HORIZON_RESEARCH.md Finding 3): lets
you log a "paper trade" straight off a live dashboard row, then have the
background scan loop check back once enough NEW candles have closed and
record what actually happened - a live, walk-forward answer to "does this
signal keep working going forward", distinct from backtest.py's replay of
already-known history.

Entry/exit follow EXACTLY the same convention as backtest.py's
_compute_trade, for a genuine apples-to-apples comparison against a
backtest run on the same symbol/timeframe/params: the row you log from is
treated as the SIGNAL bar (its own timestamp - the last CLOSED candle at
the moment you clicked "Log Paper Trade"), entry fills at the NEXT bar's
open (never the signal bar's own close - no lookahead), and exit is the
close price `horizon_bars` bars after entry. A trade sits in "open" status
(displayed as "Entry pending" until the entry bar itself has closed, then
"Open" with its live entry price) until enough bars have closed for a
resolution, at which point it moves to "resolved" with a realized
return_pct (cost-adjusted the same way backtest.py is, if you set
cost_pct/slippage_pct at log time).

PERSISTENCE CAVEAT (see config.JOURNAL_FILE): like every other piece of
scan state in this app, the journal lives in a local JSON file on the
container's own disk - there is no persistent Railway volume. A trade
that's still OPEN at the moment of a redeploy is lost; a trade that has
already RESOLVED survives (it's saved back to disk the instant it
resolves). Use the /journal page's CSV export before a deploy if you have
open trades you don't want to risk losing.
"""
import datetime as dt
import json
import logging
import os
import threading
import uuid

import pandas as pd

from .config import settings, JOURNAL_FILE
from .scanner import _load_instrument_map, fetch_candles, now_ist

log = logging.getLogger(__name__)

# How many bars ahead a paper trade resolves by default, when the caller
# (web.py's /api/journal/log) doesn't specify one - 10 bars matches the
# `ref_horizon` compute_param_weights already defaults to on the Backtest
# page, so a journal trade's realized outcome lines up with the same
# horizon you're used to seeing weight/backtest stats reported at.
DEFAULT_HORIZON_BARS = 10

# Snapshot of a live result row's own indicator reading at the moment you
# logged it - kept alongside the trade so you can look back later and ask
# "did signal_confirmed rows actually do better than unconfirmed ones",
# "did HTF-agreeing trades outperform", etc., without needing to have
# separately recorded any of this yourself.
_SNAPSHOT_FIELDS = [
    "rsi", "rsi_state", "macd_state", "ema_bb_state", "aligned",
    "vol_multiple", "vol_confirmed", "cmf", "vol_flow_direction",
    "candle_pattern", "candle_direction", "htf_direction", "adx", "regime",
    "signal_confirmed", "weighted_score", "in_opening_window",
]

_lock = threading.Lock()
_state = {"trades": []}  # list of trade dicts, newest-logged last


def _load():
    if not os.path.exists(JOURNAL_FILE):
        return
    try:
        with open(JOURNAL_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict) and isinstance(saved.get("trades"), list):
            with _lock:
                _state["trades"] = saved["trades"]
    except (json.JSONDecodeError, OSError):
        pass


def _save():
    with _lock:
        snapshot = {"trades": _state["trades"]}
    try:
        with open(JOURNAL_FILE, "w") as f:
            json.dump(snapshot, f, default=str)
    except Exception:  # noqa: BLE001 - persistence must never crash the caller
        log.exception("Failed to persist signal journal")


_load()


def get_journal_state():
    """Returns {"trades": [...]} (newest-logged first) plus a "summary"
    scoreboard computed from RESOLVED trades only - open trades have no
    outcome yet, so they're excluded from the win-rate/avg-return math
    (they still show up in the trades list itself, with status="open").

    This scoreboard is LIVE-ONLY (realized forward performance), not
    automatically compared against a backtest number - see the /journal
    page's own note on this. Comparing to what a backtest predicted for
    the same symbol/timeframe/params is a manual next step (run the same
    configuration through /backtest over a matching date range) rather
    than something computed here, to keep this a small, honest v1."""
    with _lock:
        trades = list(reversed(_state["trades"]))  # newest first for display

    resolved = [t for t in trades if t["status"] == "resolved"]
    wins = [t for t in resolved if t.get("return_pct") is not None and t["return_pct"] > 0]
    bullish_resolved = [t for t in resolved if t["direction"] == "Bullish"]
    bearish_resolved = [t for t in resolved if t["direction"] == "Bearish"]

    def _stats(group):
        if not group:
            return {"count": 0, "win_rate_pct": None, "avg_return_pct": None}
        g_wins = [t for t in group if t.get("return_pct") is not None and t["return_pct"] > 0]
        rets = [t["return_pct"] for t in group if t.get("return_pct") is not None]
        return {
            "count": len(group),
            "win_rate_pct": round(len(g_wins) / len(group) * 100, 1) if group else None,
            "avg_return_pct": round(sum(rets) / len(rets), 3) if rets else None,
        }

    summary = {
        "open_count": len([t for t in trades if t["status"] == "open"]),
        "all": _stats(resolved),
        "bullish": _stats(bullish_resolved),
        "bearish": _stats(bearish_resolved),
    }
    return {"trades": trades, "summary": summary}


def log_paper_trade(row: dict, timeframe: str, horizon_bars: int = DEFAULT_HORIZON_BARS,
                     cost_pct: float = 0.0, slippage_pct: float = 0.0):
    """Logs a new paper trade from a live scan result row (as returned by
    scanner.scan_watchlist / indicators.compute_signal, already enriched
    by background.py's _apply_* filters). `row` must have a real
    direction (no error, no None direction) - callers should check this
    before calling, same convention as everywhere else `row.get("error")`
    is checked first.

    signal_time is row["timestamp"] - the last CLOSED candle at the
    moment you're logging, which becomes the "signal bar" for entry/exit
    purposes (see this module's docstring). Raises ValueError if row has
    no usable direction/timestamp."""
    direction = row.get("direction")
    signal_time = row.get("timestamp")
    symbol = row.get("symbol")
    if not symbol or direction not in ("Bullish", "Bearish") or not signal_time:
        raise ValueError("row must have a symbol, a Bullish/Bearish direction, and a timestamp")

    try:
        horizon_bars = max(1, min(int(horizon_bars), 100))
    except (TypeError, ValueError):
        horizon_bars = DEFAULT_HORIZON_BARS
    try:
        cost_pct = max(0.0, float(cost_pct or 0.0))
    except (TypeError, ValueError):
        cost_pct = 0.0
    try:
        slippage_pct = max(0.0, float(slippage_pct or 0.0))
    except (TypeError, ValueError):
        slippage_pct = 0.0

    trade = {
        "id": uuid.uuid4().hex[:12],
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "horizon_bars": horizon_bars,
        "cost_pct": cost_pct,
        "slippage_pct": slippage_pct,
        "signal_time": signal_time,
        "logged_at": now_ist().isoformat(timespec="seconds"),
        "logged_close": row.get("close"),
        "status": "open",
        "entry_time": None,
        "entry_price": None,
        "exit_time": None,
        "exit_price": None,
        "return_pct": None,
        "mae_pct": None,
        "outcome": None,
        "snapshot": {k: row.get(k) for k in _SNAPSHOT_FIELDS},
    }
    with _lock:
        _state["trades"].append(trade)
    _save()
    return trade


def delete_trade(trade_id: str) -> bool:
    """Manually removes a logged trade (e.g. one you logged by mistake).
    Returns True if a trade was actually found and removed."""
    with _lock:
        before = len(_state["trades"])
        _state["trades"] = [t for t in _state["trades"] if t["id"] != trade_id]
        removed = len(_state["trades"]) != before
    if removed:
        _save()
    return removed


def _resolve_one(kite, trade, instruments):
    """Attempts to fill entry (if not yet filled) and/or exit (if entry is
    filled and enough bars have since closed) for a single open trade,
    mutating it in place. Returns True if the trade's status changed
    (entry filled, or fully resolved) so the caller knows whether a save
    is actually needed. Never raises - a fetch failure for one symbol
    just leaves this trade "open" to retry next cycle, same
    never-let-one-bad-symbol-break-everything convention as
    backtest.py/scanner.py."""
    token = instruments.get(trade["symbol"])
    if not token:
        return False  # symbol not resolvable right now - retry later, don't give up permanently

    try:
        df = fetch_candles(kite, token, trade["timeframe"])
    except Exception:  # noqa: BLE001
        log.exception("Journal: candle fetch failed for %s", trade["symbol"])
        return False
    if df is None or df.empty:
        return False

    signal_ts = pd.Timestamp(trade["signal_time"])
    # Bars strictly AFTER the signal bar - same "entry = next bar's open"
    # convention as backtest._compute_trade, applied to a live-fetched df
    # instead of a backtest replay.
    after = df[df.index > signal_ts]
    if after.empty:
        return False  # next bar hasn't closed yet - nothing to do this cycle

    changed = False
    if trade["entry_price"] is None:
        entry_row = after.iloc[0]
        trade["entry_time"] = after.index[0].isoformat()
        trade["entry_price"] = round(float(entry_row["open"]), 2)
        changed = True

    if trade["entry_price"] is not None and trade["exit_price"] is None:
        entry_ts = pd.Timestamp(trade["entry_time"])
        post_entry = df[df.index >= entry_ts]
        h = trade["horizon_bars"]
        if len(post_entry) > h:  # index 0 is the entry bar itself, so need h+1 rows
            hold_slice = post_entry.iloc[: h + 1]
            sign = 1 if trade["direction"] == "Bullish" else -1
            entry_price = trade["entry_price"]
            if trade["direction"] == "Bullish":
                mae_pct = min(0.0, float((hold_slice["low"].min() - entry_price) / entry_price * 100))
            else:
                mae_pct = min(0.0, float((entry_price - hold_slice["high"].max()) / entry_price * 100))
            exit_row = post_entry.iloc[h]
            exit_price = float(exit_row["close"])
            raw_return_pct = sign * (exit_price - entry_price) / entry_price * 100
            total_cost_pct = trade["cost_pct"] + 2 * trade["slippage_pct"]

            trade["exit_time"] = post_entry.index[h].isoformat()
            trade["exit_price"] = round(exit_price, 2)
            trade["return_pct"] = round(raw_return_pct - total_cost_pct, 3)
            trade["mae_pct"] = round(mae_pct, 3)
            trade["outcome"] = "win" if trade["return_pct"] > 0 else "loss"
            trade["status"] = "resolved"
            changed = True

    return changed


def resolve_open_trades(kite):
    """Called once per live scan cycle (background._run_loop, only while
    the market is open and a Kite session exists - see background.py's
    call site) to fill entries and resolve exits for every open trade.
    Groups by (symbol, timeframe) so multiple open trades on the same
    symbol/timeframe only cost one fetch each, not one per trade."""
    with _lock:
        open_trades = [t for t in _state["trades"] if t["status"] == "open"]
    if not open_trades:
        return

    instruments = _load_instrument_map(kite)
    any_changed = False
    for trade in open_trades:
        try:
            if _resolve_one(kite, trade, instruments):
                any_changed = True
        except Exception:  # noqa: BLE001 - one bad trade must never block the rest
            log.exception("Journal: failed to resolve trade %s (%s)", trade.get("id"), trade.get("symbol"))

    if any_changed:
        _save()
