"""V12.3 persistent Underlying-First Focus Desk.

The key product change is persistence. A stock does not disappear because it
falls out of a ranking or because one option contract becomes unattractive.
Underlying thesis and execution vehicle are separate state machines.

This module is pure and has no Kite/Flask/filesystem side effects. Background
threads may call update_focus from either the full-universe observer or the
deep V12.2B tactical stream.
"""
from __future__ import annotations

import copy
import datetime as dt
import math

STATE_VERSION = 1
MAX_FOCUS = 6
MIN_FOCUS_MINUTES = 45
RECENT_KEEP_MINUTES = 90
RECENT_REENTRY_COOLDOWN_MINUTES = 10
MAX_HISTORY = 24

EVENT_PRIORITY = {
    "PULLBACK_RECLAIM": 7,
    "OPENING_DRIVE": 6,
    "RANGE_EXPANSION": 5,
    "RELATIVE_SEPARATION": 4,
    "MOMENTUM_CONTINUATION": 3,
    "PRESSURE_SHIFT": 2,
}

LIFECYCLE_PRIORITY = {
    "REENTRY_READY": 9,
    "READY": 8,
    "ACTIVE": 7,
    "MANAGE": 7,
    "PROVEN_MOVER": 6,
    "PULLBACK": 5,
    "BUILDING": 4,
    "DISCOVERED": 3,
    "WEAKENING": 2,
    "INVALIDATED": 1,
    "COMPLETED": 0,
}


