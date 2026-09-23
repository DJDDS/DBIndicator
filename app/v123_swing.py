"""V12.3 persistent 1D swing Focus Desk.

This is deliberately slower than the intraday Focus Desk.  Membership is
refreshed only at a few session checkpoints and never churns every three
minutes.  The engine is underlying-first and uses already-available 15m scan
facts plus the same scan's 4H higher-timeframe opinion and 20-session range
context; it does not depend on option suitability.

This is decision support, not a validated production alpha model.
"""
from __future__ import annotations

import datetime as dt
import math

MAX_SWING_FOCUS = 5
REFRESH_SLOTS = (
    ("MORNING", 9 * 60 + 45),
    ("MIDDAY", 12 * 60 + 30),
    ("FINAL", 14 * 60 + 30),
)


def _f(v, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _iso(now):
    return now.isoformat(timespec="seconds")


def empty_state():
    return {
        "trade_date": None,
        "phase": "WAITING",
        "last_refresh_slot": None,
        "last_refresh_at": None,
        "selected": {},
        "bench": [],
    }


def _slot(now):
    minute = now.hour * 60 + now.minute
    active = None
    for name, start in REFRESH_SLOTS:
        if minute >= start:
            active = name
    return active


def _direction(row):
    close = _f(row.get("close"))
    hi20 = _f(row.get("prior_high_20d"))
    lo20 = _f(row.get("prior_low_20d"))
    if close is not None and hi20 is not None and close >= hi20:
        return "Bullish", "20D_RANGE_BREAK"
    if close is not None and lo20 is not None and close <= lo20:
        return "Bearish", "20D_RANGE_BREAK"

    bdir = row.get("breakout_direction") or row.get("retained_breakout_direction")
    htf = row.get("htf_direction")
    if bdir in ("Bullish", "Bearish") and (htf is None or htf == bdir):
        return bdir, "BREAKOUT_CONTINUATION"

    day = _f(row.get("price_chg_today_pct"))
    lead = _f(row.get("stock_sector_lead_pct"))
    if htf == "Bullish" and day is not None and day > 0 and (lead is None or lead >= 0):
        return "Bullish", "4H_TREND_CONTINUATION"
    if htf == "Bearish" and day is not None and day < 0 and (lead is None or lead <= 0):
        return "Bearish", "4H_TREND_CONTINUATION"
    return None, None


def _oi_support(row, direction):
    structure = str(row.get("oi_structure") or "")
    if direction == "Bullish":
        return structure in ("Long Buildup", "Short Covering")
    return structure in ("Short Buildup", "Long Unwinding")


def _candidate(row):
    if row.get("error"):
        return None
    direction, family = _direction(row)
    if direction is None:
        return None

    close = _f(row.get("close"))
    prev = _f(row.get("prev_close"))
    atr = _f(row.get("atr"))
    if close is None or prev is None or atr is None or atr <= 0:
        return None

    sign = 1.0 if direction == "Bullish" else -1.0
    day_move_pct = (close / prev - 1.0) * 100.0 if prev else 0.0
    if sign * day_move_pct <= 0:
        return None

    consumed_atr = abs(close - prev) / atr
    htf = row.get("htf_direction")
    htf_agrees = htf is None or htf == direction
    sector_lead = _f(row.get("stock_sector_lead_pct"))
    sector_agrees = sector_lead is None or sign * sector_lead >= 0
    oi_support = _oi_support(row, direction)
    participation = max(_f(row.get("tod_rvol"), 0.0), _f(row.get("vol_multiple"), 0.0))
    participation_ok = participation >= 1.0
    recent_oi = _f(row.get("oi_chg_60m_pct"))
    recent_oi_ok = recent_oi is not None and recent_oi >= 0

    # Require actual structure plus at least two contextual witnesses.
    witnesses = sum(bool(x) for x in (htf_agrees, sector_agrees, oi_support, participation_ok, recent_oi_ok))
    if family not in ("20D_RANGE_BREAK", "BREAKOUT_CONTINUATION") and witnesses < 3:
        return None
    if family in ("20D_RANGE_BREAK", "BREAKOUT_CONTINUATION") and witnesses < 2:
        return None

    hi20 = _f(row.get("prior_high_20d"))
    lo20 = _f(row.get("prior_low_20d"))
    breakout_level = _f(row.get("breakout_level"), _f(row.get("retained_breakout_level")))
    avwap = _f(row.get("avwap"))

    if direction == "Bullish":
        trigger = breakout_level if breakout_level is not None else hi20
        base_invalid = (trigger - 0.35 * atr) if trigger is not None else (close - 0.8 * atr)
        if avwap is not None and avwap < close:
            invalidation = max(base_invalid, avwap)
        else:
            invalidation = base_invalid
    else:
        trigger = breakout_level if breakout_level is not None else lo20
        base_invalid = (trigger + 0.35 * atr) if trigger is not None else (close + 0.8 * atr)
        if avwap is not None and avwap > close:
            invalidation = min(base_invalid, avwap)
        else:
            invalidation = base_invalid

    if consumed_atr <= 0.80:
        runway = "GOOD"
        action = "ENTRY / RETEST ZONE"
    elif consumed_atr <= 1.30:
        runway = "CAUTION"
        action = "WAIT FOR RETEST"
    else:
        runway = "EXTENDED"
        action = "DO NOT CHASE — WAIT PULLBACK"

    return {
        "symbol": str(row.get("symbol") or ""),
        "direction": direction,
        "family": family,
        "live_price": round(close, 2),
        "day_change_pct": round(day_move_pct, 2),
        "move_consumed_atr": round(consumed_atr, 2),
        "runway": runway,
        "action": action,
        "trigger": round(trigger, 2) if trigger is not None else None,
        "invalidation": round(invalidation, 2) if invalidation is not None else None,
        "htf_direction": htf,
        "sector": row.get("sector"),
        "stock_sector_lead_pct": round(sector_lead, 2) if sector_lead is not None else None,
        "oi_structure": row.get("oi_structure"),
        "oi_chg_60m_pct": recent_oi,
        "tod_rvol": _f(row.get("tod_rvol")),
        "witness_count": witnesses,
    }


def _priority(row):
    family = {
        "20D_RANGE_BREAK": 4,
        "BREAKOUT_CONTINUATION": 3,
        "4H_TREND_CONTINUATION": 2,
    }.get(str(row.get("family") or ""), 0)
    runway = {"GOOD": 3, "CAUTION": 2, "EXTENDED": 1}.get(str(row.get("runway") or ""), 0)
    return (
        family,
        runway,
        int(row.get("witness_count") or 0),
        abs(_f(row.get("stock_sector_lead_pct"), 0.0)),
    )


def _thesis_invalidated(item, row):
    price = _f(row.get("close"))
    invalid = _f(item.get("invalidation"))
    if price is None or invalid is None:
        return False
    if item.get("direction") == "Bullish":
        return price <= invalid
    return price >= invalid


def update(state, rows, *, now=None):
    now = now or dt.datetime.now()
    state = dict(state or empty_state())
    today = now.date().isoformat()
    if state.get("trade_date") != today:
        state = empty_state()
        state["trade_date"] = today

    row_map = {str(r.get("symbol")): r for r in (rows or []) if r.get("symbol") and not r.get("error")}
    selected = dict(state.get("selected") or {})

    # Existing 1D names are sticky. Update live facts; remove only on thesis
    # invalidation. A new intraday rank cannot kick out a committed swing name.
    for symbol, item in list(selected.items()):
        row = row_map.get(symbol)
        if not row:
            continue
        if _thesis_invalidated(item, row):
            item = dict(item)
            item["status"] = "INVALIDATED"
            item["invalidated_at"] = _iso(now)
            selected.pop(symbol, None)
            continue
        item = dict(item)
        item["live_price"] = _f(row.get("close"), item.get("live_price"))
        prev = _f(row.get("prev_close"))
        atr = _f(row.get("atr"))
        if item.get("live_price") is not None and prev and atr and atr > 0:
            item["move_consumed_atr"] = round(abs(item["live_price"] - prev) / atr, 2)
        item["last_update"] = _iso(now)
        selected[symbol] = item

    candidates = [x for x in (_candidate(r) for r in (rows or [])) if x and x.get("symbol")]
    candidates.sort(key=_priority, reverse=True)
    state["bench"] = candidates[:10]

    slot = _slot(now)
    # Refresh membership only when a new scheduled checkpoint is crossed.
    if slot and slot != state.get("last_refresh_slot"):
        if slot == "FINAL":
            state["phase"] = "FINAL / FROZEN INTO CLOSE"
        elif slot == "MIDDAY":
            state["phase"] = "MIDDAY FOCUS"
        else:
            state["phase"] = "MORNING PROVISIONAL"

        for cand in candidates:
            if len(selected) >= MAX_SWING_FOCUS:
                break
            symbol = cand["symbol"]
            if symbol in selected:
                continue
            item = dict(cand)
            item["selected_at"] = _iso(now)
            item["selected_slot"] = slot
            item["status"] = "1D FOCUS"
            item["last_update"] = _iso(now)
            selected[symbol] = item

        state["last_refresh_slot"] = slot
        state["last_refresh_at"] = _iso(now)

    state["selected"] = selected
    state["trade_date"] = today
    return state


def dashboard(state):
    state = dict(state or empty_state())
    rows = list((state.get("selected") or {}).values())
    rows.sort(key=lambda x: (x.get("selected_slot") or "", _priority(x)), reverse=True)
    return {
        "label": "V12.3 · 1D SWING FOCUS",
        "validation_label": "UNDERLYING-FIRST · DECISION SUPPORT / NOT VALIDATED",
        "phase": state.get("phase") or "WAITING",
        "last_refresh_slot": state.get("last_refresh_slot"),
        "last_refresh_at": state.get("last_refresh_at"),
        "count": len(rows),
        "rows": rows[:MAX_SWING_FOCUS],
        "bench": list(state.get("bench") or [])[:10],
        "selection_windows": ["09:45", "12:30", "14:30 FINAL"],
    }
