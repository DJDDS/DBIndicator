"""
The early-signal engine: OI anomaly detection, normalised per symbol.

WHY THIS MODULE EXISTS
----------------------
The original screen counted four "independent" votes - RSI vs its smoothing
line, MACD vs its signal line, CMF, and relative volume - and called a stock
confirmed when enough of them agreed. The problem is that they are not four
independent readings. RSI and MACD are both derivatives of the same price
series and agree with each other most of the time by construction; CMF and
relative volume are both derived from the same volume series. So "4 of 4
agree" is closer to "2 things agree, twice" than to four separate witnesses.
Requiring all four therefore feels strict while filtering far less than the
count implies - which is exactly why a whole watchlist's worth of names kept
clearing the bar.

Worse, the arithmetic guaranteed it. `dir_match_count = max(n, 3 - n)` is
never below 2 for n in 0..3, so EVERY symbol scored at least 2 of 4 and
landed in a tier. The tier lists were not a filter at all; they were a
partition of the entire universe wearing a filter's clothes.

Open Interest is the fix, because it is the one reading in this app that is
genuinely independent of price and volume: it counts how many futures
contracts are actually open. Price can drift on thin trade and volume can
spike on churn, but OI only rises when someone opens a NEW position. That is
what "positioning ahead of a move" physically looks like in the data.

The catch - and the reason the previous OI panel never worked - is that a
raw OI percentage is meaningless across symbols. A 3% OI jump in a name that
routinely moves +/-5% a day is noise. The same 3% in a name that normally
moves +/-0.4% is a five-sigma event. Absolute percentage-point thresholds
(the old "+2.00pp = Strong") are unreachable for large caps and trivially
exceeded by illiquid ones, so the old screener read "Stable" for nearly
everything while occasionally topping its own sort with a name that traded
four lots.

So every reading here is normalised against THE SYMBOL'S OWN history. A
z-score answers the only question that matters: is this unusual FOR THIS
STOCK? That is the difference between a screener and a sorted list.

NO WARM-UP
----------
The old OI panel sampled OI every scan into an in-memory buffer, so it could
not compute a 30-minute acceleration until 60 minutes of samples existed. It
was structurally blind from 09:15 to roughly 10:15 - the exact window where
a day's trend is normally set - and a redeploy re-imposed the blackout
mid-session.

This module instead builds its baseline from Kite's DAILY OI history
(`historical_data(..., oi=True, continuous=True)`), which is available the
moment the app starts. `continuous=True` also stitches across expiries, so
the roll no longer prints a spurious double-digit OI collapse every month.

MISSING DATA NEVER FLATTERS A ROW
---------------------------------
An earlier scoring panel in this app gave a "neutral middling" score to any
component it had no reading for, on the reasoning that missing data should
not rank a row below one that actively looks bad. For a *gate* that is
right. For a *ranking* it is backwards, and it inverted the panel: a symbol
with almost no measurable data scored in the low 50s while a fully-measured
but genuinely weak one scored 31, so the emptiest rows floated to the top.

Here, a component with no reading earns ZERO and removes its own weight from
the denominator. The score is `earned / available`, and a row whose
measurable components do not cover at least MIN_COVERAGE of the total weight
is not scored at all. Absence can lower confidence or disqualify a row. It
can never raise its rank.
"""
import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Trailing window for every per-symbol baseline. 60 sessions is about a
# quarter - long enough for a stable mean and standard deviation, short
# enough to still reflect the stock's current regime rather than what it
# was doing last year.
BASELINE_DAYS = 60

# Minimum observations before a baseline is trustworthy. Below this the
# standard deviation is too unstable to divide by, and we return None
# rather than a confident-looking number built on six data points.
MIN_BASELINE_OBS = 20

# A z-score this far from the mean is what we mean by "unusual". 1.5 is
# deliberately below the conventional 2.0: we are trying to catch a move
# EARLY, and by the time OI is a clean two-sigma event the move is often
# already underway. This is the single most important tunable here.
OI_Z_THRESHOLD = 1.5

# Fraction of total score weight that must have real readings behind it
# before a row is eligible at all. Below this we do not have enough
# evidence to rank the name against others, so it is excluded rather than
# scored optimistically.
MIN_COVERAGE = 0.60


#: Baseline size for an intraday series. Larger than the daily one because
#: intraday OI is noisier per observation, but still short enough to reflect
#: the stock's CURRENT regime rather than what it was doing two months ago.
INTRADAY_BASELINE_OBS = 400


def _max_obs(intraday):
    return INTRADAY_BASELINE_OBS if intraday else BASELINE_DAYS