def _f(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _dt(value):
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat(timespec="seconds") if isinstance(value, dt.datetime) else None


def empty_state():
    return {
        "version": STATE_VERSION,
        "focus": {},
        "recent": [],
        "missed": {},
        "last_update": None,
        "trade_date": None,
    }


def _normalise(state):
    if not isinstance(state, dict):
        return empty_state()
    out = copy.deepcopy(state)
    out["version"] = STATE_VERSION
    out.setdefault("focus", {})
    out.setdefault("recent", [])
    out.setdefault("missed", {})
    out.setdefault("last_update", None)
    out.setdefault("trade_date", None)
    return out


def _history(item, state, now, note=None):
    old = str(item.get("lifecycle") or "")
    if old == state and not note:
        return
    item["lifecycle"] = state
    item["last_state_change_at"] = _iso(now)
    hist = list(item.get("history") or [])
    hist.append({
        "ts": _iso(now),
        "state": state,
        "note": note,
        "price": item.get("live_price"),
    })
    item["history"] = hist[-MAX_HISTORY:]


def _candidate_key(row):
    return (str(row.get("symbol") or ""), str(row.get("direction") or ""))


def _scan_map(scan_rows):
    return {
        str(r.get("symbol")): dict(r)
        for r in (scan_rows or [])
        if r.get("symbol") and not r.get("error")
    }


def _event_candidates(observer, event_radar):
    """Merge price-led universe events with the evidence event lane.

    No composite score is built. Duplicates are resolved by concrete event
    family priority and recency.
    """
    merged = {}
    for row in list((observer or {}).get("events") or []):
        symbol = str(row.get("symbol") or "")
        direction = str(row.get("direction") or "")
        if not symbol or direction not in ("Bullish", "Bearish"):
            continue
        item = dict(row)
        item["source"] = "FULL_UNIVERSE_PRICE"
        merged[(symbol, direction)] = item

    for row in list((event_radar or {}).get("rows") or []):
        symbol = str(row.get("symbol") or "")
        direction = str(row.get("direction") or "")
        if not symbol or direction not in ("Bullish", "Bearish"):
            continue
        state = str(row.get("event_state") or "")
        if state not in ("PRESSURE_SHIFT", "READY", "BREAK_WATCH", "BREAK_ACCEPTED", "FOLLOW_THROUGH"):
            continue
        mapped_family = {
            "PRESSURE_SHIFT": "PRESSURE_SHIFT",
            "READY": "PRESSURE_SHIFT",
            "BREAK_WATCH": "RANGE_EXPANSION",
            "BREAK_ACCEPTED": "RANGE_EXPANSION",
            "FOLLOW_THROUGH": "MOMENTUM_CONTINUATION",
        }.get(state, "PRESSURE_SHIFT")
        item = dict(row)
        item["event_family"] = mapped_family
        item["source"] = "EVENT_EVIDENCE"
        key = (symbol, direction)
        prior = merged.get(key)
        if prior is None or EVENT_PRIORITY.get(mapped_family, 0) > EVENT_PRIORITY.get(prior.get("event_family"), 0):
            merged[key] = item

    rows = list(merged.values())
    rows.sort(
        key=lambda x: (
            EVENT_PRIORITY.get(str(x.get("event_family") or ""), 0),
            abs(_f(x.get("relative_5m_vs_nifty_pct"), _f(x.get("relative_3m_vs_nifty_pct"), 0.0))),
            abs(_f(x.get("ret_5m_pct"), 0.0)),
        ),
        reverse=True,
    )
    return rows


def _tactical_map(tactical):
    return {
        _candidate_key(row): dict(row)
        for row in list((tactical or {}).get("candidates") or [])
        if row.get("symbol") and row.get("direction") in ("Bullish", "Bearish")
    }


def _underlying_invalidated(item, price):
    direction = item.get("direction")
    invalid = _f(item.get("invalidation"))
    price = _f(price)
    if invalid is None or price is None:
        return False
    if direction == "Bullish":
        return price <= invalid
    if direction == "Bearish":
        return price >= invalid
    return False


def _vehicle_state(item, trow):
    """Separate stock thesis from contract suitability."""
    lifecycle = item.get("lifecycle")
    underlying_valid = lifecycle not in ("INVALIDATED", "COMPLETED")
    live_price = _f(item.get("live_price"))

    cash = "ELIGIBLE" if underlying_valid and live_price is not None else "WAIT"

    future_age = _f((trow or {}).get("future_tick_age_s"))
    future = "WAIT"
    if underlying_valid and future_age is not None:
        future = "ELIGIBLE" if future_age <= 8.0 else "STALE"

    route = (trow or {}).get("option_route") or {}
    contract = route.get("contract") or {}
    locked_symbol = (trow or {}).get("locked_option_contract") or item.get("locked_option_contract")
    if route.get("tradeable"):
        option = "ELIGIBLE"
        option_reason = None
    elif trow:
        option = "BLOCKED"
        option_reason = route.get("reason") or (trow or {}).get("reason")
    else:
        option = "WAIT"
        option_reason = "deep option route not evaluated yet"

    if option == "ELIGIBLE":
        preferred = "OPTION"
    elif future == "ELIGIBLE":
        preferred = "FUTURE"
    elif cash == "ELIGIBLE":
        preferred = "CASH"
    else:
        preferred = "WAIT"

    return {
        "cash": cash,
        "future": future,
        "option": option,
        "option_reason": option_reason,
        "option_contract": locked_symbol or contract.get("symbol"),
        "option_dte": contract.get("dte"),
        "option_spread_pct": contract.get("spread_pct"),
        "preferred_available_vehicle": preferred,
    }


def _derive_lifecycle(item, event, trow, now):
    prior = str(item.get("lifecycle") or "DISCOVERED")
    tstate = str((trow or {}).get("state") or "")
    estate = str((event or {}).get("event_state") or "")
    family = str((event or {}).get("event_family") or item.get("event_family") or "")

    price = _f((trow or {}).get("live_price"), _f((event or {}).get("live_price"), _f(item.get("live_price"))))
    item["live_price"] = price

    if (trow or {}).get("trigger") is not None:
        item["trigger"] = (trow or {}).get("trigger")
    if (trow or {}).get("invalidation") is not None:
        item["invalidation"] = (trow or {}).get("invalidation")

    if _underlying_invalidated(item, price):
        return "INVALIDATED", "underlying crossed structural invalidation"

    if tstate == "PROFIT_PROTECT" or estate == "FOLLOW_THROUGH":
        return "MANAGE", "follow-through established; manage the same thesis"

    if tstate in ("TRADEABLE", "TRIGGERED") or estate == "BREAK_ACCEPTED":
        return "ACTIVE", "underlying trigger accepted"

    if tstate == "READY" or estate == "READY":
        if prior in ("ACTIVE", "MANAGE", "PULLBACK", "WEAKENING"):
            return "REENTRY_READY", "same-direction continuation/re-entry setup is ready"
        return "READY", "exact entry structure is ready"

    if family == "PULLBACK_RECLAIM":
        if prior in ("ACTIVE", "MANAGE", "PULLBACK", "WEAKENING"):
            return "REENTRY_READY", "pullback reclaim detected"
        return "BUILDING", "pullback-reclaim event detected"

    if tstate in ("TIME_EXIT", "CANCELLED"):
        if prior in ("ACTIVE", "MANAGE", "READY", "REENTRY_READY"):
            return "WEAKENING", (trow or {}).get("reason") or "fast execution premise weakened"

    if tstate == "EXIT":
        reason = (trow or {}).get("reason") or "entry exit"
        episode_result = str((trow or {}).get("entry_episode_result") or "")
        # A fast entry can close profitably while the larger underlying thesis
        # remains alive.  Only a true underlying structural invalidation kills
        # the thesis; profit-protection exits become PROVEN_MOVER so the same
        # stock stays on the desk for continuation/re-entry.
        if "underlying structural invalidation" in reason.lower():
            return "INVALIDATED", reason
        if episode_result == "PROVEN_MOVE" or "profit-protection" in reason.lower():
            return "PROVEN_MOVER", "entry closed after a proved move; keep same underlying thesis for continuation"
        return "WEAKENING", reason

    if event:
        if prior in ("ACTIVE", "MANAGE") and family in ("MOMENTUM_CONTINUATION", "RELATIVE_SEPARATION"):
            return "ACTIVE", "underlying momentum remains active"
        if prior in ("ACTIVE", "MANAGE") and tstate in ("FORMING", ""):
            return "PULLBACK", "original move active; waiting for next continuation entry"
        if estate == "PRESSURE_SHIFT" or family in (
            "OPENING_DRIVE", "RANGE_EXPANSION", "RELATIVE_SEPARATION", "MOMENTUM_CONTINUATION", "PRESSURE_SHIFT",
        ):
            return "BUILDING", "underlying event is developing"

    # Persistence: absence from the latest snapshot is not invalidation.
    if prior in ("READY", "REENTRY_READY"):
        return prior, "setup retained; waiting for trigger or invalidation"
    if prior in ("ACTIVE", "MANAGE"):
        return "PULLBACK", "no fresh expansion; keep thesis and wait for continuation"
    if prior == "PROVEN_MOVER":
        return "PROVEN_MOVER", "successful first move retained; wait for next valid continuation structure"
    return prior, None


def _new_focus_item(event, scan, now):
    symbol = str(event.get("symbol") or "")
    direction = str(event.get("direction") or "")
    return {
        "symbol": symbol,
        "direction": direction,
        "selected_at": _iso(now),
        "last_seen_at": _iso(now),
        "last_state_change_at": _iso(now),
        "lifecycle": "DISCOVERED",
        "event_family": event.get("event_family"),
        "event_source": event.get("source"),
        "live_price": event.get("live_price") if event.get("live_price") is not None else scan.get("close"),
        "atr": event.get("atr") if event.get("atr") is not None else scan.get("atr"),
        "sector": event.get("sector") or scan.get("sector"),
        "day_change_pct": event.get("day_change_pct"),
        "ret_5m_pct": event.get("ret_5m_pct"),
        "relative_5m_vs_nifty_pct": event.get("relative_5m_vs_nifty_pct"),
        "why": list(event.get("why") or []),
        "trigger": event.get("trigger"),
        "invalidation": event.get("invalidation"),
        "history": [{
            "ts": _iso(now),
            "state": "DISCOVERED",
            "note": str(event.get("event_family") or "underlying event"),
            "price": event.get("live_price") if event.get("live_price") is not None else scan.get("close"),
        }],
        "opposite_first_seen_at": None,
    }


def _minutes_since(value, now):
    ts = _dt(value)
    if ts is None:
        return None
    if ts.tzinfo is not None and now.tzinfo is None:
        ts = ts.replace(tzinfo=None)
    elif ts.tzinfo is None and now.tzinfo is not None:
        ts = ts.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - ts).total_seconds() / 60.0)


