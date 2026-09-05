"""Market-microstructure feasibility summary for V12.0.

This is deliberately not an efficacy test. It answers whether the forward
Indian stock-option panel is liquid and complete enough to justify a later
pre-registered Trial 25.
"""
from __future__ import annotations

import math
from statistics import median


def _finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _median(values):
    vals = [float(v) for v in values if _finite(v)]
    return round(float(median(vals)), 4) if vals else None


def _percentile(values, q):
    vals = sorted(float(v) for v in values if _finite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 4)
    pos = (len(vals) - 1) * float(q)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        value = vals[lo]
    else:
        value = vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)
    return round(value, 4)


def summarize_feasibility(state: dict | None) -> dict:
    state = state or {}
    captured = state.get("captured_slots") or {}
    trading_days = len([day for day, slots in captured.items() if slots])
    slots_captured = sum(len(slots or []) for slots in captured.values())
    symbol_stats = state.get("symbol_stats") or {}

    tradeable = []
    all_spreads = []
    threshold_counts = {"1": 0, "2": 0, "4": 0, "5": 0}
    term_n = 0
    broad_n = 0
    earnings_usable = 0
    symbol_rows = {}
    for symbol, raw in symbol_stats.items():
        broad = int(raw.get("broad_snapshots") or 0)
        two = int(raw.get("two_sided_snapshots") or 0)
        coverage = two / broad if broad > 0 else 0.0
        spreads = [float(v) for v in (raw.get("spread_values") or []) if _finite(v)]
        med = _median(spreads)
        all_spreads.extend(spreads)
        term = int(raw.get("term_structure_snapshots") or 0)
        earnings = int(raw.get("earnings_quote_snapshots") or 0)
        term_n += term
        broad_n += broad
        earnings_usable += earnings
        qualifies = bool(broad > 0 and coverage >= 0.70 and med is not None and med <= 4.0)
        if qualifies:
            tradeable.append(str(symbol))
        for threshold in (1, 2, 4, 5):
            if broad > 0 and coverage >= 0.70 and med is not None and med <= threshold:
                threshold_counts[str(threshold)] += 1
        symbol_rows[str(symbol)] = {
            "coverage_pct": round(coverage * 100.0, 1),
            "median_straddle_spread_pct": med,
            "tradeable": qualifies,
        }

    median_spread = _median(all_spreads)
    p75_spread = _percentile(all_spreads, 0.75)
    quote_contracts = int(state.get("quote_contracts") or 0)
    stale_contracts = int(state.get("stale_contracts") or 0)
    stale_rate = stale_contracts / quote_contracts * 100.0 if quote_contracts > 0 else None
    term_coverage = term_n / broad_n * 100.0 if broad_n > 0 else None

    if trading_days < 10:
        status = "RECORDING — NO FEASIBILITY VERDICT"
    elif len(tradeable) >= 20:
        status = "STOCK OPTIONS PRACTICALLY TESTABLE"
    else:
        status = "STOCK OPTION LIQUIDITY GATE NOT MET"

    return {
        "status": status,
        "trial25_locked": True,
        "trading_days_recorded": trading_days,
        "slots_captured": slots_captured,
        "tradeable_symbols": len(tradeable),
        "tradeable_symbol_list": sorted(tradeable),
        "median_straddle_spread_pct": median_spread,
        "p75_straddle_spread_pct": p75_spread,
        "symbols_below_spread_pct": threshold_counts,
        "stale_quote_rate_pct": round(stale_rate, 2) if stale_rate is not None else None,
        "term_structure_coverage_pct": round(term_coverage, 1) if term_coverage is not None else None,
        "earnings_quote_snapshots": earnings_usable,
        "final_week_samples": int(state.get("final_week_samples") or 0),
        "symbol_metrics": symbol_rows,
        "gate": {
            "minimum_trading_days": 10,
            "minimum_tradeable_symbols": 20,
            "minimum_two_sided_coverage_pct": 70.0,
            "maximum_median_straddle_spread_pct": 4.0,
        },
    }
