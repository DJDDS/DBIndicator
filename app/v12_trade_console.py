"""V12.0 operational trade-candidate console.

This module does not create or revive a validated alpha model. It takes the
existing live opportunity radar and asks a narrower operational question:
which already-ranked names are merely interesting, which have a defined
setup, and which have a currently executable derivative route?

Every returned row is permanently labelled NOT VALIDATED.
"""
from __future__ import annotations

import math
from typing import Any

FUTURES_SPREAD_MAX_BPS = 12.0
OPTION_SPREAD_MAX_PCT = 4.0
OBSERVE_MIN_SCORE = 40.0
WATCH_MIN_SCORE = 55.0
SETUP_MIN_SCORE = 70.0


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _num(value: Any, default=None):
    return float(value) if _finite(value) else default


def _structural_reference(result: dict) -> float | None:
    for key in (
        "breakout_level",
        "retained_breakout_level",
        "failed_breakout_level",
        "vwap",
    ):
        value = _num(result.get(key))
        if value is not None:
            return value
    return None


def _option_executable(result: dict) -> bool:
    intel = result.get("option_intelligence") or {}
    contract = intel.get("contract") or {}
    symbol = contract.get("symbol") or result.get("option_contract")
    bid = _num(contract.get("bid"))
    ask = _num(contract.get("ask"))
    spread = _num(contract.get("spread_pct"), _num(result.get("option_spread_pct")))
    return bool(
        symbol
        and bid is not None and bid > 0
        and ask is not None and ask >= bid
        and spread is not None and spread <= OPTION_SPREAD_MAX_PCT
    )


def _futures_executable(result: dict) -> bool:
    price = _num(result.get("fut_price_near"))
    spread = _num(result.get("fut_spread_bps"))
    return bool(price is not None and price > 0 and spread is not None and spread <= FUTURES_SPREAD_MAX_BPS)


def _route(result: dict) -> str:
    fut = _futures_executable(result)
    opt = _option_executable(result)
    if fut and opt:
        return "BOTH"
    if fut:
        return "FUTURES"
    if opt:
        return "OPTION"
    return "WAIT"


def _classify(radar_row: dict, result: dict) -> dict:
    score = _num(radar_row.get("score"), 0.0)
    chase_guard = str(radar_row.get("chase_guard") or "OK").upper()
    reference = _structural_reference(result)
    route = _route(result)

    if score < WATCH_MIN_SCORE:
        state = "OBSERVE"
        reason = "Attention only; live score has not reached WATCH quality."
    elif score < SETUP_MIN_SCORE:
        state = "WATCH"
        reason = "Quality is building; wait for stronger structure/participation."
    elif chase_guard == "EXTENDED":
        state = "WATCH"
        reason = "Extended >1.25 ATR — do not chase; wait for a reset."
    elif reference is None:
        state = "WATCH"
        reason = "No live structural trigger/invalidation reference is available yet."
    elif route == "WAIT":
        state = "SETUP"
        reason = "Structure is defined, but live derivative liquidity is not executable yet."
    else:
        state = "EXECUTABLE"
        reason = f"Setup is defined and current {route.lower()} liquidity clears the operational gate."

    out = dict(radar_row)
    out.update({
        "trade_state": state,
        "display_state": "EXECUTABLE CANDIDATE · NOT VALIDATED" if state == "EXECUTABLE" else f"{state} · NOT VALIDATED",
        "validation_label": "NOT VALIDATED",
        "not_validated": True,
        "execution_route": route,
        "trigger_reference": reference,
        "invalidation_reference": reference,
        "operational_reason": reason,
        "fut_spread_bps": _num(result.get("fut_spread_bps")),
        "fut_price_near": _num(result.get("fut_price_near")),
        "option_contract": result.get("option_contract") or ((result.get("option_intelligence") or {}).get("contract") or {}).get("symbol"),
        "option_spread_pct": _num(result.get("option_spread_pct"), _num(((result.get("option_intelligence") or {}).get("contract") or {}).get("spread_pct"))),
        "option_dte": result.get("option_dte"),
        "close": _num(result.get("close")),
        "vwap": _num(result.get("vwap")),
        "atr": _num(result.get("atr")),
    })
    return out


def _ordered(rows: list[dict], result_map: dict[str, dict], limit: int) -> list[dict]:
    classified = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        classified.append(_classify(row, result_map.get(symbol, {})))
    priority = {"EXECUTABLE": 4, "SETUP": 3, "WATCH": 2, "OBSERVE": 1}
    classified.sort(
        key=lambda row: (
            priority.get(row.get("trade_state"), 0),
            _num(row.get("score"), _num(row.get("horizon_score"), 0.0)),
            row.get("symbol") or "",
        ),
        reverse=True,
    )
    return classified[: max(0, int(limit))]


def build_trade_console(radar: dict | None, swing_research: dict | None, results: list[dict] | None, *, limit: int = 5) -> dict:
    """Build operational intraday and swing candidate surfaces.

    This is downstream of the existing research/shadow opportunity radar. It
    never mutates the underlying scan rows and never claims a validated edge.
    """
    radar = radar or {}
    swing_research = swing_research or {}
    result_map = {
        str(row.get("symbol")): row
        for row in (results or [])
        if row.get("symbol") and not row.get("error")
    }
    intraday_rows = list(radar.get("bullish") or []) + list(radar.get("bearish") or [])
    intraday = _ordered(intraday_rows, result_map, limit)

    swing = {}
    for horizon in ("1D", "2D"):
        block = swing_research.get(horizon) or {}
        rows = list(block.get("bullish") or []) + list(block.get("bearish") or [])
        # Horizon score is the primary ordering score for the swing view but
        # operational state remains based on the live attention score.
        enriched = _ordered(rows, result_map, limit)
        enriched.sort(key=lambda row: (_num(row.get("horizon_score"), _num(row.get("score"), 0.0)), row.get("symbol") or ""), reverse=True)
        swing[horizon] = enriched[: max(0, int(limit))]

    return {
        "label": "V12 LIVE TRADE OPPORTUNITY CONSOLE",
        "is_validated_strategy": False,
        "validation_label": "NOT VALIDATED",
        "intraday": intraday,
        "swing": swing,
        "counts": {
            "intraday": len(intraday),
            "intraday_executable": sum(row.get("trade_state") == "EXECUTABLE" for row in intraday),
            "swing_1d": len(swing.get("1D") or []),
            "swing_2d": len(swing.get("2D") or []),
        },
    }
