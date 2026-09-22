"""Pure selection/diagnostic helpers for the OI Screener web view.

Kept free of Flask/Kite imports so the ranking rules can be regression-tested
without a live broker session.
"""

import datetime as dt
import math

from .early_onset import assess_early_onset


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


def _parse_dt(value):
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _early_lifecycle(item, row, lifecycle_state, now):
    """Attach persistent first-scout maturity so a mature move cannot reset.

    The old radar could become "early" again after a large move paused for an
    hour because only rolling 60-minute travel was observed.  This state keeps
    the first scout price/time across scans and blocks re-entry into the early
    lane once meaningful runway has already been consumed.
    """
    if lifecycle_state is None or now is None:
        return item

    symbol = str(item.get("symbol") or "")
    direction = str(item.get("direction") or "")
    if not symbol or direction not in ("Bullish", "Bearish"):
        return item

    key = f"{symbol}|{direction}"
    close = _num(row.get("close"))
    atr = _num(row.get("atr"))
    state = dict(lifecycle_state.get(key) or {})
    first_seen = _parse_dt(state.get("first_seen_at"))
    cooldown = _parse_dt(state.get("cooldown_until"))

    if cooldown is not None:
        if cooldown.tzinfo is not None and now.tzinfo is None:
            cooldown = cooldown.replace(tzinfo=None)
        elif cooldown.tzinfo is None and now.tzinfo is not None:
            cooldown = cooldown.replace(tzinfo=now.tzinfo)

    # Start the maturity clock at hidden SCOUT, not at the visible alert.
    # That makes the system measure how much of the move was already consumed
    # before the user ever sees READY/FRESH_BREAK.
    if item.get("scout_eligible") and close is not None and atr is not None and atr > 0:
        if first_seen is None or (cooldown is not None and now >= cooldown and not state.get("active", True)):
            state = {
                "first_seen_at": now.isoformat(timespec="seconds"),
                "first_seen_price": close,
                "atr_at_first_seen": atr,
                "direction": direction,
                "active": True,
                "last_seen_at": now.isoformat(timespec="seconds"),
            }
            first_seen = now
        else:
            state["last_seen_at"] = now.isoformat(timespec="seconds")
            state["active"] = True

    anchor = _num(state.get("first_seen_price"))
    anchor_atr = _num(state.get("atr_at_first_seen"), atr)
    signed = None
    if close is not None and anchor is not None and anchor_atr is not None and anchor_atr > 0:
        sign = 1.0 if direction == "Bullish" else -1.0
        signed = sign * (close - anchor) / anchor_atr
        item["move_since_first_scout_atr"] = round(signed, 3)

    if first_seen is not None:
        if first_seen.tzinfo is not None and now.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=None)
        elif first_seen.tzinfo is None and now.tzinfo is not None:
            first_seen = first_seen.replace(tzinfo=now.tzinfo)
        try:
            item["first_scout_age_min"] = round(max(0.0, (now - first_seen).total_seconds() / 60.0), 1)
        except TypeError:
            pass
        item["first_scout_at"] = state.get("first_seen_at")

    # Hard maturity guard.  This is intentionally about consumed opportunity,
    # not a score penalty.  Once the first scout has already delivered a large
    # favourable displacement, the stock leaves this EARLY radar.  A later
    # pullback/reclaim may still be traded by the separate 3m tactical engine.
    if signed is not None and signed > 0.75:
        item["early_eligible"] = False
        item["early_state"] = "LATE"
        item["action_stage"] = "LATE"
        item["phase"] = "EXTENDED"
        item["maturity"] = "MISSED"
        reasons = list(item.get("late_reasons") or [])
        reasons.append(f"{signed:.2f} ATR since first scout")
        item["late_reasons"] = reasons
        state["active"] = False
        state["matured_at"] = now.isoformat(timespec="seconds")
        state["cooldown_until"] = (now + dt.timedelta(minutes=45)).isoformat(timespec="seconds")

    # A scout that immediately goes materially the wrong way is no longer a
    # useful early-direction candidate; downstream 3m reversal logic can find a
    # fresh opposite setup independently.
    if signed is not None and signed < -0.45:
        item["early_eligible"] = False
        item["scout_eligible"] = False
        item["early_state"] = "FADING"
        item["action_stage"] = "FADING"
        item["phase"] = "FADING"
        item["maturity"] = "FAILED"
        state["active"] = False
        state["cooldown_until"] = (now + dt.timedelta(minutes=20)).isoformat(timespec="seconds")

    lifecycle_state[key] = state
    return item


