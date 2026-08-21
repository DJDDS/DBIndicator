"""
A small background thread that re-runs the scan every SCAN_INTERVAL_SECONDS
during market hours, so the web page always has a recent result to show
without the visitor having to trigger anything themselves.
"""
import datetime as dt
import json
import logging
import os
import threading
import time

from . import alerts, journal, kite_auth
from .config import settings, SCAN_RESULTS_FILE, PARAM_WEIGHTS_FILE, MULTI_TF_RESULTS_FILE
from .scanner import (
    scan_watchlist, is_market_open, now_ist, compute_oi_acceleration,
    classify_oi_structure, fetch_index_direction,
)

log = logging.getLogger(__name__)

# How far back (in minutes) to keep OI samples per symbol.
# compute_oi_acceleration needs up to 120 minutes of history (its
# "prior 60-minute" window looks 60-120 minutes back), so this keeps a
# safety margin beyond that regardless of the configured scan interval
# - unlike a fixed sample-count cap, this stays correct whether scans
# run every 60s or every 5 minutes.
OI_HISTORY_MAX_MINUTES = 150

# --------------------------------------------------------------------------
# The screener's fixed 4-parameter confluence check: RSI (vs its
# smoothing line), MACD (vs signal line), EMA9 (vs Bollinger mid), and
# Relative Volume (vs its own 20-bar average, threshold configurable on
# the Settings page). indicators.compute_signal already does the real
# work of counting how many of these 4 agree with a row's direction -
# that count comes back as `aligned` (0-4). SCREEN_PARAM_DEFS below is
# kept purely as display labels (footnotes, tooltips) - it's not used
# for any matching logic anymore, so there's a single source of truth
# for "how many parameters agree" instead of two systems that could
# quietly disagree with each other.
# --------------------------------------------------------------------------

SCREEN_PARAM_DEFS = [
    {"id": "rsi_state", "label": "RSI (vs its smoothing line)"},
    {"id": "macd_state", "label": "MACD (vs signal line)"},
    {"id": "ema_bb_state", "label": "EMA9 vs Bollinger Mid"},
    {"id": "rel_volume", "label": "Relative Volume (vs 20-bar avg, threshold on Settings page)"},
]


def _apply_param_tier(results):
    """Mutates each result dict in place, attaching param_tier (2, 3,
    or 4 - the bucket this row belongs to, straight from
    indicators.compute_signal's `aligned`; None if it matched fewer
    than 2, has no signal at all, or is still inside the opening
    window). Each row lands in exactly ONE tier (its exact match
    count), not every tier it clears, so the dashboard's three tier
    sections never show the same stock twice."""
    for r in results:
        aligned = r.get("aligned")
        if r.get("error") or not r.get("direction") or r.get("in_opening_window") or aligned is None:
            r["param_tier"] = None
            continue
        r["param_tier"] = aligned if aligned >= 2 else None


def _apply_index_filter(results, index_direction):
    """Mutates each result dict in place, attaching index_agrees - does
    this row's own direction match NIFTY 50's current confluence
    direction on the same timeframe (see scanner.fetch_index_direction)?
    None means "no index reading available this scan" (a fetch hiccup,
    or the token hasn't resolved yet) - treated as agreeing, same
    convention as indicators.py's htf_agrees, so a transient index
    fetch failure can never silently filter out every row.

    When settings.REQUIRE_INDEX_AGREEMENT is on, a row that disagrees
    with the index also loses its signal_confirmed status - counter-
    trend trades have historically had a lower win rate, so this is an
    optional stricter gate layered on top, not a replacement for
    anything else. Off by default; index_agrees is always attached
    either way purely for display."""
    require = settings.REQUIRE_INDEX_AGREEMENT
    for r in results:
        if r.get("error") or not r.get("direction"):
            r["index_agrees"] = None
            continue
        r["index_agrees"] = True if index_direction is None else (r["direction"] == index_direction)
        if require and r.get("signal_confirmed") and not r["index_agrees"]:
            r["signal_confirmed"] = False


