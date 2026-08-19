"""
A small background thread that re-runs the scan every SCAN_INTERVAL_SECONDS
during market hours, so the web page always has a recent result to show
without the visitor having to trigger anything themselves.
"""
import json
import logging
import os
import threading
import time

from . import alerts, kite_auth
from .config import settings, SCAN_RESULTS_FILE
from .indicators import RSI_OVERBOUGHT, RSI_OVERSOLD
from .scanner import scan_watchlist, is_market_open, now_ist, classify_oi_trend, classify_oi_structure

log = logging.getLogger(__name__)

# How many recent OI samples to keep per symbol for trend/acceleration
# classification. Only the main-timeframe scan carries OI, so this
# grows by one entry per symbol per main scan cycle.
OI_HISTORY_MAX = 20

# --------------------------------------------------------------------------
# The dashboard's own "My Filters" panel - tick any of these and only
# stocks meeting your chosen count of them show up anywhere on the page
# (Matching Now, High Conviction, the main table). Unlike the backtest's
# PARAM_DEFS (crossover EVENTS replayed bar-by-bar over history), these
# are CURRENT STATE checks evaluated fresh on every scan - appropriate
# for "what does the live screener show me right now", not a historical
# entry trigger. web.py reads the ?fparams=...&frequired=N query string
# (a real GET form, so it's shareable/bookmarkable and the dashboard's
# existing 20s auto-refresh - which re-fetches the same URL including
# its query string - keeps respecting it automatically).
# --------------------------------------------------------------------------

SCREEN_PARAM_DEFS = [
    {"id": "rsi_state", "label": "RSI (vs its smoothing line)"},
    {"id": "macd_state", "label": "MACD (vs signal line)"},
    {"id": "ema_bb_state", "label": "EMA9 vs Bollinger Mid"},
    {"id": "rsi_threshold", "label": f"RSI > {RSI_OVERBOUGHT} (Bearish: RSI < {RSI_OVERSOLD})"},
    {"id": "rel_volume", "label": "Relative Volume > 1.2x (20-bar avg)"},
    {"id": "oi_structure", "label": "OI % Structure agrees (today's price+OI Buildup matching direction)"},
    {"id": "oi_break_signal", "label": "OI Break Signal (building AND accelerating, matching direction)"},
    {"id": "htf_trend", "label": "Higher-timeframe (4h) trend agrees (15-min scans only)"},
]
SCREEN_PARAM_IDS = [p["id"] for p in SCREEN_PARAM_DEFS]


def _screen_param_match(r, param_id) -> bool:
    """Does this row's CURRENT direction hold up under one selected
    filter parameter? Missing/unavailable data (e.g. OI not fetchable
    for this symbol) counts as NOT matching rather than silently
    passing - you ticked the box because you want that condition
    actually confirmed, not assumed."""
    direction = r.get("direction")
    if not direction:
        return False
    if param_id == "rsi_state":
        return r.get("rsi_state") == direction
    if param_id == "macd_state":
        return r.get("macd_state") == direction
    if param_id == "ema_bb_state":
        return r.get("ema_bb_state") == direction
    if param_id == "rsi_threshold":
        rsi = r.get("rsi")
        if rsi is None:
            return False
        return rsi > RSI_OVERBOUGHT if direction == "Bullish" else rsi < RSI_OVERSOLD
    if param_id == "rel_volume":
        return bool(r.get("vol_confirmed"))
    if param_id == "oi_structure":
        # Today's cumulative price+OI% move since session open (see
        # scanner.classify_oi_structure) - the softer OI check: OI is
        # building in your direction, no requirement on how fast.
        structure = r.get("oi_structure")
        if not structure:
            return False
        return (
            (direction == "Bullish" and structure == "Long Buildup")
            or (direction == "Bearish" and structure == "Short Buildup")
        )
    if param_id == "oi_break_signal":
        # The stricter, scan-to-scan-aware OI check: not just building,
        # but building FASTER than its own recent pace right now (or an
        # outright spike) - see _apply_oi_screener_fields' accel_strong.
        # This is "percentage AND acceleration" combined into one flag.
        break_signal = r.get("oi_break_signal")
        if not break_signal:
            return False
        return (
            (direction == "Bullish" and break_signal == "Break Up")
            or (direction == "Bearish" and break_signal == "Break Down")
        )
    if param_id == "htf_trend":
        return bool(r.get("htf_agrees", True))
    return False


def custom_filter_match(r, params, required: int) -> bool:
    """True if this row currently satisfies at least `required` of your
    ticked `params`. Always excludes rows still inside the noisy
    opening-window (see indicators.OPENING_WINDOW_MINUTES) and rows
    with no computed signal at all (errors, or aligned/direction
    missing) - a custom filter should never surface those."""
    if r.get("error") or not r.get("direction") or r.get("aligned") is None:
        return False
    if r.get("in_opening_window"):
        return False
    if not params:
        return True
    count = sum(1 for p in params if _screen_param_match(r, p))
    return count >= required

