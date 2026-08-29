"""V8 Dual Alpha cross-sectional scanner helpers.

V8 deliberately separates bullish and bearish opportunity engines. Price reveals
side through a 15-minute Recent-Range escape; cross-sectional structure,
participation, relative performance, and derivatives evidence then rank the
opportunity. Conventional indicator votes are not hard eligibility gates.
"""
from __future__ import annotations

from statistics import median
from typing import Iterable

import numpy as np
import pandas as pd

TRADE_ALPHA_MIN = 85.0
WATCH_ALPHA_MIN = 70.0
PARTICIPATION_MIN = 70.0
MAX_EXTENSION_ATR = 1.25
RECENT_RANGE_SOURCE = "Recent Range"


def _finite(value) -> bool:
    try:
        return value is not None and bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _float(value, default=None):
    return float(value) if _finite(value) else default


def _direction(row: dict) -> str | None:
    value = (
        row.get("v8_direction")
        or row.get("breakout_direction")
        or row.get("retained_breakout_direction")
        or row.get("direction")
    )
    return value if value in ("Bullish", "Bearish") else None


def percentile_rank(values: Iterable, *, higher_better: bool = True) -> list[float | None]:
    """Return 0..100 cross-sectional percentiles, preserving missing values."""
    ser = pd.to_numeric(pd.Series(list(values), dtype="object"), errors="coerce")
    valid = int(ser.notna().sum())
    if valid == 0:
        return [None] * len(ser)
    ranks = ser.rank(pct=True, method="average") * 100.0
    if not higher_better:
        ranks = 100.0 - ranks + (100.0 / valid)
    return [round(float(v), 2) if pd.notna(v) else None for v in ranks]


def consensus_score(values: Iterable) -> float | None:
    """Median consensus of available evidence; missing evidence never becomes 0."""
    clean = [float(v) for v in values if _finite(v)]
    if not clean:
        return None
    return round(float(median(clean)), 2)


def directional_clv(row: dict, direction: str) -> float | None:
    """Directional close-location value in the signal candle, 0..100."""
    hi, lo, close = (_float(row.get(k)) for k in ("high", "low", "close"))
    if hi is None or lo is None or close is None or hi <= lo:
        # Historical events may carry a pre-computed close-position field.
        cp = _float(row.get("close_position_pct"))
        if cp is None:
            return None
        return round(cp if direction == "Bullish" else 100.0 - cp, 2)
    raw = (close - lo) / (hi - lo) * 100.0
    raw = float(np.clip(raw, 0.0, 100.0))
    return round(raw if direction == "Bullish" else 100.0 - raw, 2)


def classify_oi_state(price_chg_pct, oi_chg_pct) -> str | None:
    """Direction-free four-quadrant futures positioning state."""
    p, oi = _float(price_chg_pct), _float(oi_chg_pct)
    if p is None or oi is None or p == 0 or oi == 0:
        return None
    if p > 0 and oi > 0:
        return "Long Buildup"
    if p > 0 and oi < 0:
        return "Short Covering"
    if p < 0 and oi > 0:
        return "Fresh Short Buildup"
    return "Long Unwinding"


_BULL_OI_BASE = {
    "Long Buildup": 100.0,
    "Short Covering": 70.0,
    "Long Unwinding": 20.0,
    "Fresh Short Buildup": 0.0,
}
_BEAR_OI_BASE = {
    "Fresh Short Buildup": 100.0,
    "Long Unwinding": 70.0,
    "Short Covering": 20.0,
    "Long Buildup": 0.0,
}