def _apply_volume_flow_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_VOLUME_FLOW_AGREEMENT is on, a row that already has
    vol_flow_agrees=False (set by indicators.compute_signal via Chaikin
    Money Flow - see PARAMETER_ANALYSIS_2.md Finding #2) also loses its
    signal_confirmed status - same shape as _apply_index_filter just
    above, just reading a field compute_signal already attached instead
    of a separately-fetched index reading. Off by default; vol_flow_
    direction/vol_flow_agrees are always attached by compute_signal
    either way, purely for display (the small ▲/▼ badge next to Volume)."""
    if not settings.REQUIRE_VOLUME_FLOW_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("vol_flow_agrees") is False:
            r["signal_confirmed"] = False


def _apply_candle_pattern_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_CANDLE_PATTERN_AGREEMENT is on, a row that already has
    candle_agrees=False (set by indicators.compute_signal via
    _compute_candle_pattern) also loses its signal_confirmed status -
    same shape as _apply_volume_flow_filter just above. Off by default;
    candle_pattern/candle_direction/candle_agrees are always attached by
    compute_signal either way, purely for display (the small candle
    badge next to the Signal column)."""
    if not settings.REQUIRE_CANDLE_PATTERN_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("candle_agrees") is False:
            r["signal_confirmed"] = False


# Equal-weight fallback for weighted_score below, until you've run
# "Auto-Weight Parameters" on the Backtest page at least once - matches
# the plain aligned/4 count in spirit (every parameter counts the same).
_DEFAULT_PARAM_WEIGHTS = {
    "rsi_cross": 0.25, "macd_cross": 0.25, "ema_bb_cross": 0.25, "rel_volume": 0.25,
}
_param_weights_cache = {"mtime": None, "weights": None}


def _load_param_weights():
    """Re-reads PARAM_WEIGHTS_FILE only when its mtime has changed since
    the last call (cheap: one stat() per scan cycle in the common case
    of nothing new). Falls back to equal weighting if the file doesn't
    exist yet (no "Auto-Weight Parameters" run so far) or is corrupt."""
    try:
        mtime = os.path.getmtime(PARAM_WEIGHTS_FILE)
    except OSError:
        return _DEFAULT_PARAM_WEIGHTS
    if _param_weights_cache["mtime"] != mtime:
        try:
            with open(PARAM_WEIGHTS_FILE) as f:
                data = json.load(f)
            weights = data.get("weights") or {}
            if weights:
                _param_weights_cache["weights"] = weights
                _param_weights_cache["mtime"] = mtime
        except (json.JSONDecodeError, OSError):
            pass
    return _param_weights_cache["weights"] or _DEFAULT_PARAM_WEIGHTS


def _apply_weighted_score(results):
    """Mutates each result dict in place, attaching weighted_score (0-100) -
    a backtest-informed alternative to the plain aligned/4 count. Rather
    than treating RSI/MACD/EMA-BB/Relative Volume as equally weighted,
    this multiplies each one's current agreement with the row's
    direction by that parameter's own recent historical win rate (see
    backtest.compute_param_weights, run manually from the Backtest
    page's "Auto-Weight Parameters" panel and persisted to
    PARAM_WEIGHTS_FILE) - a parameter that's actually been predictive
    lately counts for more than one that hasn't. Purely an additional,
    informational sort/display field - doesn't replace aligned/
    param_tier/signal_confirmed anywhere, and falls back to equal
    25%-each weighting (identical in spirit to aligned/4) until you've
    run a weight computation at least once."""
    weights = _load_param_weights()
    for r in results:
        if r.get("error") or not r.get("direction"):
            r["weighted_score"] = None
            continue
        direction = r["direction"]
        score = 0.0
        if r.get("rsi_state") == direction:
            score += weights.get("rsi_cross", 0)
        if r.get("macd_state") == direction:
            score += weights.get("macd_cross", 0)
        if r.get("ema_bb_state") == direction:
            score += weights.get("ema_bb_cross", 0)
        if r.get("vol_confirmed"):
            score += weights.get("rel_volume", 0)
        r["weighted_score"] = round(score * 100, 1)


_state_lock = threading.Lock()
_state = {
    "results": [],
    "last_scan": None,
    "last_error": None,
    "oi_history": {},
    "oi_day_baseline": {},
    "oi_structure_prev": {},
    "oi_label_prev": {},
    "index_direction": None,
    "index_close": None,
    "index_chg_pct": None,
}

# Set by web.py whenever a Quick Settings / Settings change is applied
# (timeframe, indicator lengths, watchlist, etc.) so the very next scan
# picks up the new settings within a second or two, instead of the
# dashboard silently showing stale results for up to
# SCAN_INTERVAL_SECONDS (default 3 minutes) - which read as "the
# timeframe switch isn't working" even though it was actually just
# waiting for the next scheduled cycle.
_rescan_event = threading.Event()


def trigger_rescan():
    _rescan_event.set()


def _apply_oi_trend(results):
    """Mutates each result dict in place, attaching the rolling-window
    OI acceleration fields (oi_chg_15m_pct, oi_chg_30m_pct,
    oi_chg_60m_pct, oi_acceleration, oi_accel_label - see
    scanner.compute_oi_acceleration) based on this symbol's timestamped
    OI history across scans. Must be called while holding _state_lock -
    it reads and appends to _state["oi_history"].

    oi_trend_label is kept as an alias for oi_accel_label (falling back
    to "New" when there's not enough history yet) purely so existing
    call sites/templates that already read oi_trend_label keep working
    without having to touch every one of them."""
    history = _state["oi_history"]
    now = now_ist()
    cutoff = now - dt.timedelta(minutes=OI_HISTORY_MAX_MINUTES)
    for r in results:
        symbol, oi = r.get("symbol"), r.get("oi")
        if not symbol or oi is None:
            continue
        hist = history.setdefault(symbol, [])
        # Migration guard: older persisted state stored plain numbers
        # instead of {"ts", "oi"} dicts - those can't be time-windowed,
        # so drop them rather than let a stale format crash the scan.
        hist[:] = [e for e in hist if isinstance(e, dict) and e.get("ts")]
        hist.append({"ts": now.isoformat(), "oi": oi})
        cutoff_iso = cutoff.isoformat()
        hist[:] = [e for e in hist if e["ts"] >= cutoff_iso]

        accel = compute_oi_acceleration(hist, now)
        r["oi_chg_15m_pct"] = accel["chg_15m"]
        r["oi_chg_30m_pct"] = accel["chg_30m"]
        r["oi_chg_60m_pct"] = accel["chg_60m"]
        r["oi_chg_prior_30m_pct"] = accel["chg_prior_30m"]
        r["oi_chg_prior_60m_pct"] = accel["chg_prior_60m"]
        r["oi_acceleration"] = accel["acceleration"]
        r["oi_accel_label"] = accel["accel_label"]
        r["oi_trend_label"] = accel["accel_label"] or "New"


def _apply_oi_screener_fields(results):
    """Attaches the dashboard's OI-driven fields to each result: works
    out today's price/OI move since session open (distinct from
    _apply_oi_trend's rolling-window numbers above) plus today's OI
    move vs. YESTERDAY's closing OI ("Day OI Change %"), classifies the
    4-quadrant OI Structure from the since-open move, flags whether
    that structure just changed this scan ("stage": "New"), derives a
    decisive "oi_break_signal" (Break Up / Break Down) when OI is
    building in a direction AND accelerating faster than its own recent
    pace, and cross-references the existing confluence signal (this
    symbol's own aligned/direction) to mark "positional_qualified" - a
    stock that isn't just showing an OI structure, but one that agrees
    with your confluence signal too. Must be called after
    _apply_oi_trend (needs oi_accel_label already set) and while
    holding _state_lock."""
    today = now_ist().date().isoformat()
    baseline = _state["oi_day_baseline"]
    prev_structure = _state["oi_structure_prev"]

    for r in results:
        symbol, oi, close = r.get("symbol"), r.get("oi"), r.get("close")
        if not symbol or r.get("error"):
            continue

        base = baseline.get(symbol)
        if base is None or base.get("date") != today:
            # First scan of a new trading day (or first time we've ever
            # seen this symbol) - carry forward whatever OI we last saw
            # yesterday as "previous day close" for Day OI Change %,
            # then reset today's open/close baseline to right now.
            prev_close_oi = base.get("oi_last") if base else None
            if oi is not None and close is not None:
                baseline[symbol] = {
                    "date": today, "oi": oi, "close": close,
                    "prev_close_oi": prev_close_oi, "oi_last": oi,
                }
            base = baseline.get(symbol)
        elif oi is not None:
            # Same trading day - keep today's open snapshot fixed, but
            # track the latest OI seen so it's ready to become
            # TOMORROW's "previous close" on the next day's rollover.
            base["oi_last"] = oi

        price_chg_pct = oi_chg_pct = day_oi_chg_pct = None
        if base and base.get("date") == today and oi is not None and close is not None:
            if base["close"]:
                price_chg_pct = (close - base["close"]) / base["close"] * 100
            if base["oi"]:
                oi_chg_pct = (oi - base["oi"]) / base["oi"] * 100
            prev_close_oi = base.get("prev_close_oi")
            if prev_close_oi:
                day_oi_chg_pct = (oi - prev_close_oi) / prev_close_oi * 100

        structure = classify_oi_structure(price_chg_pct, oi_chg_pct)
        r["price_chg_today_pct"] = price_chg_pct
        r["oi_chg_today_pct"] = oi_chg_pct
        r["oi_day_chg_pct"] = day_oi_chg_pct
        r["oi_structure"] = structure

        r["stage"] = "New" if structure and prev_structure.get(symbol) not in (None, structure) else None
        if structure:
            prev_structure[symbol] = structure

        # A decisive OI signal: not just "OI is building", but "OI is
        # building AND doing so faster than its own recent pace right
        # now" - that combination is what actually suggests fresh
        # conviction rather than routine drift. Long Buildup + strong/
        # moderate acceleration reads as a bullish break-up; Short
        # Buildup + strong/moderate acceleration reads as a bearish
        # break-down. Relies on oi_accel_label already being set by
        # _apply_oi_trend, which always runs first in the scan loop.
        accel_strong = r.get("oi_accel_label") in ("Strong acceleration", "Moderate acceleration")
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
        # index_ok: the Index/Market-trend filter (see _apply_index_filter,
        # which must run before this function so r["index_agrees"] is
        # already set) - only actually gates anything when
        # REQUIRE_INDEX_AGREEMENT is on; otherwise every row passes this
        # check regardless of what index_agrees says, same as before that
        # setting existed.
        index_ok = (not settings.REQUIRE_INDEX_AGREEMENT) or bool(r.get("index_agrees"))
        r["positional_qualified"] = bool(
            aligned >= settings.MIN_REQUIRED and structure_agrees and not r.get("in_opening_window") and index_ok
        )

        # High Conviction: a deliberately narrow filter meant to surface
        # only a handful of stocks, by stacking EVERY signal this app
        # tracks and requiring all of them to point the same way at
        # once - strict 4-of-4 confluence (RSI, MACD, EMA/BB, and
        # Relative Volume all agreeing - not just your Required
        # setting), OI structure, an actively accelerating OI break
        # signal, an even higher volume bar (>=1.5x average, stricter
        # than the 4-of-4's own Relative Volume threshold), and price on
        # the right side of VWAP. This is a stricter superset of
        # "Positional Qualified" above, not a separate independent
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
            r["positional_qualified"] and aligned == 4 and break_agrees and vol_confirmed and vs_vwap_agrees
        )


