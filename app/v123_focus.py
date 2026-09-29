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
MAX_CONTINUATION_WATCH = 6
MAX_DEEP_MONITORED = 12
BUILDING_STALE_MINUTES = 6
CONTINUATION_WATCH_MINUTES = 45
STALE_REARM_BLOCK_SECONDS = 90
RECENT_KEEP_MINUTES = 90
MAX_HISTORY = 24

EVENT_PRIORITY = {
    "MULTISCALE_MOVE": 10,
    "REGIME_PERSISTENCE": 9,
    "HTF_REGIME_CONTINUATION": 8,
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
    "CONTINUATION_WATCH": 3,
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
        "continuation_watch": {},
        "missed": {},
        "forensics": {},
        "forensic_coverage": {},
        "last_update": None,
        "trade_date": None,
        "swing_1d": {},
    }


def _normalise(state):
    if not isinstance(state, dict):
        return empty_state()
    out = copy.deepcopy(state)
    out["version"] = STATE_VERSION
    out.setdefault("focus", {})
    out.setdefault("recent", [])
    out.setdefault("continuation_watch", {})
    out.setdefault("missed", {})
    out.setdefault("forensics", {})
    out.setdefault("forensic_coverage", {})
    out.setdefault("last_update", None)
    out.setdefault("trade_date", None)
    out.setdefault("swing_1d", {})
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
    family priority and recency. Whole-universe rows carry the live market/
    sector context so an evidence-lane event cannot accidentally discard it.
    """
    merged = {}
    context_by_symbol = {
        str(row.get("symbol")): dict(row)
        for row in list((observer or {}).get("quant_rows") or [])
        if row.get("symbol")
    }
    for row in list((observer or {}).get("events") or []):
        symbol = str(row.get("symbol") or "")
        direction = str(row.get("direction") or "")
        if not symbol or direction not in ("Bullish", "Bearish"):
            continue
        item = dict(row)
        item["source"] = "FULL_UNIVERSE_PRICE"
        merged[(symbol, direction)] = item

    # Production admission is underlying-price first. Event-radar/OI evidence
    # may be shown elsewhere, but it cannot create a Focus candidate by itself.

    context_keys = (
        "ret_3m_pct", "ret_5m_pct", "ret_10m_pct",
        "relative_5m_vs_nifty_pct", "market_day_change_pct",
        "market_ret_5m_pct", "market_ret_10m_pct",
        "sector", "sector_index", "sector_day_change_pct",
        "sector_ret_5m_pct", "sector_ret_10m_pct",
        "relative_5m_vs_sector_pct", "volume_rate_accel", "near_session_extreme",
        "ret_15m_pct",
        "direction_lock_state", "direction_lock_phase", "direction_lock_since",
        "direction_up_evidence", "direction_down_evidence",
        "direction_evidence", "direction_stock_z", "direction_residual_z",
        "direction_residual_evidence", "direction_factor_status",
        "direction_beta_market", "direction_beta_sector",
        "movement_horizon_seconds", "movement_z", "movement_p_value",
        "movement_coherence", "movement_significant_horizons_seconds",
        "movement_familywise_alpha",
    )
    rows = list(merged.values())
    for item in rows:
        ctx = context_by_symbol.get(str(item.get("symbol") or "")) or {}
        for key in context_keys:
            if item.get(key) is None and ctx.get(key) is not None:
                item[key] = ctx.get(key)

    rows.sort(
        key=lambda x: (
            EVENT_PRIORITY.get(str(x.get("event_family") or ""), 0),
            abs(_f(x.get("movement_z"), 0.0)),
            _f(x.get("direction_evidence"), 0.0),
        ),
        reverse=True,
    )
    return rows



CONTEXT_NOISE_FLOOR_PCT = 0.05


def _directional_ok(direction, value, minimum=0.0):
    value = _f(value)
    if value is None:
        return False
    return value >= minimum if direction == "Bullish" else value <= -minimum


def _context_relation(direction, ret_5m, ret_10m, day_change=None):
    """Classify one context without turning it into a score.

    0.05% is not a tuned alpha threshold; it is the same noise floor already
    used by the V12.3 live-axis direction logic.  Conflicting 5m/10m context is
    MIXED rather than being silently called supportive.
    """
    labels = []
    for value in (ret_5m, ret_10m, day_change):
        value = _f(value)
        if value is None or abs(value) < CONTEXT_NOISE_FLOOR_PCT:
            continue
        labels.append("ALIGNED" if _directional_ok(direction, value) else "OPPOSED")
    if not labels:
        return "UNKNOWN"
    if all(label == "ALIGNED" for label in labels):
        return "ALIGNED"
    if all(label == "OPPOSED" for label in labels):
        return "OPPOSED"
    return "MIXED"


def _spotting_context_decision(event):
    """Causal admission with an adaptive production lane and legacy test lane.

    Production observer events are MULTISCALE_MOVE / REGIME_PERSISTENCE only.
    Legacy named-event handling is retained solely so historical/research
    callbacks can still be replayed; event-radar rows no longer create Focus.
    """
    event = dict(event or {})
    direction = str(event.get("direction") or "")
    if direction not in ("Bullish", "Bearish"):
        return False, "INVALID", "missing directional event"

    family = str(event.get("event_family") or "")

    # Data-derived production spotting: factor-residual multiscale significance.
    if family == "MULTISCALE_MOVE":
        p_value = _f(event.get("movement_p_value"))
        alpha = _f(event.get("movement_familywise_alpha"), 0.01)
        horizon = event.get("movement_horizon_seconds")
        if p_value is not None and alpha is not None and p_value <= alpha:
            return True, "MULTISCALE_RESIDUAL_MOVE", (
                f"family-wise p={p_value:.5g}; natural horizon={horizon}s"
            )
        return False, "MULTISCALE_NOT_SIGNIFICANT", (
            f"movement p={p_value}; alpha={alpha}"
        )

    # Slower quant persistence lane.
    market = _context_relation(
        direction,
        event.get("market_ret_5m_pct"),
        event.get("market_ret_10m_pct"),
        event.get("market_day_change_pct"),
    )
    sector = _context_relation(
        direction,
        event.get("sector_ret_5m_pct"),
        event.get("sector_ret_10m_pct"),
        event.get("sector_day_change_pct"),
    )
    opposed = market in ("OPPOSED", "MIXED") or sector in ("OPPOSED", "MIXED")

    if family == "REGIME_PERSISTENCE":
        if not opposed:
            if market == "UNKNOWN" and sector == "UNKNOWN":
                return True, "CONTEXT_UNAVAILABLE", "market/sector context unavailable; fail-soft"
            return True, "QUANT_REGIME", f"market={market}; sector={sector}"
        residual_evidence = _f(event.get("direction_residual_evidence"), 0.0) or 0.0
        if residual_evidence >= 7.5:
            return True, "INDEPENDENT_QUANT_REGIME", (
                f"market={market}; sector={sector}; residual CUSUM={residual_evidence:.2f}"
            )
        return False, "CONTEXT_REJECTED", (
            f"market={market}; sector={sector}; residual CUSUM {residual_evidence:.2f} < 7.50"
        )

    # Legacy research/replay lane: preserve prior deterministic behavior.
    if not opposed:
        if market == "UNKNOWN" and sector == "UNKNOWN":
            return True, "CONTEXT_UNAVAILABLE", "market/sector context unavailable; fail-soft"
        return True, "CONTEXT_SUPPORTED", f"market={market}; sector={sector}"

    checks = {
        "10m continuation": _directional_ok(direction, event.get("ret_10m_pct"), 0.35),
        "vs NIFTY": _directional_ok(direction, event.get("relative_5m_vs_nifty_pct"), 0.30),
        "vs sector": _directional_ok(direction, event.get("relative_5m_vs_sector_pct"), 0.30),
        "volume acceleration": (
            _f(event.get("volume_rate_accel")) is not None
            and _f(event.get("volume_rate_accel")) >= 1.20
        ),
        "session extreme": bool(event.get("near_session_extreme")),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if not failed:
        return True, "INDEPENDENT_BREAKAWAY", (
            f"market={market}; sector={sector}; residual move sustained"
        )
    return (
        False,
        "CONTEXT_REJECTED",
        f"market={market}; sector={sector}; missing " + ", ".join(failed),
    )


def _swing_regime_candidates(state, observer):
    """Bridge slow 1D direction into the intraday desk without auto-trading it.

    A swing pick is only promoted when the live quant CUSUM lock independently
    agrees with the same direction. This prevents the
    1D lane from becoming an entry signal while avoiding rediscovery from zero.
    """
    selected = ((state or {}).get("swing_1d") or {}).get("selected") or {}
    context = {
        str(row.get("symbol") or ""): dict(row)
        for row in list((observer or {}).get("quant_rows") or [])
        if row.get("symbol")
    }
    out = []
    for symbol, swing in selected.items():
        row = context.get(str(symbol)) or {}
        direction = str((swing or {}).get("direction") or "")
        locked = str(row.get("direction_lock_state") or "")
        locked_direction = "Bullish" if locked == "BULLISH" else ("Bearish" if locked == "BEARISH" else "")
        if direction not in ("Bullish", "Bearish") or locked_direction != direction:
            continue
        item = dict(row)
        item.update({
            "symbol": str(symbol),
            "direction": direction,
            "event_family": "HTF_REGIME_CONTINUATION",
            "source": "SWING_1D+QUANT_LOCK",
            "detected_at": row.get("direction_lock_since"),
            "why": ["1D swing direction", "live persistent direction agrees"],
        })
        out.append(item)
    return out


def _tactical_map(tactical):
    return {
        _candidate_key(row): dict(row)
        for row in list((tactical or {}).get("candidates") or [])
        if row.get("symbol") and row.get("direction") in ("Bullish", "Bearish")
    }


def _underlying_invalidated(item, price):
    """Broader thesis invalidation, deliberately separate from entry SL.

    Tactical 3m invalidation belongs to one entry attempt.  It must not erase a
    still-valid session thesis.  The broader thesis level is fixed when Focus is
    first admitted and may only be changed explicitly by thesis logic.
    """
    direction = item.get("direction")
    invalid = _f(item.get("thesis_invalidation"))
    price = _f(price)
    if invalid is None or price is None:
        return False
    if direction == "Bullish":
        return price <= invalid
    if direction == "Bearish":
        return price >= invalid
    return False


def _entry_invalidated(direction, price, invalidation):
    price, invalidation = _f(price), _f(invalidation)
    if price is None or invalidation is None:
        return False
    if direction == "Bullish":
        return price <= invalidation
    if direction == "Bearish":
        return price >= invalidation
    return False


def _initial_thesis_invalidation(direction, price, atr, entry_invalidation):
    price = _f(price)
    atr = _f(atr)
    entry = _f(entry_invalidation)
    broad = None
    if price is not None and atr is not None and atr > 0:
        broad = price - 0.90 * atr if direction == "Bullish" else price + 0.90 * atr
    if entry is None:
        return broad
    if broad is None:
        return entry
    # Thesis invalidation must be broader than the current micro-entry level.
    return min(entry, broad) if direction == "Bullish" else max(entry, broad)


def _vehicle_state(item, trow):
    """Separate stock thesis from contract suitability."""
    lifecycle = item.get("lifecycle")
    underlying_valid = lifecycle not in ("INVALIDATED", "COMPLETED")
    live_price = _f(item.get("live_price"))

    cash = "ELIGIBLE" if underlying_valid and live_price is not None else "WAIT"

    data_ok = (trow or {}).get("data_ok")
    future_age = _f((trow or {}).get("future_tick_age_s"))
    future = "WAIT"
    if underlying_valid and future_age is not None:
        future = "ELIGIBLE" if future_age <= 8.0 else "STALE"
    if data_ok is False:
        future = "STALE"

    route = (trow or {}).get("option_route") or {}
    contract = route.get("contract") or {}
    locked_symbol = (trow or {}).get("locked_option_contract") or item.get("locked_option_contract")
    route_health = str((trow or {}).get("route_health") or "")
    execution_window_open = bool((trow or {}).get("execution_window_open"))
    if data_ok is False:
        option = "WAIT"
        option_reason = "deep tactical data stale; execution held"
    elif route.get("tradeable"):
        option = "ELIGIBLE"
        option_reason = None
    elif trow and route_health == "DEGRADED" and execution_window_open:
        option = "DEGRADED"
        option_reason = (trow or {}).get("route_health_reason") or route.get("reason") or (trow or {}).get("reason")
    elif trow and route_health == "WAIT":
        option = "WAIT"
        option_reason = "deep option route not evaluated yet"
    elif trow:
        option = "BLOCKED"
        option_reason = (trow or {}).get("route_health_reason") or route.get("reason") or (trow or {}).get("reason")
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
        "route_health": route_health or None,
        "execution_window_open": execution_window_open,
        "execution_window_state": (trow or {}).get("execution_window_state"),
        "five_minute_witness": ((trow or {}).get("five_minute_witness") or {}).get("state"),
        "preferred_available_vehicle": preferred,
    }


def _derive_lifecycle(item, event, trow, now):
    prior = str(item.get("lifecycle") or "DISCOVERED")
    tstate = str((trow or {}).get("state") or "")
    estate = str((event or {}).get("event_state") or "")
    family = str((event or {}).get("event_family") or item.get("event_family") or "")

    data_ok = (trow or {}).get("data_ok")
    if data_ok is False:
        # A stale deep tactical stream must not drive lifecycle churn. Prefer
        # the independent underlying observer price when available.
        price = _f((event or {}).get("live_price"), _f(item.get("live_price")))
        item["data_ok"] = False
    else:
        price = _f((trow or {}).get("live_price"), _f((event or {}).get("live_price"), _f(item.get("live_price"))))
        if trow:
            item["data_ok"] = True
    item["live_price"] = price

    if (trow or {}).get("trigger") is not None:
        item["trigger"] = (trow or {}).get("trigger")
    if (trow or {}).get("invalidation") is not None:
        item["invalidation"] = (trow or {}).get("invalidation")

    if _underlying_invalidated(item, price):
        return "INVALIDATED", "broader underlying thesis invalidation hit"

    if data_ok is False:
        return prior, "deep tactical data stale; lifecycle held"

    entry_invalid = _f((trow or {}).get("invalidation"))
    if tstate in ("READY", "TRIGGERED", "TRADEABLE") and _entry_invalidated(item.get("direction"), price, entry_invalid):
        return "CONTINUATION_WATCH", "entry structure invalidated; broader underlying thesis remains alive"

    if tstate == "PROFIT_PROTECT" or estate == "FOLLOW_THROUGH":
        return "MANAGE", "follow-through established; manage the same thesis"

    if bool((trow or {}).get("underlying_triggered")) and tstate in ("ROUTE_DEGRADED", "OPTION_NOT_TRADEABLE"):
        return "ACTIVE", "underlying trigger accepted; option execution route is not currently healthy"

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
        if prior in ("ACTIVE", "MANAGE", "READY", "REENTRY_READY", "PROVEN_MOVER"):
            return "CONTINUATION_WATCH", (trow or {}).get("reason") or "entry attempt ended; retain underlying thesis"

    if tstate == "EXIT":
        reason = (trow or {}).get("reason") or "entry exit"
        episode_result = str((trow or {}).get("entry_episode_result") or "")
        if episode_result == "PROVEN_MOVE" or "profit-protection" in reason.lower():
            return "CONTINUATION_WATCH", "proved entry closed; keep underlying campaign alive for a fresh re-entry event"
        return "CONTINUATION_WATCH", "entry attempt closed; broader thesis remains on continuation watch"

    if event:
        if prior in ("ACTIVE", "MANAGE") and family in ("MOMENTUM_CONTINUATION", "RELATIVE_SEPARATION"):
            return "ACTIVE", "underlying momentum remains active"
        if prior in ("ACTIVE", "MANAGE") and tstate in ("FORMING", ""):
            return "PULLBACK", "original move active; waiting for next continuation entry"
        if estate == "PRESSURE_SHIFT" or family in (
            "REGIME_PERSISTENCE", "HTF_REGIME_CONTINUATION",
            "OPENING_DRIVE", "RANGE_EXPANSION", "RELATIVE_SEPARATION",
            "MOMENTUM_CONTINUATION", "PRESSURE_SHIFT",
        ):
            if family in ("MULTISCALE_MOVE", "REGIME_PERSISTENCE"):
                return "BUILDING", "quantified underlying movement established"
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
        "spotting_context_state": event.get("spotting_context_state"),
        "spotting_context_reason": event.get("spotting_context_reason"),
        "market_day_change_pct": event.get("market_day_change_pct"),
        "market_ret_5m_pct": event.get("market_ret_5m_pct"),
        "sector_day_change_pct": event.get("sector_day_change_pct"),
        "sector_ret_5m_pct": event.get("sector_ret_5m_pct"),
        "relative_5m_vs_sector_pct": event.get("relative_5m_vs_sector_pct"),
        "direction_lock_state": event.get("direction_lock_state"),
        "direction_lock_phase": event.get("direction_lock_phase"),
        "direction_lock_since": event.get("direction_lock_since"),
        "direction_up_evidence": event.get("direction_up_evidence"),
        "direction_down_evidence": event.get("direction_down_evidence"),
        "direction_stock_z": event.get("direction_stock_z"),
        "direction_residual_z": event.get("direction_residual_z"),
        "direction_evidence": event.get("direction_evidence"),
        "direction_residual_evidence": event.get("direction_residual_evidence"),
        "direction_factor_status": event.get("direction_factor_status"),
        "direction_beta_market": event.get("direction_beta_market"),
        "direction_beta_sector": event.get("direction_beta_sector"),
        "ret_15m_pct": event.get("ret_15m_pct"),
        "movement_horizon_seconds": event.get("movement_horizon_seconds"),
        "movement_z": event.get("movement_z"),
        "movement_p_value": event.get("movement_p_value"),
        "movement_coherence": event.get("movement_coherence"),
        "movement_significant_horizons_seconds": event.get("movement_significant_horizons_seconds"),
        "movement_familywise_alpha": event.get("movement_familywise_alpha"),
        "why": list(event.get("why") or []),
        "trigger": event.get("trigger"),
        "invalidation": event.get("invalidation"),
        "entry_invalidation": event.get("invalidation"),
        "thesis_invalidation": _initial_thesis_invalidation(
            direction,
            event.get("live_price") if event.get("live_price") is not None else scan.get("close"),
            event.get("atr") if event.get("atr") is not None else scan.get("atr"),
            event.get("invalidation"),
        ),
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


def _event_rearm_decision(recent, symbol, direction, event, trow, scan, now):
    """Return (allowed, reason) for a previously completed same-direction thesis.

    The ABB fix remains: the exact stale callback cannot resurrect immediately.
    But a genuine new market event can re-arm the campaign without waiting an
    arbitrary ten/90-minute ban.
    """
    prior = None
    for row in reversed(recent or []):
        if str(row.get("symbol") or "") == symbol and str(row.get("direction") or "") == direction:
            prior = row
            break
    if prior is None:
        return True, "NEW_SYMBOL_DIRECTION"

    completed = _dt(prior.get("completed_at") or prior.get("last_state_change_at"))
    age_s = None
    if completed is not None:
        if completed.tzinfo is not None and now.tzinfo is None:
            completed = completed.replace(tzinfo=None)
        elif completed.tzinfo is None and now.tzinfo is not None:
            completed = completed.replace(tzinfo=now.tzinfo)
        age_s = max(0.0, (now - completed).total_seconds())

    family = str((event or {}).get("event_family") or "")
    prior_family = str(prior.get("event_family") or "")
    price = _f((event or {}).get("live_price"))
    prior_price = _f(prior.get("live_price"))
    atr = _f((scan or {}).get("atr"), _f(prior.get("atr")))
    signed_move_atr = None
    if price is not None and prior_price is not None and atr and atr > 0:
        sign = 1.0 if direction == "Bullish" else -1.0
        signed_move_atr = sign * (price - prior_price) / atr

    current_trigger = _f((trow or {}).get("trigger"))
    prior_trigger = _f(prior.get("trigger"))
    trigger_shift_atr = None
    if current_trigger is not None and prior_trigger is not None and atr and atr > 0:
        trigger_shift_atr = abs(current_trigger - prior_trigger) / atr

    relative = _f((event or {}).get("relative_5m_vs_nifty_pct"))
    ret5 = _f((event or {}).get("ret_5m_pct"))
    sign = 1.0 if direction == "Bullish" else -1.0

    fresh_reasons = []
    if family == "PULLBACK_RECLAIM":
        fresh_reasons.append("fresh pullback-reclaim")
    if family and family != prior_family:
        fresh_reasons.append("event family changed")
    if signed_move_atr is not None and signed_move_atr >= 0.20:
        fresh_reasons.append("price advanced >=0.20 ATR from prior closeout")
    if trigger_shift_atr is not None and trigger_shift_atr >= 0.15:
        fresh_reasons.append("new structural trigger")
    if relative is not None and sign * relative >= 0.25 and ret5 is not None and sign * ret5 >= 0.20:
        fresh_reasons.append("renewed 5m relative acceleration")

    if fresh_reasons:
        return True, "; ".join(fresh_reasons)

    if age_s is not None and age_s < STALE_REARM_BLOCK_SECONDS:
        return False, "STALE_CALLBACK_GUARD"

    return False, "NO_FRESH_REARM_EVENT"



def _continuation_rearm_decision(item, event, trow, scan, now):
    """Fresh-event test for an Alumni/Continuation item.

    Unlike Recent dedupe, a watch item already represents a known thesis, so
    merely seeing the same callback again can never re-arm it.
    """
    if not event:
        return False, "NO_LIVE_EVENT"

    age_s = None
    started = _dt(item.get("watch_started_at"))
    if started is not None:
        if started.tzinfo is not None and now.tzinfo is None:
            started = started.replace(tzinfo=None)
        elif started.tzinfo is None and now.tzinfo is not None:
            started = started.replace(tzinfo=now.tzinfo)
        age_s = max(0.0, (now - started).total_seconds())

    family = str(event.get("event_family") or "")
    prior_family = str(item.get("watch_reference_family") or item.get("event_family") or "")
    direction = str(item.get("direction") or "")
    sign = 1.0 if direction == "Bullish" else -1.0

    price = _f(event.get("live_price"))
    ref_price = _f(item.get("watch_reference_price"), _f(item.get("live_price")))
    atr = _f((scan or {}).get("atr"), _f(item.get("atr")))
    move_atr = None
    if price is not None and ref_price is not None and atr and atr > 0:
        move_atr = sign * (price - ref_price) / atr

    current_trigger = _f((trow or {}).get("trigger"))
    prior_trigger = _f(item.get("trigger"))
    trigger_shift_atr = None
    if current_trigger is not None and prior_trigger is not None and atr and atr > 0:
        trigger_shift_atr = abs(current_trigger - prior_trigger) / atr

    relative = _f(event.get("relative_5m_vs_nifty_pct"))
    ret5 = _f(event.get("ret_5m_pct"))
    family_changed = bool(family and prior_family and family != prior_family)
    pullback_reclaim = family == "PULLBACK_RECLAIM"

    # First protect against the exact ABB-style repeated callback loop.  A
    # genuinely different event family may still re-arm quickly.
    if age_s is not None and age_s < STALE_REARM_BLOCK_SECONDS and not family_changed and not pullback_reclaim:
        return False, "STALE_CALLBACK_GUARD"

    reasons = []
    if pullback_reclaim:
        reasons.append("fresh pullback-reclaim")
    if family_changed:
        reasons.append("event family changed")
    if move_atr is not None and move_atr >= 0.20:
        reasons.append("price renewed >=0.20 ATR from watch reference")
    if trigger_shift_atr is not None and trigger_shift_atr >= 0.15:
        reasons.append("new structural trigger")
    if relative is not None and sign * relative >= 0.25 and ret5 is not None and sign * ret5 >= 0.20:
        reasons.append("renewed 5m relative acceleration")

    if reasons:
        return True, "; ".join(reasons)
    return False, "CONTINUATION_NO_FRESH_REARM_EVENT"


def _missed_movers(state, observer, focus_symbols, continuation_symbols, event_by_key, scan_map, tactical_by_key, promotion_trace, now):
    """Forensic audit of actual movers, not only selected candidates.

    When the private whole-universe observer rows are present, audit every F&O
    underlying with a valid day move.  The public leader/laggard slices remain
    only as a backwards-compatible fallback for non-market-stream callbacks.
    """
    missed = dict(state.get("missed") or {})
    forensics = dict(state.get("forensics") or {})

    private_rows = list((observer or {}).get("quant_rows") or [])
    whole_universe = bool(private_rows)
    source_rows = private_rows if whole_universe else (
        list((observer or {}).get("leaders") or []) + list((observer or {}).get("laggards") or [])
    )

    rows = []
    seen = set()
    for mover in source_rows:
        symbol = str((mover or {}).get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        rows.append(mover)

    meaningful = 0
    stage_counts = {}
    current_misses = 0

    for mover in rows:
        symbol = str(mover.get("symbol") or "")
        day = _f(mover.get("day_change_pct"))
        movement_direction = str(mover.get("movement_direction") or "")
        movement_p = _f(mover.get("movement_p_value"))
        movement_alpha = _f(mover.get("movement_familywise_alpha"))
        has_quant_fields = (
            mover.get("movement_significant") is not None
            or movement_p is not None
            or mover.get("movement_horizon_seconds") is not None
        )
        quant_meaningful = bool(
            movement_direction in ("Bullish", "Bearish")
            and movement_p is not None
            and movement_alpha is not None
            and movement_p <= movement_alpha
        )
        # Production audit: a "pipeline miss" means a statistically significant
        # underlying move was outside Focus/Continuation. Legacy callers without
        # multiscale fields retain the historical ±0.75% day-move fallback.
        if has_quant_fields:
            if not quant_meaningful:
                continue
            direction = movement_direction
        else:
            if day is None or abs(day) < 0.75:
                continue
            direction = "Bullish" if day > 0 else "Bearish"
        meaningful += 1
        event = event_by_key.get((symbol, direction))
        trow = tactical_by_key.get((symbol, direction))
        trace = list(promotion_trace.get(symbol) or [])

        if symbol in focus_symbols:
            stage, reason = "FOCUS", "ACTIVE_FOCUS"
        elif symbol in continuation_symbols:
            stage, reason = "CONTINUATION", "CONTINUATION_WATCH"
        elif symbol not in scan_map:
            stage, reason = "METADATA", "SCAN_METADATA_MISSING"
        elif event is None:
            stage = "DISCOVERY"
            failed = list(mover.get("discovery_failed_gates") or [])
            reason = str(mover.get("discovery_reason") or "MOVE_WITHOUT_QUALIFYING_LIVE_EVENT")
            if failed:
                reason += " · " + "; ".join(str(x) for x in failed[:4])
        elif trow and str(trow.get("state") or "") == "OPTION_NOT_TRADEABLE":
            stage, reason = "OPTION_ROUTE", str(trow.get("reason") or "OPTION_NOT_TRADEABLE")
        elif trace:
            reason = trace[-1]
            stage = "SPOTTING_CONTEXT" if str(reason).startswith("SPOTTING_CONTEXT_BLOCK:") else "PROMOTION"
        elif len(focus_symbols) >= MAX_FOCUS:
            stage, reason = "FOCUS_CAPACITY", "FOCUS_CAPACITY_WHILE_COMMITTED_THESIS_PERSISTED"
        else:
            stage, reason = "PROMOTION", "EVENT_SEEN_BUT_NOT_PROMOTED"

        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        if stage not in ("FOCUS", "CONTINUATION"):
            current_misses += 1

        entry = dict(forensics.get(symbol) or {})
        history = list(entry.get("history") or [])
        stamp = (stage, reason)
        if not history or (history[-1].get("stage"), history[-1].get("reason")) != stamp:
            history.append({"ts": _iso(now), "stage": stage, "reason": reason})
        entry.update({
            "symbol": symbol,
            "direction": direction,
            "day_change_pct": round(day, 3) if day is not None else None,
            "movement_direction": mover.get("movement_direction"),
            "movement_horizon_seconds": mover.get("movement_horizon_seconds"),
            "movement_z": mover.get("movement_z"),
            "movement_p_value": mover.get("movement_p_value"),
            "movement_coherence": mover.get("movement_coherence"),
            "ret_3m_pct": mover.get("ret_3m_pct"),
            "ret_5m_pct": mover.get("ret_5m_pct"),
            "ret_10m_pct": mover.get("ret_10m_pct"),
            "relative_5m_vs_nifty_pct": mover.get("relative_5m_vs_nifty_pct"),
            "volume_rate_accel": mover.get("volume_rate_accel"),
            "near_session_extreme": mover.get("near_session_extreme"),
            "discovery_qualified": mover.get("discovery_qualified"),
            "discovery_failed_gates": mover.get("discovery_failed_gates"),
            "stage": stage,
            "reason": reason,
            "event_family": (event or {}).get("event_family"),
            "tactical_state": (trow or {}).get("state"),
            "option_reason": ((trow or {}).get("option_route") or {}).get("reason") or (trow or {}).get("reason"),
            "focus_count": len(focus_symbols),
            "continuation_count": len(continuation_symbols),
            "last_seen_at": _iso(now),
            "history": history[-12:],
        })
        entry.setdefault("first_seen_at", _iso(now))
        forensics[symbol] = entry
        if symbol not in focus_symbols and symbol not in continuation_symbols:
            missed[symbol] = dict(entry)

    coverage = {
        "mode": "WHOLE_UNIVERSE" if whole_universe else "TOP_MOVER_FALLBACK",
        "universe_count": int(_f((observer or {}).get("universe_count"), 0.0) or 0),
        "observed_rows": len(rows),
        "meaningful_movers": meaningful,
        "meaningful_definition": (
            "FAMILYWISE_SIGNIFICANT_MULTISCALE_MOVE"
            if whole_universe else "LEGACY_ABS_DAY_MOVE_GE_0_75"
        ),
        "focus_movers": stage_counts.get("FOCUS", 0),
        "continuation_movers": stage_counts.get("CONTINUATION", 0),
        "captured_movers": stage_counts.get("FOCUS", 0) + stage_counts.get("CONTINUATION", 0),
        "pipeline_misses": current_misses,
        "stage_counts": stage_counts,
        "asof": _iso(now),
    }

    cutoff = now - dt.timedelta(hours=4)
    def fresh_map(src):
        out = {}
        for symbol, row in src.items():
            ts = _dt(row.get("last_seen_at"))
            if ts is None:
                continue
            if ts.tzinfo is not None and cutoff.tzinfo is None:
                ts = ts.replace(tzinfo=None)
            elif ts.tzinfo is None and cutoff.tzinfo is not None:
                ts = ts.replace(tzinfo=cutoff.tzinfo)
            if ts >= cutoff:
                out[symbol] = row
        return out
    return fresh_map(missed), fresh_map(forensics), coverage



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

    # Freeze the intraday lifecycle exactly at 15:30. Market-closed WebSocket
    # callbacks and later scan jobs may still publish, but they must not mutate
    # the day's Focus/Continuation state after the close.
    second = now.hour * 3600 + now.minute * 60 + now.second
    if now.weekday() < 5 and second >= 15 * 3600 + 30 * 60:
        state["session_frozen_at"] = state.get("session_frozen_at") or _iso(now)
        return state
    state.pop("session_frozen_at", None)

    scans = _scan_map(scan_rows)
    candidates = _event_candidates(observer, event_radar)
    candidates.sort(
        key=lambda x: (
            EVENT_PRIORITY.get(str(x.get("event_family") or ""), 0),
            abs(_f(x.get("movement_z"), 0.0)),
            _f(x.get("direction_evidence"), 0.0),
        ),
        reverse=True,
    )
    event_by_key = {_candidate_key(row): row for row in candidates}
    tactical_by_key = _tactical_map(tactical)
    focus = dict(state.get("focus") or {})
    continuation = dict(state.get("continuation_watch") or {})
    recent = _recent_cleanup(state.get("recent") or [], now)
    promotion_trace = {}

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
            item["direction_lock_state"] = same.get("direction_lock_state", item.get("direction_lock_state"))
            item["direction_lock_phase"] = same.get("direction_lock_phase", item.get("direction_lock_phase"))
            item["direction_lock_since"] = same.get("direction_lock_since", item.get("direction_lock_since"))
            item["direction_up_evidence"] = same.get("direction_up_evidence", item.get("direction_up_evidence"))
            item["direction_down_evidence"] = same.get("direction_down_evidence", item.get("direction_down_evidence"))
            item["direction_stock_z"] = same.get("direction_stock_z", item.get("direction_stock_z"))
            item["direction_residual_z"] = same.get("direction_residual_z", item.get("direction_residual_z"))
            item["direction_evidence"] = same.get("direction_evidence", item.get("direction_evidence"))
            item["direction_residual_evidence"] = same.get("direction_residual_evidence", item.get("direction_residual_evidence"))
            item["direction_factor_status"] = same.get("direction_factor_status", item.get("direction_factor_status"))
            item["movement_horizon_seconds"] = same.get("movement_horizon_seconds", item.get("movement_horizon_seconds"))
            item["movement_z"] = same.get("movement_z", item.get("movement_z"))
            item["movement_p_value"] = same.get("movement_p_value", item.get("movement_p_value"))
            item["movement_coherence"] = same.get("movement_coherence", item.get("movement_coherence"))
            item["movement_significant_horizons_seconds"] = same.get("movement_significant_horizons_seconds", item.get("movement_significant_horizons_seconds"))
            item["movement_familywise_alpha"] = same.get("movement_familywise_alpha", item.get("movement_familywise_alpha"))
            item["why"] = list(same.get("why") or item.get("why") or [])

        if opposite and not same:
            if not item.get("opposite_first_seen_at"):
                item["opposite_first_seen_at"] = _iso(now)
            if item.get("lifecycle") not in ("INVALIDATED", "COMPLETED"):
                if item.get("lifecycle") in ("ACTIVE", "MANAGE", "READY", "REENTRY_READY"):
                    _history(item, "WEAKENING", now, "opposite live event appeared; thesis not flipped automatically")
        else:
            item["opposite_first_seen_at"] = None

        prior_lifecycle = str(item.get("lifecycle") or "")
        lifecycle, note = _derive_lifecycle(item, same, trow, now)
        if lifecycle != prior_lifecycle:
            _history(item, lifecycle, now, note)
            if lifecycle == "CONTINUATION_WATCH" and prior_lifecycle != "CONTINUATION_WATCH":
                # A genuine new watch episode must never inherit a clock/reference
                # from an earlier watch that was re-armed back into Focus.
                item.pop("watch_started_at", None)
                item.pop("watch_until", None)
                item.pop("watch_reference_price", None)
                item.pop("watch_reference_family", None)

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
            item["execution_window_open"] = bool(trow.get("execution_window_open"))
            item["execution_window_state"] = trow.get("execution_window_state")
            item["route_health"] = trow.get("route_health")
            item["route_health_reason"] = trow.get("route_health_reason")
            item["route_degraded_seconds"] = trow.get("route_degraded_seconds")
            item["last_executable_at"] = trow.get("last_executable_at")
            item["five_minute_witness"] = ((trow.get("five_minute_witness") or {}).get("state"))
            item["fresh_entry_gate"] = trow.get("fresh_entry_gate")
            item["fresh_entry_reason"] = trow.get("fresh_entry_reason")
            item["continuation_math"] = trow.get("continuation_math")
            item["risk_plan"] = trow.get("risk_plan")
            item["data_ok"] = trow.get("data_ok", True)
            item["data_status"] = trow.get("data_status")
            item["execution_paused"] = bool(trow.get("execution_paused"))
            item["entry_zone"] = trow.get("entry_zone")
            item["max_option_entry_price"] = trow.get("max_option_entry_price")
            item["signal_age_seconds"] = trow.get("signal_age_seconds")
            item["session_entry_allowed"] = trow.get("session_entry_allowed")
            if trow.get("ret_5m_pct") is not None:
                item["ret_5m_pct"] = trow.get("ret_5m_pct")
            if trow.get("relative_5m_vs_nifty_pct") is not None:
                item["relative_5m_vs_nifty_pct"] = trow.get("relative_5m_vs_nifty_pct")
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
                item["entry_invalidation"] = trow.get("invalidation")
                if item.get("thesis_invalidation") is None:
                    item["thesis_invalidation"] = _initial_thesis_invalidation(
                        direction, item.get("live_price"), item.get("atr"), trow.get("invalidation")
                    )
        elif same and same.get("live_price") is not None:
            item["live_price"] = same.get("live_price")

        item["vehicles"] = _vehicle_state(item, trow)
        item["focus_age_min"] = round(_focus_age_minutes(item, now), 1)

        # Building persistence now comes from continuing evidence, not a fixed
        # 45-minute reservation.  If neither the locked-direction observer nor
        # the tactical stream has refreshed the thesis for six minutes, free
        # the Focus slot.  Strong names can remain indefinitely while evidence
        # continues; weak one-off spots rotate out quickly.
        since_seen = _minutes_since(item.get("last_seen_at"), now)
        if (
            item.get("lifecycle") in ("DISCOVERED", "BUILDING")
            and since_seen is not None and since_seen >= BUILDING_STALE_MINUTES
            and not same and not opposite and not trow
        ):
            _history(item, "COMPLETED", now, "building evidence expired; direction/event no longer persistent")
        elif (
            item.get("lifecycle") in ("PULLBACK", "WEAKENING", "PROVEN_MOVER")
            and item["focus_age_min"] >= 90.0
            and since_seen is not None and since_seen >= 30.0
            and not same
        ):
            _history(item, "COMPLETED", now, "continuation window expired")

        if item.get("lifecycle") == "CONTINUATION_WATCH":
            item["watch_started_at"] = item.get("watch_started_at") or _iso(now)
            item["watch_reference_price"] = item.get("watch_reference_price") or item.get("live_price")
            item["watch_reference_family"] = item.get("watch_reference_family") or item.get("event_family")
            item["watch_until"] = _iso(now + dt.timedelta(minutes=CONTINUATION_WATCH_MINUTES))
            item["continuation_reason"] = item.get("tactical_reason") or note or "entry episode ended"
            continuation[symbol] = item
            focus.pop(symbol, None)
        elif item.get("lifecycle") in ("INVALIDATED", "COMPLETED"):
            item["completed_at"] = _iso(now)
            recent.append(item)
            focus.pop(symbol, None)
            continuation.pop(symbol, None)
        else:
            focus[symbol] = item

    # Keep recently important names under deep monitoring after an entry episode
    # ends.  A fresh market event can re-arm them without rediscovering from
    # zero; stale repeated callbacks cannot.
    for symbol, item in list(continuation.items()):
        item = dict(item)
        direction = str(item.get("direction") or "")
        same = event_by_key.get((symbol, direction))
        trow = tactical_by_key.get((symbol, direction))
        scan = scans.get(symbol) or {}
        price = _f((trow or {}).get("live_price"), _f((same or {}).get("live_price"), _f(scan.get("close"), _f(item.get("live_price")))))
        item["live_price"] = price
        if same:
            item["last_seen_at"] = _iso(now)
            item["day_change_pct"] = same.get("day_change_pct", item.get("day_change_pct"))
            item["ret_5m_pct"] = same.get("ret_5m_pct", item.get("ret_5m_pct"))
            item["relative_5m_vs_nifty_pct"] = same.get("relative_5m_vs_nifty_pct", item.get("relative_5m_vs_nifty_pct"))
        if trow:
            item["tactical_state"] = trow.get("state")
            item["tactical_reason"] = trow.get("reason")
            item["fresh_entry_gate"] = trow.get("fresh_entry_gate")
            item["fresh_entry_reason"] = trow.get("fresh_entry_reason")
            item["continuation_math"] = trow.get("continuation_math")
            item["risk_plan"] = trow.get("risk_plan")
            item["data_ok"] = trow.get("data_ok", True)
            item["data_status"] = trow.get("data_status")
            item["execution_paused"] = bool(trow.get("execution_paused"))
            item["entry_zone"] = trow.get("entry_zone")
            item["max_option_entry_price"] = trow.get("max_option_entry_price")
            item["signal_age_seconds"] = trow.get("signal_age_seconds")
            item["session_entry_allowed"] = trow.get("session_entry_allowed")
            item["vehicles"] = _vehicle_state(item, trow)
            if trow.get("locked_option_contract"):
                item["locked_option_contract"] = trow.get("locked_option_contract")
                item["locked_option_delta"] = trow.get("locked_option_delta")

        if _underlying_invalidated(item, price):
            _history(item, "INVALIDATED", now, "broader thesis invalidation hit during continuation watch")
            item["completed_at"] = _iso(now)
            recent.append(item)
            continuation.pop(symbol, None)
            continue

        age = _minutes_since(item.get("watch_started_at"), now)
        if age is not None and age >= CONTINUATION_WATCH_MINUTES:
            _history(item, "COMPLETED", now, "continuation watch expired without a fresh re-entry event")
            item["completed_at"] = _iso(now)
            recent.append(item)
            continuation.pop(symbol, None)
            continue

        if same:
            allowed, rearm_reason = _continuation_rearm_decision(
                item, same, trow, scan, now
            )
            if allowed and len(focus) < MAX_FOCUS:
                item["event_family"] = same.get("event_family") or item.get("event_family")
                item["event_source"] = same.get("source") or item.get("event_source")
                item["last_seen_at"] = _iso(now)
                item["rearmed_at"] = _iso(now)
                item["rearm_reason"] = rearm_reason
                item["entry_episode_open"] = False
                # Re-arm ends the prior continuation-watch episode. Clear its
                # clock/reference so a later watch starts a fresh 45-minute timer.
                item.pop("watch_started_at", None)
                item.pop("watch_until", None)
                item.pop("watch_reference_price", None)
                item.pop("watch_reference_family", None)
                _history(item, "BUILDING", now, "re-armed from continuation watch: " + str(rearm_reason))
                focus[symbol] = item
                continuation.pop(symbol, None)
                promotion_trace.setdefault(symbol, []).append("REARMED_FROM_CONTINUATION")
            elif same and not allowed:
                promotion_trace.setdefault(symbol, []).append("CONTINUATION_NO_FRESH_REARM_EVENT")
        if symbol in continuation:
            continuation[symbol] = item

    # Bound the Alumni/Continuation pool without touching active Focus.
    if len(continuation) > MAX_CONTINUATION_WATCH:
        ranked = sorted(
            continuation.items(),
            key=lambda kv: (
                1 if str((kv[1] or {}).get("entry_episode_result") or "") == "PROVEN_MOVE" else 0,
                str((kv[1] or {}).get("watch_started_at") or ""),
            ),
            reverse=True,
        )
        keep = {symbol for symbol, _ in ranked[:MAX_CONTINUATION_WATCH]}
        for symbol, item in list(continuation.items()):
            if symbol in keep:
                continue
            item = dict(item)
            _history(item, "COMPLETED", now, "continuation-watch capacity rotated to stronger/recent alumni")
            item["completed_at"] = _iso(now)
            recent.append(item)
            continuation.pop(symbol, None)

    for event in candidates:
        symbol = str(event.get("symbol") or "")
        direction = str(event.get("direction") or "")
        if not symbol or symbol in focus or symbol in continuation:
            continue

        context_allowed, context_state, context_reason = _spotting_context_decision(event)
        event["spotting_context_state"] = context_state
        event["spotting_context_reason"] = context_reason
        if not context_allowed:
            promotion_trace.setdefault(symbol, []).append(
                "SPOTTING_CONTEXT_BLOCK: " + str(context_reason)
            )
            continue

        trow = tactical_by_key.get((symbol, direction))
        allowed, rearm_reason = _event_rearm_decision(
            recent, symbol, direction, event, trow, scans.get(symbol) or {}, now
        )
        if not allowed:
            promotion_trace.setdefault(symbol, []).append(str(rearm_reason))
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
                promotion_trace.setdefault(symbol, []).append("FOCUS_CAPACITY_WHILE_COMMITTED_THESIS_PERSISTED")
                continue
            old = dict(focus.pop(replace_symbol))
            _history(old, "COMPLETED", now, "replaced by materially stronger live underlying event")
            old["completed_at"] = _iso(now)
            recent.append(old)

        item = _new_focus_item(event, scans.get(symbol) or {}, now)
        if rearm_reason and rearm_reason != "NEW_SYMBOL_DIRECTION":
            item["rearmed_at"] = _iso(now)
            item["rearm_reason"] = rearm_reason
            promotion_trace.setdefault(symbol, []).append("REARMED: " + str(rearm_reason))
        lifecycle, note = _derive_lifecycle(item, event, trow, now)
        if lifecycle != item["lifecycle"]:
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
            item["execution_window_open"] = bool(trow.get("execution_window_open"))
            item["execution_window_state"] = trow.get("execution_window_state")
            item["route_health"] = trow.get("route_health")
            item["route_health_reason"] = trow.get("route_health_reason")
            item["route_degraded_seconds"] = trow.get("route_degraded_seconds")
            item["last_executable_at"] = trow.get("last_executable_at")
            item["five_minute_witness"] = ((trow.get("five_minute_witness") or {}).get("state"))
            item["fresh_entry_gate"] = trow.get("fresh_entry_gate")
            item["fresh_entry_reason"] = trow.get("fresh_entry_reason")
            item["continuation_math"] = trow.get("continuation_math")
            item["risk_plan"] = trow.get("risk_plan")
            item["data_ok"] = trow.get("data_ok", True)
            item["data_status"] = trow.get("data_status")
            item["execution_paused"] = bool(trow.get("execution_paused"))
            item["entry_zone"] = trow.get("entry_zone")
            item["max_option_entry_price"] = trow.get("max_option_entry_price")
            item["signal_age_seconds"] = trow.get("signal_age_seconds")
            item["session_entry_allowed"] = trow.get("session_entry_allowed")
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
                item["entry_invalidation"] = trow.get("invalidation")
        item["vehicles"] = _vehicle_state(item, trow)
        item["focus_age_min"] = 0.0

        # If the first tactical structure already failed but the broader
        # thesis is intact, start as Alumni/Continuation rather than either
        # flashing READY or treating the whole stock as dead.
        if item.get("lifecycle") == "CONTINUATION_WATCH":
            item["watch_started_at"] = _iso(now)
            item["watch_reference_price"] = item.get("live_price")
            item["watch_reference_family"] = item.get("event_family")
            item["watch_until"] = _iso(now + dt.timedelta(minutes=CONTINUATION_WATCH_MINUTES))
            continuation[symbol] = item
        elif item.get("lifecycle") in ("INVALIDATED", "COMPLETED"):
            item["completed_at"] = _iso(now)
            recent.append(item)
            recent = _recent_cleanup(recent, now)
        else:
            focus[symbol] = item

    focus_symbols = set(focus)
    continuation_symbols = set(continuation)
    missed, forensics, forensic_coverage = _missed_movers(
        state, observer, focus_symbols, continuation_symbols,
        event_by_key, scans, tactical_by_key, promotion_trace, now
    )

    state["focus"] = focus
    state["continuation_watch"] = continuation
    state["recent"] = _recent_cleanup(recent, now)
    state["missed"] = missed
    state["forensics"] = forensics
    # Only the market-stream callback has the private whole-universe rows.
    # Tactical callbacks keep the latest complete coverage rather than
    # replacing it with the public top-mover fallback.
    if forensic_coverage.get("mode") == "WHOLE_UNIVERSE" or not state.get("forensic_coverage"):
        state["forensic_coverage"] = forensic_coverage
    state["last_update"] = _iso(now)
    return state


def tactical_candidates(state, scan_rows):
    """Return persistent Focus Desk names for the deep V12.2B stream."""
    state = _normalise(state)
    scans = _scan_map(scan_rows)
    rows = []
    combined = list((state.get("focus") or {}).values()) + list((state.get("continuation_watch") or {}).values())
    seen_symbols = set()
    for item in combined:
        if str(item.get("symbol") or "") in seen_symbols:
            continue
        seen_symbols.add(str(item.get("symbol") or ""))
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
        base["focus_rearmed_at"] = item.get("rearmed_at")
        base["focus_rearm_reason"] = item.get("rearm_reason")
        base["watch_reference_price"] = item.get("watch_reference_price")
        base["watch_reference_family"] = item.get("watch_reference_family")
        base["ret_5m_pct"] = item.get("ret_5m_pct")
        base["relative_5m_vs_nifty_pct"] = item.get("relative_5m_vs_nifty_pct")
        base["direction_lock_state"] = item.get("direction_lock_state")
        base["direction_lock_phase"] = item.get("direction_lock_phase")
        base["direction_lock_since"] = item.get("direction_lock_since")
        base["direction_up_evidence"] = item.get("direction_up_evidence")
        base["direction_down_evidence"] = item.get("direction_down_evidence")
        base["direction_stock_z"] = item.get("direction_stock_z")
        base["direction_residual_z"] = item.get("direction_residual_z")
        base["direction_evidence"] = item.get("direction_evidence")
        base["direction_residual_evidence"] = item.get("direction_residual_evidence")
        base["direction_factor_status"] = item.get("direction_factor_status")
        base["ret_15m_pct"] = item.get("ret_15m_pct")
        base["movement_horizon_seconds"] = item.get("movement_horizon_seconds")
        base["movement_z"] = item.get("movement_z")
        base["movement_p_value"] = item.get("movement_p_value")
        base["movement_coherence"] = item.get("movement_coherence")
        base["movement_significant_horizons_seconds"] = item.get("movement_significant_horizons_seconds")
        base["movement_familywise_alpha"] = item.get("movement_familywise_alpha")
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
    return rows[:MAX_DEEP_MONITORED]


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
    continuation = sorted(
        [dict(x) for x in (state.get("continuation_watch") or {}).values()],
        key=lambda x: str(x.get("watch_started_at") or ""),
        reverse=True,
    )[:MAX_CONTINUATION_WATCH]
    missed = sorted(
        [dict(x) for x in (state.get("missed") or {}).values()],
        key=lambda x: abs(_f(x.get("day_change_pct"), 0.0)),
        reverse=True,
    )[:20]
    forensics = sorted(
        [dict(x) for x in (state.get("forensics") or {}).values()],
        key=lambda x: abs(_f(x.get("day_change_pct"), 0.0)),
        reverse=True,
    )[:30]

    return {
        "label": "V12.3 UNDERLYING-FIRST LIVE FOCUS DESK",
        "validation_label": "DECISION SUPPORT · FORWARD EVIDENCE REQUIRED",
        "focus_count": len(focus),
        "active_now": active,
        "building_next": building,
        "recent": recent,
        "continuation_watch": continuation,
        "missed_movers": missed,
        "mover_forensics": forensics,
        "forensic_coverage": state.get("forensic_coverage") or {},
        "all_focus": focus,
        "last_update": state.get("last_update"),
        "swing_1d": state.get("swing_1d") or {},
        "rules": {
            "max_focus": MAX_FOCUS,
            "max_continuation_watch": MAX_CONTINUATION_WATCH,
            "building_stale_minutes": BUILDING_STALE_MINUTES,
            "continuation_watch_minutes": CONTINUATION_WATCH_MINUTES,
            "direction_flip": "requires invalidation; opposite event alone does not flip thesis",
            "vehicle_separation": "underlying thesis is independent of option/future/cash eligibility",
        },
    }