def _focus_age_minutes(item, now):
    selected = _dt(item.get("selected_at"))
    if selected is None:
        return 0.0
    if selected.tzinfo is not None and now.tzinfo is None:
        selected = selected.replace(tzinfo=None)
    elif selected.tzinfo is None and now.tzinfo is not None:
        selected = selected.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - selected).total_seconds() / 60.0)


def _recent_cleanup(rows, now):
    """Keep one latest Recent/Completed card per symbol + direction.

    A persistent invalidation event may be published by multiple live callbacks.
    Recent/Completed is a thesis ledger, not an event log, so repeated copies of
    the same thesis must collapse to the newest card.
    """
    cutoff = now - dt.timedelta(minutes=RECENT_KEEP_MINUTES)
    latest = {}
    for row in rows or []:
        ts = _dt(row.get("completed_at") or row.get("last_state_change_at"))
        if ts is None:
            continue
        if ts.tzinfo is not None and cutoff.tzinfo is None:
            ts = ts.replace(tzinfo=None)
        elif ts.tzinfo is None and cutoff.tzinfo is not None:
            ts = ts.replace(tzinfo=cutoff.tzinfo)
        if ts < cutoff:
            continue
        key = (str(row.get("symbol") or ""), str(row.get("direction") or ""))
        if not key[0]:
            continue
        prior = latest.get(key)
        prior_ts = _dt((prior or {}).get("completed_at") or (prior or {}).get("last_state_change_at"))
        if prior is None or prior_ts is None or ts >= prior_ts:
            latest[key] = row
    out = list(latest.values())
    out.sort(key=lambda r: str(r.get("completed_at") or r.get("last_state_change_at") or ""))
    return out[-30:]