_ACCELERATING_LABELS = ("Strong acceleration", "Moderate acceleration")


def _detect_oi_accel_events(results):
    """Returns the rows whose oi_accel_label (see
    scanner.compute_oi_acceleration) just transitioned INTO "Strong
    acceleration" or "Moderate acceleration" on this scan, compared to
    what it was last scan - i.e. the moment OI starts accelerating for
    that symbol, not every scan while it stays accelerating. Must be
    called after _apply_oi_trend (needs this scan's oi_accel_label
    already set) and while holding _state_lock, same as the OI
    functions above - it reads and updates _state["oi_label_prev"]."""
    prev = _state["oi_label_prev"]
    events = []
    for r in results:
        symbol = r.get("symbol")
        if not symbol or r.get("error"):
            continue
        label = r.get("oi_accel_label")
        if label in _ACCELERATING_LABELS and prev.get(symbol) not in _ACCELERATING_LABELS:
            events.append(r)
        if label:
            prev[symbol] = label
    return events


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
                _state["oi_label_prev"] = saved.get("oi_label_prev", {})
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
            "oi_label_prev": _state["oi_label_prev"],
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
                    # One extra Kite call per cycle for the Index/Market-
                    # trend filter - fetch_index_direction swallows its
                    # own exceptions and returns (None, None, None) on
                    # any failure, so a bad index fetch can never cost
                    # this cycle's actual stock results.
                    index_direction, index_close, index_chg_pct = fetch_index_direction(kite, settings.TIMEFRAME)
                    with _state_lock:
                        _apply_param_tier(results)
                        _apply_index_filter(results, index_direction)
                        _apply_volume_flow_filter(results)
                        _apply_candle_pattern_filter(results)
                        _apply_weighted_score(results)
                        _apply_oi_trend(results)
                        _apply_oi_screener_fields(results)
                        oi_events = _detect_oi_accel_events(results)
                        _state["results"] = results
                        _state["index_direction"] = index_direction
                        _state["index_close"] = index_close
                        _state["index_chg_pct"] = index_chg_pct
                        _state["last_scan"] = now_ist().isoformat(timespec="seconds")
                        _state["last_error"] = None
                    try:
                        alerts.process_scan_results(results, settings.TIMEFRAME)
                    except Exception:  # noqa: BLE001 - alerting must never break scanning
                        log.exception("Alert processing failed")
                    if oi_events:
                        try:
                            alerts.process_oi_events(oi_events, settings.TIMEFRAME)
                        except Exception:  # noqa: BLE001 - alerting must never break scanning
                            log.exception("OI acceleration alert processing failed")
                    # Forward-testing signal journal (NEXT_HORIZON_RESEARCH.md
                    # Finding 3): fills entries and resolves exits for any
                    # open paper trades, using freshly-fetched candles - same
                    # per-cycle cadence as everything else in this branch, and
                    # deliberately only attempted while the market is open
                    # (see journal.resolve_open_trades) since no new candles
                    # close otherwise, so an off-hours attempt would just be a
                    # wasted no-op fetch.
                    try:
                        journal.resolve_open_trades(kite)
                    except Exception:  # noqa: BLE001 - journal resolution must never break scanning
                        log.exception("Signal journal resolution failed")
                except Exception as exc:  # noqa: BLE001
                    log.exception("Background scan failed")
                    with _state_lock:
                        _state["last_error"] = str(exc)

                _save_persisted_state()
                # wait() instead of a plain sleep() so a Quick Settings /
                # Settings change (web.py calls trigger_rescan()) wakes
                # this loop immediately instead of leaving the dashboard
                # showing results from the OLD settings for up to
                # SCAN_INTERVAL_SECONDS - that delay is what made a
                # timeframe switch look like it "wasn't working".
                _rescan_event.wait(timeout=settings.SCAN_INTERVAL_SECONDS)
                _rescan_event.clear()
            else:
                # Not logged in yet today, or outside market hours - the
                # last scan's results (loaded from disk on startup, or still
                # in memory from earlier today) are left untouched so
                # there's always something on screen to analyse. Check back
                # periodically without hammering anything.
                _rescan_event.wait(timeout=30)
                _rescan_event.clear()
        except Exception:  # noqa: BLE001 - never let this thread die
            log.exception("Background scan loop hit an unexpected error - retrying")
            with _state_lock:
                _state["last_error"] = "Background loop hit an unexpected error - see server logs."
            time.sleep(30)


