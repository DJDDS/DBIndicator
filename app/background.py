"""
A small background thread that re-runs the scan every SCAN_INTERVAL_SECONDS
during market hours, so the web page always has a recent result to show
without the visitor having to trigger anything themselves.
"""
import logging
import threading
import time

from . import alerts, kite_auth
from .config import settings
from .scanner import scan_watchlist, is_market_open, now_ist

log = logging.getLogger(__name__)

_state_lock = threading.Lock()
_state = {
    "results": [],
    "last_scan": None,
    "last_error": None,
}


def get_state():
    with _state_lock:
        return dict(_state)


def _run_loop():
    while True:
        kite = kite_auth.get_kite_client()
        if kite is not None and is_market_open():
            try:
                results = scan_watchlist(kite)
                with _state_lock:
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
            time.sleep(settings.SCAN_INTERVAL_SECONDS)
        else:
            # Not logged in yet today, or outside market hours - check
            # back periodically without hammering anything.
            time.sleep(30)


def start_background_scanner():
    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