def _same_level(a, b, tolerance=1e-9):
    a, b = _f(a), _f(b)
    if a is None or b is None:
        return False
    return abs(a - b) <= max(tolerance, 1e-6 * max(abs(a), abs(b), 1.0))


def _recent_reentry_blocked(recent, symbol, direction, event, trow, now):
    """Suppress immediate resurrection of the exact setup just invalidated.

    A genuinely new pullback/reclaim or materially changed trigger/invalidation
    can return later. The stale same setup cannot re-enter on every WebSocket
    callback and flood Recent/Completed.
    """
    for row in reversed(recent or []):
        if str(row.get("symbol") or "") != symbol or str(row.get("direction") or "") != direction:
            continue
        completed = _dt(row.get("completed_at") or row.get("last_state_change_at"))
        if completed is None:
            return False
        if completed.tzinfo is not None and now.tzinfo is None:
            completed = completed.replace(tzinfo=None)
        elif completed.tzinfo is None and now.tzinfo is not None:
            completed = completed.replace(tzinfo=now.tzinfo)
        age_min = max(0.0, (now - completed).total_seconds() / 60.0)

        current_family = str((event or {}).get("event_family") or "")
        prior_family = str(row.get("event_family") or "")
        current_trigger = (trow or {}).get("trigger")
        current_invalid = (trow or {}).get("invalidation")
        same_structure = (
            current_family == prior_family
            and (
                current_trigger is None or row.get("trigger") is None
                or _same_level(current_trigger, row.get("trigger"))
            )
            and (
                current_invalid is None or row.get("invalidation") is None
                or _same_level(current_invalid, row.get("invalidation"))
            )
        )

        if same_structure:
            return True

        # Different structure may return, but not in the same few callbacks.
        if age_min < RECENT_REENTRY_COOLDOWN_MINUTES:
            return True

        # Pullback/reclaim is an explicit later re-entry family once cooldown
        # has expired; other materially changed structures may also re-enter.
        return False
    return False


