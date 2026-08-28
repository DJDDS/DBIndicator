"""Live NSE F&O early-movement ranking.

The engine separates *stored energy* (compression) from *directional ignition*.
That matters because a Bollinger/ATR coil can predict expansion without knowing
which side will win.  Best Entries therefore require a fresh directional trigger,
while Energy Building can surface earlier as a radar state.
"""
from __future__ import annotations

WEIGHTS = {
    "oi": 25,
    "compression": 20,
    "participation": 15,
    "momentum": 15,
    "relative_strength": 10,
    "structure": 10,
    "htf": 5,
}
MIN_SCORE = 72.0
MIN_COVERAGE = 0.80
ENERGY_SCORE = 60.0


def _directional(value, direction):
    if value is None:
        return None
    return value if direction == "Bullish" else -value


def score_candidate(row: dict) -> dict:
    direction = row.get("direction")
    compression_score = row.get("compression_score")
    energy_building = bool(row.get("energy_building") or (
        compression_score is not None and compression_score >= ENERGY_SCORE
    ))
    if direction not in ("Bullish", "Bearish"):
        return {
            "score": None, "coverage": 0.0, "eligible": False,
            "stage": "Energy Building" if energy_building else None,
            "compression_score": compression_score,
            "parts": [], "blockers": ["no direction"],
        }

    trigger = row.get("entry_trigger")
    bars_ago = row.get("entry_trigger_bars_ago")
    hard_blockers = []
    timing_blockers = []
    if trigger != direction:
        timing_blockers.append("no fresh trigger")
    elif bars_ago is None or bars_ago > 1:
        timing_blockers.append("stale trigger")

    if row.get("trend_state") not in (None, direction):
        hard_blockers.append("trend state against trade")
    if row.get("htf_agrees") is False:
        hard_blockers.append("higher timeframe against trade")
    if row.get("vwap_side_agrees") is False:
        hard_blockers.append("wrong side of VWAP")
    if row.get("entry_is_extended") is True:
        hard_blockers.append("extended entry")

    oi_agrees = row.get("oi_recent_agrees")
    if oi_agrees is None:
        oi_agrees = row.get("oi_agrees")
    oi60 = row.get("oi_chg_60m_pct")
    oi30 = row.get("oi_chg_30m_pct")
    accel = row.get("oi_acceleration")
    if oi_agrees is not True:
        hard_blockers.append("OI not confirming")
    if oi60 is None or oi60 <= 0:
        hard_blockers.append("no fresh 60m OI build")
    if accel is None or accel < -0.15:
        hard_blockers.append("OI fading")

    tod = row.get("tod_rvol")
    if tod is None:
        hard_blockers.append("no time-of-day volume baseline")
    elif tod < 0.90:
        hard_blockers.append("participation too weak")

    rs = _directional(row.get("rs_pct"), direction)
    if rs is not None and rs < 0:
        hard_blockers.append("relative strength against trade")
    if row.get("sector_agrees") is False:
        hard_blockers.append("sector against trade")

    parts = []
    earned = 0.0
    available = 0.0

    # F&O positioning: 25
    if oi_agrees is not None or oi60 is not None or accel is not None or row.get("oi_z") is not None:
        available += WEIGHTS["oi"]
        pts = 0.0
        if oi_agrees is True:
            pts += 10
        if oi60 is not None:
            pts += 6 if oi60 >= 2.0 else 5 if oi60 >= 1.0 else 3 if oi60 > 0 else 0
        if oi30 is not None and oi30 > 0:
            pts += 3
        if accel is not None:
            pts += 4 if accel >= 0.50 else 3 if accel >= 0 else 0
        z = row.get("oi_z")
        if z is not None and abs(z) >= 1.5:
            pts += 2
        pts = min(WEIGHTS["oi"], pts)
        earned += pts
        parts.append({"id": "oi", "points": pts, "max": WEIGHTS["oi"]})

    # Directionless stored energy: 20
    if compression_score is not None:
        available += WEIGHTS["compression"]
        cs = max(0.0, min(100.0, float(compression_score)))
        pts = WEIGHTS["compression"] * cs / 100.0
        earned += pts
        parts.append({"id": "compression", "points": round(pts, 1), "max": WEIGHTS["compression"]})

    # Time-of-day participation: 15
    if tod is not None:
        available += WEIGHTS["participation"]
        pts = 0.0
        if tod >= 1.60:
            pts += 9
        elif tod >= 1.30:
            pts += 8
        elif tod >= 1.10:
            pts += 6
        elif tod >= 0.90:
            pts += 4
        ta = row.get("tod_rvol_accel")
        if ta is not None:
            pts += 4 if ta >= 1.20 else 2 if ta >= 1.00 else 0
        if row.get("vol_rising") is True:
            pts += 2
        pts = min(WEIGHTS["participation"], pts)
        earned += pts
        parts.append({"id": "participation", "points": pts, "max": WEIGHTS["participation"]})

    # Momentum inflection / fresh trigger: 15
    momentum_available = trigger is not None or row.get("momentum_inflection_agrees") is not None or row.get("macd_hist_agrees") is not None
    if momentum_available:
        available += WEIGHTS["momentum"]
        pts = 0.0
        if trigger == direction and bars_ago is not None:
            pts += 7 if bars_ago == 0 else 5 if bars_ago == 1 else 0
        if row.get("momentum_inflection_agrees") is True:
            pts += 4
        if row.get("macd_hist_agrees") is True:
            pts += 2
        # A still-aligned MACD gets only a small amount; slope/trigger matter more.
        if row.get("macd_agrees") is True:
            pts += 2
        pts = min(WEIGHTS["momentum"], pts)
        earned += pts
        parts.append({"id": "momentum", "points": pts, "max": WEIGHTS["momentum"]})

    # Relative strength / acceleration: 10
    if rs is not None or row.get("rs_improving") is not None or row.get("rs_acceleration") is not None:
        available += WEIGHTS["relative_strength"]
        pts = 0.0
        if rs is not None:
            pts += 5 if rs >= 1.0 else 4 if rs >= 0.5 else 2 if rs > 0 else 0
        if row.get("rs_improving") is True:
            pts += 2
        rsa = _directional(row.get("rs_acceleration"), direction)
        if rsa is not None:
            pts += 3 if rsa >= 0.25 else 2 if rsa > 0 else 0
        pts = min(WEIGHTS["relative_strength"], pts)
        earned += pts
        parts.append({"id": "relative_strength", "points": pts, "max": WEIGHTS["relative_strength"]})

    # Entry location: 10
    if row.get("vwap_side_agrees") is not None or row.get("entry_is_extended") is not None:
        available += WEIGHTS["structure"]
        pts = 0.0
        if row.get("vwap_side_agrees") is True:
            pts += 5
        if row.get("entry_is_extended") is False:
            pts += 3
        breakout = row.get("breakout_state")
        if (direction == "Bullish" and breakout == "Breakout") or (direction == "Bearish" and breakout == "Breakdown"):
            pts += 2
        pts = min(WEIGHTS["structure"], pts)
        earned += pts
        parts.append({"id": "structure", "points": pts, "max": WEIGHTS["structure"]})

    # HTF context: 5, kept small so it confirms rather than makes the entry late.
    if row.get("htf_agrees") is not None:
        available += WEIGHTS["htf"]
        pts = WEIGHTS["htf"] if row.get("htf_agrees") is True else 0.0
        earned += pts
        parts.append({"id": "htf", "points": pts, "max": WEIGHTS["htf"]})

    coverage = available / sum(WEIGHTS.values()) if WEIGHTS else 0.0
    score = round(earned / available * 100.0, 1) if available else None

    # Stage is deliberately earlier than eligibility. A coil with fresh
    # participation/positioning belongs on radar even before direction fires.
    ignition = trigger == direction and bars_ago is not None and bars_ago <= 1
    evidence_waking = bool(
        (oi60 is not None and oi60 > 0) or
        (tod is not None and tod >= 1.10) or
        row.get("rs_improving") is True
    )
    stage = None
    if energy_building and evidence_waking and not ignition:
        stage = "Energy Building"
    elif ignition:
        stage = "Ignition"

    blockers = hard_blockers + timing_blockers
    eligible = bool(score is not None and coverage >= MIN_COVERAGE and score >= MIN_SCORE and not blockers)
    if eligible:
        stage = "Best Entry"

    return {
        "score": score,
        "coverage": round(coverage, 3),
        "eligible": eligible,
        "stage": stage,
        "compression_score": compression_score,
        "parts": parts,
        "blockers": blockers,
    }