def _cleanup_early_lifecycle(lifecycle_state, now):
    if lifecycle_state is None or now is None:
        return
    cutoff = now - dt.timedelta(hours=3)
    for key in list(lifecycle_state):
        last = _parse_dt((lifecycle_state.get(key) or {}).get("last_seen_at"))
        if last is None:
            continue
        if last.tzinfo is not None and cutoff.tzinfo is None:
            last = last.replace(tzinfo=None)
        elif last.tzinfo is None and cutoff.tzinfo is not None:
            last = last.replace(tzinfo=cutoff.tzinfo)
        if last < cutoff:
            lifecycle_state.pop(key, None)


def live_opportunity_radar(
    results, *, limit=5, index_direction=None, index_chg_pct=None,
    market_breadth=None, lifecycle_state=None, now=None,
):
    """Concrete EARLY-move radar; not a generic ranking of already-moving stocks.

    Visible rows must be FORMING, READY or a very fresh break.  A wider hidden
    SCOUT lane feeds the bounded V12.2B 3-minute/depth engine before the visible
    bar is mature enough to show.  Large trigger overshoots, extended hourly
    travel and persistent first-scout maturity are hard exclusions rather than
    score penalties.

    The radar is still research/shadow: it surfaces evidence and exact maturity
    states but does not place orders or promote any frozen playbook.
    """
    rows = [r for r in (results or []) if not r.get("error") and r.get("symbol")]
    market = live_market_state(
        rows, top_n=0, index_direction=index_direction, index_chg_pct=index_chg_pct,
        market_breadth=market_breadth,
    )
    market_bias = market.get("bias")
    market_bias_strength = _num(market.get("bias_strength_pct"), 0.0)
    buckets = {"Bullish": [], "Bearish": []}
    scouts = {"Bullish": [], "Bearish": []}
    hidden_mature = 0
    hidden_fading = 0

    for row in rows:
        direction, _ = _opportunity_direction(row)
        if direction not in buckets:
            continue

        # Legacy score survives only as a fallback field for old consumers.
        # It does not decide visibility in V12.2C.
        legacy_score = 0.0
        structure = row.get("oi_structure")
        if structure in ("Long Buildup", "Short Buildup"):
            legacy_score += 20.0
        elif structure in ("Short Covering", "Long Unwinding"):
            legacy_score += 8.0

        onset = assess_early_onset(row, direction, fallback_score=legacy_score)
        early_state = str(onset.get("early_state") or "OBSERVE")
        scout_eligible = bool(onset.get("scout_eligible"))
        early_eligible = bool(onset.get("early_eligible"))

        reasons = []
        oi15 = _num(onset.get("fresh_oi_15m_pct"))
        if oi15 is not None:
            reasons.append(f"OI15 {oi15:+.2f}%")
        if onset.get("oi_accelerating_now"):
            reasons.append("OI accelerating now")
        if onset.get("participation_accelerating_now"):
            reasons.append("Participation accelerating")
        elif onset.get("participation_active_now"):
            reasons.append("Participation active")
        compression = _num(row.get("compression_score"))
        if compression is not None and compression >= 50:
            reasons.append("Compressed / coiled")
        dist = _num(onset.get("trigger_distance_atr"))
        if dist is not None:
            if dist >= 0:
                reasons.append(f"{dist:.2f} ATR to trigger")
            else:
                reasons.append(f"{abs(dist):.2f} ATR past trigger")
        if market_bias == direction and market_bias_strength >= 55:
            reasons.append(f"Market context agrees {market_bias_strength:.0f}%")

        item = {
            "symbol": str(row.get("symbol")),
            "direction": direction,
            "score": onset.get("score"),
            "status": onset.get("action_stage"),
            "phase": onset.get("phase"),
            "early_state": early_state,
            "early_eligible": early_eligible,
            "scout_eligible": scout_eligible,
            "maturity": onset.get("maturity"),
            "late_reasons": list(onset.get("late_reasons") or []),
            "action_stage": onset.get("action_stage"),
            "pressure": onset.get("pressure"),
            "onset_coverage": onset.get("coverage"),
            "runway": onset.get("runway"),
            "runway_label": onset.get("runway_label"),
            "price_move_60m_atr": onset.get("price_move_60m_atr"),
            "day_move_atr": onset.get("day_move_atr"),
            "extension_atr": onset.get("extension_atr"),
            "trigger_level": onset.get("trigger_level"),
            "trigger_distance_atr": onset.get("trigger_distance_atr"),
            "trigger_overshoot_atr": onset.get("trigger_overshoot_atr"),
            "trigger_crossed": onset.get("trigger_crossed"),
            "oi_concentration_pct": onset.get("oi_concentration_pct"),
            "oi_15m_chg_pct": onset.get("fresh_oi_15m_pct"),
            "oi_30m_chg_pct": onset.get("fresh_oi_30m_pct"),
            "oi_acceleration": _num(row.get("oi_acceleration")),
            "oi_accel_label": row.get("oi_accel_label"),
            "oi_structure": structure,
            "tod_rvol": _num(row.get("tod_rvol")),
            "vol_multiple": _num(row.get("vol_multiple")),
            "relative": _num(row.get("v8_relative")),
            "participation": _num(row.get("v8_participation")),
            "technical": _num(row.get("v8_structure")),
            "htf_direction": row.get("htf_direction"),
            "vwap_agrees": _opportunity_vwap_agrees(row, direction),
            "compression_score": compression,
            "shadow_movement_stage": row.get("shadow_movement_stage"),
            "oi_z": _num(row.get("oi_z")),
            "reasons": reasons[:8],
        }
        item = _early_lifecycle(item, row, lifecycle_state, now)

        if item.get("early_state") == "LATE":
            hidden_mature += 1
        elif item.get("early_state") == "FADING":
            hidden_fading += 1

        # Hidden SCOUT is wider than what the user sees; it starts 3m/depth
        # observation before the 15m bar has fully earned READY status.
        if item.get("scout_eligible") and item.get("early_state") not in ("LATE", "FADING"):
            scouts[direction].append(dict(item))

        # Visible radar has a hard maturity contract.
        if item.get("early_eligible") and item.get("early_state") in ("FORMING", "READY", "FRESH_BREAK"):
            buckets[direction].append(item)

    _cleanup_early_lifecycle(lifecycle_state, now)

    def order_visible(items):
        state_priority = {"READY": 4, "FRESH_BREAK": 3, "FORMING": 2}
        items.sort(
            key=lambda item: (
                state_priority.get(str(item.get("early_state") or ""), 0),
                float(item.get("pressure") or 0.0),
                float(item.get("runway") or 0.0),
                -abs(float(item.get("trigger_distance_atr") or 0.0)),
            ),
            reverse=True,
        )
        return items[:max(0, int(limit))]

    def order_scout(items):
        state_priority = {"READY": 5, "FRESH_BREAK": 4, "FORMING": 3, "OBSERVE": 1}
        items.sort(
            key=lambda item: (
                state_priority.get(str(item.get("early_state") or ""), 1),
                float(item.get("pressure") or 0.0),
                float(item.get("runway") or 0.0),
            ),
            reverse=True,
        )
        return items[:max(8, int(limit) * 2)]

    bullish = order_visible(buckets["Bullish"])
    bearish = order_visible(buckets["Bearish"])
    scout_bullish = order_scout(scouts["Bullish"])
    scout_bearish = order_scout(scouts["Bearish"])
    return {
        "label": "EARLY MOVE · RESEARCH / SHADOW",
        "is_trade_signal": False,
        "market_bias": market_bias,
        "market_bias_strength_pct": market.get("bias_strength_pct"),
        "market_regime_score": market.get("regime_score"),
        "market_regime_factors": market.get("regime_factors"),
        "bullish": bullish,
        "bearish": bearish,
        # Internal feeder lanes. UI ignores them; V12.2B consumes them.
        "scout_bullish": scout_bullish,
        "scout_bearish": scout_bearish,
        "counts": {
            "bullish": len(buckets["Bullish"]),
            "bearish": len(buckets["Bearish"]),
            "displayed": len(bullish) + len(bearish),
            "scout": len(scouts["Bullish"]) + len(scouts["Bearish"]),
            "hidden_mature": hidden_mature,
            "hidden_fading": hidden_fading,
        },
    }