def _missed_movers(state, observer, focus_symbols, event_keys, scan_map, now):
    missed = dict(state.get("missed") or {})
    rows = list((observer or {}).get("leaders") or [])[:8] + list((observer or {}).get("laggards") or [])[:8]
    for mover in rows:
        symbol = str(mover.get("symbol") or "")
        day = _f(mover.get("day_change_pct"))
        if not symbol or day is None or abs(day) < 1.0 or symbol in focus_symbols:
            continue

        directions = [d for s, d in event_keys if s == symbol]
        if symbol not in scan_map:
            reason = "SCAN_METADATA_MISSING"
        elif not directions:
            reason = "MOVE_WITHOUT_QUALIFYING_LIVE_EVENT"
        elif len(focus_symbols) >= MAX_FOCUS:
            reason = "FOCUS_CAPACITY_WHILE_OTHER_THESIS_PERSISTED"
        else:
            reason = "EVENT_SEEN_BUT_NOT_PROMOTED"

        row = dict(missed.get(symbol) or {})
        row.update({
            "symbol": symbol,
            "day_change_pct": round(day, 3),
            "ret_5m_pct": mover.get("ret_5m_pct"),
            "reason": reason,
            "last_seen_at": _iso(now),
        })
        row.setdefault("first_seen_at", _iso(now))
        missed[symbol] = row

    cutoff = now - dt.timedelta(hours=4)
    clean = {}
    for symbol, row in missed.items():
        ts = _dt(row.get("last_seen_at"))
        if ts is None:
            continue
        if ts.tzinfo is not None and cutoff.tzinfo is None:
            ts = ts.replace(tzinfo=None)
        elif ts.tzinfo is None and cutoff.tzinfo is not None:
            ts = ts.replace(tzinfo=cutoff.tzinfo)
        if ts >= cutoff:
            clean[symbol] = row
    return clean