def start_background_scanner():
    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()


# --------------------------------------------------------------------------
# Multi-timeframe dashboard panel - scans 15-minute, 60-minute, and 4-hour
# independently of the single Settings > Timeframe pipeline above, each on
# its own schedule matched to how often that candle size actually produces
# new information (no point re-scanning a 4-hour bar every 3 minutes the
# way the 15-minute one needs). Added because picking one timeframe to see
# "the" signal meant switching back and forth in Settings just to check
# what a different timeframe was saying - this keeps all three visible on
# the dashboard at once instead, with the existing single-timeframe
# pipeline (and everything downstream of it - OI Screener, Alerts,
# Backtest, positional_qualified/high_conviction) completely untouched.
#
# Deliberately its own state/lock/persistence file rather than folding
# into _state above - this runs 3 independent scan_watchlist() calls on 3
# different schedules from one thread, which is a different shape than
# the single always-in-sync _state the rest of this module manages.
# --------------------------------------------------------------------------

MULTI_TF_TIMEFRAMES = ("15minute", "60minute", "4hour")

# Seconds between re-scans, PER timeframe - not how often the dashboard
# page refreshes (that's still the usual 20s client-side poll industry-
# wide across this app); this is how often each timeframe's own Kite
# historical_data call actually re-runs. 15-minute matches the existing
# single-timeframe default (SCAN_INTERVAL_SECONDS' own default of 180s);
# 60-minute and 4-hour are deliberately slower since their own candles
# close far less often, so there's little informational gain (and real
# extra Kite API load) in scanning them as fast as the 15-minute one.
MULTI_TF_SCAN_INTERVAL_SECONDS = {
    "15minute": 180,   # 3 min
    "60minute": 600,   # 10 min - candle only closes hourly
    "4hour": 900,      # 15 min - candle only closes every 4 hours
}

