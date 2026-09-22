"""Pure early-onset ranking helpers for the live research radar.

This module is intentionally broker-free and recorder-free.  It only interprets
fields that already exist on a live scan row, so deploying it cannot change
Trial-25, option-recorder, feasibility, or frozen-research state.

The purpose is operational: surface *forming* moves before cumulative day
statistics become large, while demoting moves whose runway is already spent.
It is research/shadow telemetry, not a validated trade model.
"""
from __future__ import annotations

import math
from typing import Any


def _num(value: Any, default=None):
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        return default
    return value if value is not None and math.isfinite(value) else default


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def _scale_positive(value, full_scale: float) -> float | None:
    value = _num(value)
    if value is None:
        return None
    if full_scale <= 0:
        return None
    return _clip(max(0.0, value) / full_scale * 100.0)


def _weighted_axis(parts):
    available = [(float(weight), float(score)) for weight, score in parts if score is not None]
    if not available:
        return None
    total = sum(weight for weight, _ in available)
    if total <= 0:
        return None
    return sum(weight * score for weight, score in available) / total


def _vwap_agrees(row: dict, direction: str) -> bool | None:
    direct = row.get("vwap_side_agrees")
    if isinstance(direct, bool):
        return direct
    side = str(row.get("vs_vwap") or "").strip().lower()
    if not side:
        return None
    if direction == "Bullish":
        return side == "above"
    if direction == "Bearish":
        return side == "below"
    return None


def derive_price_move_60m_atr(row: dict) -> float | None:
    """Return the last-hour price travel in ATR units when the row supports it."""
    direct = _num(row.get("price_move_60m_atr"))
    if direct is not None:
        return direct

    ret = _num(row.get("price_chg_60m_pct"))
    close = _num(row.get("close"))
    atr = _num(row.get("atr"))
    if ret is None or close is None or atr is None or close <= 0 or atr <= 0:
        return None

    atr_pct = atr / close * 100.0
    if atr_pct <= 0:
        return None
    return ret / atr_pct


def _trigger_level(row: dict, direction: str) -> float | None:
    if direction == "Bullish":
        keys = (
            "breakout_level",
            "retained_breakout_level",
            "prior_high_20d",
            "failed_breakout_level",
        )
    else:
        keys = (
            "failed_breakout_level",
            "breakout_level",
            "prior_low_20d",
            "retained_breakout_level",
        )
    for key in keys:
        value = _num(row.get(key))
        if value is not None and value > 0:
            return value
    return None


def _trigger_distance_atr(row: dict, direction: str, trigger: float | None) -> float | None:
    close = _num(row.get("close"))
    atr = _num(row.get("atr"))
    if trigger is None or close is None or atr is None or atr <= 0:
        return None
    if direction == "Bullish":
        return (trigger - close) / atr
    return (close - trigger) / atr


def _trigger_crossed(row: dict, direction: str, trigger: float | None) -> bool | None:
    close = _num(row.get("close"))
    if trigger is None or close is None:
        return None
    return close >= trigger if direction == "Bullish" else close <= trigger


def _runway_multiplier(travel_atr: float | None) -> tuple[float, str]:
    if travel_atr is None:
        return 0.85, "UNKNOWN"
    spent = abs(travel_atr)
    if spent <= 0.35:
        return 1.00, "FULL"
    if spent <= 0.75:
        return 0.90, "IGNITION"
    if spent <= 1.25:
        return 0.65, "LOW"
    return 0.35, "SPENT"


def _positioning_match(row: dict, direction: str) -> float | None:
    structure = str(row.get("oi_structure") or "")
    if not structure:
        return None
    if direction == "Bullish":
        if structure == "Long Buildup":
            return 100.0
        if structure == "Short Covering":
            return 60.0
        if structure in ("Short Buildup", "Long Unwinding"):
            return 0.0
    elif direction == "Bearish":
        if structure == "Short Buildup":
            return 100.0
        if structure == "Long Unwinding":
            return 60.0
        if structure in ("Long Buildup", "Short Covering"):
            return 0.0
    return 35.0