_state_lock = threading.Lock()
_state = {
    "results": [],
    "last_scan": None,
    "last_error": None,
    "oi_history": {},
    "oi_day_baseline": {},
    "oi_structure_prev": {},
}


def _apply_oi_trend(results):
    """Mutates each result dict in place, attaching oi_change,
    oi_change_pct, oi_trend_label and oi_unusual based on this symbol's
    OI history across scans. Must be called while holding _state_lock -
    it reads and appends to _state["oi_history"]."""
    history = _state["oi_history"]
    for r in results:
        symbol, oi = r.get("symbol"), r.get("oi")
        if not symbol or oi is None:
            continue
        hist = history.setdefault(symbol, [])
        hist.append(oi)
        if len(hist) > OI_HISTORY_MAX:
            del hist[: len(hist) - OI_HISTORY_MAX]
        trend = classify_oi_trend(hist)
        r["oi_change"] = trend["change"]
        r["oi_change_pct"] = trend["change_pct"]
        r["oi_trend_label"] = trend["label"]
        r["oi_unusual"] = trend["unusual"]


def _apply_oi_screener_fields(results):
    """Attaches the dashboard's OI-driven fields to each result: works
    out today's price/OI move since session open (distinct from
    _apply_oi_trend's scan-to-scan numbers above), classifies the
    4-quadrant OI Structure from that, flags whether that structure just
    changed this scan ("stage": "New"), derives a decisive "oi_break_signal"
    (Break Up / Break Down) when OI is building in a direction AND
    accelerating faster than its own recent pace, and cross-references
    the existing confluence signal (this symbol's own aligned/direction)
    to mark "positional_qualified" - a stock that isn't just showing an
    OI structure, but one that agrees with your confluence signal too.
    Must be called after _apply_oi_trend (needs oi_trend_label/
    oi_unusual already set) and while holding _state_lock."""
    today = now_ist().date().isoformat()
    baseline = _state["oi_day_baseline"]
    prev_structure = _state["oi_structure_prev"]

    for r in results:
        symbol, oi, close = r.get("symbol"), r.get("oi"), r.get("close")
        if not symbol or r.get("error"):
            continue

        base = baseline.get(symbol)
        if base is None or base.get("date") != today or oi is None or close is None:
            # First scan of a new trading day (or first time we've ever
            # seen this symbol, or OI/close briefly unavailable) - reset
            # today's baseline to whatever we have right now.
            if oi is not None and close is not None:
                baseline[symbol] = {"date": today, "oi": oi, "close": close}
            base = baseline.get(symbol)

        price_chg_pct = oi_chg_pct = None
        if base and base.get("date") == today and oi is not None and close is not None:
            if base["close"]:
                price_chg_pct = (close - base["close"]) / base["close"] * 100
            if base["oi"]:
                oi_chg_pct = (oi - base["oi"]) / base["oi"] * 100

        structure = classify_oi_structure(price_chg_pct, oi_chg_pct)
        r["price_chg_today_pct"] = price_chg_pct
        r["oi_chg_today_pct"] = oi_chg_pct
        r["oi_structure"] = structure

        r["stage"] = "New" if structure and prev_structure.get(symbol) not in (None, structure) else None
        if structure:
            prev_structure[symbol] = structure

        # A decisive OI signal: not just "OI is building", but "OI is
        # building AND doing so faster than its own recent pace" (or an
        # outright spike) - that combination is what actually suggests
        # fresh conviction rather than routine drift. Long Buildup +
        # strong acceleration reads as a bullish break-up; Short
        # Buildup + strong acceleration reads as a bearish break-down.
        # Relies on oi_trend_label/oi_unusual already being set by
        # _apply_oi_trend, which always runs first in the scan loop.
        accel_strong = r.get("oi_trend_label") == "Accelerating" or bool(r.get("oi_unusual"))
        oi_break_signal = None
        if structure == "Long Buildup" and accel_strong:
            oi_break_signal = "Break Up"
        elif structure == "Short Buildup" and accel_strong:
            oi_break_signal = "Break Down"
        r["oi_break_signal"] = oi_break_signal

        direction = r.get("direction")
        aligned = r.get("aligned") or 0
        structure_agrees = (
            (direction == "Bullish" and structure == "Long Buildup")
            or (direction == "Bearish" and structure == "Short Buildup")
        )
        r["positional_qualified"] = bool(
            aligned >= settings.MIN_REQUIRED and structure_agrees and not r.get("in_opening_window")
        )

        # High Conviction: a deliberately narrow filter meant to surface
        # only a handful of stocks, by stacking EVERY signal this app
        # tracks and requiring all of them to point the same way at
        # once - strict 3-of-3 confluence (not just your Required
        # setting), OI structure, an actively accelerating OI break
        # signal, volume confirming the move (>=1.5x average), and
        # price on the right side of VWAP. This is a stricter superset
        # of "Positional Qualified" above, not a separate independent
        # check.
        #
        # IMPORTANT - read before trading off this: stacking filters
        # like this narrows the list, but it does NOT by itself imply
        # any particular win rate. Nobody has backtested this exact
        # rule combination against real historical outcomes yet - so
        # treat "High Conviction" as "everything currently agrees",
        # not as a validated or guaranteed-odds signal.
        vs_vwap_agrees = (
            (direction == "Bullish" and r.get("vs_vwap") == "Above")
            or (direction == "Bearish" and r.get("vs_vwap") == "Below")
        )
        break_agrees = (
            (direction == "Bullish" and oi_break_signal == "Break Up")
            or (direction == "Bearish" and oi_break_signal == "Break Down")
        )
        vol_confirmed = (r.get("vol_multiple") or 0) >= 1.5
        r["high_conviction"] = bool(
            r["positional_qualified"] and aligned == 3 and break_agrees and vol_confirmed and vs_vwap_agrees
        )