_multi_tf_lock = threading.Lock()
_multi_tf_state = {
    tf: {
        "results": [], "last_scan": None, "last_error": None,
        "index_direction": None, "index_close": None, "index_chg_pct": None,
    }
    for tf in MULTI_TF_TIMEFRAMES
}
# Epoch seconds (time.time()) each timeframe is next due to be re-scanned -
# 0.0 for all three at startup so every timeframe scans on the very first
# loop iteration after a (re)start, rather than waiting a full interval.
_multi_tf_next_due = {tf: 0.0 for tf in MULTI_TF_TIMEFRAMES}


def _load_persisted_multi_tf_state():
    if not os.path.exists(MULTI_TF_RESULTS_FILE):
        return
    try:
        with open(MULTI_TF_RESULTS_FILE) as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            return
        with _multi_tf_lock:
            for tf in MULTI_TF_TIMEFRAMES:
                entry = saved.get(tf)
                if isinstance(entry, dict):
                    _multi_tf_state[tf]["results"] = entry.get("results", [])
                    _multi_tf_state[tf]["last_scan"] = entry.get("last_scan")
                    _multi_tf_state[tf]["last_error"] = None
                    _multi_tf_state[tf]["index_direction"] = entry.get("index_direction")
                    _multi_tf_state[tf]["index_close"] = entry.get("index_close")
                    _multi_tf_state[tf]["index_chg_pct"] = entry.get("index_chg_pct")
    except (json.JSONDecodeError, OSError):
        pass