def _oi_velocity_axis(row: dict) -> float | None:
    oi15 = _num(row.get("oi_chg_15m_pct"))
    oi30 = _num(row.get("oi_chg_30m_pct"))
    accel = _num(row.get("oi_acceleration"))
    prior30 = _num(row.get("oi_chg_prior_30m_pct"))

    latest = _scale_positive(oi15, 1.25)
    medium = _scale_positive(oi30, 2.50)
    acceleration = None
    if accel is not None:
        acceleration = _clip(50.0 + accel * 25.0)
    elif oi15 is not None and prior30 is not None:
        acceleration = _clip(50.0 + (oi15 * 2.0 - prior30) * 20.0)

    # Latest window gets the largest share: stale 30/60-minute builds must not
    # outrank pressure that is increasing right now.
    return _weighted_axis(((0.55, latest), (0.20, medium), (0.25, acceleration)))


def _participation_axis(row: dict) -> float | None:
    tod = _num(row.get("tod_rvol"))
    accel = _num(row.get("tod_rvol_accel"))
    rising = row.get("vol_rising")
    range_atr = _num(row.get("bar_range_atr"))

    tod_score = None if tod is None else _clip((tod - 0.75) / 1.00 * 100.0)
    accel_score = None if accel is None else _clip(50.0 + accel * 35.0)
    rising_score = 100.0 if rising is True else (20.0 if rising is False else None)
    range_score = None if range_atr is None else _clip(range_atr / 1.00 * 100.0)
    return _weighted_axis(((0.50, tod_score), (0.20, accel_score), (0.15, rising_score), (0.15, range_score)))


def _concentration_axis(row: dict) -> float | None:
    oi15 = _num(row.get("oi_chg_15m_pct"))
    oi60 = _num(row.get("oi_chg_60m_pct"))
    if oi15 is None or oi60 is None or oi60 <= 0:
        return None
    return _clip(oi15 / max(oi60, 1e-9) * 100.0)


def _location_axis(row: dict, direction: str, trigger_distance_atr: float | None) -> float | None:
    compression = _num(row.get("compression_score"))
    compression_score = _clip(compression) if compression is not None else None

    proximity = None
    if trigger_distance_atr is not None:
        # Best early location is just below/above the structural trigger.
        distance = abs(trigger_distance_atr)
        proximity = _clip(100.0 - distance / 0.80 * 100.0)

    return _weighted_axis(((0.55, compression_score), (0.45, proximity)))


def _structure_axis(row: dict, direction: str) -> float | None:
    pos = _positioning_match(row, direction)
    vwap = _vwap_agrees(row, direction)
    vwap_score = 100.0 if vwap is True else (20.0 if vwap is False else None)
    htf = row.get("htf_direction")
    htf_score = 100.0 if htf == direction else (25.0 if htf in ("Bullish", "Bearish") else None)
    return _weighted_axis(((0.55, pos), (0.25, vwap_score), (0.20, htf_score)))


def _relative_axis(row: dict, direction: str) -> float | None:
    relative = _num(row.get("v8_relative"))
    rs = _num(row.get("rs_pct"))
    rs_accel = _num(row.get("rs_acceleration"))
    if direction == "Bearish":
        rs_score = None if rs is None else _clip(50.0 - rs * 20.0)
        accel_score = None if rs_accel is None else _clip(50.0 - rs_accel * 25.0)
    else:
        rs_score = None if rs is None else _clip(50.0 + rs * 20.0)
        accel_score = None if rs_accel is None else _clip(50.0 + rs_accel * 25.0)
    rel_score = _clip(relative) if relative is not None else None
    return _weighted_axis(((0.55, rel_score), (0.30, rs_score), (0.15, accel_score)))


def _extension_atr(row: dict) -> float | None:
    for key in (
        "breakout_extension_atr",
        "retained_breakout_extension_atr",
        "failed_breakout_extension_atr",
    ):
        value = _num(row.get(key))
        if value is not None:
            return abs(value)
    return None


def _day_move_atr(row: dict) -> float | None:
    pct = _num(row.get("price_chg_today_pct"))
    close = _num(row.get("close"))
    atr = _num(row.get("atr"))
    if pct is None or close is None or atr is None or close <= 0 or atr <= 0:
        return None
    atr_pct = atr / close * 100.0
    if atr_pct <= 0:
        return None
    return abs(pct) / atr_pct


