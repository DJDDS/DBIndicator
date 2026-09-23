"""Forward evidence ledger for V12.2D event-driven early detection.

Purpose
-------
The audit's strongest reservation was synthetic ground truth.  This module
therefore records the *first live occurrence* of observable event states and
measures what the underlying actually did afterward.

It is intentionally separate from Trial-25, V12 stock-option recording and
V12.1 index recording.  It does not place orders and it does not auto-promote
any playbook.

Recorded stages
---------------
PRESSURE_SHIFT, READY, BREAK_ACCEPTED.

For each first occurrence per symbol+direction+stage+trading-day, the ledger
stores the reference underlying price and measures direction-adjusted returns
at 5m / 15m / 30m / 60m, plus running MFE/MAE.  This lets us compare whether
an earlier event really adds usable information or merely creates noise.
"""
from __future__ import annotations

import copy
import datetime as dt
import math

STATE_VERSION = 1
TRACKED_STATES = ("PRESSURE_SHIFT", "READY", "BREAK_ACCEPTED")
HORIZONS_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}
MAX_EVENTS = 3000


def _finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _parse_dt(value):
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def empty_state():
    return {"version": STATE_VERSION, "events": [], "last_update": None}


def _normalise(state):
    if not isinstance(state, dict):
        return empty_state()
    out = copy.deepcopy(state)
    if not isinstance(out.get("events"), list):
        out["events"] = []
    out["version"] = STATE_VERSION
    out.setdefault("last_update", None)
    return out


def _key(day, symbol, direction, stage):
    return f"{day}|{symbol}|{direction}|{stage}"


def _directed_return(entry, price, direction):
    if not (_finite(entry) and _finite(price)) or float(entry) <= 0:
        return None
    raw = (float(price) / float(entry) - 1.0) * 100.0
    return round(raw if direction == "Bullish" else -raw, 5)


def _update_existing(event, price, now):
    if not _finite(price):
        return
    ret = _directed_return(event.get("entry_price"), price, event.get("direction"))
    if ret is None:
        return

    event["last_price"] = round(float(price), 4)
    event["last_seen_at"] = now.isoformat(timespec="seconds")
    event["mfe_pct"] = round(max(float(event.get("mfe_pct") or 0.0), ret), 5)
    event["mae_pct"] = round(min(float(event.get("mae_pct") or 0.0), ret), 5)

    started = _parse_dt(event.get("first_seen_at"))
    if started is None:
        return
    if started.tzinfo is not None and now.tzinfo is None:
        started = started.replace(tzinfo=None)
    elif started.tzinfo is None and now.tzinfo is not None:
        started = started.replace(tzinfo=now.tzinfo)

    elapsed = (now - started).total_seconds() / 60.0
    outcomes = event.setdefault("outcomes", {})
    for label, minutes in HORIZONS_MINUTES.items():
        if label in outcomes or elapsed < minutes:
            continue
        outcomes[label] = {
            "observed_at": now.isoformat(timespec="seconds"),
            "exit_price": round(float(price), 4),
            "directional_return_pct": ret,
            "positive": bool(ret > 0),
        }


def process(state, event_radar, *, now=None):
    """Update the live event ledger from one event-radar snapshot."""
    now = now or dt.datetime.now()
    state = _normalise(state)
    rows = list((event_radar or {}).get("rows") or [])

    live_price = {
        (str(row.get("symbol") or ""), str(row.get("direction") or "")): row.get("live_price")
        for row in rows
        if row.get("symbol") and row.get("direction") in ("Bullish", "Bearish") and _finite(row.get("live_price"))
    }

    # Resolve already-recorded events while their symbol remains observable.
    for event in state["events"]:
        price = live_price.get((str(event.get("symbol") or ""), str(event.get("direction") or "")))
        if price is not None:
            _update_existing(event, price, now)

    existing = {str(e.get("key")) for e in state["events"] if e.get("key")}
    day = now.date().isoformat()
    for row in rows:
        stage = str(row.get("event_state") or "")
        if stage not in TRACKED_STATES:
            continue
        symbol = str(row.get("symbol") or "")
        direction = str(row.get("direction") or "")
        price = row.get("live_price")
        if not symbol or direction not in ("Bullish", "Bearish") or not _finite(price):
            continue
        key = _key(day, symbol, direction, stage)
        if key in existing:
            continue

        state["events"].append({
            "key": key,
            "trade_date": day,
            "first_seen_at": now.isoformat(timespec="seconds"),
            "symbol": symbol,
            "direction": direction,
            "event_state": stage,
            "event_source": row.get("event_source"),
            "entry_price": round(float(price), 4),
            "last_price": round(float(price), 4),
            "trigger": row.get("trigger"),
            "invalidation": row.get("invalidation"),
            "setup": row.get("setup"),
            "option_contract": row.get("option_contract"),
            "option_dte": row.get("option_dte"),
            "move_since_first_scout_atr": row.get("move_since_first_scout_atr"),
            "witnesses": {
                "participation_shift": bool(row.get("participation_shift")),
                "oi_accelerating": bool(row.get("oi_accelerating")),
                "relative_shift": bool(row.get("relative_shift")),
                "microstructure_shift": bool(row.get("microstructure_shift")),
                "independent_evidence_count": row.get("independent_evidence_count"),
            },
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "outcomes": {},
        })
        existing.add(key)

    if len(state["events"]) > MAX_EVENTS:
        state["events"] = state["events"][-MAX_EVENTS:]
    state["last_update"] = now.isoformat(timespec="seconds")
    return state


def _stats(events, horizon):
    values = []
    mfes = []
    maes = []
    for event in events:
        outcome = (event.get("outcomes") or {}).get(horizon)
        if outcome and _finite(outcome.get("directional_return_pct")):
            values.append(float(outcome["directional_return_pct"]))
        if _finite(event.get("mfe_pct")):
            mfes.append(float(event["mfe_pct"]))
        if _finite(event.get("mae_pct")):
            maes.append(float(event["mae_pct"]))
    if not values:
        return {
            "n": 0, "positive_pct": None, "avg_return_pct": None,
            "median_return_pct": None, "avg_mfe_pct": None, "avg_mae_pct": None,
        }
    values_sorted = sorted(values)
    n = len(values_sorted)
    mid = n // 2
    median = values_sorted[mid] if n % 2 else (values_sorted[mid-1] + values_sorted[mid]) / 2.0
    return {
        "n": n,
        "positive_pct": round(sum(v > 0 for v in values) / n * 100.0, 1),
        "avg_return_pct": round(sum(values) / n, 5),
        "median_return_pct": round(median, 5),
        "avg_mfe_pct": round(sum(mfes) / len(mfes), 5) if mfes else None,
        "avg_mae_pct": round(sum(maes) / len(maes), 5) if maes else None,
    }


def summarize(state):
    state = _normalise(state)
    events = state["events"]
    return {
        "events": len(events),
        "last_update": state.get("last_update"),
        "by_stage": {
            stage: {
                horizon: _stats([e for e in events if e.get("event_state") == stage], horizon)
                for horizon in HORIZONS_MINUTES
            }
            for stage in TRACKED_STATES
        },
        "method": "First live occurrence per symbol+direction+stage+day; direction-adjusted 5m/15m/30m/60m underlying outcomes with running MFE/MAE.",
    }