def _pct_changes(series, max_obs=BASELINE_DAYS, intraday=False):
    """Percentage changes of a series, most recent last, NaNs dropped.

    `intraday` drops every change that spans an overnight gap, and it
    matters more than it looks. Open Interest genuinely resets its
    character between sessions - positions settle, the next morning opens
    on a different book - so the first bar of each day carries a change
    that is systematically far larger than any within-session move. Leave
    those in the baseline and that one daily jump dominates the standard
    deviation, which makes every real intraday build look unremarkable by
    comparison. The detector would go quiet exactly when it should speak.

    Requires a DatetimeIndex to do it; without one it degrades to the
    plain calculation rather than silently pretending it filtered."""
    if series is None:
        return None
    s = pd.Series(series).dropna()
    s = s[s > 0]  # OI and volume are strictly positive; a zero is missing data
    if len(s) < 3:
        return None
    changes = s.pct_change() * 100.0
    if intraday and isinstance(s.index, pd.DatetimeIndex):
        same_session = pd.Series(s.index.normalize()).diff().eq(pd.Timedelta(0))
        changes = changes[same_session.values]
    return changes.dropna().tail(max_obs)


def _is_stale(series, changes):
    """Does the newest retained change belong to the newest bar?

    Only meaningful for timestamped series; without an index we cannot tell,
    so we do not claim staleness we have not established."""
    try:
        idx = pd.Series(series).dropna().index
        if not isinstance(idx, pd.DatetimeIndex) or not isinstance(changes.index, pd.DatetimeIndex):
            return False
        return bool(len(idx) and len(changes) and changes.index[-1] != idx[-1])
    except Exception:  # noqa: BLE001 - a shape we cannot check is not a shape we reject
        return False


def oi_zscore(oi_history, intraday=False):
    """How unusual is the latest OI change FOR THIS SYMBOL?

    Returns (z, latest_pct_change, sigma) or (None, None, None) when there
    is not enough history to say. The z-score is the whole point: it makes
    a mega-cap and a mid-cap directly comparable, which an absolute
    percentage-point threshold can never do.

    The baseline deliberately EXCLUDES the latest observation - otherwise
    today's change contributes to the mean and sigma it is being measured
    against, which shrinks every z-score toward zero and does so most for
    exactly the large moves we are trying to detect."""
    changes = _pct_changes(oi_history, max_obs=_max_obs(intraday), intraday=intraday)
    if changes is None or len(changes) < MIN_BASELINE_OBS:
        return None, None, None

    # The retained changes may not include the CURRENT bar's.
    #
    # At a session's first bar the only available change spans the overnight
    # gap, and the intraday filter drops it - correctly, because it is not a
    # within-session move. But then `changes.iloc[-1]` is whatever the last
    # within-session change was, which can be from a previous day entirely.
    # Scoring that as "the latest reading" reports a stale number with full
    # confidence: every morning the OI gate would fire on a change that
    # happened yesterday.
    #
    # So verify the newest retained change actually belongs to the newest
    # bar. When it does not, there is no current intraday reading yet, and
    # None is the honest answer - the row simply goes unscored on OI until
    # the session's second bar closes.
    if _is_stale(oi_history, changes):
        return None, None, None

    latest = float(changes.iloc[-1])
    baseline = changes.iloc[:-1]
    mu = float(baseline.mean())
    sigma = float(baseline.std(ddof=1))

    # A near-zero sigma means the OI has been essentially frozen. Dividing
    # by it manufactures enormous z-scores out of rounding noise, so treat
    # it as "no usable baseline" instead.
    if not np.isfinite(sigma) or sigma < 1e-6:
        return None, latest, None

    return round((latest - mu) / sigma, 2), round(latest, 2), round(sigma, 3)


def oi_acceleration_ratio(oi_history, intraday=False):
    """Latest OI change as a multiple of this symbol's own typical move.

    A companion to the z-score that is easier to read at a glance: 3.0
    means "three times the size of a normal day's OI change for this
    stock". Uses mean ABSOLUTE change, so a symbol whose OI drifts up and
    down in equal measure still gets a sensible denominator (its plain
    mean would be near zero and the ratio would explode)."""
    changes = _pct_changes(oi_history, max_obs=_max_obs(intraday), intraday=intraday)
    if changes is None or len(changes) < MIN_BASELINE_OBS:
        return None
    if _is_stale(oi_history, changes):
        return None
    latest = float(changes.iloc[-1])
    # MEDIAN absolute change, not mean. OI series carry occasional huge
    # one-off moves (expiry week, a block trade, an index rebalance). A mean
    # absorbs those into the denominator, so one outlier a month permanently
    # raises the bar and quietly suppresses every ordinary-but-real reading
    # afterwards. The median ignores them, which is what "typical" should
    # mean.
    typical = float(changes.iloc[:-1].abs().median())
    if not np.isfinite(typical) or typical < 1e-6:
        return None
    return round(latest / typical, 2)