def directional_derivatives_score(
    direction: str,
    *,
    price_chg_pct=None,
    oi_chg_pct=None,
    oi_strength_percentile=None,
    basis_acceleration=None,
) -> dict:
    """Score direction-aware OI state and basis without making either a veto.

    OI-state conviction is shrunk toward neutral (50) when the absolute OI move
    is ordinary cross-sectionally. Basis is a small independent confirmation;
    missing basis is ignored rather than penalised.
    """
    state = classify_oi_state(price_chg_pct, oi_chg_pct)
    if state is None:
        oi_score = 50.0
    else:
        base = (_BULL_OI_BASE if direction == "Bullish" else _BEAR_OI_BASE).get(state, 50.0)
        strength = _float(oi_strength_percentile, 50.0)
        strength = float(np.clip(strength, 0.0, 100.0)) / 100.0
        oi_score = 50.0 + (base - 50.0) * strength

    basis_score = None
    accel = _float(basis_acceleration)
    if accel is not None:
        directed = accel if direction == "Bullish" else -accel
        if directed > 0.02:
            basis_score = 75.0
        elif directed < -0.02:
            basis_score = 25.0
        else:
            basis_score = 50.0
    score = consensus_score([oi_score, basis_score])
    return {
        "score": round(score if score is not None else 50.0, 2),
        "oi_state": state or "OI Neutral/Unavailable",
        "oi_score": round(oi_score, 2),
        "basis_score": basis_score,
    }


def classify_opportunity(row: dict, *, alpha, participation) -> dict:
    """Map evidence to a transparent TRADE / WATCH / NO EDGE state."""
    a, p = _float(alpha), _float(participation)
    source = row.get("breakout_source") or row.get("retained_breakout_source")
    ext = _float(row.get("breakout_extension_atr"), _float(row.get("retained_breakout_extension_atr")))
    recent = source == RECENT_RANGE_SOURCE
    chased = ext is not None and ext > MAX_EXTENSION_ATR

    eligible = bool(
        a is not None and p is not None
        and a >= TRADE_ALPHA_MIN and p >= PARTICIPATION_MIN
        and recent and not chased
    )
    if eligible:
        state = "TRADE CANDIDATE"
    elif (a is not None and a >= WATCH_ALPHA_MIN) or (recent and a is not None and a >= 60.0):
        state = "WATCH"
    else:
        state = "NO EDGE"

    reasons = []
    if recent:
        reasons.append("Recent-Range escape")
    if a is not None:
        reasons.append(f"Alpha P{a:.0f}")
    if p is not None:
        reasons.append(f"Participation P{p:.0f}")
    if chased:
        reasons.append(f"Chase risk {ext:.2f} ATR")
    return {"state": state, "eligible": eligible, "chased": chased, "reasons": reasons}



def classify_swing_opportunity(row: dict, *, direction: str, alpha, participation, derivatives, now_time=None) -> dict:
    """Separate late-session 1-2D continuation read; never upgrades weak intraday alpha.

    V8 swing is intentionally a second decision surface.  It adds persistence
    and day-location evidence only after 14:15 IST; before then a strong setup
    can be WATCH but cannot be labelled a swing TRADE candidate.
    """
    import datetime as _dt

    a, p, dscore = _float(alpha), _float(participation), _float(derivatives)
    cp = _float(row.get("close_position_pct"))
    day_location = None if cp is None else (cp if direction == "Bullish" else 100.0 - cp)
    retained = bool(row.get("breakout_retained"))
    retest = bool(row.get("retest_confirmed") or row.get("breakout_retest_confirmed"))
    fresh = bool(row.get("fresh_breakout"))
    persistence = 100.0 if (retained or retest) else (70.0 if fresh else 40.0)
    swing_alpha = consensus_score([a, p, dscore, day_location, persistence])

    if isinstance(now_time, str):
        try:
            hh, mm = now_time.split(":")[:2]
            clock = _dt.time(int(hh), int(mm))
        except Exception:
            clock = None
    elif isinstance(now_time, _dt.datetime):
        clock = now_time.time()
    elif isinstance(now_time, _dt.time):
        clock = now_time
    else:
        clock = None
    late = clock is not None and clock >= _dt.time(14, 15)

    source = row.get("breakout_source") or row.get("retained_breakout_source")
    ext = _float(row.get("breakout_extension_atr"), _float(row.get("retained_breakout_extension_atr")))
    not_chased = ext is None or ext <= MAX_EXTENSION_ATR
    eligible = bool(
        late and source == RECENT_RANGE_SOURCE and not_chased
        and a is not None and a >= 80.0
        and swing_alpha is not None and swing_alpha >= TRADE_ALPHA_MIN
        and p is not None and p >= PARTICIPATION_MIN
        and (retained or retest)
    )
    if eligible:
        state = "TRADE CANDIDATE"
    elif swing_alpha is not None and swing_alpha >= WATCH_ALPHA_MIN:
        state = "WATCH"
    else:
        state = "NO EDGE"
    return {
        "state": state, "eligible": eligible, "alpha": swing_alpha,
        "day_location": round(day_location, 2) if day_location is not None else None,
        "persistence": persistence, "late_session": late,
    }

