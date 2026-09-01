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


def _ratio_score(bullish, bearish):
    total = float(bullish or 0) + float(bearish or 0)
    if total <= 0:
        return None
    return max(-100.0, min(100.0, (float(bullish or 0) - float(bearish or 0)) / total * 100.0))


def _factor_label(score):
    if score is None:
        return "Unavailable"
    if score >= 20.0:
        return "Bullish"
    if score <= -20.0:
        return "Bearish"
    return "Balanced"


def _multi_factor_regime(rows, *, index_direction=None, index_chg_pct=None, market_breadth=None, oi_breadth=None):
    """Return a signed -100..+100 market-regime score from independent live axes.

    Missing axes are omitted and the remaining weights are re-normalized, so a
    failed sector/index fetch can reduce coverage but can never manufacture a
    neutral vote.  Positive is bullish; negative is bearish.
    """
    factors = {}

    idx_parts = []
    if index_direction in ("Bullish", "Bearish"):
        idx_parts.append(60.0 if index_direction == "Bullish" else -60.0)
    idx_chg = _num(index_chg_pct)
    if idx_chg is not None:
        idx_parts.append(max(-100.0, min(100.0, idx_chg * 100.0)))
    factors["index"] = {"weight": 25.0, "score": (sum(idx_parts) / len(idx_parts)) if idx_parts else None}

    # True same-session price breadth comes directly from each stock's
    # close-vs-previous-close move.  The legacy background breadth is a
    # technical-direction proxy, so use it only as a fallback when price
    # change is unavailable.
    price_moves = [_num(row.get("price_chg_today_pct")) for row in rows]
    price_moves = [v for v in price_moves if v is not None and v != 0]
    price_bull = sum(v > 0 for v in price_moves)
    price_bear = sum(v < 0 for v in price_moves)
    price_breadth_score = _ratio_score(price_bull, price_bear)
    if price_breadth_score is None:
        mb = market_breadth or {}
        bull_pct, bear_pct = _num(mb.get("bullish_pct")), _num(mb.get("bearish_pct"))
        if bull_pct is not None and bear_pct is not None:
            price_breadth_score = max(-100.0, min(100.0, bull_pct - bear_pct))
        else:
            price_breadth_score = _ratio_score(mb.get("bullish"), mb.get("bearish"))
    factors["price_breadth"] = {"weight": 20.0, "score": price_breadth_score}

    ob = oi_breadth or {}
    oi_bull = (ob.get("long_buildup") or 0) + (ob.get("short_covering") or 0)
    oi_bear = (ob.get("short_buildup") or 0) + (ob.get("long_unwinding") or 0)
    factors["oi_breadth"] = {"weight": 20.0, "score": _ratio_score(oi_bull, oi_bear)}

    sector_map = {}
    for row in rows:
        sector = row.get("sector")
        direction = row.get("sector_direction")
        if sector and direction in ("Bullish", "Bearish"):
            sector_map[str(sector)] = direction
    sector_bull = sum(v == "Bullish" for v in sector_map.values())
    sector_bear = sum(v == "Bearish" for v in sector_map.values())
    factors["sector_breadth"] = {"weight": 15.0, "score": _ratio_score(sector_bull, sector_bear)}

    rs = [_num(row.get("rs_pct")) for row in rows]
    rs = [v for v in rs if v is not None and v != 0]
    rs_bull = sum(v > 0 for v in rs)
    rs_bear = sum(v < 0 for v in rs)
    factors["relative_strength"] = {"weight": 10.0, "score": _ratio_score(rs_bull, rs_bear)}

    above = below = 0
    for row in rows:
        side = str(row.get("vs_vwap") or "").strip().lower()
        if side == "above":
            above += 1
        elif side == "below":
            below += 1
        elif _num(row.get("vwap")) is not None and _num(row.get("close")) is not None:
            if _num(row.get("close")) > _num(row.get("vwap")):
                above += 1
            elif _num(row.get("close")) < _num(row.get("vwap")):
                below += 1
    factors["vwap"] = {"weight": 10.0, "score": _ratio_score(above, below)}

    available_weight = sum(v["weight"] for v in factors.values() if v["score"] is not None)
    if available_weight:
        score = sum(v["score"] * v["weight"] for v in factors.values() if v["score"] is not None) / available_weight
    else:
        score = 0.0
    score = round(max(-100.0, min(100.0, score)), 1)
    strength = round(abs(score), 1)
    bias = "Bullish" if score >= 20.0 else ("Bearish" if score <= -20.0 else "Balanced")
    regime_label = ("Strong " + bias) if bias != "Balanced" and strength >= 50.0 else bias
    coverage = round(available_weight, 1)
    for value in factors.values():
        value["score"] = round(value["score"], 1) if value["score"] is not None else None
        value["label"] = _factor_label(value["score"])
        value["available"] = value["score"] is not None
    return {
        "bias": bias,
        "regime_label": regime_label,
        "regime_score": score,
        "bias_strength_pct": strength,
        "regime_coverage_pct": coverage,
        "regime_factors": factors,
    }


