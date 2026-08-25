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

from . import alerts, delivery, journal, kite_auth, news
from .config import (
    settings, SCAN_RESULTS_FILE, PARAM_WEIGHTS_FILE, MULTI_TF_RESULTS_FILE,
    TIMEFRAME_LABELS, WATCHLIST_TIMEFRAME,
)
from .scanner import (
    scan_watchlist, is_market_open, now_ist, compute_oi_acceleration,
    classify_oi_structure, fetch_index_direction, fetch_sector_directions,
    SYMBOL_SECTOR_MAP,
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
    {"id": "cmf", "label": "Chaikin Money Flow (directional volume)"},
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


def _apply_macd_hist_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_MACD_HIST_AGREEMENT is on, a row that already has
    macd_hist_agrees=False (set by indicators.compute_signal - is the
    MACD histogram growing in this row's own direction, i.e. momentum
    accelerating rather than fading) also loses its signal_confirmed
    status - same shape as _apply_volume_flow_filter/_apply_candle_
    pattern_filter above. Off by default; macd_hist/macd_hist_rising/
    macd_hist_agrees are always attached by compute_signal either way,
    purely for display."""
    if not settings.REQUIRE_MACD_HIST_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("macd_hist_agrees") is False:
            r["signal_confirmed"] = False


def _apply_big_candle_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_BIG_CANDLE_AGREEMENT is on, a row that already has
    big_candle_agrees=False (set by indicators.compute_signal - does the
    most recent qualifying range-expansion "big candle" within
    BIG_CANDLE_LOOKBACK bars agree with this row's own direction) also
    loses its signal_confirmed status - same shape as _apply_volume_flow_
    filter/_apply_candle_pattern_filter/_apply_macd_hist_filter above.
    Off by default; big_candle/big_candle_direction/big_candle_level/
    big_candle_recent_*/big_candle_continuation/big_candle_agrees are
    always attached by compute_signal either way, purely for display."""
    if not settings.REQUIRE_BIG_CANDLE_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("big_candle_agrees") is False:
            r["signal_confirmed"] = False


def _apply_strong_close_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_STRONG_CLOSE_AGREEMENT is on, a row that already has
    strong_close_agrees=False (set by indicators.compute_signal - did
    this bar's own close land in the extreme top/bottom
    STRONG_CLOSE_THRESHOLD_PCT% of its own high-low range, in this row's
    own direction) also loses its signal_confirmed status - a BTST-
    oriented "closed with real conviction" gate, same shape as every
    other filter here. Off by default; close_position_pct/strong_close_
    agrees are always attached by compute_signal either way, purely for
    display."""
    if not settings.REQUIRE_STRONG_CLOSE_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("strong_close_agrees") is False:
            r["signal_confirmed"] = False


def _apply_entry_location_filter(results):
    """Mutates each result dict in place: when settings.
    REQUIRE_ENTRY_LOCATION_AGREEMENT is on, a row that already has
    entry_location_agrees=False (set by indicators.compute_signal - price
    is already more than MAX_ENTRY_EXTENSION_ATR ATRs past its own VWAP
    in this row's own direction, i.e. the move is being CHASED rather
    than caught early) also loses its signal_confirmed status. Off by
    default; entry_extension_atr/entry_is_extended/entry_reference/
    entry_location_agrees are always attached by compute_signal either
    way, purely for display."""
    if not settings.REQUIRE_ENTRY_LOCATION_AGREEMENT:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("entry_location_agrees") is False:
            r["signal_confirmed"] = False


def _apply_atr_floor_filter(results):
    """Mutates each result dict in place: when settings.REQUIRE_ATR_FLOOR
    is on, a row that already has atr_floor_agrees=False (set by
    indicators.compute_signal - this stock's ATR as a % of its own price
    is below settings.MIN_ATR_PCT, i.e. it isn't currently moving enough
    to plausibly deliver a big move regardless of how many parameters
    agree) also loses its signal_confirmed status. Off by default;
    atr_pct/atr_floor_agrees are always attached by compute_signal either
    way, purely for display."""
    if not settings.REQUIRE_ATR_FLOOR:
        return
    for r in results:
        if r.get("error"):
            continue
        if r.get("signal_confirmed") and r.get("atr_floor_agrees") is False:
            r["signal_confirmed"] = False


def _apply_delivery_filter(results):
    """Mutates each result dict in place, attaching delivery_pct/
    delivery_date/delivery_agrees from app/delivery.py's cache (see that
    module's docstring for the timing/reliability caveats - this is
    NEVER a same-day-live number, and the fetch can be blocked entirely
    depending on where this app is hosted). None (no delivery data
    available for this symbol yet) always reads delivery_agrees=True,
    same "never block on missing data" convention as every other gate.

    When settings.REQUIRE_DELIVERY_AGREEMENT is on, a row whose delivery
    reading is below settings.DELIVERY_THRESHOLD_PCT also loses its
    signal_confirmed status. Off by default; delivery_pct/delivery_date/
    delivery_agrees are always attached either way, purely for display.
    Does NOT call delivery.refresh_if_stale() itself - see _run_loop,
    which triggers that at most once per cycle so the multi-tf loop's own
    calls to this function (see _scan_one_multi_tf) never trigger a
    second, redundant network attempt."""
    require = settings.REQUIRE_DELIVERY_AGREEMENT
    threshold = settings.DELIVERY_THRESHOLD_PCT
    for r in results:
        symbol = r.get("symbol")
        if r.get("error") or not symbol:
            r["delivery_pct"] = None
            r["delivery_date"] = None
            r["delivery_agrees"] = None
            continue
        pct, date = delivery.get_delivery_pct(symbol)
        r["delivery_pct"] = pct
        r["delivery_date"] = date
        r["delivery_agrees"] = True if pct is None else (pct >= threshold)
        if require and r.get("signal_confirmed") and not r["delivery_agrees"]:
            r["signal_confirmed"] = False


def _apply_sector_filter(results, sector_directions):
    """Mutates each result dict in place, attaching sector (the NSE
    sectoral index this symbol maps to, or None if it isn't in
    scanner.SYMBOL_SECTOR_MAP), sector_direction (that index's own
    current confluence direction, from sector_directions - see
    scanner.fetch_sector_directions), and sector_agrees - does this
    row's own direction match its sector's? Same "None means agree"
    convention used everywhere else: a symbol with no sector mapping,
    or a sector whose fetch didn't resolve this cycle, always reads
    sector_agrees=True, never blocking anything on its own.

    When settings.REQUIRE_SECTOR_AGREEMENT is on, a row that disagrees
    with its own sector also loses its signal_confirmed status - same
    shape as _apply_index_filter, just keyed per-symbol by sector
    instead of one shared index-wide value. Off by default; sector/
    sector_direction/sector_agrees are always attached either way,
    purely for display (the small sector badge next to Signal)."""
    require = settings.REQUIRE_SECTOR_AGREEMENT
    for r in results:
        if r.get("error") or not r.get("direction"):
            r["sector"] = None
            r["sector_direction"] = None
            r["sector_agrees"] = None
            continue
        sector = SYMBOL_SECTOR_MAP.get(r.get("symbol"))
        r["sector"] = sector
        sector_direction = sector_directions.get(sector) if sector else None
        r["sector_direction"] = sector_direction
        r["sector_agrees"] = True if sector_direction is None else (r["direction"] == sector_direction)
        if require and r.get("signal_confirmed") and not r["sector_agrees"]:
            r["signal_confirmed"] = False


def _compute_breadth(results):
    """Advances/declines across the CURRENT watchlist's own scan results
    (not full-NSE breadth - Kite has no cheap all-market advance/decline
    endpoint, so this is a watchlist-scoped proxy computed for free from
    data this cycle already fetched, labelled as such wherever it's
    shown). Only rows with a clear, error-free direction count toward
    the total; rows with no signal at all are excluded rather than
    counted as neutral. Returns {"bullish": int, "bearish": int,
    "total": int, "bullish_pct": float|None, "bearish_pct": float|None}
    - the two _pct fields are None when total is 0 (e.g. every row
    errored), so callers never divide by zero."""
    bullish = sum(1 for r in results if not r.get("error") and r.get("direction") == "Bullish")
    bearish = sum(1 for r in results if not r.get("error") and r.get("direction") == "Bearish")
    total = bullish + bearish
    return {
        "bullish": bullish,
        "bearish": bearish,
        "total": total,
        "bullish_pct": round(bullish / total * 100, 1) if total else None,
        "bearish_pct": round(bearish / total * 100, 1) if total else None,
    }


def _apply_breadth_filter(results, breadth):
    """Mutates each result dict in place, attaching breadth_agrees - is
    at least settings.BREADTH_THRESHOLD_PCT of the CURRENT watchlist's
    resolved rows also pointing this row's own direction? None/empty
    breadth (no resolved rows this cycle) always reads breadth_agrees
    =True, same "never block on missing data" convention as every other
    gate here.

    When settings.REQUIRE_BREADTH_AGREEMENT is on, a row whose own
    direction is decisively against the watchlist's current advance/
    decline split also loses its signal_confirmed status - operationalizes
    NEXT_HORIZON_RESEARCH.md Finding 5's "don't fully trust a bullish
    breakout on a day the broader market is mostly declining" as a
    watchlist-scoped proxy. Off by default; breadth_agrees is always
    attached either way, purely for display."""
    require = settings.REQUIRE_BREADTH_AGREEMENT
    threshold = settings.BREADTH_THRESHOLD_PCT
    total = breadth.get("total") or 0
    for r in results:
        if r.get("error") or not r.get("direction"):
            r["breadth_agrees"] = None
            continue
        if total == 0:
            r["breadth_agrees"] = True
        elif r["direction"] == "Bullish":
            r["breadth_agrees"] = (breadth.get("bullish_pct") or 0) >= threshold
        else:
            r["breadth_agrees"] = (breadth.get("bearish_pct") or 0) >= threshold
        if require and r.get("signal_confirmed") and not r["breadth_agrees"]:
            r["signal_confirmed"] = False


# Equal-weight fallback for weighted_score below, until you've run
# "Auto-Weight Parameters" on the Backtest page at least once - matches
# the plain aligned/4 count in spirit (every parameter counts the same).
_DEFAULT_PARAM_WEIGHTS = {
    "rsi_cross": 0.25, "macd_cross": 0.25, "cmf_flow": 0.25, "rel_volume": 0.25,
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
    than treating RSI/MACD/CMF/Relative Volume as equally weighted,
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
        if r.get("vol_flow_direction") == direction:
            score += weights.get("cmf_flow", 0)
        if r.get("vol_confirmed"):
            score += weights.get("rel_volume", 0)
        r["weighted_score"] = round(score * 100, 1)


# --------------------------------------------------------------------------
# Entry-quality ranking - the "Best Entries" dashboard panel.
#
# This does NOT decide whether a row has a signal (aligned/signal_confirmed
# already did that, and this never changes either). It answers the SECOND
# question, the one you're left doing by eye otherwise: given several rows
# that all currently qualify, which are the better ENTRIES right now - early
# rather than chasing, in a stock that actually moves, with the anticipatory
# reads pointing the same way.
#
# IMPORTANT, and stated the same way _apply_oi_screener_fields states it for
# "High Conviction": the weights below are REASONED, not backtested. They
# encode a specific opinion - that entry location matters most, that a stock
# too quiet to move is a bad entry regardless of how many indicators agree,
# and that the range-expansion/contraction reads carry real information -
# but nobody has measured this exact combination against historical outcomes.
# Treat the ordering as "a sensible way to read the shortlist," not as a
# validated edge. Every component's own contribution is shown in the panel's
# tooltip precisely so this stays a transparent ranking rather than a black
# box, and so a component you disagree with is visible rather than buried.
#
# All six components read fields compute_signal already attached - no extra
# Kite call, no new state, safe to recompute every scan cycle.
# --------------------------------------------------------------------------

ENTRY_QUALITY_TOP_N = 8  # how many rows the panel shows - a shortlist, not another full table


def _entry_quality_score(r):
    """Returns (score 0-100, parts) for one already-confirmed row. `parts`
    is a list of {label, points, max, note} dicts driving the panel's
    tooltip breakdown. A component with no reading scores a middling
    "neutral" value rather than zero - missing data should never rank a
    row below one that actively looks bad (same "None never blocks"
    convention every gate in this module already follows)."""
    parts = []
    direction = r.get("direction")

    # 1. Entry location (0-30, the heaviest single component) - how far
    # price has already run past VWAP in ATR units, in this row's own
    # direction. Weighted highest because it's the one component that
    # directly separates "catching a move" from "chasing one."
    ext = r.get("entry_extension_atr")
    max_ext = settings.MAX_ENTRY_EXTENSION_ATR
    if ext is None:
        pts, note = 15, "no VWAP reading on this timeframe"
    elif ext <= 0:
        pts, note = 30, f"{ext}R - at or behind VWAP, not chasing"
    elif ext <= max_ext / 2:
        pts, note = 24, f"{ext}R - still early in the move"
    elif ext <= max_ext:
        pts, note = 14, f"{ext}R - move already underway"
    else:
        pts, note = 0, f"{ext}R - extended past your {max_ext}R limit"
    parts.append({"label": "Entry location", "points": pts, "max": 30, "note": note})

    # 2. Big candle (0-25) - has a real range-expansion bar fired in this
    # row's direction, and has price actually followed through on the
    # level it set? A cleared level is the strongest read here; one that's
    # been set but not yet taken out is weaker but still constructive.
    bc_dir = r.get("big_candle_recent_direction")
    if bc_dir is None:
        pts, note = 8, "no recent range-expansion bar"
    elif bc_dir != direction:
        pts, note = 0, f"last big candle was {bc_dir} - against this row"
    elif r.get("big_candle_continuation") is True:
        pts, note = 25, f"level {r.get('big_candle_recent_level')} cleared"
    elif r.get("big_candle_recent_bars_ago") == 0:
        pts, note = 20, "this bar IS the range expansion"
    else:
        pts, note = 15, f"level {r.get('big_candle_recent_level')} set, not yet cleared"
    parts.append({"label": "Big candle", "points": pts, "max": 25, "note": note})

    # 3. Volatility floor (0-15) - is there enough daily range here to
    # produce a move worth taking? A stock below the floor scores zero
    # outright rather than merely losing points: no amount of indicator
    # agreement makes a dead stock a good entry.
    atr_pct = r.get("atr_pct")
    floor = settings.MIN_ATR_PCT
    if atr_pct is None:
        pts, note = 7, "no ATR reading"
    elif atr_pct >= floor * 2:
        pts, note = 15, f"ATR {atr_pct}% - moves plenty"
    elif atr_pct >= floor:
        pts, note = 12, f"ATR {atr_pct}% - clears your {floor}% floor"
    else:
        pts, note = 0, f"ATR {atr_pct}% - below your {floor}% floor, too quiet"
    parts.append({"label": "Volatility", "points": pts, "max": 15, "note": note})

    # 4. Coiling (0-15) - the anticipatory read. Deliberately scored as a
    # BONUS with a non-zero floor rather than a penalty: a stock that
    # isn't currently coiled hasn't done anything wrong, it just doesn't
    # get the extra credit a squeezed one does.
    coil, nr7 = bool(r.get("vol_contracting")), bool(r.get("nr7"))
    if coil and nr7:
        pts, note = 15, "band width tight AND narrowest range of 7"
    elif coil:
        pts, note = 12, f"band width in the tightest {settings.VOL_CONTRACTION_THRESHOLD_PCT}%"
    elif nr7:
        pts, note = 8, "narrowest range of the last 7 bars"
    else:
        pts, note = 5, "range is normal, not coiled"
    parts.append({"label": "Coiling", "points": pts, "max": 15, "note": note})

    # 5. Strong close (0-10) - did the bar close decisively in this row's
    # own direction? Partial credit for the right half of the range even
    # when it isn't extreme enough to count as decisive.
    cp = r.get("close_position_pct")
    thr = settings.STRONG_CLOSE_THRESHOLD_PCT
    # Name the bar explicitly (see config.BTST_TIMEFRAMES): on a 15-minute
    # scan this component is scoring the last 15 minutes, NOT the day, and
    # the tooltip should never let that pass unnoticed.
    tf_label = TIMEFRAME_LABELS.get(WATCHLIST_TIMEFRAME, WATCHLIST_TIMEFRAME)
    if cp is None:
        pts, note = 4, "no close-position reading"
    elif (direction == "Bullish" and cp >= thr) or (direction == "Bearish" and cp <= 100 - thr):
        pts, note = 10, f"closed at {cp}% of its {tf_label} range - decisive"
    elif (direction == "Bullish" and cp >= 50) or (direction == "Bearish" and cp <= 50):
        pts, note = 6, f"closed at {cp}% of its {tf_label} range - right side, not decisive"
    else:
        pts, note = 0, f"closed at {cp}% of its {tf_label} range - wrong side for a {direction} row"
    parts.append({"label": "Strong close", "points": pts, "max": 10, "note": note})

    # 6. Delivery (0-5, deliberately the lightest) - real overnight
    # conviction, but it's never a live number and is frequently
    # unavailable entirely (see delivery.py), so it can nudge a ranking
    # rather than drive one.
    dp = r.get("delivery_pct")
    if dp is None:
        pts, note = 3, "no delivery data available"
    elif dp >= settings.DELIVERY_THRESHOLD_PCT:
        pts, note = 5, f"{dp}% delivered ({r.get('delivery_date')})"
    else:
        pts, note = 0, f"{dp}% delivered - below your {settings.DELIVERY_THRESHOLD_PCT}% mark"
    parts.append({"label": "Delivery", "points": pts, "max": 5, "note": note})

    return sum(p["points"] for p in parts), parts



# --------------------------------------------------------------------------
# BTST / STBT candidates - replaces the old "High Conviction" panel.
#
# High Conviction stacked several conditions that move together in practice
# (a 4-of-4 row is already likely to be above VWAP and volume-heavy), so it
# looked far more selective than it actually was, and its own docstring
# admitted nothing about it had been backtested. Worse, it answered a
# question nobody asked: "which row has the most things lit up?"
#
# This answers the question actually being traded instead: "which of these
# is worth HOLDING OVERNIGHT?" - which is a genuinely different bar, because
# an overnight position carries gap risk that an intraday one does not. And
# rather than emitting one opaque flag, every candidate carries its own
# reasoning: each check is shown as met or not met, in plain words, so the
# panel argues its case instead of asserting it.
#
# The one non-negotiable is a STRONG CLOSE in the row's own direction. That
# is the defining BTST/STBT condition - holding something overnight that
# closed weak into the bell is the opposite of the setup, no matter how many
# indicators agree. Everything else is supporting evidence, counted and
# shown, never silently required.
#
# TIMING CAVEAT, surfaced in the panel itself: the daily bar is still FORMING
# during the session, so "closed strong" only becomes final near 15:30. Read
# this panel late in the day; earlier it is a provisional read on a bar that
# has not closed.
# --------------------------------------------------------------------------

def _btst_reasons(r, direction):
    """Each entry is {ok, text}: ok=True met, False not met, None unknown
    (missing data, which is never counted against a row - same convention
    every gate in this module uses)."""
    out = []

    cp = r.get("close_position_pct")
    if cp is not None:
        pos = cp if direction == "Bullish" else 100 - cp
        out.append({"ok": True, "text": f"Closed at {cp}% of the day's range - buyers held it into the bell"
                                        if direction == "Bullish" else
                                        f"Closed at {cp}% of the day's range - sellers held it into the bell",
                    "_pos": pos})

    out.append({"ok": r.get("entry_location_agrees") is not False,
                "text": ("Not extended - you're not carrying an already-stretched move overnight"
                         if r.get("entry_location_agrees") is not False else
                         f"Already {r.get('entry_extension_atr')} ATR past VWAP - carrying an extended move overnight is where gaps hurt")})

    out.append({"ok": r.get("atr_floor_agrees") is not False,
                "text": (f"Moves enough to be worth the gap risk (ATR {r.get('atr_pct')}%)"
                         if r.get("atr_floor_agrees") is not False else
                         f"Too quiet (ATR {r.get('atr_pct')}%) - little upside to offset overnight gap risk")})

    out.append({"ok": bool(r.get("htf_agrees")),
                "text": ("Weekly trend agrees" if r.get("htf_agrees")
                         else "Against the weekly trend - a gap against you is more likely")})

    out.append({"ok": bool(r.get("vol_confirmed")),
                "text": (f"Real participation today ({r.get('vol_multiple')}x average volume)"
                         if r.get("vol_confirmed") else
                         f"Thin participation ({r.get('vol_multiple')}x average) - the move lacks backing")})

    flow = r.get("vol_flow_direction")
    out.append({"ok": None if flow is None else (flow == direction),
                "text": ("No clear money-flow read" if flow is None else
                         ("Money flow agrees - volume skewed the same way" if flow == direction
                          else "Money flow disagrees - today's volume leaned the other way"))})

    dp = r.get("delivery_pct")
    out.append({"ok": None if dp is None else bool(r.get("delivery_agrees")),
                "text": ("No delivery data (NSE publishes after the close)" if dp is None else
                         (f"{dp}% delivery - real positional buying, not intraday churn"
                          if r.get("delivery_agrees") else
                          f"Only {dp}% delivery - mostly intraday churn, weak overnight conviction"))})

    if r.get("big_candle_recent_direction") == direction:
        out.append({"ok": bool(r.get("big_candle_continuation")),
                    "text": (f"Cleared its range-expansion level ({r.get('big_candle_recent_level')})"
                             if r.get("big_candle_continuation") else
                             f"Range-expansion level {r.get('big_candle_recent_level')} not cleared yet")})

    out.append({"ok": r.get("index_agrees") is not False,
                "text": ("NIFTY agrees - overnight gaps are largely market-driven"
                         if r.get("index_agrees") is not False else
                         "Counter to NIFTY - overnight gaps are largely market-driven")})

    for o in out:
        o.pop("_pos", None)
    return out


def _apply_btst_candidates(results):
    """Attaches btst_side ("BTST"/"STBT"/None), btst_reasons and btst_score.

    Only Confirmed rows that ALSO closed strong in their own direction
    qualify - see the module note above on why the strong close is the one
    hard requirement. Everything else is counted, shown, and never silently
    decisive. Must run after every gate that can revoke signal_confirmed and
    after the index/delivery filters, so the reasoning reflects final state."""
    threshold = settings.STRONG_CLOSE_THRESHOLD_PCT
    for r in results:
        r["btst_side"] = None
        r["btst_reasons"] = None
        r["btst_score"] = None
        if r.get("error") or not r.get("signal_confirmed"):
            continue
        direction = r.get("direction")
        cp = r.get("close_position_pct")
        if direction not in ("Bullish", "Bearish") or cp is None:
            continue
        closed_strong = cp >= threshold if direction == "Bullish" else cp <= (100 - threshold)
        if not closed_strong:
            continue
        reasons = _btst_reasons(r, direction)
        r["btst_side"] = "BTST" if direction == "Bullish" else "STBT"
        r["btst_reasons"] = reasons
        r["btst_score"] = sum(1 for x in reasons if x["ok"] is True)


def _apply_entry_quality(results):
    """Mutates each result dict in place, attaching entry_quality (0-100)
    and entry_quality_parts (the per-component breakdown behind it).
    Only scored for rows that are currently signal_confirmed - this ranks
    among VALID signals, it never promotes a row that didn't qualify in
    the first place, so the panel can only ever be a re-ordering of what
    the screener already surfaced. Everything else gets None and is
    simply absent from the panel.

    Must run AFTER every gate that can revoke signal_confirmed (the
    REQUIRE_* filters above, including breadth) so it never ranks a row
    that a later filter would have thrown out."""
    for r in results:
        if r.get("error") or not r.get("signal_confirmed") or not r.get("direction"):
            r["entry_quality"] = None
            r["entry_quality_parts"] = None
            continue
        score, parts = _entry_quality_score(r)
        r["entry_quality"] = score
        r["entry_quality_parts"] = parts


def _apply_journal_confidence(results):
    """Mutates each result dict in place, attaching journal_confidence -
    a REALIZED win rate/avg return/count from YOUR OWN Signal Journal
    history for rows sharing this row's (direction, aligned) setup (see
    journal.get_setup_confidence/CONFIDENCE_MIN_SAMPLE). None whenever
    that exact setup hasn't cleared the minimum sample yet - shown as
    nothing on the dashboard rather than a misleadingly precise number
    from a handful of trades. Cheap (in-memory list comprehensions over a
    personal-sized journal, no I/O) - safe to call every scan cycle."""
    for r in results:
        if r.get("error") or not r.get("direction") or r.get("aligned") is None:
            r["journal_confidence"] = None
            continue
        r["journal_confidence"] = journal.get_setup_confidence(r["direction"], r["aligned"])


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
    "breadth": None,
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
                    index_direction, index_close, index_chg_pct = fetch_index_direction(kite, WATCHLIST_TIMEFRAME)
                    # One more Kite call PER DISTINCT SECTOR actually
                    # present in this cycle's results (typically well
                    # under a dozen, not one per watchlist symbol) for
                    # the sector relative-strength filter - see
                    # scanner.fetch_sector_directions, same swallow-all-
                    # failures contract as the index fetch above.
                    sectors_needed = {SYMBOL_SECTOR_MAP[r["symbol"]] for r in results
                                       if r.get("symbol") in SYMBOL_SECTOR_MAP}
                    sector_directions = fetch_sector_directions(kite, sectors_needed, WATCHLIST_TIMEFRAME) \
                        if sectors_needed else {}
                    # Once per cycle, not once per result - see delivery.
                    # refresh_if_stale's own docstring for why this is
                    # cheap to call unconditionally (it no-ops unless the
                    # cache is genuinely stale AND enough time has passed
                    # since the last attempt).
                    try:
                        delivery.refresh_if_stale(now_ist())
                    except Exception:  # noqa: BLE001 - delivery refresh must never break scanning
                        log.exception("Delivery data refresh failed")
                    with _state_lock:
                        _apply_param_tier(results)
                        _apply_index_filter(results, index_direction)
                        _apply_candle_pattern_filter(results)
                        _apply_macd_hist_filter(results)
                        _apply_big_candle_filter(results)
                        _apply_strong_close_filter(results)
                        _apply_entry_location_filter(results)
                        _apply_atr_floor_filter(results)
                        _apply_delivery_filter(results)
                        _apply_sector_filter(results, sector_directions)
                        breadth = _compute_breadth(results)
                        _apply_breadth_filter(results, breadth)
                        # Must come after every REQUIRE_* gate above - it
                        # ranks only rows that are still signal_confirmed
                        # once all of them have had their say.
                        _apply_entry_quality(results)
                        _apply_btst_candidates(results)
                        _apply_weighted_score(results)
                        _apply_journal_confidence(results)
                        _apply_oi_trend(results)
                        _apply_oi_screener_fields(results)
                        oi_events = _detect_oi_accel_events(results)
                        _state["results"] = results
                        _state["index_direction"] = index_direction
                        _state["index_close"] = index_close
                        _state["index_chg_pct"] = index_chg_pct
                        _state["breadth"] = breadth
                        _state["last_scan"] = now_ist().isoformat(timespec="seconds")
                        _state["last_error"] = None
                    try:
                        # Time-driven, once per day - see alerts.publish_btst_candidates
                        alerts.publish_btst_candidates(results, now_ist())
                    except Exception:  # noqa: BLE001 - must never break scanning
                        log.exception("BTST publish failed")
                    try:
                        alerts.process_scan_results(results, WATCHLIST_TIMEFRAME)
                    except Exception:  # noqa: BLE001 - alerting must never break scanning
                        log.exception("Alert processing failed")
                    if oi_events:
                        try:
                            alerts.process_oi_events(oi_events, WATCHLIST_TIMEFRAME)
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
                    # News (NEXT_HORIZON_RESEARCH.md predates this - added
                    # later, at your request): scoped to symbols CURRENTLY
                    # Confirmed (the rows you'd actually act on), throttled/
                    # capped internally by news.py to stay well under the
                    # free Marketaux tier's 100/day budget. A skipped
                    # (throttled/capped/not-configured) fetch just returns
                    # the existing cache, so this is cheap to call every
                    # cycle regardless.
                    try:
                        if news.news_enabled():
                            confirmed_symbols = [r["symbol"] for r in results
                                                  if not r.get("error") and r.get("signal_confirmed")]
                            news_by_symbol = news.fetch_news_for_symbols(confirmed_symbols)
                            for r in results:
                                if r["symbol"] in news_by_symbol:
                                    r["news"] = news_by_symbol[r["symbol"]][:3]
                            new_articles = news.detect_new_articles(news_by_symbol, confirmed_symbols)
                            if new_articles:
                                alerts.process_news_articles(new_articles, WATCHLIST_TIMEFRAME)
                    except Exception:  # noqa: BLE001 - news must never break scanning
                        log.exception("News fetch/alert failed")
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
# Backtest, positional_qualified) completely untouched.
#
# Deliberately its own state/lock/persistence file rather than folding
# into _state above - this runs 3 independent scan_watchlist() calls on 3
# different schedules from one thread, which is a different shape than
# the single always-in-sync _state the rest of this module manages.
# --------------------------------------------------------------------------

# 60-minute was dropped: it sat between the two timeframes that
# actually do a job here - 15-minute for entry timing, 4-hour as the
# trend it's cross-checked against (indicators._HTF_RESAMPLE maps
# 15minute -> 4hour), so a third panel in the middle added scanning
# load and screen clutter without answering a question the other two
# didn't already answer.
MULTI_TF_TIMEFRAMES = ("15minute", "4hour")

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
    _apply_candle_pattern_filter(results)
    _apply_macd_hist_filter(results)
    _apply_big_candle_filter(results)
    _apply_strong_close_filter(results)
    _apply_entry_location_filter(results)
    _apply_atr_floor_filter(results)
    _apply_delivery_filter(results)  # cache lookup only - see that function's docstring, no network call here
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