def _row_turnover(row: dict):
    direct = _float(row.get("turnover_notional"))
    if direct is not None:
        return direct
    close, volume = _float(row.get("close")), _float(row.get("volume"))
    return close * volume if close is not None and volume is not None else None


def _relative_raw(row: dict) -> float | None:
    return consensus_score([
        row.get("rs_pct"),
        row.get("stock_sector_lead_pct"),
        row.get("stock_index_lead_pct"),
    ])


def _rank_map(rows: list[dict], getter, *, higher_better=True) -> list[float | None]:
    return percentile_rank([getter(r) for r in rows], higher_better=higher_better)



def score_preranked_row(row: dict) -> dict:
    """Score one row whose raw evidence has already been ranked vs the full universe.

    Historical V8 backtests attach these percentiles point-in-time across every
    researched F&O stock before scoring breakout events. This preserves live /
    research parity without keeping a fitted weighting model.
    """
    r = dict(row)
    direction = _direction(r)
    r["v8_direction"] = direction
    if direction not in ("Bullish", "Bearish"):
        r.update({
            "v8_structure": None, "v8_participation": None, "v8_relative": None,
            "v8_derivatives": None, "v8_oi_state": "OI Neutral/Unavailable",
            "v8_alpha": None, "v8_state": "NO EDGE", "v8_eligible": False,
            "v8_chased": False, "v8_reasons": [],
        })
        return r

    participation = consensus_score([
        r.get("v8_tod_rvol_percentile"),
        r.get("v8_opening_rvol_percentile"),
        r.get("v8_range_shock_percentile"),
        r.get("v8_gap_shock_percentile"),
        r.get("v8_turnover_percentile"),
    ])
    structure = consensus_score([
        r.get("v8_breakout_strength_percentile"),
        directional_clv(r, direction),
    ])
    relative = _float(r.get("v8_relative_percentile"))
    derivatives = directional_derivatives_score(
        direction,
        price_chg_pct=r.get("price_chg_60m_pct", r.get("price_chg_pct")),
        oi_chg_pct=r.get("oi_chg_60m_pct"),
        oi_strength_percentile=r.get("v8_oi_strength_percentile"),
        basis_acceleration=r.get("basis_acceleration"),
    )
    alpha = consensus_score([structure, participation, relative, derivatives.get("score")])
    classification = classify_opportunity(r, alpha=alpha, participation=participation)
    r["v8_structure"] = structure
    r["v8_participation"] = participation
    r["v8_relative"] = relative
    r["v8_derivatives"] = derivatives.get("score")
    r["v8_oi_state"] = derivatives.get("oi_state")
    r["v8_alpha"] = alpha
    r["v8_state"] = classification["state"]
    r["v8_eligible"] = classification["eligible"]
    r["v8_chased"] = classification["chased"]
    reasons = list(classification.get("reasons") or [])
    if structure is not None and structure >= 80:
        reasons.append("Strong price acceptance")
    if relative is not None and relative >= 80:
        reasons.append("Relative leader" if direction == "Bullish" else "Relative laggard")
    if derivatives.get("score") is not None and derivatives["score"] >= 70:
        reasons.append(derivatives.get("oi_state"))
    r["v8_reasons"] = reasons[:5]
    return r

