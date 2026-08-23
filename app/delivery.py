"""
NSE delivery percentage - what fraction of a symbol's traded volume on a
given session actually resulted in DELIVERY (shares that changed demat
ownership) rather than being squared off intraday. This is real evidence
of positional/institutional conviction that Kite Connect has no equivalent
for at all - its quote/historical-data APIs carry no delivery information
whatsoever, this is purely an NSE bhavcopy field, published once per day
from NSE's own public archives (no login/API key needed).

Genuinely useful for BTST: a stock you're holding overnight had real
delivery-based buying behind today's move, not just intraday churn that's
more likely to reverse at tomorrow's open.

TWO IMPORTANT LIMITATIONS, read before relying on this:

1. TIMING - this is never a same-day-LIVE number. NSE only publishes a
   session's own delivery data AFTER that session's close (typically
   ~6:30-7pm IST) - so while you're actually making a same-day BTST
   decision (during market hours, well before the 3:30pm close), "today's"
   figure doesn't exist yet. get_delivery_pct() always returns the most
   RECENTLY PUBLISHED reading (usually yesterday's, or today's own once
   it's out after ~7pm) alongside the actual date it's FOR, so it's never
   silently mistaken for a live number - use it as a same-morning gut
   check on yesterday's conviction, not an intraday gate.

2. RELIABILITY - NSE's archives are known to aggressively rate-limit or
   outright block requests from datacenter/cloud IPs (AWS/GCP/Railway-
   style hosts), even with browser-like headers and a warmed-up session,
   in a way that's genuinely inconsistent and outside this app's control.
   Every function here is written defensively (session warm-up, short
   timeouts, swallowed exceptions) and degrades to "no data" rather than
   raising - so a blocked fetch never breaks the scan loop, it just means
   delivery_pct reads None for every symbol until a fetch succeeds. Check
   get_status() (surfaced on the Settings page) after a deploy to see
   whether it's actually getting through from wherever this is hosted.
"""
import datetime as dt
import io
import json
import logging
import os
import threading

import requests

from .config import DELIVERY_DATA_FILE

log = logging.getLogger(__name__)

