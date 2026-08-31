"""Pure selection/diagnostic helpers for the OI Screener web view.

Kept free of Flask/Kite imports so the ranking rules can be regression-tested
without a live broker session.
"""

import math


def _num(value, default=None):
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return default
    return number if number is not None and math.isfinite(number) else default


def _abs_or(value, default=-1.0):
    value = _num(value)
    return abs(value) if value is not None else default


def select_oi_screener_rows(results, *, unusual_only=False, min_tier=None, z_threshold=1.5):
    """Return F&O rows that actually have a live OI reading.

    The live OI radar must be able to lead the technical screen, so legacy
    parameter-tier alignment is optional rather than a prerequisite. Statistical
    unusualness is also an optional view filter. Ranking emphasizes the most
    recent OI movement first so a stale whole-day/z-score spike cannot outrank
    a stock whose positioning is changing now.
    """
    selected = []
    for row in results or []:
        if row.get("error"):
            continue
        tier = row.get("param_tier")
        if min_tier is not None and (tier is None or tier < min_tier):
            continue
        live_oi = row.get("oi_total") if row.get("oi_total") is not None else row.get("oi")
        if live_oi is None:
            continue
        if unusual_only:
            z = _num(row.get("oi_z"))
            if z is None or abs(z) < z_threshold:
                continue
        selected.append(row)

    selected.sort(
        key=lambda row: (
            _abs_or(row.get("oi_chg_60m_pct")),
            _num(row.get("oi_acceleration"), -999.0),
            _abs_or(row.get("oi_chg_30m_pct")),
            _abs_or(row.get("oi_day_chg_pct")),
            _num(row.get("param_tier"), 0.0),
            _abs_or(row.get("oi_z")),
        ),
        reverse=True,
    )
    return selected


def oi_history_readiness(results, *, min_tier=None):
    eligible = []
    for r in (results or []):
        if r.get("error"):
            continue
        tier = r.get("param_tier")
        if min_tier is not None and (tier is None or tier < min_tier):
            continue
        live_oi = r.get("oi_total") if r.get("oi_total") is not None else r.get("oi")
        if live_oi is not None:
            eligible.append(r)
    ready_30m = sum(r.get("oi_chg_30m_pct") is not None for r in eligible)
    ready_60m = sum(r.get("oi_chg_60m_pct") is not None for r in eligible)
    total = len(eligible)
    return {
        "eligible_with_oi": total,
        "ready_30m": ready_30m,
        "ready_60m": ready_60m,
        "warming_up": bool(total and ready_60m < total),
    }


_OI_NUMERIC_FIELDS = (
    "close", "price_chg_today_pct", "oi_day_chg_pct",
    "oi_chg_15m_pct", "oi_chg_30m_pct", "oi_chg_60m_pct",
    "oi_acceleration", "vol_multiple", "oi_z", "param_tier",
)
_OI_TEXT_FIELDS = ("symbol", "oi_accel_label", "oi_structure", "direction")


def serialize_oi_screener_row(row):
    """Return only OI-view fields using strict JSON-safe primitive types.

    Persisted Railway scan state may restore numeric values as strings and live
    pandas/numpy values are not guaranteed to be Flask-JSON serializable.  The
    OI endpoint therefore normalizes its own small contract instead of returning
    the scanner's full 100+ field row.
    """
    out = {field: (str(row.get(field)) if row.get(field) is not None else None)
           for field in _OI_TEXT_FIELDS}
    for field in _OI_NUMERIC_FIELDS:
        out[field] = _num(row.get(field))
    live_oi = row.get("oi_total") if row.get("oi_total") is not None else row.get("oi")
    out["oi_total"] = _num(live_oi)
    return out