def _save_persisted_multi_tf_state():
    with _multi_tf_lock:
        snapshot = {
            tf: {
                "results": _multi_tf_state[tf]["results"],
                "last_scan": _multi_tf_state[tf]["last_scan"],
                "index_direction": _multi_tf_state[tf]["index_direction"],
                "index_close": _multi_tf_state[tf]["index_close"],
                "index_chg_pct": _multi_tf_state[tf]["index_chg_pct"],
            }
            for tf in MULTI_TF_TIMEFRAMES
        }
    try:
        with open(MULTI_TF_RESULTS_FILE, "w") as f:
            json.dump(snapshot, f, default=str)
    except Exception:  # noqa: BLE001 - persistence must never crash the scan loop
        log.exception("Failed to persist multi-timeframe results")


_load_persisted_multi_tf_state()


def get_multi_tf_state():
    with _multi_tf_lock:
        return {tf: dict(_multi_tf_state[tf]) for tf in MULTI_TF_TIMEFRAMES}


def _scan_one_multi_tf(kite, tf):
    """One timeframe's worth of the multi-tf panel: the same
    scan_watchlist() + param-tier bucketing + index-agreement gate the
    single-timeframe pipeline uses (so "Confirmed" means the same thing
    here as it does everywhere else in the app), but deliberately skipping
    _apply_oi_trend/_apply_oi_screener_fields/_apply_weighted_score - this
    panel only needs symbol/direction/aligned/signal_confirmed/close, and
    running the OI-history/day-baseline machinery 3x per cycle for data
    this panel never displays would just be extra work and extra state to
    persist for nothing."""
    results = scan_watchlist(kite, timeframe=tf)
    index_direction, index_close, index_chg_pct = fetch_index_direction(kite, tf)
    _apply_param_tier(results)
    _apply_index_filter(results, index_direction)
    _apply_volume_flow_filter(results)
    _apply_candle_pattern_filter(results)
    with _multi_tf_lock:
        _multi_tf_state[tf]["results"] = results
        _multi_tf_state[tf]["last_scan"] = now_ist().isoformat(timespec="seconds")
        _multi_tf_state[tf]["last_error"] = None
        _multi_tf_state[tf]["index_direction"] = index_direction
        _multi_tf_state[tf]["index_close"] = index_close
        _multi_tf_state[tf]["index_chg_pct"] = index_chg_pct


