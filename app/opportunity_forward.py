"""Forward validation for the V9.2 live opportunity radar.

This is intentionally *not* a historical backtest and does not promote the
research/shadow radar into a production strategy.  It records the first time a
symbol+direction enters the displayed top-5 on a trading day and measures the
underlying's direction-adjusted return from the first available live scan at or
after 30m, 1h, 2h, 4h and the next-session same-time (1D) horizon.

The state is persisted by background.py inside the existing SCAN_RESULTS_FILE,
so it survives the same Railway restarts/redeploys as the scanner state.
"""
from __future__ import annotations

import copy
import datetime as dt
import math

INTRADAY_HORIZONS_MINUTES = {
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
}
ALL_HORIZONS = ("30m", "1h", "2h", "4h", "1D")
STATE_VERSION = 1
MAX_EVENTS = 2500


def _finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _parse_dt(value):
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def empty_state():
    return {"version": STATE_VERSION, "events": [], "last_scan": None}


def _normalise_state(state):
    if not isinstance(state, dict):
        return empty_state()
    out = copy.deepcopy(state)
    if not isinstance(out.get("events"), list):
        out["events"] = []
    out["version"] = STATE_VERSION
    out.setdefault("last_scan", None)
    return out


def _event_key(day, symbol, direction):
    return f"{day}|{symbol}|{direction}"


def _directional_return(entry, exit_price, direction):
    if not (_finite(entry) and _finite(exit_price)) or float(entry) <= 0:
        return None
    raw = (float(exit_price) / float(entry) - 1.0) * 100.0
    directed = raw if direction == "Bullish" else -raw
    return round(directed, 4)


def _outcome(event, price, now):
    ret = _directional_return(event.get("entry_price"), price, event.get("direction"))
    if ret is None:
        return None
    return {
        "exit_price": round(float(price), 4),
        "observed_at": now.isoformat(timespec="seconds"),
        "directional_return_pct": ret,
        "win": bool(ret > 0),
    }


def _resolve_event(event, price, now):
    started = _parse_dt(event.get("first_seen_at"))
    if started is None or not _finite(price):
        return
    outcomes = event.setdefault("outcomes", {})
    expired = event.setdefault("expired_horizons", [])

    for horizon, minutes in INTRADAY_HORIZONS_MINUTES.items():
        if horizon in outcomes or horizon in expired:
            continue
        if now.date() == started.date():
            if (now - started).total_seconds() >= minutes * 60:
                value = _outcome(event, price, now)
                if value is not None:
                    outcomes[horizon] = value
        elif now.date() > started.date():
            # Intraday horizons must remain same-session measurements. If the
            # market closed before a horizon matured, do not contaminate it
            # with an overnight move; mark it unavailable instead.
            expired.append(horizon)

    if "1D" not in outcomes:
        # 1D = first available later-session scan at or after the event's
        # original clock time. Weekend/holiday gaps naturally roll to the
        # next trading session because there are no scans on closed days.
        if now.date() > started.date() and now.time() >= started.time():
            value = _outcome(event, price, now)
            if value is not None:
                outcomes["1D"] = value


def _score_band(score):
    try:
        score = float(score)
    except (TypeError, ValueError):
        return "<55"
    if score >= 70:
        return "70+"
    if score >= 55:
        return "55-69"
    return "<55"


def process_scan(state, radar, results, *, now=None):
    """Resolve existing events and capture new displayed radar events.

    Dedupe contract: one event per symbol+direction per trading date, using the
    first appearance and its original score/rank/entry price.  This tests the
    *early discovery* quality instead of repeatedly counting the same stock on
    every 3-minute refresh.
    """
    now = now or dt.datetime.now()
    state = _normalise_state(state)
    price_map = {
        str(row.get("symbol")): float(row.get("close"))
        for row in (results or [])
        if row.get("symbol") and not row.get("error") and _finite(row.get("close"))
    }

    for event in state["events"]:
        price = price_map.get(str(event.get("symbol")))
        if price is not None:
            _resolve_event(event, price, now)

    existing = {str(event.get("key")) for event in state["events"] if event.get("key")}
    market_bias = (radar or {}).get("market_bias")
    market_strength = (radar or {}).get("market_bias_strength_pct")
    for bucket in ("bullish", "bearish"):
        for rank, row in enumerate((radar or {}).get(bucket) or [], 1):
            symbol = str(row.get("symbol") or "")
            direction = row.get("direction")
            price = price_map.get(symbol)
            if not symbol or direction not in ("Bullish", "Bearish") or price is None:
                continue
            key = _event_key(now.date().isoformat(), symbol, direction)
            if key in existing:
                continue
            state["events"].append({
                "key": key,
                "trade_date": now.date().isoformat(),
                "first_seen_at": now.isoformat(timespec="seconds"),
                "symbol": symbol,
                "direction": direction,
                "rank": rank,
                "score": row.get("score"),
                "score_band": _score_band(row.get("score")),
                "status": row.get("status"),
                "oi_structure": row.get("oi_structure"),
                "chase_guard": row.get("chase_guard"),
                "entry_price": round(float(price), 4),
                "market_bias": market_bias,
                "market_bias_strength_pct": market_strength,
                "outcomes": {},
                "expired_horizons": [],
            })
            existing.add(key)

    if len(state["events"]) > MAX_EVENTS:
        state["events"] = state["events"][-MAX_EVENTS:]
    state["last_scan"] = now.isoformat(timespec="seconds")
    return state


def _aggregate(events, horizon):
    values = []
    for event in events:
        outcome = (event.get("outcomes") or {}).get(horizon)
        if outcome and _finite(outcome.get("directional_return_pct")):
            values.append(float(outcome["directional_return_pct"]))
    n = len(values)
    if not n:
        return {"n": 0, "win_rate_pct": None, "avg_directional_return_pct": None}
    return {
        "n": n,
        "win_rate_pct": round(sum(v > 0 for v in values) / n * 100.0, 1),
        "avg_directional_return_pct": round(sum(values) / n, 4),
    }


def summarize(state, *, today=None):
    state = _normalise_state(state)
    events = state["events"]
    today = today or dt.date.today()
    horizon_stats = {h: _aggregate(events, h) for h in ALL_HORIZONS}
    by_direction = {
        direction: {h: _aggregate([e for e in events if e.get("direction") == direction], h) for h in ALL_HORIZONS}
        for direction in ("Bullish", "Bearish")
    }
    score_bands = {
        band: {h: _aggregate([e for e in events if e.get("score_band") == band], h) for h in ALL_HORIZONS}
        for band in ("70+", "55-69", "<55")
    }
    return {
        "events": len(events),
        "captured_today": sum(e.get("trade_date") == today.isoformat() for e in events),
        "last_scan": state.get("last_scan"),
        "horizons": horizon_stats,
        "by_direction": by_direction,
        "score_bands": score_bands,
        "method": "First displayed top-5 appearance per symbol+direction/day; first live scan at/after each horizon.",
    }
