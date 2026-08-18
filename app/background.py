"""
A small background thread that re-runs the scan every SCAN_INTERVAL_SECONDS
during market hours, so the web page always has a recent result to show
without the visitor having to trigger anything themselves.

It also runs a second, separate confluence scan specifically on the
4-hour timeframe (regardless of whatever timeframe the dashboard/Quick
Settings is currently set to) - 4-hour candles only move every 4 hours,
so this runs on a much slower cadence than the main scan to avoid
wasting API calls re-fetching data that hasn't changed.
"""
import json
import logging
import os
import threading
import time

from . import alerts, kite_auth
from .config import settings, SCAN_RESULTS_FILE
from .scanner import scan_watchlist, is_market_open, now_ist, classify_oi_trend

log = logging.getLogger(__name__)

# How often (seconds) to re-run the dedicated 4-hour scan. This is
# intentionally independent of settings.SCAN_INTERVAL_SECONDS (the main
# scan's cadence) since 4-hour candles don't close often enough to
# justify checking every 2-3 minutes like the main scan does.
FOUR_HOUR_SCAN_INTERVAL_SECONDS = 900

# How many recent OI samples to keep per symbol for trend/acceleration
# classification. Only the main-timeframe scan carries OI, so this
# grows by one entry per symbol per main scan cycle.
OI_HISTORY_MAX = 20

_state_lock = threading.Lock()
_state = {
    "results": [],
    "last_scan": None,
    "last_error": None,
    "results_4h": [],
    "last_scan_4h": None,
    "oi_history": {},
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


def _load_persisted_state():
    """Restores the last scan (both the main timeframe and the 4-hour
    pass) from disk on startup, so a restart (a redeploy, the host
    restarting the container, etc.) doesn't wipe the day's data -
    after-hours, this is the only thing keeping results on screen for
    you to still analyse."""
    if not os.path.exists(SCAN_RESULTS_FILE):
        return
    try:
        with open(SCAN_RESULTS_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict) and "results" in saved:
            with _state_lock:
                _state["results"] = saved.get("results", [])
                _state["last_scan"] = saved.get("last_scan")
                _state["results_4h"] = saved.get("results_4h", [])
                _state["last_scan_4h"] = saved.get("last_scan_4h")
                _state["oi_history"] = saved.get("oi_history", {})
                _state["last_error"] = None
    except (json.JSONDecodeError, OSError):
        pass


def _save_persisted_state():
    with _state_lock:
        snapshot = {
            "results": _state["results"],
            "last_scan": _state["last_scan"],
            "results_4h": _state["results_4h"],
            "last_scan_4h": _state["last_scan_4h"],
            "oi_history": _state["oi_history"],
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
    last_4h_run = 0.0
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

                now_monotonic = time.monotonic()
                if now_monotonic - last_4h_run >= FOUR_HOUR_SCAN_INTERVAL_SECONDS:
                    try:
                        # OI doesn't vary by timeframe (it's the same
                        # futures-contract value regardless), so skip
                        # re-fetching it here - the main scan above already has it.
                        results_4h = scan_watchlist(kite, timeframe="4hour", with_oi=False)
                        with _state_lock:
                            _state["results_4h"] = results_4h
                            _state["last_scan_4h"] = now_ist().isoformat(timespec="seconds")
                        try:
                            alerts.process_scan_results(results_4h, "4hour")
                        except Exception:  # noqa: BLE001
                            log.exception("4-hour alert processing failed")
                    except Exception:  # noqa: BLE001 - never let the 4H pass break the main scan
                        log.exception("4-hour background scan failed")
                    last_4h_run = now_monotonic

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
