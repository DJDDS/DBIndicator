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


def live_market_state(results, *, top_n=5):
    """Summarize live F&O positioning without promoting any trade playbook.

    This is market-state telemetry only: OI breadth, directional positioning,
    current acceleration readiness, and compact ranked names. It deliberately
    does not create TRADE/WATCH candidates or bypass the V9 evidence gate.
    """
    rows = []
    for row in results or []:
        if row.get("error"):
            continue
        live_oi = row.get("oi_total") if row.get("oi_total") is not None else row.get("oi")
        if live_oi is None:
            continue
        rows.append(row)

    structures = {
        "long_buildup": "Long Buildup",
        "short_buildup": "Short Buildup",
        "short_covering": "Short Covering",
        "long_unwinding": "Long Unwinding",
    }
    breadth = {key: sum(r.get("oi_structure") == label for r in rows)
               for key, label in structures.items()}
    breadth["neutral"] = sum(r.get("oi_structure") not in structures.values() for r in rows)

    long_n = breadth["long_buildup"]
    short_n = breadth["short_buildup"]
    if long_n == 0 and short_n == 0:
        bias = "Neutral/Unavailable"
    elif long_n == 0:
        bias = "Bearish"
    elif short_n == 0:
        bias = "Bullish"
    elif short_n >= long_n * 1.25:
        bias = "Bearish"
    elif long_n >= short_n * 1.25:
        bias = "Bullish"
    else:
        bias = "Balanced"

    def item(row, *, score=None):
        return {
            "symbol": str(row.get("symbol") or ""),
            "structure": row.get("oi_structure"),
            "price_chg_pct": _num(row.get("price_chg_today_pct")),
            "oi_day_chg_pct": _num(row.get("oi_day_chg_pct")),
            "oi_30m_chg_pct": _num(row.get("oi_chg_30m_pct")),
            "vol_multiple": _num(row.get("vol_multiple")),
            "score": round(float(score), 4) if score is not None and math.isfinite(float(score)) else None,
        }

    expansion = sorted(
        [r for r in rows if _num(r.get("oi_day_chg_pct")) is not None],
        key=lambda r: abs(_num(r.get("oi_day_chg_pct"), 0.0)), reverse=True,
    )[:max(0, int(top_n))]

    confirmation = []
    for r in rows:
        if r.get("oi_structure") not in ("Long Buildup", "Short Buildup"):
            continue
        p = _num(r.get("price_chg_today_pct"))
        o = _num(r.get("oi_day_chg_pct"))
        if p is None or o is None:
            continue
        confirmation.append((abs(p) * abs(o), r))
    confirmation.sort(key=lambda x: x[0], reverse=True)

    volume_oi = []
    for r in rows:
        vol = _num(r.get("vol_multiple"))
        oi_move = max(
            abs(_num(r.get("oi_chg_30m_pct"), 0.0)),
            abs(_num(r.get("oi_day_chg_pct"), 0.0)),
        )
        if vol is None or vol < 1.0 or oi_move <= 0:
            continue
        volume_oi.append((vol * oi_move, r))
    volume_oi.sort(key=lambda x: x[0], reverse=True)

    readiness = oi_history_readiness(rows, min_tier=None)
    acceleration = {
        "strong": sum(r.get("oi_accel_label") == "Strong acceleration" for r in rows),
        "moderate": sum(r.get("oi_accel_label") == "Moderate acceleration" for r in rows),
        "ready_30m": readiness["ready_30m"],
        "ready_60m": readiness["ready_60m"],
        "eligible_with_oi": readiness["eligible_with_oi"],
        "warming_up": readiness["warming_up"],
    }

    return {
        "breadth": breadth,
        "bias": bias,
        "oi_expansion": [item(r) for r in expansion],
        "price_oi_confirmation": [item(r, score=score) for score, r in confirmation[:max(0, int(top_n))]],
        "volume_oi": [item(r, score=score) for score, r in volume_oi[:max(0, int(top_n))]],
        "acceleration": acceleration,
    }