def _run_multi_tf_loop():
    while True:
        try:
            kite = kite_auth.get_kite_client()
            if kite is not None and is_market_open():
                now = time.time()
                scanned_any = False
                for tf in MULTI_TF_TIMEFRAMES:
                    if now < _multi_tf_next_due[tf]:
                        continue
                    try:
                        _scan_one_multi_tf(kite, tf)
                    except Exception as exc:  # noqa: BLE001 - one timeframe's failure must never block the others
                        log.exception("Multi-timeframe scan failed for %s", tf)
                        with _multi_tf_lock:
                            _multi_tf_state[tf]["last_error"] = str(exc)
                    _multi_tf_next_due[tf] = now + MULTI_TF_SCAN_INTERVAL_SECONDS[tf]
                    scanned_any = True
                if scanned_any:
                    _save_persisted_multi_tf_state()
            # Check every 30s regardless of each timeframe's own interval -
            # cheap (just a few dict comparisons), and keeps whichever
            # timeframe is due next from waiting longer than necessary.
            time.sleep(30)
        except Exception:  # noqa: BLE001 - never let this thread die
            log.exception("Multi-timeframe scan loop hit an unexpected error - retrying")
            time.sleep(30)


def start_multi_tf_scanner():
    thread = threading.Thread(target=_run_multi_tf_loop, daemon=True)
    thread.start()