_NSE_HOME_URL = "https://www.nseindia.com/"
_BHAVCOPY_URL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/csv,text/plain,*/*",
}
_FETCH_TIMEOUT_SECONDS = 12
_MAX_LOOKBACK_DAYS = 6          # how many calendar days back to try if the most
                                 # recent trading day's file isn't published yet
_MIN_RETRY_INTERVAL_SECONDS = 30 * 60  # don't hammer NSE every scan cycle if blocked

_lock = threading.Lock()
_state = {
    "date": None,          # ISO date the cached data is FOR (not when it was fetched)
    "data": {},             # {symbol: delivery_pct}
    "last_attempt": None,   # ISO datetime of the last fetch attempt (success or fail)
    "last_success": None,   # ISO datetime of the last successful fetch
    "last_error": None,
}


def _load_persisted():
    if not os.path.exists(DELIVERY_DATA_FILE):
        return
    try:
        with open(DELIVERY_DATA_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            with _lock:
                _state["date"] = saved.get("date")
                _state["data"] = saved.get("data", {})
                _state["last_success"] = saved.get("last_success")
    except (json.JSONDecodeError, OSError):
        pass


def _save_persisted():
    with _lock:
        snapshot = {"date": _state["date"], "data": _state["data"], "last_success": _state["last_success"]}
    try:
        with open(DELIVERY_DATA_FILE, "w") as f:
            json.dump(snapshot, f)
    except OSError:
        log.exception("Failed to persist delivery data cache")


_load_persisted()


def _parse_bhavcopy_csv(text: str) -> dict:
    """Parses the SYMBOL/SERIES/DELIV_PER columns out of NSE's full
    bhavcopy CSV. Only the "EQ" series is kept (avoids double-counting a
    symbol that also has BE/BZ/etc. rows) - matches how a stock actually
    trades in the F&O-linked cash market. Rows with a non-numeric
    DELIV_PER ("-", blank - happens for series with no delivery concept)
    are skipped rather than coerced to 0, so a real "no data" reads as
    missing, not as "0% delivery"."""
    import csv
    out = {}
    reader = csv.DictReader(io.StringIO(text))
    # NSE's own CSV headers carry stray whitespace (" SYMBOL", " SERIES ",
    # " DELIV_PER") - normalize once so this doesn't silently return
    # nothing the moment NSE tweaks spacing.
    fieldmap = {}
    if reader.fieldnames:
        for fn in reader.fieldnames:
            fieldmap[fn.strip().upper()] = fn
    sym_key, series_key, deliv_key = fieldmap.get("SYMBOL"), fieldmap.get("SERIES"), fieldmap.get("DELIV_PER")
    if not (sym_key and series_key and deliv_key):
        return {}
    for row in reader:
        series = (row.get(series_key) or "").strip().upper()
        if series != "EQ":
            continue
        symbol = (row.get(sym_key) or "").strip().upper()
        raw = (row.get(deliv_key) or "").strip()
        if not symbol or not raw or raw == "-":
            continue
        try:
            out[symbol] = float(raw)
        except ValueError:
            continue
    return out


def _fetch_for_date(session: requests.Session, date: dt.date) -> dict:
    """One attempt for one calendar date. Returns {} on ANY failure
    (network error, 403, non-200, empty/unparseable body) - never raises,
    per this module's whole "degrade to no data" contract."""
    url = _BHAVCOPY_URL.format(date=date.strftime("%d%m%Y"))
    try:
        resp = session.get(url, headers={**_HEADERS, "Referer": _NSE_HOME_URL}, timeout=_FETCH_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.info("Delivery fetch failed for %s: %s", date, exc)
        return {}
    if resp.status_code != 200 or not resp.text:
        return {}
    try:
        return _parse_bhavcopy_csv(resp.text)
    except Exception:  # noqa: BLE001 - a parse hiccup must never propagate
        log.exception("Failed to parse delivery bhavcopy for %s", date)
        return {}


def _fetch_most_recent(now_ist_dt) -> tuple:
    """Tries today, then walks backward up to _MAX_LOOKBACK_DAYS calendar
    days (covers weekends/holidays, when no bhavcopy is published at
    all), returning the first date that yields real data. A single short-
    lived session is used for the warm-up + the actual fetch, since NSE's
    archives generally expect the cookies set by a prior visit to
    nseindia.com's own homepage - swallows the warm-up's own failure too
    (some hosts can reach the archive but not the interactive site, or
    vice versa; either way, a failed warm-up just means the CSV request
    goes out without those cookies rather than not going out at all)."""
    session = requests.Session()
    try:
        session.get(_NSE_HOME_URL, headers=_HEADERS, timeout=_FETCH_TIMEOUT_SECONDS)
    except requests.RequestException:
        pass  # see docstring - proceed without the warm-up cookies

    for back in range(_MAX_LOOKBACK_DAYS + 1):
        date = now_ist_dt.date() - dt.timedelta(days=back)
        if date.weekday() >= 5:  # Saturday/Sunday - NSE never publishes for these
            continue
        data = _fetch_for_date(session, date)
        if data:
            return data, date
    return {}, None


def refresh_if_stale(now_ist_dt) -> None:
    """Call once per scan cycle (background.py's _run_loop only - see
    that module). No-ops (cheap: one dict read) unless the cache is
    genuinely stale AND at least _MIN_RETRY_INTERVAL_SECONDS has passed
    since the last attempt - the second guard is what keeps a blocked
    deployment from hammering NSE (and logging a failure) every single
    scan cycle all day. "Stale" means the cached date isn't today's
    calendar date - the underlying bhavcopy only changes once a day
    anyway, so there's nothing to gain from checking more often even when
    it IS reachable."""
    with _lock:
        cached_date = _state["date"]
        last_attempt = _state["last_attempt"]
    today_iso = now_ist_dt.date().isoformat()
    if cached_date == today_iso:
        return
    if last_attempt:
        try:
            last_dt = dt.datetime.fromisoformat(last_attempt)
            if (now_ist_dt - last_dt).total_seconds() < _MIN_RETRY_INTERVAL_SECONDS:
                return
        except ValueError:
            pass

    with _lock:
        _state["last_attempt"] = now_ist_dt.isoformat()
    try:
        data, used_date = _fetch_most_recent(now_ist_dt)
    except Exception as exc:  # noqa: BLE001 - refresh must never break the scan loop
        log.exception("Delivery data refresh failed unexpectedly")
        with _lock:
            _state["last_error"] = str(exc)
        return

    if not data:
        with _lock:
            _state["last_error"] = "Could not fetch NSE delivery data (blocked, or not yet published)."
        return

    with _lock:
        _state["date"] = used_date.isoformat()
        _state["data"] = data
        _state["last_success"] = now_ist_dt.isoformat()
        _state["last_error"] = None
    _save_persisted()


def get_delivery_pct(symbol: str):
    """Returns (delivery_pct, data_date_iso) for `symbol`, or (None, None)
    if no data is cached yet (never fetched, blocked, or this symbol
    wasn't in the most recently fetched file)."""
    with _lock:
        pct = _state["data"].get(symbol.upper())
        date = _state["date"]
    if pct is None:
        return None, None
    return pct, date


def get_status() -> dict:
    """Summary for the Settings page - lets you actually see whether this
    is working from wherever the app is deployed, rather than silently
    reading 'unavailable' with no explanation."""
    with _lock:
        return {
            "available": bool(_state["data"]),
            "data_date": _state["date"],
            "symbol_count": len(_state["data"]),
            "last_attempt": _state["last_attempt"],
            "last_success": _state["last_success"],
            "last_error": _state["last_error"],
        }
