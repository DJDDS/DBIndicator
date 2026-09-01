"""Forward validation for the V9.2 live opportunity radar.

This is intentionally *not* a historical backtest and does not promote the
research/shadow radar into a production strategy.  It records the first time a
symbol+direction enters the displayed top-5 on a trading day and measures the
underlying's direction-adjusted return from the first available live scan at or
after 30m, 1h, 2h, 4h plus the first and second later trading-session same-time horizons (1D/2D).

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
ALL_HORIZONS = ("30m", "1h", "2h", "4h", "1D", "2D")
STATE_VERSION = 2
MAX_EVENTS = 2500
RESEARCH_FRICTION_PCT = 0.18  # 0.08% costs + 0.05% slippage per side


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

    # 2D = first available scan on a *second distinct later trading session*
    # at/after the original clock time.  We anchor it to the date on which 1D
    # actually matured rather than adding 48 calendar hours, so weekends and
    # exchange holidays do not corrupt the holding-period definition.
    if "2D" not in outcomes and "1D" in outcomes:
        first_later = _parse_dt((outcomes.get("1D") or {}).get("observed_at"))
        if first_later is not None and now.date() > first_later.date() and now.time() >= started.time():
            value = _outcome(event, price, now)
            if value is not None:
                outcomes["2D"] = value


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


def _swing_horizon_map(swing_research):
    mapping = {}
    for horizon in ("1D", "2D"):
        block = (swing_research or {}).get(horizon) or {}
        for bucket in ("bullish", "bearish"):
            for row in block.get(bucket) or []:
                symbol = str(row.get("symbol") or "")
                direction = row.get("direction")
                if symbol and direction in ("Bullish", "Bearish"):
                    mapping[(symbol, direction)] = horizon
    return mapping


def process_scan(state, radar, results, *, now=None, swing_research=None):
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
    horizon_map = _swing_horizon_map(swing_research)
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
                "research_horizon": horizon_map.get((symbol, direction)),
                "outcomes": {},
                "expired_horizons": [],
            })
            existing.add(key)

    if len(state["events"]) > MAX_EVENTS:
        state["events"] = state["events"][-MAX_EVENTS:]
    state["last_scan"] = now.isoformat(timespec="seconds")
    return state


def _wilson_interval_pct(wins, n, z=1.959963984540054):
    if not n:
        return (None, None)
    p = float(wins) / float(n)
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * n * n))) / denom
    return (round(max(0.0, centre - half) * 100.0, 1), round(min(1.0, centre + half) * 100.0, 1))


def _return_stats(values):
    n = len(values)
    if not n:
        return {
            "n": 0, "win_rate_pct": None, "win_rate_ci95_low_pct": None,
            "win_rate_ci95_high_pct": None, "avg_return_pct": None,
            "profit_factor": None, "avg_win_pct": None, "avg_loss_pct": None,
        }
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    gp = float(sum(wins))
    gl = abs(float(sum(losses)))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else None)
    lo, hi = _wilson_interval_pct(len(wins), n)
    return {
        "n": n,
        "win_rate_pct": round(len(wins) / n * 100.0, 1),
        "win_rate_ci95_low_pct": lo,
        "win_rate_ci95_high_pct": hi,
        "avg_return_pct": round(sum(values) / n, 4),
        "profit_factor": round(float(pf), 3) if pf is not None and math.isfinite(pf) else pf,
        "avg_win_pct": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 4) if losses else None,
    }


def _aggregate(events, horizon):
    values = []
    for event in events:
        outcome = (event.get("outcomes") or {}).get(horizon)
        if outcome and _finite(outcome.get("directional_return_pct")):
            values.append(float(outcome["directional_return_pct"]))
    raw = _return_stats(values)
    if not values:
        return {
            "n": 0, "win_rate_pct": None, "win_rate_ci95_low_pct": None,
            "win_rate_ci95_high_pct": None, "avg_directional_return_pct": None,
            "profit_factor": None, "avg_win_pct": None, "avg_loss_pct": None,
            "net_win_rate_pct": None, "net_win_rate_ci95_low_pct": None,
            "net_win_rate_ci95_high_pct": None, "avg_net_return_pct": None,
            "net_profit_factor": None, "research_friction_pct": RESEARCH_FRICTION_PCT,
        }
    net = _return_stats([v - RESEARCH_FRICTION_PCT for v in values])
    return {
        "n": raw["n"],
        "win_rate_pct": raw["win_rate_pct"],
        "win_rate_ci95_low_pct": raw["win_rate_ci95_low_pct"],
        "win_rate_ci95_high_pct": raw["win_rate_ci95_high_pct"],
        "avg_directional_return_pct": raw["avg_return_pct"],
        "profit_factor": raw["profit_factor"],
        "avg_win_pct": raw["avg_win_pct"],
        "avg_loss_pct": raw["avg_loss_pct"],
        "net_win_rate_pct": net["win_rate_pct"],
        "net_win_rate_ci95_low_pct": net["win_rate_ci95_low_pct"],
        "net_win_rate_ci95_high_pct": net["win_rate_ci95_high_pct"],
        "avg_net_return_pct": net["avg_return_pct"],
        "net_profit_factor": net["profit_factor"],
        "research_friction_pct": RESEARCH_FRICTION_PCT,
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
    by_research_horizon = {
        target: {
            h: _aggregate([e for e in events if e.get("research_horizon") == target], h)
            for h in ALL_HORIZONS
        }
        for target in ("1D", "2D")
    }
    trade_days = sorted({str(e.get("trade_date")) for e in events if e.get("trade_date")})
    return {
        "events": len(events),
        "captured_today": sum(e.get("trade_date") == today.isoformat() for e in events),
        "distinct_trade_days": len(trade_days),
        "trade_days": trade_days,
        "last_scan": state.get("last_scan"),
        "horizons": horizon_stats,
        "by_direction": by_direction,
        "score_bands": score_bands,
        "by_research_horizon": by_research_horizon,
        "method": "First displayed top-5 appearance per symbol+direction/day; first live scan at/after each horizon.",
    }