def update_focus(state, observer, event_radar, tactical, scan_rows, *, now=None):
    """Update persistent focus state from all live evidence sources."""
    now = now or dt.datetime.now()
    state = _normalise(state)
    today = now.date().isoformat()
    if state.get("trade_date") not in (None, today):
        # Focus is a session workspace. Never carry yesterday's live thesis
        # into a new trading day; research ledgers remain separate.
        state = empty_state()
    state["trade_date"] = today
    scans = _scan_map(scan_rows)
    candidates = _event_candidates(observer, event_radar)
    event_by_key = {_candidate_key(row): row for row in candidates}
    tactical_by_key = _tactical_map(tactical)
    focus = dict(state.get("focus") or {})
    recent = _recent_cleanup(state.get("recent") or [], now)

    for symbol, item in list(focus.items()):
        item = dict(item)
        direction = str(item.get("direction") or "")
        same = event_by_key.get((symbol, direction))
        opposite_direction = "Bearish" if direction == "Bullish" else "Bullish"
        opposite = event_by_key.get((symbol, opposite_direction))
        trow = tactical_by_key.get((symbol, direction))

        if same:
            item["last_seen_at"] = _iso(now)
            item["event_family"] = same.get("event_family") or item.get("event_family")
            item["event_source"] = same.get("source") or item.get("event_source")
            item["day_change_pct"] = same.get("day_change_pct", item.get("day_change_pct"))
            item["ret_5m_pct"] = same.get("ret_5m_pct", item.get("ret_5m_pct"))
            item["relative_5m_vs_nifty_pct"] = same.get("relative_5m_vs_nifty_pct", item.get("relative_5m_vs_nifty_pct"))
            item["why"] = list(same.get("why") or item.get("why") or [])

        if opposite and not same:
            if not item.get("opposite_first_seen_at"):
                item["opposite_first_seen_at"] = _iso(now)
            if item.get("lifecycle") not in ("INVALIDATED", "COMPLETED"):
                if item.get("lifecycle") in ("ACTIVE", "MANAGE", "READY", "REENTRY_READY"):
                    _history(item, "WEAKENING", now, "opposite live event appeared; thesis not flipped automatically")
        else:
            item["opposite_first_seen_at"] = None

        lifecycle, note = _derive_lifecycle(item, same, trow, now)
        if lifecycle != item.get("lifecycle"):
            _history(item, lifecycle, now, note)

        if trow:
            item["live_price"] = trow.get("live_price") if trow.get("live_price") is not None else item.get("live_price")
            item["setup"] = trow.get("setup")
            item["tactical_state"] = trow.get("state")
            item["tactical_reason"] = trow.get("reason")
            item["entry_episode_no"] = trow.get("entry_episode_no")
            item["entry_episode_open"] = trow.get("entry_episode_open")
            item["entry_episode_result"] = trow.get("entry_episode_result")
            item["entry_episode_exit_reason"] = trow.get("entry_episode_exit_reason")
            item["entry_episode_started_at"] = trow.get("entry_episode_started_at")
            item["entry_episode_closed_at"] = trow.get("entry_episode_closed_at")
            if trow.get("locked_option_contract"):
                item["locked_option_contract"] = trow.get("locked_option_contract")
                item["locked_option_strike"] = trow.get("locked_option_strike")
                item["locked_option_delta"] = trow.get("locked_option_delta")
                item["locked_option_expiry"] = trow.get("locked_option_expiry")
                item["contract_selection_reason"] = trow.get("contract_selection_reason")
                item["contract_reroute_reason"] = trow.get("contract_reroute_reason")
            if trow.get("trigger") is not None:
                item["trigger"] = trow.get("trigger")
            if trow.get("invalidation") is not None:
                item["invalidation"] = trow.get("invalidation")
        elif same and same.get("live_price") is not None:
            item["live_price"] = same.get("live_price")

        item["vehicles"] = _vehicle_state(item, trow)
        item["focus_age_min"] = round(_focus_age_minutes(item, now), 1)

        # Persistence is deliberate, but not immortality.  A focus survives
        # short ranking/data gaps; after its minimum observation commitment,
        # an unproductive thesis can leave the desk so a new mover can enter.
        since_seen = _minutes_since(item.get("last_seen_at"), now)
        if (
            item.get("lifecycle") in ("DISCOVERED", "BUILDING")
            and item["focus_age_min"] >= MIN_FOCUS_MINUTES
            and since_seen is not None and since_seen >= 15.0
            and not same and not trow
        ):
            _history(item, "COMPLETED", now, "focus observation expired without an actionable structure")
        elif (
            item.get("lifecycle") in ("PULLBACK", "WEAKENING", "PROVEN_MOVER")
            and item["focus_age_min"] >= 90.0
            and since_seen is not None and since_seen >= 30.0
            and not same
        ):
            _history(item, "COMPLETED", now, "continuation window expired")

        if item.get("lifecycle") in ("INVALIDATED", "COMPLETED"):
            item["completed_at"] = _iso(now)
            recent.append(item)
            focus.pop(symbol, None)
        else:
            focus[symbol] = item

    for event in candidates:
        symbol = str(event.get("symbol") or "")
        direction = str(event.get("direction") or "")
        if not symbol or symbol in focus:
            continue

        trow = tactical_by_key.get((symbol, direction))
        if _recent_reentry_blocked(recent, symbol, direction, event, trow, now):
            continue

        if len(focus) >= MAX_FOCUS:
            # Exceptional-event replacement is intentionally rare.  It can
            # only replace a non-actionable DISCOVERED/BUILDING thesis that
            # has already had at least ten minutes of observation, and only
            # when the incoming event family is materially stronger.
            incoming_p = EVENT_PRIORITY.get(str(event.get("event_family") or ""), 0)
            replace_symbol = None
            replace_key = None
            for fsym, fitem in focus.items():
                if fitem.get("lifecycle") not in ("DISCOVERED", "BUILDING"):
                    continue
                age = _focus_age_minutes(fitem, now)
                if age < 10.0:
                    continue
                current_p = EVENT_PRIORITY.get(str(fitem.get("event_family") or ""), 0)
                if incoming_p < current_p + 2:
                    continue
                key = (current_p, -age)
                if replace_key is None or key < replace_key:
                    replace_key = key
                    replace_symbol = fsym
            if replace_symbol is None:
                continue
            old = dict(focus.pop(replace_symbol))
            _history(old, "COMPLETED", now, "replaced by materially stronger live underlying event")
            old["completed_at"] = _iso(now)
            recent.append(old)

        item = _new_focus_item(event, scans.get(symbol) or {}, now)
        lifecycle, note = _derive_lifecycle(item, event, trow, now)
        if lifecycle != item["lifecycle"]:
            _history(item, lifecycle, now, note)
        item["vehicles"] = _vehicle_state(item, trow)
        item["focus_age_min"] = 0.0

        # If the freshly promoted structure is already beyond invalidation,
        # record it once as a completed thesis and never let it occupy/recycle
        # a Focus slot.
        if item.get("lifecycle") in ("INVALIDATED", "COMPLETED"):
            item["completed_at"] = _iso(now)
            recent.append(item)
            recent = _recent_cleanup(recent, now)
        else:
            focus[symbol] = item

    focus_symbols = set(focus)
    missed = _missed_movers(state, observer, focus_symbols, set(event_by_key), scans, now)

    state["focus"] = focus
    state["recent"] = _recent_cleanup(recent, now)
    state["missed"] = missed
    state["last_update"] = _iso(now)
    return state