def _fresh_participation(row: dict) -> tuple[bool, bool]:
    tod = _num(row.get("tod_rvol"))
    tod_accel = _num(row.get("tod_rvol_accel"))
    rising = row.get("vol_rising")
    active = bool(
        (tod is not None and tod >= 1.15)
        or (tod_accel is not None and tod_accel > 0.15)
        or rising is True
    )
    accelerating = bool(
        (tod_accel is not None and tod_accel > 0)
        or (tod is not None and tod >= 1.35)
        or rising is True
    )
    return active, accelerating


def _fresh_oi(row: dict) -> tuple[bool, bool]:
    oi15 = _num(row.get("oi_chg_15m_pct"))
    accel = _num(row.get("oi_acceleration"))
    label = str(row.get("oi_accel_label") or "").lower()
    building = oi15 is not None and oi15 > 0
    accelerating = bool(
        (accel is not None and accel > 0)
        or "strong acceleration" in label
        or "moderate acceleration" in label
    )
    return building, accelerating


def _trigger_axis(row: dict) -> float | None:
    bars = _num(row.get("entry_trigger_bars_ago"))
    cross = row.get("rsi_cross")
    scores = []
    if bars is not None:
        if bars <= 0:
            scores.append((0.75, 100.0))
        elif bars <= 1:
            scores.append((0.75, 80.0))
        elif bars <= 2:
            scores.append((0.75, 55.0))
        else:
            scores.append((0.75, 20.0))
    if cross:
        scores.append((0.25, 75.0))
    return _weighted_axis(scores)