def rank_cross_section(rows: Iterable[dict]) -> list[dict]:
    """Attach V8 component ranks and independent Bull/Bear Alpha to rows.

    All percentile comparisons use the current supplied cross-section. A row's
    own direction decides how relative performance and derivatives are read.
    """
    out = [dict(r) for r in (rows or [])]
    if not out:
        return out

    # Cross-sectional participation inputs.
    tod_rank = _rank_map(out, lambda r: r.get("tod_rvol"))
    opening_rank = _rank_map(out, lambda r: r.get("opening_rvol"))
    range_rank = _rank_map(out, lambda r: r.get("bar_range_atr"))
    gap_rank = _rank_map(out, lambda r: abs(_float(r.get("gap_atr"), 0.0)) if _finite(r.get("gap_atr")) else None)
    turnover_rank = _rank_map(out, _row_turnover)
    breakout_rank = _rank_map(
        out,
        lambda r: _float(r.get("breakout_extension_atr"), _float(r.get("retained_breakout_extension_atr"))),
    )
    oi_strength_rank = _rank_map(
        out, lambda r: abs(_float(r.get("oi_chg_60m_pct"), 0.0)) if _finite(r.get("oi_chg_60m_pct")) else None
    )

    # A single directed relative value is ranked across all candidates. Bearish
    # rows negate it so severe underperformance becomes a high rank.
    directed_relative = []
    for r in out:
        d = _direction(r)
        raw = _relative_raw(r)
        directed_relative.append(raw if d == "Bullish" else (-raw if d == "Bearish" and raw is not None else None))
    relative_rank = percentile_rank(directed_relative)

    for i, r in enumerate(out):
        direction = _direction(r)
        r["v8_direction"] = direction
        r["v8_turnover_percentile"] = turnover_rank[i]
        r["v8_breakout_strength_percentile"] = breakout_rank[i]
        r["v8_oi_strength_percentile"] = oi_strength_rank[i]

        participation = consensus_score([
            tod_rank[i], opening_rank[i], range_rank[i], gap_rank[i], turnover_rank[i]
        ])
        clv = directional_clv(r, direction) if direction else None
        structure = consensus_score([breakout_rank[i], clv]) if direction else None
        relative = relative_rank[i] if direction else None
        derivatives = directional_derivatives_score(
            direction or "Bullish",
            price_chg_pct=r.get("price_chg_60m_pct", r.get("price_chg_pct")),
            oi_chg_pct=r.get("oi_chg_60m_pct"),
            oi_strength_percentile=oi_strength_rank[i],
            basis_acceleration=r.get("basis_acceleration"),
        ) if direction else {"score": None, "oi_state": "OI Neutral/Unavailable"}
        alpha = consensus_score([structure, participation, relative, derivatives.get("score")]) if direction else None
        classification = classify_opportunity(r, alpha=alpha, participation=participation)

        r["v8_structure"] = structure
        r["v8_participation"] = participation
        r["v8_relative"] = relative
        r["v8_derivatives"] = derivatives.get("score")
        r["v8_oi_state"] = derivatives.get("oi_state")
        r["v8_alpha"] = alpha
        r["v8_state"] = classification["state"] if direction else "NO EDGE"
        r["v8_eligible"] = classification["eligible"] if direction else False
        r["v8_chased"] = classification["chased"] if direction else False

        reasons = list(classification.get("reasons") or [])
        if structure is not None and structure >= 80:
            reasons.append("Strong price acceptance")
        if relative is not None and relative >= 80:
            reasons.append("Relative leader" if direction == "Bullish" else "Relative laggard")
        if derivatives.get("score") is not None and derivatives["score"] >= 70:
            reasons.append(derivatives.get("oi_state"))
        r["v8_reasons"] = reasons[:5]

    # Alpha percentile/rank is itself useful for deterministic display order.
    for direction in ("Bullish", "Bearish"):
        idxs = [i for i, r in enumerate(out) if r.get("v8_direction") == direction and _finite(r.get("v8_alpha"))]
        ordered = sorted(idxs, key=lambda i: float(out[i]["v8_alpha"]), reverse=True)
        for rank, i in enumerate(ordered, 1):
            out[i]["v8_rank"] = rank
    return out