def live_market_state(results, *, top_n=5, index_direction=None, index_chg_pct=None, market_breadth=None):
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

    regime = _multi_factor_regime(
        rows, index_direction=index_direction, index_chg_pct=index_chg_pct,
        market_breadth=market_breadth, oi_breadth=breadth,
    )
    bias = regime["bias"]

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
        "regime_label": regime["regime_label"],
        "regime_score": regime["regime_score"],
        "bias_strength_pct": regime["bias_strength_pct"],
        "regime_coverage_pct": regime["regime_coverage_pct"],
        "regime_factors": regime["regime_factors"],
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


def live_opportunity_radar(results, *, limit=5, index_direction=None, index_chg_pct=None, market_breadth=None):
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
    market = live_market_state(
        rows, top_n=0, index_direction=index_direction, index_chg_pct=index_chg_pct,
        market_breadth=market_breadth,
    )
    market_bias = market.get("bias")
    market_bias_strength = _num(market.get("bias_strength_pct"), 0.0)
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
            # Regime is context, never a veto.  Stronger multi-factor
            # agreement earns a little more ranking lift, capped so it can
            # never overwhelm the stock's own price/OI evidence.
            score += 3.0 + min(4.0, market_bias_strength / 25.0)
            reasons.append(f"Market regime {market_bias.lower()} {market_bias_strength:.0f}%")

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
            # V9.3 research-only horizon routing inputs.  These are copied
            # through unchanged so the Swing Research Console can distinguish
            # an active ignition from a quieter positioning/compression build.
            "compression_score": _num(row.get("compression_score")),
            "shadow_movement_stage": row.get("shadow_movement_stage"),
            "oi_z": _num(row.get("oi_z")),
            "price_move_60m_atr": _num(row.get("price_move_60m_atr")),
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
        "market_bias_strength_pct": market.get("bias_strength_pct"),
        "market_regime_score": market.get("regime_score"),
        "market_regime_factors": market.get("regime_factors"),
        "bullish": bullish,
        "bearish": bearish,
        "counts": {
            "bullish": len(buckets["Bullish"]),
            "bearish": len(buckets["Bearish"]),
            "displayed": len(bullish) + len(bearish),
        },
    }


def swing_research_console(radar, *, limit=5):
    """Route live opportunity names to a research-only 1D *or* 2D horizon.

    This is deliberately not a production classifier.  It makes the dashboard's
    1D/2D swing tab observable while V9.3 learns which holding period actually
    owns each precursor.  A fast ignition is routed to 1D; a quiet abnormal-OI
    / compression build is routed to 2D so the same symbol is never counted in
    both buckets.  The routing is descriptive and remains shadow-only.
    """
    radar = radar or {}
    out = {
        "label": "RESEARCH / SHADOW",
        "is_trade_signal": False,
        "market_bias": radar.get("market_bias"),
        "market_bias_strength_pct": radar.get("market_bias_strength_pct"),
        "1D": {"bullish": [], "bearish": []},
        "2D": {"bullish": [], "bearish": []},
    }

    for side_key, target_side in (("bullish", "bullish"), ("bearish", "bearish")):
        routed = {"1D": [], "2D": []}
        for raw in radar.get(side_key) or []:
            row = dict(raw)
            stage = str(row.get("shadow_movement_stage") or "")
            oi_z = _num(row.get("oi_z"))
            price_flat = _num(row.get("price_move_60m_atr"))
            compression = _num(row.get("compression_score"), 0.0)
            rvol = max(_num(row.get("tod_rvol"), 0.0), _num(row.get("vol_multiple"), 0.0))
            recent_oi = max(_num(row.get("oi_30m_chg_pct"), 0.0), _num(row.get("oi_60m_chg_pct"), 0.0))
            base = _num(row.get("score"), 0.0)
            silent_positioning = bool(oi_z is not None and oi_z >= 1.5 and price_flat is not None and abs(price_flat) <= 0.5)
            ignition = stage in ("Ignition", "Best Entry")

            score_1d = base + (8.0 if ignition else 0.0) + min(6.0, max(0.0, rvol - 1.0) * 3.0) + min(5.0, max(0.0, recent_oi) * 2.0)
            score_2d = base + (10.0 if silent_positioning else 0.0) + min(6.0, compression / 15.0)
            if row.get("htf_direction") == row.get("direction"):
                score_2d += 4.0
            if row.get("chase_guard") == "EXTENDED":
                score_1d -= 8.0
                score_2d -= 8.0

            # Quiet positioning/compression is intentionally allowed to win the
            # 2D route even if the generic opportunity score is already high.
            # Otherwise a real ignition belongs to 1D.
            horizon = "2D" if (silent_positioning and not ignition and score_2d >= score_1d) else "1D"
            row["research_horizon"] = horizon
            row["horizon_score"] = round(max(score_1d, score_2d), 1)
            row["horizon_reason"] = (
                "Quiet abnormal OI / compression build — allow more time"
                if horizon == "2D" else
                "Ignition / active participation — earlier swing resolution"
            )
            routed[horizon].append(row)

        for horizon in ("1D", "2D"):
            routed[horizon].sort(key=lambda x: (float(x.get("horizon_score") or 0.0), float(x.get("score") or 0.0)), reverse=True)
            out[horizon][target_side] = routed[horizon][:max(0, int(limit))]
    return out