def assess_early_onset(row: dict, direction: str, *, fallback_score: float | None = None) -> dict:
    """Return early-move telemetry for one directed live row.

    V12.2C principle: this function is not allowed to call a mature move
    "IGNITION" merely because fresh OI appears again after price has already
    travelled.  The radar has one job: surface FORMING / READY / very-fresh
    break states.  Mature continuation belongs downstream in the 3-minute
    tactical engine, not in the early radar.
    """
    trigger = _trigger_level(row, direction)
    trigger_distance = _trigger_distance_atr(row, direction, trigger)
    crossed = _trigger_crossed(row, direction, trigger)
    travel = derive_price_move_60m_atr(row)
    extension = _extension_atr(row)
    day_move = _day_move_atr(row)
    runway, runway_label = _runway_multiplier(travel)

    axes = [
        ("oi_velocity", 24.0, _oi_velocity_axis(row)),
        ("participation", 20.0, _participation_axis(row)),
        ("positioning", 12.0, _positioning_match(row, direction)),
        ("concentration", 10.0, _concentration_axis(row)),
        ("location", 10.0, _location_axis(row, direction, trigger_distance)),
        ("structure", 10.0, _structure_axis(row, direction)),
        ("relative", 8.0, _relative_axis(row, direction)),
        ("trigger", 6.0, _trigger_axis(row)),
    ]
    available_weight = sum(weight for _, weight, score in axes if score is not None)
    weighted = sum(weight * score for _, weight, score in axes if score is not None)
    pressure = weighted / available_weight if available_weight > 0 else None
    coverage = available_weight / 100.0
    fallback = _clip(fallback_score or 0.0)

    oi_building, oi_accelerating = _fresh_oi(row)
    participation_active, participation_accelerating = _fresh_participation(row)
    compression = _num(row.get("compression_score"))
    coiled = compression is not None and compression >= 50.0
    spent_60 = abs(travel) if travel is not None else None
    overshoot = max(0.0, -trigger_distance) if trigger_distance is not None else None

    late_reasons = []
    # A very large trigger overshoot is the exact failure visible in the live
    # screenshot: several ATR past the trigger can never remain "TRIGGERED".
    if overshoot is not None and overshoot > 0.35:
        late_reasons.append(f"{overshoot:.2f} ATR past trigger")
    # Early radar is intentionally stricter than a continuation model.
    if spent_60 is not None and spent_60 > 0.85:
        late_reasons.append(f"{spent_60:.2f} ATR moved in 60m")
    if extension is not None and extension > 0.90:
        late_reasons.append(f"{extension:.2f} ATR breakout extension")
    # Large same-day displacement is only a veto when the current hour has
    # also moved meaningfully, so an opening gap alone does not kill the lane.
    if day_move is not None and day_move > 2.0 and spent_60 is not None and spent_60 > 0.50:
        late_reasons.append(f"{day_move:.2f} ATR moved today")

    fading = bool(
        (_num(row.get("oi_acceleration")) is not None and _num(row.get("oi_acceleration")) < 0
         and _num(row.get("oi_chg_15m_pct")) is not None and _num(row.get("oi_chg_15m_pct")) <= 0)
        or str(row.get("oi_accel_label") or "").lower().startswith("decel")
    )

    early_state = "OBSERVE"
    maturity = "UNCONFIRMED"
    phase = "QUIET"
    if fading:
        early_state = "FADING"
        maturity = "FADING"
        phase = "FADING"
    elif late_reasons:
        early_state = "LATE"
        maturity = "MISSED"
        phase = "EXTENDED"
    else:
        # Very fresh break: crossed only slightly, while fresh positioning and
        # participation are still present.  A break 0.36 ATR+ beyond the level
        # is already too mature for this panel.
        if (
            crossed is True
            and overshoot is not None and overshoot <= 0.20
            and oi_building
            and participation_active
            and (oi_accelerating or participation_accelerating)
        ):
            early_state = "FRESH_BREAK"
            maturity = "JUST BROKE"
            phase = "IGNITION"
        # READY is deliberately pre-break.  This is the most actionable early
        # state for the downstream 3m engine.
        elif (
            trigger_distance is not None
            and 0.0 <= trigger_distance <= 0.30
            and oi_building
            and (oi_accelerating or participation_accelerating)
            and (participation_active or coiled)
        ):
            early_state = "READY"
            maturity = "AT TRIGGER"
            phase = "PRE-IGNITION"
        # FORMING can exist without a useful long-horizon trigger.  That allows
        # V12.2B to receive a genuinely early candidate and construct its own
        # compact 3m micro trigger instead of waiting for a distant 20-day high.
        elif (
            oi_building
            and oi_accelerating
            and (participation_active or participation_accelerating)
            and (spent_60 is None or spent_60 <= 0.40)
            and (
                coiled
                or trigger_distance is None
                or (0.30 < trigger_distance <= 0.80)
            )
        ):
            early_state = "FORMING"
            maturity = "EARLY"
            phase = "PRE-IGNITION"

    early_eligible = early_state in ("FORMING", "READY", "FRESH_BREAK")
    if early_state == "FRESH_BREAK":
        action_stage = "TRIGGERED"
    elif early_state == "READY":
        action_stage = "ARMED"
    elif early_state == "FORMING":
        action_stage = "FORMING"
    elif early_state == "LATE":
        action_stage = "LATE"
    else:
        action_stage = early_state

    if pressure is None or coverage < 0.40:
        usable = False
        score = fallback
    else:
        usable = True
        # Ranking remains secondary. Eligibility is determined by the concrete
        # state machine above; score can only order candidates within a state.
        score = _clip(pressure * runway)

    return {
        "usable": usable,
        "coverage": round(coverage, 3),
        "pressure": round(pressure, 1) if pressure is not None else None,
        "score": round(score, 1),
        "phase": phase if usable else "FALLBACK",
        "early_state": early_state,
        "early_eligible": bool(early_eligible and usable),
        "maturity": maturity,
        "late_reasons": late_reasons,
        "runway": round(runway, 2),
        "runway_label": runway_label,
        "price_move_60m_atr": travel,
        "day_move_atr": round(day_move, 3) if day_move is not None else None,
        "extension_atr": round(extension, 3) if extension is not None else None,
        "trigger_level": trigger,
        "trigger_distance_atr": round(trigger_distance, 3) if trigger_distance is not None else None,
        "trigger_overshoot_atr": round(overshoot, 3) if overshoot is not None else None,
        "trigger_crossed": crossed,
        "oi_concentration_pct": round(_concentration_axis(row), 1) if _concentration_axis(row) is not None else None,
        "fresh_oi_15m_pct": _num(row.get("oi_chg_15m_pct")),
        "fresh_oi_30m_pct": _num(row.get("oi_chg_30m_pct")),
        "oi_accelerating_now": oi_accelerating,
        "participation_active_now": participation_active,
        "participation_accelerating_now": participation_accelerating,
        "action_stage": action_stage,
        "axis_scores": {name: (round(axis_score, 1) if axis_score is not None else None) for name, _, axis_score in axes},
    }