def build_live_leaderboards(rows: Iterable[dict], *, limit: int = 8) -> dict:
    """Return independently sorted bullish and bearish leaderboards."""
    usable = [dict(r) for r in (rows or []) if _finite(r.get("v8_alpha"))]
    priority = {"TRADE CANDIDATE": 2, "WATCH": 1, "NO EDGE": 0}

    def side(direction):
        candidates = [r for r in usable if r.get("v8_direction") == direction]
        candidates.sort(key=lambda r: (priority.get(r.get("v8_state"), 0), float(r.get("v8_alpha") or 0)), reverse=True)
        return candidates[: max(0, int(limit))]

    return {"bullish": side("Bullish"), "bearish": side("Bearish")}


def _json_number(value):
    if not _finite(value):
        return None
    return round(float(value), 3)


def _compact_dashboard_row(row: dict, *, swing: bool = False) -> dict:
    direction = row.get("v8_direction")
    alpha_key = "v8_swing_alpha" if swing else "v8_alpha"
    state_key = "v8_swing_state" if swing else "v8_state"
    return {
        "symbol": row.get("symbol"),
        "direction": direction,
        "alpha": _json_number(row.get(alpha_key)),
        "state": row.get(state_key) or "NO EDGE",
        "structure": _json_number(row.get("v8_structure")),
        "participation": _json_number(row.get("v8_participation")),
        "relative": _json_number(row.get("v8_relative")),
        "derivatives": _json_number(row.get("v8_derivatives")),
        "oi_state": row.get("v8_oi_state") or "OI Neutral/Unavailable",
        "extension_atr": _json_number(row.get("breakout_extension_atr")),
        "close": _json_number(row.get("close")),
        "tod_rvol": _json_number(row.get("tod_rvol")),
        "oi_chg_60m_pct": _json_number(row.get("oi_chg_60m_pct")),
        "swing_persistence": _json_number(row.get("v8_swing_persistence")) if swing else None,
        "reasons": [str(x) for x in (row.get("v8_reasons") or [])[:5]],
    }


def dashboard_payload(state: dict, *, limit: int = 8) -> dict:
    """Compact JSON-safe V8 decision-console payload for dynamic dashboard polling."""
    rows = [r for r in (state.get("results") or []) if not r.get("error")]
    intraday_boards = build_live_leaderboards(rows, limit=limit)
    priority = {"TRADE CANDIDATE": 2, "WATCH": 1, "NO EDGE": 0}

    def swing_side(direction):
        candidates = [
            r for r in rows
            if r.get("v8_direction") == direction and _finite(r.get("v8_swing_alpha"))
        ]
        candidates.sort(
            key=lambda r: (
                priority.get(r.get("v8_swing_state"), 0),
                float(r.get("v8_swing_alpha") or 0),
            ),
            reverse=True,
        )
        return [_compact_dashboard_row(r, swing=True) for r in candidates[: max(0, int(limit))]]

    intraday = {
        "bullish": [_compact_dashboard_row(r) for r in intraday_boards["bullish"]],
        "bearish": [_compact_dashboard_row(r) for r in intraday_boards["bearish"]],
    }
    swing = {"bullish": swing_side("Bullish"), "bearish": swing_side("Bearish")}
    return {
        "last_scan": state.get("last_scan"),
        "last_error": state.get("last_error"),
        "market": {
            "index_direction": state.get("index_direction"),
            "index_chg_pct": _json_number(state.get("index_chg_pct")),
            "regime": state.get("market_regime"),
        },
        "counts": {
            "universe": len(rows),
            "intraday_trade": sum(r.get("v8_state") == "TRADE CANDIDATE" for r in rows),
            "intraday_watch": sum(r.get("v8_state") == "WATCH" for r in rows),
            "swing_trade": sum(r.get("v8_swing_state") == "TRADE CANDIDATE" for r in rows),
            "swing_watch": sum(r.get("v8_swing_state") == "WATCH" for r in rows),
        },
        "intraday": intraday,
        "swing": swing,
    }