def overlay_tactical_radar(radar, tactical, *, limit=5):
    """Fuse live 3m/depth states into the visible early radar.

    This is a presentation overlay only.  The underlying 15m radar still feeds
    research/forward logging unchanged.  A hidden scout that reaches a concrete
    3m READY/TRADEABLE state can surface immediately instead of waiting for the
    next 15m scan cycle.
    """
    base = dict(radar or {})
    bulls = [dict(x) for x in (base.get("bullish") or [])]
    bears = [dict(x) for x in (base.get("bearish") or [])]
    buckets = {"Bullish": bulls, "Bearish": bears}
    by_key = {}
    for direction, rows in buckets.items():
        for row in rows:
            by_key[(str(row.get("symbol")), direction)] = row

    promote_states = {"READY", "TRIGGERED", "TRADEABLE"}
    for trow in list((tactical or {}).get("candidates") or []):
        direction = str(trow.get("direction") or "")
        symbol = str(trow.get("symbol") or "")
        state = str(trow.get("state") or "")
        if direction not in buckets or not symbol:
            continue

        key = (symbol, direction)
        existing = by_key.get(key)
        if existing is None and state in promote_states:
            existing = {
                "symbol": symbol,
                "direction": direction,
                "early_state": "READY" if state == "READY" else "FRESH_BREAK",
                "early_eligible": True,
                "scout_eligible": True,
                "maturity": "3M CONFIRMED",
                "phase": "PRE-IGNITION" if state == "READY" else "IGNITION",
                "action_stage": "ARMED" if state == "READY" else "TRIGGERED",
                "pressure": trow.get("candidate_pressure"),
                "runway": trow.get("candidate_runway"),
                "reasons": ["3m tactical structure active"],
            }
            buckets[direction].append(existing)
            by_key[key] = existing

        if existing is None:
            continue
        existing["tactical_state"] = state
        existing["tactical_setup"] = trow.get("setup")
        existing["tactical_trigger"] = trow.get("trigger")
        existing["tactical_invalidation"] = trow.get("invalidation")
        existing["tactical_live_price"] = trow.get("live_price")
        existing["tactical_rvol_3m"] = trow.get("rvol_3m")
        existing["tactical_relative_3m"] = trow.get("relative_3m_vs_nifty_pct")
        existing["tactical_depth_support"] = ((trow.get("depth") or {}).get("support_fraction"))
        existing["tactical_basis_change"] = ((trow.get("basis") or {}).get("basis_change_60s_pct_points"))
        existing["tactical_reason"] = trow.get("reason")
        contract = ((trow.get("option_route") or {}).get("contract") or {})
        existing["tactical_option_contract"] = contract.get("symbol")
        existing["tactical_option_dte"] = contract.get("dte")
        existing["tactical_option_spread_pct"] = contract.get("spread_pct")
        existing["tactical_tradeable"] = bool(trow.get("tradeable"))

    tactical_priority = {"TRADEABLE": 6, "TRIGGERED": 5, "READY": 4}
    early_priority = {"READY": 4, "FRESH_BREAK": 3, "FORMING": 2}
    for direction in ("Bullish", "Bearish"):
        buckets[direction].sort(
            key=lambda row: (
                tactical_priority.get(str(row.get("tactical_state") or ""), 0),
                early_priority.get(str(row.get("early_state") or ""), 0),
                float(row.get("pressure") or 0.0),
            ),
            reverse=True,
        )
        buckets[direction] = buckets[direction][:max(0, int(limit))]

    base["bullish"] = buckets["Bullish"]
    base["bearish"] = buckets["Bearish"]
    counts = dict(base.get("counts") or {})
    counts["displayed"] = len(base["bullish"]) + len(base["bearish"])
    counts["bullish"] = len(base["bullish"])
    counts["bearish"] = len(base["bearish"])
    base["counts"] = counts
    return base

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
