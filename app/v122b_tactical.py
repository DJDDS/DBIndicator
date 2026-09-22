"""V12.2B tactical stock-option execution primitives.

This module is deliberately pure: no Flask, no Kite client and no filesystem.
It turns an already-selected 15-minute stock candidate plus bounded 3-minute
and live quote evidence into deterministic tactical states.  It does NOT place
orders and it does not write into Trial-25, V12, V12.1 or any frozen research
artifact.

Design contract (audit-approved shape):
    15m discovery -> bounded pool -> 3m structure -> live futures witness
    -> DTE/contract route -> hard vetoes -> decision-support state.

The constants below are explicitly PROPOSED operating priors for the interim
engine, not validated alpha thresholds.  They are kept in one place so forward
evidence can evaluate them without silent UI tuning.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Iterable

IST_OFFSET = dt.timedelta(hours=5, minutes=30)

TACTICAL_POOL_MAX = 8
TACTICAL_POOL_PER_SIDE = 4
FAILED_BREAK_RESEARCH_ONLY = True

# Audit response: friction should be a fraction of the expected option move,
# not a vague spread label.  30% is the high end of the auditor's proposed
# 25-30% prior and is intentionally labelled PROPOSED.
PROPOSED_MAX_FRICTION_TO_EXPECTED_MOVE = 0.30

# The time stop is signal-decay / opportunity-cost control, not "theta over
# nine minutes".  It varies by speed class.
PROPOSED_FOLLOWTHROUGH_BARS = {
    "IMPULSE": 3,        # 9 minutes on 3m bars
    "CONTINUATION": 5,   # 15 minutes
    "SWING": None,       # never judged by an intraday 6-9m clock
}
PROPOSED_MIN_FOLLOWTHROUGH_FRACTION = 0.25
PROPOSED_PROFIT_PROTECT_FRACTION = 0.50

PROPOSED_PRE_RESULT_NEXT_MONTH_DTE = 8
PROPOSED_MAX_SAME_DIRECTION_ACTIVE = 2
PROPOSED_STALE_SECONDS = 8.0
PROPOSED_MAX_BASIS_SKEW_SECONDS = 2.5
PROPOSED_MICRO_PERSIST_SECONDS = 30.0
PROPOSED_OPPOSING_DEPTH_PERSISTENCE = 0.60


def _f(value: Any, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _i(value: Any, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dt(value: Any):
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _date(value: Any):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value:
        try:
            return dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            pass
    return None


def _sign(direction: str) -> int:
    return 1 if direction == "Bullish" else (-1 if direction == "Bearish" else 0)


def _candidate_direction(row: dict) -> str | None:
    direction = (
        row.get("direction")
        or row.get("trade_direction")
        or row.get("v8_direction")
        or row.get("failed_breakout_direction")
    )
    return direction if direction in ("Bullish", "Bearish") else None


def _candidate_rank_tuple(row: dict) -> tuple:
    """Rank only to bound API work; the rank is not a trade score."""
    phase = str(row.get("phase") or "")
    stage = str(row.get("action_stage") or row.get("status") or "")
    phase_rank = {
        "PRE-IGNITION": 6,
        "IGNITION": 5,
        "QUIET": 2,
        "UNDERWAY": 1,
        "EXTENDED": 0,
        "FADING": -1,
    }.get(phase, 1)
    stage_rank = {"ARMED": 4, "FORMING": 3, "TRIGGERED": 5}.get(stage, 1)
    pressure = _f(row.get("pressure"), _f(row.get("score"), 0.0))
    runway = _f(row.get("runway"), 0.0)
    return (phase_rank, stage_rank, pressure, runway, str(row.get("symbol") or ""))


def select_tactical_pool(radar: dict, results: Iterable[dict], *, max_pool: int = TACTICAL_POOL_MAX) -> list[dict]:
    """Return a two-sided bounded pool without turning rank into an entry gate."""
    by_symbol = {str(r.get("symbol")): r for r in (results or []) if r.get("symbol") and not r.get("error")}
    per_side = max(1, min(TACTICAL_POOL_PER_SIDE, int(max_pool) // 2 or 1))
    out: list[dict] = []
    for key, direction in (("bullish", "Bullish"), ("bearish", "Bearish")):
        rows = []
        for item in list((radar or {}).get(key) or []):
            symbol = str(item.get("symbol") or "")
            base = by_symbol.get(symbol)
            if not base:
                continue
            merged = dict(base)
            merged.update(item)
            merged["direction"] = direction
            if str(merged.get("phase") or "") in ("EXTENDED", "FADING"):
                continue
            rows.append(merged)
        rows.sort(key=_candidate_rank_tuple, reverse=True)
        out.extend(rows[:per_side])
    return out[:max_pool]


def earnings_context(earnings_state: dict | None, symbol: str, now: dt.datetime) -> dict:
    """Classify only official NSE board-meeting observations as CONFIRMED.

    If the point-in-time calendar has not refreshed within 30 hours, confidence
    is degraded to STALE and the strict pre-result route is not activated.
    """
    state = earnings_state or {}
    event = dict((state.get("events") or {}).get(str(symbol)) or {})
    meeting = _date(event.get("meeting_date"))
    refresh = _dt(state.get("last_refresh_at"))
    refresh_age_h = None
    if refresh is not None:
        if refresh.tzinfo is not None and now.tzinfo is None:
            refresh = refresh.replace(tzinfo=None)
        elif refresh.tzinfo is None and now.tzinfo is not None:
            refresh = refresh.replace(tzinfo=now.tzinfo)
        try:
            refresh_age_h = max(0.0, (now - refresh).total_seconds() / 3600.0)
        except TypeError:
            refresh_age_h = None

    active = event.get("state") in ("ACTIVE", "REVISED")
    official = bool(active and meeting is not None and state.get("status") == "OK")
    stale = refresh_age_h is None or refresh_age_h > 30.0
    confidence = "UNKNOWN"
    if official and not stale:
        confidence = "CONFIRMED"
    elif official and stale:
        confidence = "STALE"

    days = (meeting - now.date()).days if meeting is not None else None
    pre_result = confidence == "CONFIRMED" and days is not None and 0 <= days <= 15
    return {
        "confidence": confidence,
        "meeting_date": meeting.isoformat() if meeting else None,
        "days_to_result": days,
        "pre_result": pre_result,
        "calendar_refresh_age_hours": round(refresh_age_h, 2) if refresh_age_h is not None else None,
        "event_state": event.get("state"),
        "purpose": event.get("purpose"),
    }


def weighted_depth_metrics(tick: dict | None) -> dict:
    """Compute L1/L5 imbalance, spread and microprice from genuine Kite depth."""
    depth = (tick or {}).get("depth") or {}
    buys = [x for x in list(depth.get("buy") or [])[:5] if isinstance(x, dict)]
    sells = [x for x in list(depth.get("sell") or [])[:5] if isinstance(x, dict)]

    def qty(level):
        return max(0.0, _f(level.get("quantity"), 0.0))

    def px(level):
        p = _f(level.get("price"))
        return p if p is not None and p > 0 else None

    bid = px(buys[0]) if buys else None
    ask = px(sells[0]) if sells else None
    bidq = qty(buys[0]) if buys else 0.0
    askq = qty(sells[0]) if sells else 0.0

    l1 = None
    denom = bidq + askq
    if denom > 0:
        l1 = (bidq - askq) / denom

    # Nearest levels matter most, but all five remain observable.
    weights = (1.0, 0.75, 0.55, 0.40, 0.30)
    wb = sum(weights[i] * qty(x) for i, x in enumerate(buys[:5]))
    ws = sum(weights[i] * qty(x) for i, x in enumerate(sells[:5]))
    l5 = (wb - ws) / (wb + ws) if wb + ws > 0 else None

    midpoint = spread = spread_bps = microprice = micro_bias_bps = None
    if bid is not None and ask is not None and ask >= bid and bid > 0:
        midpoint = (bid + ask) / 2.0
        spread = ask - bid
        spread_bps = spread / midpoint * 10000.0 if midpoint else None
        if bidq + askq > 0:
            # More bid quantity pushes microprice toward the ask.
            microprice = (ask * bidq + bid * askq) / (bidq + askq)
            micro_bias_bps = (microprice - midpoint) / midpoint * 10000.0 if midpoint else None

    return {
        "l1_imbalance": round(l1, 6) if l1 is not None else None,
        "l5_imbalance": round(l5, 6) if l5 is not None else None,
        "best_bid": bid,
        "best_ask": ask,
        "spread": spread,
        "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
        "microprice": round(microprice, 6) if microprice is not None else None,
        "microprice_bias_bps": round(micro_bias_bps, 3) if micro_bias_bps is not None else None,
    }


def depth_persistence(samples: Iterable[dict], direction: str, *, now: dt.datetime, seconds: float = PROPOSED_MICRO_PERSIST_SECONDS) -> dict:
    sign = _sign(direction)
    if sign == 0:
        return {"count": 0, "support_fraction": None, "oppose_fraction": None}
    cutoff = now - dt.timedelta(seconds=float(seconds))
    vals = []
    for sample in samples or []:
        ts = _dt(sample.get("ts"))
        if ts is None:
            continue
        if ts.tzinfo is not None and now.tzinfo is None:
            ts = ts.replace(tzinfo=None)
        if ts < cutoff:
            continue
        v = _f(sample.get("l5_imbalance"))
        if v is not None:
            vals.append(v * sign)
    if not vals:
        return {"count": 0, "support_fraction": None, "oppose_fraction": None}
    support = sum(v > 0 for v in vals) / len(vals)
    oppose = sum(v < 0 for v in vals) / len(vals)
    return {
        "count": len(vals),
        "support_fraction": round(support, 3),
        "oppose_fraction": round(oppose, 3),
        "mean_signed_l5": round(sum(vals) / len(vals), 6),
    }


def synchronized_basis(cash: dict | None, future: dict | None, *, max_skew_seconds: float = PROPOSED_MAX_BASIS_SKEW_SECONDS) -> dict:
    cp = _f((cash or {}).get("last_price"))
    fp = _f((future or {}).get("last_price"))
    ct = _dt((cash or {}).get("_received_at") or (cash or {}).get("exchange_timestamp"))
    ft = _dt((future or {}).get("_received_at") or (future or {}).get("exchange_timestamp"))
    if cp is None or fp is None or cp <= 0 or ct is None or ft is None:
        return {"valid": False, "basis_pct": None, "skew_seconds": None}
    if ct.tzinfo is not None and ft.tzinfo is None:
        ft = ft.replace(tzinfo=ct.tzinfo)
    elif ct.tzinfo is None and ft.tzinfo is not None:
        ct = ct.replace(tzinfo=ft.tzinfo)
    skew = abs((ct - ft).total_seconds())
    if skew > float(max_skew_seconds):
        return {"valid": False, "basis_pct": None, "skew_seconds": round(skew, 3)}
    basis = (fp - cp) / cp * 100.0
    return {"valid": True, "basis_pct": round(basis, 6), "skew_seconds": round(skew, 3)}


@dataclass
class ThreeMinuteBarBuilder:
    """Build 3-minute OHLCV bars from streamed last price + cumulative volume."""
    current: dict | None = None
    previous_cum_volume: float | None = None

    @staticmethod
    def bucket_start(ts: dt.datetime) -> dt.datetime:
        minute = (ts.minute // 3) * 3
        return ts.replace(minute=minute, second=0, microsecond=0)

    def update(self, price: float, cumulative_volume: float | None, ts: dt.datetime) -> dict | None:
        price = _f(price)
        if price is None or price <= 0:
            return None
        bucket = self.bucket_start(ts)
        cum = _f(cumulative_volume)
        delta_vol = 0.0
        if cum is not None and self.previous_cum_volume is not None and cum >= self.previous_cum_volume:
            delta_vol = cum - self.previous_cum_volume
        self.previous_cum_volume = cum if cum is not None else self.previous_cum_volume

        completed = None
        if self.current is None or self.current.get("bucket") != bucket:
            if self.current is not None:
                completed = dict(self.current)
                completed["complete"] = True
            self.current = {
                "bucket": bucket,
                "ts": bucket.isoformat(timespec="seconds"),
                "open": price, "high": price, "low": price, "close": price,
                "volume": max(0.0, delta_vol),
                "complete": False,
            }
        else:
            self.current["high"] = max(float(self.current["high"]), price)
            self.current["low"] = min(float(self.current["low"]), price)
            self.current["close"] = price
            self.current["volume"] = float(self.current.get("volume") or 0.0) + max(0.0, delta_vol)
        return completed


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


def _session_vwap(bars: list[dict]) -> list[float | None]:
    out = []
    pv = vol = 0.0
    current_day = None
    for bar in bars:
        ts = _dt(bar.get("ts"))
        day = ts.date() if ts else current_day
        if current_day is not None and day != current_day:
            pv = vol = 0.0
        current_day = day
        v = max(0.0, _f(bar.get("volume"), 0.0))
        typical = (_f(bar.get("high"), 0.0) + _f(bar.get("low"), 0.0) + _f(bar.get("close"), 0.0)) / 3.0
        pv += typical * v
        vol += v
        out.append(pv / vol if vol > 0 else None)
    return out


def fast_trend_veto(bars: list[dict], direction: str) -> dict:
    """9EMA + VWAP slope is a veto/witness only, never an entry trigger."""
    if len(bars) < 10:
        return {"veto": False, "reason": None, "ema9": None, "vwap": None}
    closes = [_f(b.get("close")) for b in bars]
    if any(x is None for x in closes):
        return {"veto": False, "reason": None, "ema9": None, "vwap": None}
    ema9 = _ema([float(x) for x in closes], 9)
    vwaps = _session_vwap(bars)
    v0, v1 = vwaps[-1], vwaps[-2] if len(vwaps) >= 2 else None
    price = closes[-1]
    sign = _sign(direction)
    ema_slope = ema9[-1] - ema9[-2]
    vwap_slope = None if v0 is None or v1 is None else v0 - v1

    wrong_ema = sign * (price - ema9[-1]) < 0 and sign * ema_slope < 0
    wrong_vwap = v0 is not None and sign * (price - v0) < 0 and vwap_slope is not None and sign * vwap_slope < 0
    veto = bool(wrong_ema and wrong_vwap)
    return {
        "veto": veto,
        "reason": "3m price/EMA9 and VWAP slope both oppose the candidate" if veto else None,
        "ema9": round(ema9[-1], 4),
        "ema9_slope": round(ema_slope, 6),
        "vwap": round(v0, 4) if v0 is not None else None,
        "vwap_slope": round(vwap_slope, 6) if vwap_slope is not None else None,
    }


def _opening_range(bars: list[dict]) -> tuple[float | None, float | None]:
    session = []
    for b in bars:
        ts = _dt(b.get("ts"))
        if ts is None:
            continue
        mins = ts.hour * 60 + ts.minute
        if 9 * 60 + 15 <= mins < 9 * 60 + 30:
            session.append(b)
    if not session:
        return None, None
    return max(_f(b.get("high"), -math.inf) for b in session), min(_f(b.get("low"), math.inf) for b in session)


def detect_structural_setup(
    completed_bars: list[dict],
    current_bar: dict | None,
    candidate: dict,
    *,
    now: dt.datetime,
) -> dict:
    """Detect one of four structural setups from 3m bars.

    FAILED_BREAK_REVERSAL is observable but research-only in the first build.
    """
    direction = _candidate_direction(candidate)
    sign = _sign(direction or "")
    if sign == 0 or len(completed_bars) < 2 or not current_bar:
        return {"setup": None, "ready": False, "triggered": False}

    price = _f(current_bar.get("close"))
    atr = _f(candidate.get("atr"))
    if price is None:
        return {"setup": None, "ready": False, "triggered": False}

    last2 = completed_bars[-2:]
    hi2 = max(_f(x.get("high"), price) for x in last2)
    lo2 = min(_f(x.get("low"), price) for x in last2)
    range_height = max(0.0, hi2 - lo2)

    # 1) Micro breakout: compact completed range, live price crossing edge.
    micro_trigger = hi2 if sign > 0 else lo2
    micro_invalid = lo2 if sign > 0 else hi2
    micro_distance = sign * (micro_trigger - price)
    micro_ready = micro_distance >= -0.10 * max(atr or 0.0, 1e-9) and micro_distance <= 0.25 * max(atr or range_height or 1.0, 1e-9)
    micro_triggered = sign * (price - micro_trigger) > 0

    # 2) Pullback/reclaim around the existing 15m structural reference.
    reference = _f(
        candidate.get("trigger_level"),
        _f(candidate.get("breakout_level"), _f(candidate.get("retained_breakout_level"))),
    )
    pullback_ready = pullback_triggered = False
    pullback_invalid = None
    if reference is not None:
        touched = any((_f(b.get("low"), price) <= reference if sign > 0 else _f(b.get("high"), price) >= reference) for b in last2)
        pullback_triggered = bool(touched and sign * (price - reference) > 0)
        pullback_ready = bool(touched and abs(price - reference) <= 0.25 * max(atr or range_height or 1.0, 1e-9))
        pullback_invalid = lo2 if sign > 0 else hi2

    # 3) Opening drive: break the completed first-15-minute range after 09:30.
    orh, orl = _opening_range(completed_bars)
    minute = now.hour * 60 + now.minute
    opening_trigger = orh if sign > 0 else orl
    opening_invalid = orl if sign > 0 else orh
    opening_triggered = bool(opening_trigger is not None and minute >= 9 * 60 + 30 and sign * (price - opening_trigger) > 0)
    opening_ready = bool(
        opening_trigger is not None
        and minute >= 9 * 60 + 30
        and sign * (opening_trigger - price) >= -0.10 * max(atr or 1.0, 1e-9)
        and sign * (opening_trigger - price) <= 0.25 * max(atr or 1.0, 1e-9)
    )

    # 4) Failed-break reversal: detect but do not make tradeable initially.
    last = completed_bars[-1]
    failed_setup = None
    failed_trigger = failed_invalid = None
    if reference is not None:
        if direction == "Bullish" and _f(last.get("high"), -math.inf) > reference and _f(last.get("close"), price) < reference:
            failed_setup = "FAILED_BREAK_REVERSAL"
            failed_trigger = _f(last.get("low"))
            failed_invalid = _f(last.get("high"))
        elif direction == "Bearish" and _f(last.get("low"), math.inf) < reference and _f(last.get("close"), price) > reference:
            failed_setup = "FAILED_BREAK_REVERSAL"
            failed_trigger = _f(last.get("high"))
            failed_invalid = _f(last.get("low"))

    candidates = []
    if pullback_triggered or pullback_ready:
        candidates.append(("PULLBACK_RECLAIM", pullback_triggered, pullback_ready, reference, pullback_invalid, max(range_height, abs((reference or price) - (pullback_invalid or price)))))
    if opening_triggered or opening_ready:
        candidates.append(("OPENING_DRIVE", opening_triggered, opening_ready, opening_trigger, opening_invalid, abs((orh or price) - (orl or price))))
    if micro_triggered or micro_ready:
        candidates.append(("MICRO_BREAKOUT", micro_triggered, micro_ready, micro_trigger, micro_invalid, range_height))
    if failed_setup and failed_trigger is not None:
        reverse_triggered = (-sign) * (price - failed_trigger) > 0
        candidates.append((failed_setup, reverse_triggered, True, failed_trigger, failed_invalid, range_height))

    if not candidates:
        return {
            "setup": None, "ready": False, "triggered": False,
            "micro_trigger": micro_trigger, "micro_invalidation": micro_invalid,
        }

    # Prefer a triggered structure; otherwise reclaim > opening > micro.
    candidates.sort(key=lambda x: (1 if x[1] else 0, {"PULLBACK_RECLAIM": 3, "OPENING_DRIVE": 2, "MICRO_BREAKOUT": 1, "FAILED_BREAK_REVERSAL": 0}.get(x[0], 0)), reverse=True)
    setup, triggered, ready, trigger, invalidation, expected_move = candidates[0]
    trade_direction = direction
    research_only = False
    if setup == "FAILED_BREAK_REVERSAL":
        trade_direction = "Bearish" if direction == "Bullish" else "Bullish"
        research_only = FAILED_BREAK_RESEARCH_ONLY

    speed = "CONTINUATION" if setup == "PULLBACK_RECLAIM" else "IMPULSE"
    return {
        "setup": setup,
        "direction": trade_direction,
        "ready": bool(ready),
        "triggered": bool(triggered),
        "trigger": round(trigger, 4) if trigger is not None else None,
        "invalidation": round(invalidation, 4) if invalidation is not None else None,
        "expected_move_abs": round(max(0.0, _f(expected_move, 0.0)), 4),
        "speed_class": speed,
        "research_only": research_only,
    }


def projected_three_minute_rvol(current_bar: dict | None, baseline_volume: float | None, now: dt.datetime) -> float | None:
    if not current_bar:
        return None
    base = _f(baseline_volume)
    vol = _f(current_bar.get("volume"))
    bucket = _dt(current_bar.get("ts"))
    if base is None or base <= 0 or vol is None or bucket is None:
        return None
    if bucket.tzinfo is not None and now.tzinfo is None:
        bucket = bucket.replace(tzinfo=None)
    elapsed = max(5.0, min(180.0, (now - bucket).total_seconds()))
    projected = vol * 180.0 / elapsed
    return round(projected / base, 3)


def classify_state(
    setup: dict,
    *,
    fast_veto: dict,
    stale: bool,
    depth_persist: dict | None,
    option_route: dict | None = None,
    active_same_direction: int = 0,
) -> dict:
    if stale:
        return {"state": "STALE", "tradeable": False, "reason": "live tactical feed is stale"}
    if not setup.get("setup"):
        return {"state": "FORMING", "tradeable": False, "reason": "15m candidate; no 3m structure yet"}
    if setup.get("research_only"):
        return {"state": "RESEARCH_ONLY", "tradeable": False, "reason": "failed-break reversal is observable but not yet promoted"}
    if fast_veto.get("veto"):
        return {"state": "CANCELLED", "tradeable": False, "reason": fast_veto.get("reason")}

    persistence = depth_persist or {}
    oppose = _f(persistence.get("oppose_fraction"))
    if _i(persistence.get("count"), 0) >= 3 and oppose is not None and oppose >= PROPOSED_OPPOSING_DEPTH_PERSISTENCE:
        return {"state": "CANCELLED", "tradeable": False, "reason": "persistent futures depth opposes the setup"}

    if setup.get("triggered"):
        if active_same_direction >= PROPOSED_MAX_SAME_DIRECTION_ACTIVE:
            return {"state": "BLOCKED_EXPOSURE", "tradeable": False, "reason": "same-direction tactical exposure cap reached"}
        if option_route is None:
            return {"state": "TRIGGERED", "tradeable": False, "reason": "underlying triggered; waiting for option route"}
        if not option_route.get("tradeable"):
            return {"state": "OPTION_NOT_TRADEABLE", "tradeable": False, "reason": option_route.get("reason")}
        return {"state": "TRADEABLE", "tradeable": True, "reason": "underlying trigger + executable option route"}

    if setup.get("ready"):
        return {"state": "READY", "tradeable": False, "reason": "structure defined; waiting for actual price trigger"}
    return {"state": "FORMING", "tradeable": False, "reason": "candidate still forming"}


def _quote_top(quote: dict | None) -> tuple[float | None, float | None, float | None]:
    depth = (quote or {}).get("depth") or {}
    buys = list(depth.get("buy") or [])
    sells = list(depth.get("sell") or [])
    bid = _f(buys[0].get("price")) if buys and isinstance(buys[0], dict) else None
    ask = _f(sells[0].get("price")) if sells and isinstance(sells[0], dict) else None
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return bid, ask, None
    return bid, ask, (bid + ask) / 2.0


def estimated_round_trip_cost_pct(mid: float, lot_size: int, spread_pct: float) -> float | None:
    """Approximate Indian long-option round-trip friction as % of premium outlay.

    Uses current 2026 statutory schedule assumptions.  This is a pre-trade
    estimate; realised costs belong in the forward evidence log.
    """
    mid = _f(mid)
    spread_pct = _f(spread_pct)
    lot = max(1, _i(lot_size, 1))
    if mid is None or mid <= 0 or spread_pct is None:
        return None
    one_side = mid * lot
    turnover = 2.0 * one_side
    brokerage = 40.0                    # ₹20 buy + ₹20 sell
    stt = 0.0015 * one_side             # sell-side option premium, 0.15%
    exchange = 0.0003553 * turnover     # NSE option transaction charge
    gst = 0.18 * (brokerage + exchange)
    sebi = 0.000001 * turnover
    stamp = 0.00003 * one_side          # buy-side approximation
    statutory_pct = (brokerage + stt + exchange + gst + sebi + stamp) / one_side * 100.0
    return round(spread_pct + statutory_pct, 4)


def expected_premium_move_pct(contract: dict, spot: float, expected_underlying_move_abs: float) -> float | None:
    mid = _f(contract.get("mid"))
    delta = abs(_f(contract.get("delta"), 0.0))
    move = abs(_f(expected_underlying_move_abs, 0.0))
    if mid is None or mid <= 0 or delta <= 0 or move <= 0:
        return None
    return round(delta * move / mid * 100.0, 4)


def route_option(
    snapshots: list[dict],
    *,
    direction: str,
    spot: float,
    now: dt.datetime,
    speed_class: str,
    expected_underlying_move_abs: float,
    earnings: dict | None = None,
    max_friction_ratio: float = PROPOSED_MAX_FRICTION_TO_EXPECTED_MOVE,
) -> dict:
    """Choose ATM/modest-ITM long option and apply DTE + friction hard vetoes."""
    typ = "CE" if direction == "Bullish" else "PE"
    live = [dict(x) for x in snapshots or [] if x.get("type") == typ and _f(x.get("mid")) not in (None, 0)]
    if not live:
        return {"tradeable": False, "reason": "no live quoted directional option"}

    expiries = sorted({str(x.get("expiry")) for x in live if x.get("expiry")})
    if not expiries:
        return {"tradeable": False, "reason": "no valid option expiry"}

    event = earnings or {}
    pre_result = bool(event.get("pre_result"))
    preferred_expiry = expiries[0]
    nearest_rows = [x for x in live if str(x.get("expiry")) == expiries[0]]
    nearest_dte = min((_i(x.get("dte"), 9999) for x in nearest_rows), default=9999)

    # User-requested result-season rule: <=8 DTE routes to next month by default.
    if pre_result and nearest_dte <= PROPOSED_PRE_RESULT_NEXT_MONTH_DTE and len(expiries) >= 2:
        preferred_expiry = expiries[1]
    elif speed_class == "SWING" and nearest_dte <= 12 and len(expiries) >= 2:
        preferred_expiry = expiries[1]

    pool = [x for x in live if str(x.get("expiry")) == preferred_expiry]
    if not pool:
        return {"tradeable": False, "reason": "preferred expiry has no quoted contract"}

    spot = _f(spot, 0.0)
    # ATM and modest ITM are preferred; OTM lottery contracts are deliberately
    # not selected just because their premium is cheaper.
    def strike_key(x):
        strike = _f(x.get("strike"), spot)
        itm_penalty = 0
        if direction == "Bullish" and strike > spot:
            itm_penalty = 1
        if direction == "Bearish" and strike < spot:
            itm_penalty = 1
        delta = abs(_f(x.get("delta"), 0.0))
        spread = _f(x.get("spread_pct"), 999.0)
        return (itm_penalty, abs(strike - spot), abs(delta - 0.55), spread)

    pool.sort(key=strike_key)
    chosen = None
    reject_reasons = []
    for contract in pool:
        mid = _f(contract.get("mid"))
        spread = _f(contract.get("spread_pct"))
        if mid is None or spread is None:
            reject_reasons.append("missing bid/ask")
            continue
        friction = estimated_round_trip_cost_pct(mid, _i(contract.get("lot_size"), 1), spread)
        expected = expected_premium_move_pct(contract, spot, expected_underlying_move_abs)
        if friction is None or expected is None or expected <= 0:
            reject_reasons.append("cannot estimate friction/expected move")
            continue
        ratio = friction / expected
        contract["estimated_round_trip_friction_pct"] = friction
        contract["expected_premium_move_pct"] = expected
        contract["friction_to_expected_move"] = round(ratio, 4)
        if ratio > float(max_friction_ratio):
            reject_reasons.append(f"friction consumes {ratio*100:.1f}% of expected premium move")
            continue
        chosen = contract
        break

    if chosen is None:
        return {
            "tradeable": False,
            "reason": reject_reasons[0] if reject_reasons else "no contract passed friction gate",
            "preferred_expiry": preferred_expiry,
            "pre_result_next_month": bool(pre_result and preferred_expiry != expiries[0]),
        }
    return {
        "tradeable": True,
        "reason": None,
        "contract": chosen,
        "preferred_expiry": preferred_expiry,
        "pre_result_next_month": bool(pre_result and preferred_expiry != expiries[0]),
    }


def one_lot_risk_preview(contract: dict | None, *, spot: float, invalidation: float | None) -> dict:
    contract = contract or {}
    mid = _f(contract.get("mid"))
    lot = max(1, _i(contract.get("lot_size"), 1))
    delta = abs(_f(contract.get("delta"), 0.0))
    spot = _f(spot)
    invalidation = _f(invalidation)
    if mid is None or spot is None or invalidation is None:
        return {"premium_outlay": None, "estimated_loss_at_invalidation": None, "estimated_loss_pct": None}
    outlay = mid * lot
    underlying_loss = abs(spot - invalidation)
    estimated_option_loss = min(mid, delta * underlying_loss)
    loss = estimated_option_loss * lot
    return {
        "premium_outlay": round(outlay, 2),
        "estimated_loss_at_invalidation": round(loss, 2),
        "estimated_loss_pct": round(loss / outlay * 100.0, 2) if outlay > 0 else None,
        "note": "delta-only preview; realised IV/gamma/spread can change the option loss",
    }


def latency_tax(trigger_option_mid: float | None, fill_price: float | None) -> float | None:
    trigger = _f(trigger_option_mid)
    fill = _f(fill_price)
    if trigger is None or fill is None or trigger <= 0:
        return None
    return round((fill - trigger) / trigger * 100.0, 4)