def rvol_acceleration(volume_series, fast=1, slow=5):
    """Is participation still BUILDING, or did it spike once and fade?

    A static relative-volume reading cannot tell those apart - both show a
    high number today. This compares the latest bar's volume against the
    average of the preceding `slow` bars, so a genuine build reads above
    1.0 and a fading one-day spike reads below it.

    Also returns whether volume has risen for consecutive bars, which is
    the shape that usually precedes a real move rather than accompanying
    the end of one."""
    if volume_series is None:
        return None, None
    v = pd.Series(volume_series).dropna()
    if len(v) < slow + fast + 1:
        return None, None
    latest = float(v.iloc[-fast:].mean())
    prior = float(v.iloc[-(slow + fast):-fast].mean())
    if not np.isfinite(prior) or prior <= 0:
        return None, None
    ratio = round(latest / prior, 2)
    rising = bool(len(v) >= 3 and v.iloc[-1] > v.iloc[-2] > v.iloc[-3])
    return ratio, rising


def relative_strength(close_series, index_series, lookback=20):
    """Is this stock OUTPERFORMING the index, and is that lead widening?

    This is the axis the old four-vote screen was missing entirely. RSI,
    MACD, CMF and relative volume all describe the stock in isolation, so
    on a day the whole market rallies they light up across the board and
    the screen returns half the universe. Relative strength is measured
    against the market, so it stays discriminating exactly when the others
    stop being: it asks not "is this going up" but "is this going up MORE
    than everything else", which is the question that actually separates a
    leader from a passenger.

    Returns (rs_pct, improving) where rs_pct is the stock's return minus
    the index's over `lookback` bars, in percentage points."""
    if close_series is None or index_series is None:
        return None, None
    c = pd.Series(close_series).dropna()
    i = pd.Series(index_series).dropna()
    if len(c) < lookback + 1 or len(i) < lookback + 1:
        return None, None

    def _ret(s, n):
        past = float(s.iloc[-(n + 1)])
        if not np.isfinite(past) or past <= 0:
            return None
        return (float(s.iloc[-1]) / past - 1.0) * 100.0

    stock_ret, index_ret = _ret(c, lookback), _ret(i, lookback)
    if stock_ret is None or index_ret is None:
        return None, None
    rs = stock_ret - index_ret

    # "Improving" compares the recent half-window against the full one. A
    # stock can carry a large lead earned weeks ago while currently lagging;
    # that is a very different setup from one pulling ahead right now.
    half = max(3, lookback // 2)
    s_half, i_half = _ret(c, half), _ret(i, half)
    improving = None
    if s_half is not None and i_half is not None:
        improving = bool((s_half - i_half) > 0)
    return round(rs, 2), improving


def classify_oi_structure(price_chg_pct, oi_chg_pct, oi_z=None,
                          price_threshold=0.3, require_unusual=True):
    """The classic price/OI quadrant, but only when the OI move is unusual.

    The four names are standard F&O reading:

      price up   + OI up   -> Long Buildup    (new longs; bullish)
      price down + OI up   -> Short Buildup   (new shorts; bearish)
      price up   + OI down -> Short Covering  (shorts closing; bullish but
                                               it is exit flow, not fresh
                                               conviction - it tends to
                                               fade once the shorts are out)
      price down + OI down -> Long Unwinding  (longs closing; bearish, same
                                               exit-flow caveat)

    Two deliberate departures from the previous implementation:

    1. The price threshold is 0.3%, not 0.05%. At 0.05% every symbol lands
       in a quadrant within minutes of the open and then FLIPS every time
       price crosses its own baseline, which is what made the old panel
       churn and re-flag "New" all session.

    2. With require_unusual set, a quadrant is only returned when the OI
       move itself is statistically unusual for the symbol. Otherwise every
       stock is always in some quadrant - which is true, and useless."""
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    if require_unusual and (oi_z is None or abs(oi_z) < OI_Z_THRESHOLD):
        return None

    price_up = price_chg_pct > price_threshold
    price_down = price_chg_pct < -price_threshold
    oi_up = oi_chg_pct > 0
    if not (price_up or price_down):
        return None
    if price_up:
        return "Long Buildup" if oi_up else "Short Covering"
    return "Short Buildup" if oi_up else "Long Unwinding"


#: Which quadrants count as FRESH positioning rather than position closing.
#: Buildups are new money committing in a direction, which is what precedes
#: a sustained move. Covering and unwinding are existing positions leaving;
#: they move price too, but the flow exhausts itself once the trapped side
#: is out, so they make poor multi-day entries.
FRESH_POSITIONING = {"Long Buildup": "Bullish", "Short Buildup": "Bearish"}
EXIT_FLOW = {"Short Covering": "Bullish", "Long Unwinding": "Bearish"}


def oi_direction(structure, include_exit_flow=False):
    """Which way does this OI structure point? None if it does not."""
    if not structure:
        return None
    if structure in FRESH_POSITIONING:
        return FRESH_POSITIONING[structure]
    if include_exit_flow:
        return EXIT_FLOW.get(structure)
    return None


# --------------------------------------------------------------------------
# The score.
#
# Weights are REASONED, not yet backtested - the same honesty the rest of
# this codebase applies to its other hand-chosen numbers. What they encode:
#
#   OI anomaly (30)  - the heaviest single weight, because it is the only
#                      component independent of price and volume, and the
#                      only one that can be early by construction.
#   Volume (20)      - corroboration that real participation is arriving,
#                      weighted on acceleration rather than level.
#   Momentum (20)    - the price read. Deliberately ONE axis, not two:
#                      RSI and MACD are collapsed into a single vote
#                      because counting them separately double-counts the
#                      same underlying quantity.
#   Structure (20)   - where price sits, and whether it just expanded out
#                      of compression.
#   Rel. strength (10) - is it leading or following the market.
#
# Note OI + volume together outweigh price momentum 50 to 20. That is the
# deliberate inversion of the old screen, and the whole point of this
# rewrite: positioning and participation lead, price confirms.
# --------------------------------------------------------------------------

WEIGHTS = {
    "oi_anomaly": 30,
    "volume": 20,
    "momentum": 20,
    "structure": 20,
    "rel_strength": 10,
}


def _score_oi(direction, oi_z, structure):
    """Points for the OI reading, or None when there is no usable reading."""
    if oi_z is None:
        return None, "no OI baseline yet"
    agreed = oi_direction(structure)
    if agreed is None:
        exit_dir = oi_direction(structure, include_exit_flow=True)
        if exit_dir == direction:
            # Right way, but it is exit flow - real, and worth something,
            # but it is trapped traders leaving rather than new conviction.
            return 12, f"{structure} - exit flow, not fresh positioning"
        return 0, "OI shows no unusual positioning in this direction"
    if agreed != direction:
        return 0, f"{structure} points the other way"

    mag = abs(oi_z)
    if mag >= 3.0:
        return 30, f"{structure}, {mag:.1f}x sigma - highly unusual"
    if mag >= 2.0:
        return 25, f"{structure}, {mag:.1f}x sigma - strongly unusual"
    return 18, f"{structure}, {mag:.1f}x sigma - moderately unusual"


def _score_volume(rvol, rvol_accel, rising):
    if rvol is None and rvol_accel is None:
        return None, "no volume reading"
    pts, bits = 0, []
    if rvol is not None:
        if rvol >= 2.0:
            pts += 10; bits.append(f"{rvol:.1f}x normal volume")
        elif rvol >= 1.5:
            pts += 8; bits.append(f"{rvol:.1f}x normal volume")
        elif rvol >= 1.0:
            pts += 4; bits.append(f"{rvol:.1f}x normal volume")
        else:
            bits.append(f"{rvol:.1f}x - below normal volume")
    if rvol_accel is not None:
        if rvol_accel >= 1.5:
            pts += 7; bits.append("participation still building")
        elif rvol_accel >= 1.0:
            pts += 4; bits.append("participation steady")
        else:
            bits.append("participation fading")
    if rising:
        pts += 3; bits.append("rising three bars running")
    return min(pts, WEIGHTS["volume"]), "; ".join(bits)


def _score_momentum(direction, rsi_cross, macd_agrees, rsi_above):
    """ONE momentum vote, not two.

    RSI-vs-its-average and MACD-vs-signal are both price-momentum
    derivatives and agree with each other most of the time. The old screen
    counted them as two of its four votes, so roughly half the total
    evidence was one quantity wearing two hats. Here MACD confirms the RSI
    read rather than voting alongside it."""
    if rsi_cross is None and rsi_above is None:
        return None, "no momentum reading"
    pts, bits = 0, []
    if rsi_cross:
        pts += 12; bits.append("RSI just crossed its average")
    elif rsi_above:
        pts += 7; bits.append("RSI holding above its average")
    else:
        bits.append("RSI below its average")
    if macd_agrees is True:
        pts += 8; bits.append("MACD confirms")
    elif macd_agrees is False:
        bits.append("MACD disagrees")
    return min(pts, WEIGHTS["momentum"]), "; ".join(bits)


def _score_structure(close_pos, big_candle_agrees, coiling, nr7):
    parts = [x for x in (close_pos, big_candle_agrees, coiling, nr7) if x is not None]
    if not parts:
        return None, "no structural reading"
    pts, bits = 0, []
    if close_pos is not None:
        if close_pos >= 80 or close_pos <= 20:
            pts += 8; bits.append(f"closed at {close_pos:.0f}% of range")
        elif 40 <= close_pos <= 60:
            bits.append("closed mid-range - no conviction either way")
        else:
            pts += 4; bits.append(f"closed at {close_pos:.0f}% of range")
    if big_candle_agrees is True:
        pts += 7; bits.append("range expansion in this direction")
    elif big_candle_agrees is False:
        bits.append("last range expansion went the other way")
    if coiling:
        pts += 3; bits.append("volatility compressed")
    if nr7:
        pts += 2; bits.append("narrowest range in 7")
    return min(pts, WEIGHTS["structure"]), "; ".join(bits)


def _score_rel_strength(direction, rs_pct, improving):
    if rs_pct is None:
        return None, "no relative-strength reading"
    # A short wants UNDER-performance; flip the sign so both directions are
    # scored on "is it leading its own case".
    lead = rs_pct if direction == "Bullish" else -rs_pct
    pts, bits = 0, []
    if lead >= 5.0:
        pts += 7; bits.append(f"{lead:+.1f}pp vs NIFTY - clear leader")
    elif lead >= 2.0:
        pts += 5; bits.append(f"{lead:+.1f}pp vs NIFTY")
    elif lead >= 0:
        pts += 2; bits.append(f"{lead:+.1f}pp vs NIFTY - broadly in line")
    else:
        bits.append(f"{lead:+.1f}pp vs NIFTY - lagging the market")
    if improving:
        pts += 3; bits.append("lead widening")
    return min(pts, WEIGHTS["rel_strength"]), "; ".join(bits)


def early_signal_score(direction, *, oi_z=None, oi_structure=None,
                       rvol=None, rvol_accel=None, vol_rising=None,
                       rsi_cross=None, rsi_above=None, macd_agrees=None,
                       close_pos=None, big_candle_agrees=None,
                       coiling=None, nr7=None,
                       rs_pct=None, rs_improving=None):
    """Score a row 0-100 on EVIDENCE ACTUALLY PRESENT.

    Returns a dict with `score`, `coverage`, `eligible` and a per-component
    `parts` list for display. A component with no reading contributes
    nothing AND removes its weight from the denominator, so the score is
    always "percent of what we could measure", never "percent of what we
    wished we could measure". A row covering less than MIN_COVERAGE of the
    total weight is marked ineligible rather than being given a flattering
    partial score."""
    if direction not in ("Bullish", "Bearish"):
        return {"score": None, "coverage": 0.0, "eligible": False, "parts": []}

    scored = [
        ("oi_anomaly", "OI anomaly", _score_oi(direction, oi_z, oi_structure)),
        ("volume", "Volume", _score_volume(rvol, rvol_accel, vol_rising)),
        ("momentum", "Momentum", _score_momentum(direction, rsi_cross, macd_agrees, rsi_above)),
        ("structure", "Structure", _score_structure(close_pos, big_candle_agrees, coiling, nr7)),
        ("rel_strength", "Relative strength", _score_rel_strength(direction, rs_pct, rs_improving)),
    ]

    earned = available = 0
    parts = []
    for key, label, (pts, note) in scored:
        weight = WEIGHTS[key]
        measured = pts is not None
        if measured:
            earned += pts
            available += weight
        parts.append({
            "id": key, "label": label, "points": pts if measured else None,
            "max": weight, "measured": measured, "note": note,
        })

    if available == 0:
        return {"score": None, "coverage": 0.0, "eligible": False, "parts": parts}

    total_weight = sum(WEIGHTS.values())
    coverage = available / total_weight
    return {
        "score": int(round(100.0 * earned / available)),
        "coverage": round(coverage, 2),
        "eligible": bool(coverage >= MIN_COVERAGE),
        "earned": earned,
        "available": available,
        "parts": parts,
    }