def tactical_candidates(state, scan_rows):
    """Return persistent Focus Desk names for the deep V12.2B stream."""
    state = _normalise(state)
    scans = _scan_map(scan_rows)
    rows = []
    for item in state.get("focus", {}).values():
        if item.get("lifecycle") in ("INVALIDATED", "COMPLETED"):
            continue
        symbol = str(item.get("symbol") or "")
        base = dict(scans.get(symbol) or {})
        if not base:
            base = {
                "symbol": symbol,
                "close": item.get("live_price"),
                "atr": item.get("atr"),
            }
        base["symbol"] = symbol
        base["direction"] = item.get("direction")
        base["trade_direction"] = item.get("direction")
        base["focus_lifecycle"] = item.get("lifecycle")
        base["focus_selected_at"] = item.get("selected_at")
        base["focus_event_family"] = item.get("event_family")
        base["locked_option_contract"] = item.get("locked_option_contract")
        base["entry_episode_no"] = item.get("entry_episode_no")
        rows.append(base)
    rows.sort(
        key=lambda r: (
            LIFECYCLE_PRIORITY.get(str(r.get("focus_lifecycle") or ""), 0),
            str(r.get("focus_selected_at") or ""),
        ),
        reverse=True,
    )
    return rows[:MAX_FOCUS]


def dashboard(state):
    state = _normalise(state)
    focus = [dict(x) for x in state.get("focus", {}).values()]
    focus.sort(
        key=lambda x: (
            LIFECYCLE_PRIORITY.get(str(x.get("lifecycle") or ""), 0),
            str(x.get("last_state_change_at") or ""),
        ),
        reverse=True,
    )

    building = [x for x in focus if x.get("lifecycle") in ("DISCOVERED", "BUILDING")]
    active = [x for x in focus if x.get("lifecycle") in ("READY", "REENTRY_READY", "ACTIVE", "MANAGE", "PROVEN_MOVER", "PULLBACK", "WEAKENING")]
    recent = list(reversed(state.get("recent") or []))[:12]
    missed = sorted(
        [dict(x) for x in (state.get("missed") or {}).values()],
        key=lambda x: abs(_f(x.get("day_change_pct"), 0.0)),
        reverse=True,
    )[:12]

    return {
        "label": "V12.3 UNDERLYING-FIRST LIVE FOCUS DESK",
        "validation_label": "DECISION SUPPORT · FORWARD EVIDENCE REQUIRED",
        "focus_count": len(focus),
        "active_now": active,
        "building_next": building,
        "recent": recent,
        "missed_movers": missed,
        "all_focus": focus,
        "last_update": state.get("last_update"),
        "rules": {
            "max_focus": MAX_FOCUS,
            "minimum_observation_minutes": MIN_FOCUS_MINUTES,
            "direction_flip": "requires invalidation; opposite event alone does not flip thesis",
            "vehicle_separation": "underlying thesis is independent of option/future/cash eligibility",
        },
    }