def _load_persisted_state():
    """Restores the last scan from disk on startup, so a restart (a
    redeploy, the host restarting the container, etc.) doesn't wipe the
    day's data - after-hours, this is the only thing keeping results on
    screen for you to still analyse."""
    if not os.path.exists(SCAN_RESULTS_FILE):
        return
    try:
        with open(SCAN_RESULTS_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict) and "results" in saved:
            with _state_lock:
                _state["results"] = saved.get("results", [])
                _state["last_scan"] = saved.get("last_scan")
                _state["oi_history"] = saved.get("oi_history", {})
                _state["oi_day_baseline"] = saved.get("oi_day_baseline", {})
                _state["oi_structure_prev"] = saved.get("oi_structure_prev", {})
                _state["last_error"] = None
    except (json.JSONDecodeError, OSError):
        pass


def _save_persisted_state():
    with _state_lock:
        snapshot = {
            "results": _state["results"],
            "last_scan": _state["last_scan"],
            "oi_history": _state["oi_history"],
            "oi_day_baseline": _state["oi_day_baseline"],
            "oi_structure_prev": _state["oi_structure_prev"],
        }
    try:
        # default=str is a safety net: if any result field ever ends up
        # holding a non-JSON-native type again (pandas Timestamp, numpy
        # int64, etc.) this coerces it to a string instead of raising -
        # a persistence hiccup should never be able to kill the whole
        # scan loop the way an uncaught TypeError here once did.
        with open(SCAN_RESULTS_FILE, "w") as f:
            json.dump(snapshot, f, default=str)
    except Exception:  # noqa: BLE001 - persistence must never crash the scan loop
        log.exception("Failed to persist scan results")


_load_persisted_state()


def get_state():
    with _state_lock:
        return dict(_state)


def _run_loop():
    while True:
        # The entire iteration body is wrapped in this try/except as a
        # last-resort safety net. Previously, a single uncaught exception
        # anywhere in this loop (e.g. trying to JSON-persist a pandas
        # Timestamp) would permanently kill this daemon thread - the web
        # page kept loading fine, so nothing looked "down", but scanning
        # silently stopped forever until the next redeploy. Now, no
        # matter what goes wrong on a given cycle, the loop logs it and
        # tries again next cycle instead of dying.
        try:
            kite = kite_auth.get_kite_client()
            if kite is not None and is_market_open():
                try:
                    results = scan_watchlist(kite)
                    with _state_lock:
                        _apply_oi_trend(results)
                        _apply_oi_screener_fields(results)
                        _state["results"] = results
                        _state["last_scan"] = now_ist().isoformat(timespec="seconds")
                        _state["last_error"] = None
                    try:
                        alerts.process_scan_results(results, settings.TIMEFRAME)
                    except Exception:  # noqa: BLE001 - alerting must never break scanning
                        log.exception("Alert processing failed")
                except Exception as exc:  # noqa: BLE001
                    log.exception("Background scan failed")
                    with _state_lock:
                        _state["last_error"] = str(exc)

                _save_persisted_state()
                time.sleep(settings.SCAN_INTERVAL_SECONDS)
            else:
                # Not logged in yet today, or outside market hours - the
                # last scan's results (loaded from disk on startup, or still
                # in memory from earlier today) are left untouched so
                # there's always something on screen to analyse. Check back
                # periodically without hammering anything.
                time.sleep(30)
        except Exception:  # noqa: BLE001 - never let this thread die
            log.exception("Background scan loop hit an unexpected error - retrying")
            with _state_lock:
                _state["last_error"] = "Background loop hit an unexpected error - see server logs."
            time.sleep(30)


def start_background_scanner():
    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
