"""Single source of truth for research return friction.

All percentages are percentage points. ``cost_pct`` is the non-slippage
round-trip drag; ``slippage_pct`` is charged per side.
"""
from __future__ import annotations


def round_trip_drag_pct(cost_pct: float = 0.0, slippage_pct: float = 0.0) -> float:
    return max(0.0, float(cost_pct or 0.0)) + 2.0 * max(0.0, float(slippage_pct or 0.0))


def gross_return_pct(entry: float, exit_px: float, direction: str = "Bullish") -> float:
    entry = float(entry)
    exit_px = float(exit_px)
    if entry <= 0:
        raise ValueError("entry must be positive")
    raw = (exit_px / entry - 1.0) * 100.0
    if str(direction) == "Bearish":
        raw = -raw
    return raw


def net_return_pct(entry: float, exit_px: float, direction: str = "Bullish", *,
                   cost_pct: float = 0.0, slippage_pct: float = 0.0) -> float:
    return gross_return_pct(entry, exit_px, direction) - round_trip_drag_pct(cost_pct, slippage_pct)