def _opportunity_direction(row):
    """Direction for the non-production live opportunity radar.

    Fresh buildup gets the strongest structural prior. Unwinding/covering may
    still be worth attention but intentionally receives a smaller prior. The
    V8 cross-sectional direction is only a fallback when OI structure is not
    directional yet.
    """
    structure = row.get("oi_structure")
    if structure == "Short Buildup":
        return "Bearish", 20.0
    if structure == "Long Buildup":
        return "Bullish", 20.0
    if structure == "Long Unwinding":
        return "Bearish", 8.0
    if structure == "Short Covering":
        return "Bullish", 8.0
    direction = row.get("v8_direction") or row.get("failed_breakout_direction") or row.get("breakout_direction")
    if direction in ("Bullish", "Bearish"):
        return direction, 5.0
    return None, 0.0


def _opportunity_extension(row):
    for key in ("breakout_extension_atr", "retained_breakout_extension_atr", "failed_breakout_extension_atr"):
        value = _num(row.get(key))
        if value is not None:
            return value
    return None


def _opportunity_vwap_agrees(row, direction):
    direct = row.get("vwap_side_agrees")
    if isinstance(direct, bool):
        return direct
    vs_vwap = str(row.get("vs_vwap") or "").strip().lower()
    if not vs_vwap:
        return None
    if direction == "Bearish":
        return vs_vwap == "below"
    if direction == "Bullish":
        return vs_vwap == "above"
    return None


def _scaled_positive(value, full_scale, points):
    value = _num(value)
    if value is None or value <= 0 or full_scale <= 0:
        return 0.0
    return min(1.0, value / float(full_scale)) * float(points)


def live_opportunity_radar(results, *, limit=5):
    """Rank live Bull/Bear *attention* names without promoting a trade model.

    This layer is deliberately independent of ``ACTIVE_PLAYBOOKS``. It uses
    point-in-time facts already present in the live scan (price/OI direction,
    recent OI change, acceleration, participation, relative strength/weakness,
    VWAP acceptance and anti-chase distance) to answer a different question:
    "which F&O stocks deserve attention right now?"

    The returned score is an attention/ranking score, not a win probability and
    not a backtest-validated entry signal. The production V9 evidence gate stays
    untouched.
    """
    rows = [r for r in (results or []) if not r.get("error") and r.get("symbol")]
    market = live_market_state(rows, top_n=0)
    market_bias = market.get("bias")
    buckets = {"Bullish": [], "Bearish": []}

    for row in rows:
        direction, score = _opportunity_direction(row)
        if direction not in buckets:
            continue

        reasons = []
        structure = row.get("oi_structure")
        if structure:
            reasons.append(str(structure))

        price = _num(row.get("price_chg_today_pct"))
        price_aligned = price is not None and ((direction == "Bullish" and price > 0) or (direction == "Bearish" and price < 0))
        if price_aligned:
            score += _scaled_positive(abs(price), 2.0, 10.0)
            reasons.append(f"Price {price:+.2f}%")
        elif price is not None and price != 0:
            score -= min(8.0, abs(price) * 4.0)
            reasons.append(f"Price conflicts {price:+.2f}%")

        day_oi = _num(row.get("oi_day_chg_pct"))
        if day_oi is not None and day_oi > 0:
            score += _scaled_positive(day_oi, 8.0, 15.0)
            reasons.append(f"Day OI {day_oi:+.1f}%")

        recent_values = [
            _num(row.get("oi_chg_15m_pct")),
            _num(row.get("oi_chg_30m_pct")),
            _num(row.get("oi_chg_60m_pct")),
        ]
        recent_positive = max([v for v in recent_values if v is not None] or [0.0])
        if recent_positive > 0:
            score += _scaled_positive(recent_positive, 4.0, 15.0)
            reasons.append(f"Recent OI +{recent_positive:.1f}%")

        accel_label = str(row.get("oi_accel_label") or "")
        if accel_label == "Strong acceleration":
            score += 10.0
            reasons.append("Strong OI acceleration")
        elif accel_label == "Moderate acceleration":
            score += 6.0
            reasons.append("Moderate OI acceleration")
        else:
            accel = _num(row.get("oi_acceleration"))
            if accel is not None and accel > 0:
                score += min(5.0, accel * 2.5)

        vol_candidates = [_num(row.get("vol_multiple")), _num(row.get("tod_rvol"))]
        vol = max([v for v in vol_candidates if v is not None] or [0.0])
        if vol > 0:
            score += min(10.0, vol / 2.0 * 10.0)
            if vol >= 1.0:
                reasons.append(f"RVOL {vol:.2f}x")

        relative = _num(row.get("v8_relative"))
        if relative is not None:
            score += max(0.0, min(100.0, relative)) / 100.0 * 10.0
            if relative >= 75.0:
                reasons.append("Relative laggard" if direction == "Bearish" else "Relative leader")

        participation = _num(row.get("v8_participation"))
        if participation is not None:
            score += max(0.0, min(100.0, participation)) / 100.0 * 8.0
            if participation >= 75.0:
                reasons.append("High participation")

        technical = _num(row.get("v8_structure"))
        if technical is not None:
            score += max(0.0, min(100.0, technical)) / 100.0 * 7.0

        vwap_agrees = _opportunity_vwap_agrees(row, direction)
        if vwap_agrees is True:
            score += 5.0
            reasons.append("Below VWAP" if direction == "Bearish" else "Above VWAP")
        elif vwap_agrees is False:
            score -= 3.0

        htf_direction = row.get("htf_direction")
        if htf_direction == direction:
            score += 4.0
            reasons.append("4H context agrees")
        elif htf_direction in ("Bullish", "Bearish"):
            score -= 2.0
            reasons.append("4H context conflicts — not a veto")

        if market_bias == direction:
            score += 5.0
            reasons.append(f"F&O breadth {market_bias.lower()}")

        extension = _opportunity_extension(row)
        chase_guard = "OK"
        if extension is not None and extension > 1.25:
            score -= 15.0
            chase_guard = "EXTENDED"
            reasons.insert(0, "Extended >1.25 ATR — do not chase")

        score = round(max(0.0, min(100.0, score)), 1)
        status = "HIGH ATTENTION" if score >= 70.0 else ("BUILDING" if score >= 55.0 else "EARLY")
        if score < 40.0:
            continue

        item = {
            "symbol": str(row.get("symbol")),
            "direction": direction,
            "score": score,
            "status": status,
            "reasons": reasons[:10],
            "oi_structure": structure,
            "price_chg_pct": price,
            "oi_day_chg_pct": day_oi,
            "oi_30m_chg_pct": _num(row.get("oi_chg_30m_pct")),
            "oi_60m_chg_pct": _num(row.get("oi_chg_60m_pct")),
            "oi_acceleration": _num(row.get("oi_acceleration")),
            "oi_accel_label": row.get("oi_accel_label"),
            "vol_multiple": _num(row.get("vol_multiple")),
            "tod_rvol": _num(row.get("tod_rvol")),
            "relative": relative,
            "participation": participation,
            "technical": technical,
            "htf_direction": htf_direction,
            "vwap_agrees": vwap_agrees,
            "extension_atr": extension,
            "chase_guard": chase_guard,
        }
        buckets[direction].append(item)

    def order(items):
        items.sort(
            key=lambda item: (
                float(item.get("score") or 0.0),
                abs(float(item.get("oi_day_chg_pct") or 0.0)),
                abs(float(item.get("price_chg_pct") or 0.0)),
                item.get("symbol") or "",
            ),
            reverse=True,
        )
        return items[:max(0, int(limit))]

    bullish = order(buckets["Bullish"])
    bearish = order(buckets["Bearish"])
    return {
        "label": "RESEARCH / SHADOW",
        "is_trade_signal": False,
        "market_bias": market_bias,
        "bullish": bullish,
        "bearish": bearish,
        "counts": {
            "bullish": len(buckets["Bullish"]),
            "bearish": len(buckets["Bearish"]),
            "displayed": len(bullish) + len(bearish),
        },
    }
