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

from .early_onset import assess_early_onset, derive_price_move_60m_atr

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

# Adaptive structure significance. The movement horizon is supplied by the
# full-universe multiscale detector; these are hypothesis-test levels, not
# chart timeframes.
ADAPTIVE_STRUCTURE_READY_ALPHA = 0.05
ADAPTIVE_STRUCTURE_TRIGGER_ALPHA = 0.01
ADAPTIVE_STRUCTURE_EXIT_ALPHA = 0.10
ADAPTIVE_STRUCTURE_SAMPLE_SECONDS = 5

# Forensic entry-timing fix: this is price geometry, not an alpha score.
# A fresh entry may approach from 0.25 ATR before the trigger and is
# chase-extended beyond 0.20 ATR after the trigger.
ENTRY_ZONE_APPROACH_ATR = 0.25
ENTRY_ZONE_MAX_PAST_TRIGGER_ATR = 0.20
OPENING_DRIVE_EXPIRES_MINUTE = 10 * 60 + 15

# Contract stability priors.  The preferred band is descriptive/ranking; the
# wider guard is the only lock-break condition.  This prevents a READY trade
# from silently jumping strikes as spot moves while still allowing an explicit
# re-route when the old option becomes economically inappropriate.
PROPOSED_OPTION_DELTA_TARGET = 0.55
PROPOSED_OPTION_DELTA_PREFERRED_MIN = 0.45
PROPOSED_OPTION_DELTA_PREFERRED_MAX = 0.70
PROPOSED_OPTION_DELTA_LOCK_MIN = 0.35
PROPOSED_OPTION_DELTA_LOCK_MAX = 0.80

# Dynamic risk-plan priors. These are decision-support levels, not order
# instructions and not validated alpha thresholds. Target distances come from
# the setup's own measured move. Trailing protection is deliberately
# structure-based; 3-minute ATR is recorded only for shadow calibration so we
# can learn the appropriate volatility buffer from NSE F&O winners/failures
# instead of hard-coding a borrowed ATR multiple.
RISK_PLAN_TARGET1_FRACTION = 0.50
RISK_PLAN_ATR3_LENGTH = 14


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


def _structural_direction(row: dict) -> str | None:
    """Infer a tactical side from observable futures positioning when the
    legacy indicator direction is absent.

    This is candidate-pool routing only.  A 3-minute price structure must
    still trigger before an option can become tradeable.
    """
    direct = _candidate_direction(row)
    if direct:
        return direct
    structure = str(row.get("oi_structure") or "")
    return {
        "Long Buildup": "Bullish",
        "Short Covering": "Bullish",
        "Short Buildup": "Bearish",
        "Long Unwinding": "Bearish",
    }.get(structure)


def _supplement_candidate(row: dict, direction: str) -> dict | None:
    """Create a broader 15m tactical candidate without requiring the old
    radar's visibility cutoff.

    The pool is intentionally permissive because it is NOT an entry signal:
    live 3m structure, stale-data guards and option economics remain hard
    downstream checks.  This avoids the old failure mode where no 3m engine
    ever ran simply because a stock missed an arbitrary radar score.
    """
    travel = derive_price_move_60m_atr(row)
    if travel is not None and abs(float(travel)) > 1.25:
        return None

    oi15 = _f(row.get("oi_chg_15m_pct"))
    oi30 = _f(row.get("oi_chg_30m_pct"))
    tod = _f(row.get("tod_rvol"), _f(row.get("vol_multiple")))
    compression = _f(row.get("compression_score"))
    relative = _f(row.get("rs_pct"), _f(row.get("relative_strength_pct")))
    has_live_pressure = bool(
        (oi15 is not None and oi15 > 0)
        or (oi30 is not None and oi30 > 0)
        or (tod is not None and tod >= 1.0)
        or (compression is not None and compression >= 65.0)
        or (relative is not None and abs(relative) >= 0.40)
    )
    if not has_live_pressure:
        return None

    merged = dict(row)
    merged["direction"] = direction
    onset = assess_early_onset(merged, direction, fallback_score=0.0)
    merged.update({
        "phase": onset.get("phase") or merged.get("phase"),
        "action_stage": onset.get("action_stage") or merged.get("action_stage"),
        "pressure": onset.get("pressure") if onset.get("pressure") is not None else merged.get("pressure"),
        "runway": onset.get("runway") if onset.get("runway") is not None else merged.get("runway"),
        "price_move_60m_atr": onset.get("price_move_60m_atr"),
        "trigger_level": onset.get("trigger_level") or merged.get("trigger_level"),
        "trigger_distance_atr": onset.get("trigger_distance_atr"),
        "tactical_source": "DIRECT_15M",
    })
    if str(merged.get("phase") or "") in ("EXTENDED", "FADING"):
        return None
    return merged


def select_tactical_pool(radar: dict, results: Iterable[dict], *, max_pool: int = TACTICAL_POOL_MAX) -> list[dict]:
    """Return a two-sided bounded resource pool, not a trade score shortlist.

    The existing radar gets first priority, but each side is supplemented
    directly from live 15m F&O rows when the radar is sparse.  This is the
    bridge that lets the 3m engine actually look for trades instead of being
    starved by older score/visibility cutoffs.
    """
    result_rows = [r for r in (results or []) if r.get("symbol") and not r.get("error")]
    by_symbol = {str(r.get("symbol")): r for r in result_rows}
    per_side = max(1, min(TACTICAL_POOL_PER_SIDE, int(max_pool) // 2 or 1))
    out: list[dict] = []

    for key, direction in (("bullish", "Bullish"), ("bearish", "Bearish")):
        rows: list[dict] = []
        seen: set[str] = set()

        scout_key = "scout_" + key
        source_rows = list((radar or {}).get(scout_key) or (radar or {}).get(key) or [])
        for item in source_rows:
            symbol = str(item.get("symbol") or "")
            base = by_symbol.get(symbol)
            if not base:
                continue
            merged = dict(base)
            merged.update(item)
            merged["direction"] = direction
            merged["tactical_source"] = "EARLY_SCOUT" if scout_key in (radar or {}) else "EARLY_RADAR"
            if str(merged.get("phase") or "") in ("EXTENDED", "FADING"):
                continue
            rows.append(merged)
            seen.add(symbol)

        if len(rows) < per_side:
            for base in result_rows:
                symbol = str(base.get("symbol") or "")
                if symbol in seen or _structural_direction(base) != direction:
                    continue
                extra = _supplement_candidate(base, direction)
                if extra is not None:
                    rows.append(extra)
                    seen.add(symbol)

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
                old_bucket = self.current.get("bucket")
                # Only the immediately preceding 3-minute bucket is a valid
                # completion.  After a feed/subscription gap, emitting a
                # 30-minute-old partial bar as if it just completed corrupts
                # the freshest trigger and volume calculation.
                contiguous = (
                    isinstance(old_bucket, dt.datetime)
                    and bucket - old_bucket <= dt.timedelta(minutes=3)
                )
                if contiguous:
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


def _opening_range(
    bars: list[dict],
    *,
    trading_day: dt.date | None = None,
) -> tuple[float | None, float | None]:
    """Return the 09:15-09:30 range for one trading day only.

    Seeded 3-minute history spans more than one session.  Mixing yesterday's
    opening bars into today's opening range can move the trigger far away from
    the live market, especially after a gap.  When the caller supplies today's
    date we use it explicitly; otherwise we fall back to the newest date
    present in the bar buffer for backwards-compatible callers/tests.
    """
    dated = []
    for b in bars:
        ts = _dt(b.get("ts"))
        if ts is not None:
            dated.append((ts, b))
    if not dated:
        return None, None

    day = trading_day or max(ts.date() for ts, _ in dated)
    session = []
    for ts, b in dated:
        if ts.date() != day:
            continue
        mins = ts.hour * 60 + ts.minute
        if 9 * 60 + 15 <= mins < 9 * 60 + 30:
            session.append(b)
    if not session:
        return None, None
    return max(_f(b.get("high"), -math.inf) for b in session), min(_f(b.get("low"), math.inf) for b in session)


def entry_price_zone(direction: str, price, trigger, invalidation, atr) -> dict:
    """Stable price-zone state used instead of flickering READY/TRADEABLE cards."""
    sign = _sign(direction)
    price = _f(price)
    trigger = _f(trigger)
    invalidation = _f(invalidation)
    atr = abs(_f(atr, 0.0))
    if not sign or price is None or trigger is None or atr <= 0:
        return {"state": "UNAVAILABLE", "distance_atr": None}
    distance = sign * (price - trigger) / atr
    if invalidation is not None and sign * (price - invalidation) <= 0:
        return {"state": "INVALID", "distance_atr": round(distance, 4)}
    if distance < -ENTRY_ZONE_APPROACH_ATR:
        state = "WATCH"
    elif distance < 0:
        state = "APPROACHING"
    elif distance <= ENTRY_ZONE_MAX_PAST_TRIGGER_ATR:
        state = "IN_ZONE"
    else:
        state = "EXTENDED"
    far_edge = trigger + sign * ENTRY_ZONE_MAX_PAST_TRIGGER_ATR * atr
    return {
        "state": state,
        "distance_atr": round(distance, 4),
        "far_edge_underlying": round(far_edge, 4),
    }


def max_option_price_for_entry_zone(contract: dict | None, direction: str, spot, trigger, atr):
    """E4 decision-support maximum premium at the +0.20 ATR zone edge."""
    contract = dict(contract or {})
    sign = _sign(direction)
    spot = _f(spot)
    trigger = _f(trigger)
    atr = abs(_f(atr, 0.0))
    mid = _f(contract.get("mid"))
    delta = _f(contract.get("delta"))
    gamma = max(0.0, _f(contract.get("gamma"), 0.0))
    bid = _f(contract.get("bid"))
    ask = _f(contract.get("ask"))
    if not sign or spot is None or trigger is None or mid is None or delta is None or atr <= 0 or mid <= 0:
        return None
    far_edge = trigger + sign * ENTRY_ZONE_MAX_PAST_TRIGGER_ATR * atr
    ds = far_edge - spot
    half_spread = max(0.0, (ask - bid) / 2.0) if bid is not None and ask is not None and ask >= bid else 0.0
    premium = mid + delta * ds + 0.5 * gamma * ds * ds + half_spread
    return round(max(0.0, premium), 2)


def _binomial_upper_tail(n: int, k: int) -> float:
    """P[X>=k] for X~Binomial(n, 0.5), computed exactly."""
    n = max(0, int(n))
    k = max(0, min(n, int(k)))
    if n <= 0:
        return 1.0
    denom = float(2 ** n)
    return min(1.0, max(0.0, sum(math.comb(n, j) for j in range(k, n + 1)) / denom))


def _resample_last_price(samples: list[dict] | None, *, now: dt.datetime, seconds: int) -> list[tuple[dt.datetime, float]]:
    """Collapse raw cash ticks to regular 5-second last-price observations."""
    rows = []
    for row in samples or []:
        ts = _dt(row.get("ts"))
        px = _f(row.get("price"))
        if ts is None or px is None or px <= 0:
            continue
        if ts.tzinfo is not None and now.tzinfo is None:
            ts = ts.replace(tzinfo=None)
        elif ts.tzinfo is None and now.tzinfo is not None:
            ts = ts.replace(tzinfo=now.tzinfo)
        age = (now - ts).total_seconds()
        if age < -2 or age > max(5, int(seconds) + 5):
            continue
        rows.append((ts, px))
    if not rows:
        return []
    rows.sort(key=lambda x: x[0])
    buckets = {}
    for ts, px in rows:
        key = int(ts.timestamp()) // ADAPTIVE_STRUCTURE_SAMPLE_SECONDS
        buckets[key] = (ts, px)
    return [buckets[key] for key in sorted(buckets)]


def detect_adaptive_structure(
    price_samples: list[dict] | None,
    candidate: dict,
    *,
    now: dt.datetime,
) -> dict:
    """Statistical execution structure on the market-selected natural horizon.

    No named pattern, EMA, RSI, MACD or VWAP enters this function. A structure
    is READY when same-direction 5-second increments are unlikely under a
    p=0.5 sign null (alpha=0.05); it becomes TRIGGERED at alpha=0.01.
    """
    direction = _candidate_direction(candidate)
    sign = _sign(direction)
    horizon = _i(candidate.get("movement_horizon_seconds"), 0)
    if not sign or horizon <= 0:
        return {
            "direction": direction,
            "setup": None,
            "ready": False,
            "triggered": False,
            "reason": "adaptive movement horizon unavailable",
            "structure_p_value": None,
            "horizon_seconds": horizon or None,
        }

    points = _resample_last_price(price_samples, now=now, seconds=horizon)
    if len(points) < 5:
        return {
            "direction": direction,
            "setup": None,
            "ready": False,
            "triggered": False,
            "reason": "adaptive structure history not ready",
            "structure_p_value": None,
            "horizon_seconds": horizon,
            "sample_count": len(points),
        }

    prices = [px for _, px in points]
    increments = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    signed = [sign * x for x in increments if abs(x) > 1e-12]
    n = len(signed)
    if n < 4:
        return {
            "direction": direction,
            "setup": None,
            "ready": False,
            "triggered": False,
            "reason": "too few non-zero adaptive increments",
            "structure_p_value": None,
            "horizon_seconds": horizon,
            "sample_count": n,
        }

    aligned = sum(1 for x in signed if x > 0)
    p_value = _binomial_upper_tail(n, aligned)
    net_signed = sign * (prices[-1] - prices[0])
    path = sum(abs(x) for x in increments)
    coherence = net_signed / path if path > 1e-12 else 0.0

    # Independent half-window agreement reduces the chance that one isolated
    # early impulse is mistaken for a continuing execution structure.
    split = max(1, len(prices) // 2)
    first_net = sign * (prices[split] - prices[0]) if split < len(prices) else 0.0
    second_net = sign * (prices[-1] - prices[split]) if split < len(prices) else 0.0
    halves_agree = first_net > 0 and second_net >= 0

    ready = bool(
        net_signed > 0
        and halves_agree
        and p_value <= ADAPTIVE_STRUCTURE_READY_ALPHA
    )
    triggered = bool(
        ready and p_value <= ADAPTIVE_STRUCTURE_TRIGGER_ALPHA
    )

    current = prices[-1]
    adverse_boundary = min(prices) if sign > 0 else max(prices)
    expected_move = max(0.0, net_signed)
    return {
        "direction": direction,
        "setup": "ADAPTIVE_QUANT_STRUCTURE" if ready else None,
        "ready": ready,
        "triggered": triggered,
        "research_only": False,
        "speed_class": "ADAPTIVE",
        "trigger": current if ready else None,
        "invalidation": adverse_boundary if ready else None,
        "expected_move_abs": expected_move if ready else None,
        "structure_p_value": round(p_value, 8),
        "structure_ready_alpha": ADAPTIVE_STRUCTURE_READY_ALPHA,
        "structure_trigger_alpha": ADAPTIVE_STRUCTURE_TRIGGER_ALPHA,
        "structure_exit_alpha": ADAPTIVE_STRUCTURE_EXIT_ALPHA,
        "horizon_seconds": horizon,
        "sample_count": n,
        "aligned_increment_count": aligned,
        "aligned_increment_fraction": round(aligned / float(n), 4),
        "path_coherence": round(coherence, 4),
        "first_half_progress_abs": round(first_net, 6),
        "second_half_progress_abs": round(second_net, 6),
        "reason": (
            "adaptive sign persistence reached trigger significance"
            if triggered else (
                "adaptive sign persistence statistically ready"
                if ready else "adaptive structure not statistically persistent"
            )
        ),
    }


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
    orh, orl = _opening_range(completed_bars, trading_day=now.date())
    minute = now.hour * 60 + now.minute
    opening_trigger = orh if sign > 0 else orl
    opening_invalid = orl if sign > 0 else orh
    opening_window = 9 * 60 + 30 <= minute <= OPENING_DRIVE_EXPIRES_MINUTE
    opening_triggered = bool(opening_trigger is not None and opening_window and sign * (price - opening_trigger) > 0)
    opening_ready = bool(
        opening_trigger is not None
        and opening_window
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


def five_minute_witness(direction: str, ret_5m_pct=None, relative_5m_vs_nifty_pct=None) -> dict:
    """Direction-aware 5m witness using evidence the full-universe observer already has.

    This is deliberately not another score or threshold. It only asks whether
    the measured 5m stock return and 5m relative move are pointing with,
    against, or inconsistently with the existing underlying thesis.
    """
    sign = _sign(direction)
    vals = []
    for value in (ret_5m_pct, relative_5m_vs_nifty_pct):
        value = _f(value)
        if value is not None and sign:
            vals.append(sign * value)
    if not vals:
        state = "UNMEASURED"
    elif all(v > 0 for v in vals):
        state = "SUPPORTIVE"
    elif all(v < 0 for v in vals):
        state = "OPPOSING"
    else:
        state = "MIXED"
    return {
        "state": state,
        "ret_5m_pct": _f(ret_5m_pct),
        "relative_5m_vs_nifty_pct": _f(relative_5m_vs_nifty_pct),
        "measured_inputs": len(vals),
    }


def option_route_health(option_route: dict | None) -> dict:
    """Separate current quote quality from the underlying trade opportunity.

    A transient quote/friction failure is DEGRADED, not a thesis failure.
    Structurally unavailable expiry/contract cases remain BLOCKED. This
    classification never makes a non-executable quote tradeable.
    """
    if option_route is None:
        return {"state": "WAIT", "reason": "option route not evaluated yet"}
    route = option_route or {}
    if route.get("tradeable"):
        return {"state": "HEALTHY", "reason": None}
    reason = str(route.get("reason") or "option route not executable")
    low = reason.lower()
    hard_markers = (
        "no valid option expiry",
        "preferred expiry has no quoted contract",
    )
    if any(marker in low for marker in hard_markers):
        return {"state": "BLOCKED", "reason": reason}
    return {"state": "DEGRADED", "reason": reason}



LIVE_PRECHECK_MAX_SPREAD_PCT = 4.0
LIVE_PRECHECK_DELTA_MIN = 0.25
LIVE_PRECHECK_DELTA_MAX = 0.85


def option_pre_feasibility(snapshots: list[dict], direction: str) -> dict:
    """Broad live option gate before any 3-minute structural calculation.

    This is intentionally not the full execution router. It only verifies that
    the directional option neighborhood is live enough to justify spending a
    scarce deep/3m slot. Final friction-vs-expected-move remains downstream
    because the expected underlying move comes from the 3m setup itself.
    """
    typ = "CE" if direction == "Bullish" else "PE"
    directional = [dict(x) for x in (snapshots or []) if x.get("type") == typ]
    if not directional:
        return {"eligible": False, "reason": "no live directional option snapshot", "contract": None}

    valid = []
    failures = []
    for row in directional:
        bid = _f(row.get("bid"))
        ask = _f(row.get("ask"))
        mid = _f(row.get("mid"))
        spread = _f(row.get("spread_pct"))
        delta = abs(_f(row.get("delta"), 0.0))
        dte = _i(row.get("dte"), -1)

        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid or mid is None or mid <= 0:
            failures.append("missing/invalid bid-ask")
            continue
        if spread is None or spread > LIVE_PRECHECK_MAX_SPREAD_PCT:
            failures.append("live spread above 4%")
            continue
        if dte < 0:
            failures.append("invalid expiry")
            continue
        if not (LIVE_PRECHECK_DELTA_MIN <= delta <= LIVE_PRECHECK_DELTA_MAX):
            failures.append("delta outside broad 0.25-0.85 corridor")
            continue
        row["delta_abs"] = round(delta, 4)
        valid.append(row)

    if not valid:
        reason = failures[0] if failures else "no live option passed broad precheck"
        return {"eligible": False, "reason": reason, "contract": None}

    valid.sort(
        key=lambda x: (
            abs(abs(_f(x.get("delta"), 0.0)) - PROPOSED_OPTION_DELTA_TARGET),
            _f(x.get("spread_pct"), 999.0),
            abs(_f(x.get("strike"), 0.0)),
        )
    )
    chosen = valid[0]
    return {
        "eligible": True,
        "reason": None,
        "contract": chosen,
        "candidate_count": len(valid),
        "max_spread_pct": LIVE_PRECHECK_MAX_SPREAD_PCT,
        "delta_corridor": [LIVE_PRECHECK_DELTA_MIN, LIVE_PRECHECK_DELTA_MAX],
    }


def classify_state(
    setup: dict,
    *,
    fast_veto: dict,
    stale: bool,
    depth_persist: dict | None,
    option_route: dict | None = None,
    active_same_direction: int = 0,
) -> dict:
    p_value = _f((setup or {}).get("structure_p_value"))
    route_tradeable = bool((option_route or {}).get("tradeable"))
    base = {
        "structure_p_value": p_value,
        "structure_horizon_seconds": (setup or {}).get("horizon_seconds"),
        "route_tradeable": route_tradeable,
    }
    if stale:
        return {**base, "state": "STALE", "tradeable": False, "reason": "live tactical feed is stale"}
    if not setup.get("setup"):
        return {**base, "state": "FORMING", "tradeable": False, "reason": "adaptive statistical structure not ready"}
    if setup.get("research_only"):
        return {**base, "state": "RESEARCH_ONLY", "tradeable": False, "reason": "research-only structure"}
    if fast_veto.get("veto"):
        return {**base, "state": "CANCELLED", "tradeable": False, "reason": fast_veto.get("reason")}

    persistence = depth_persist or {}
    oppose = _f(persistence.get("oppose_fraction"))
    if _i(persistence.get("count"), 0) >= 3 and oppose is not None and oppose >= PROPOSED_OPPOSING_DEPTH_PERSISTENCE:
        return {**base, "state": "CANCELLED", "tradeable": False, "reason": "persistent futures depth opposes the setup"}

    if setup.get("triggered"):
        if active_same_direction >= PROPOSED_MAX_SAME_DIRECTION_ACTIVE:
            return {**base, "state": "BLOCKED_EXPOSURE", "tradeable": False, "reason": "same-direction tactical exposure cap reached"}
        if option_route is None:
            return {**base, "state": "TRIGGERED", "tradeable": False, "reason": "underlying structure significant; waiting for option route"}
        if not option_route.get("tradeable"):
            return {**base, "state": "OPTION_NOT_TRADEABLE", "tradeable": False, "reason": option_route.get("reason")}
        return {**base, "state": "TRADEABLE", "tradeable": True, "reason": "adaptive structure + executable option route"}

    if setup.get("ready"):
        return {**base, "state": "READY", "tradeable": False, "reason": "adaptive structure statistically ready"}
    return {**base, "state": "FORMING", "tradeable": False, "reason": "adaptive structure still forming"}


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


def _expected_iv_change_points(now: dt.datetime, horizon_seconds: float) -> float:
    """Intraday IV drift prior measured in the 14-session option recorder."""
    minutes = max(0.0, float(horizon_seconds or 0.0) / 60.0)
    clock = now.hour * 60 + now.minute
    if clock < 13 * 60:
        # Median ATM IV -0.60 points from 09:30 to 13:00 (~210 min).
        rate = -0.60 / 210.0
    elif clock < 15 * 60 + 10:
        # Median ATM IV -0.06 points from 13:00 to 15:10 (~130 min).
        rate = -0.06 / 130.0
    else:
        rate = 0.0
    return rate * minutes


def route_option(
    snapshots: list[dict],
    *,
    direction: str,
    spot: float,
    now: dt.datetime,
    speed_class: str,
    expected_underlying_move_abs: float,
    expected_horizon_seconds: float | None = None,
    earnings: dict | None = None,
    max_friction_ratio: float = PROPOSED_MAX_FRICTION_TO_EXPECTED_MOVE,
    locked_contract_symbol: str | None = None,
) -> dict:
    """Choose a stable directional option and explain the route.

    Once READY has locked a contract, keep that contract through the current
    entry episode while it remains live, inside the broad delta guard and
    executable after friction.  A re-route is explicit and carries a reason;
    the UI should never silently jump from one strike to another.
    """
    typ = "CE" if direction == "Bullish" else "PE"
    live = [dict(x) for x in snapshots or [] if x.get("type") == typ and _f(x.get("mid")) not in (None, 0)]
    if not live:
        return {
            "tradeable": False, "reason": "no live quoted directional option",
            "locked_contract_requested": locked_contract_symbol,
        }

    expiries = sorted({str(x.get("expiry")) for x in live if x.get("expiry")})
    if not expiries:
        return {"tradeable": False, "reason": "no valid option expiry"}

    event = earnings or {}
    pre_result = bool(event.get("pre_result"))
    preferred_expiry = expiries[0]
    nearest_rows = [x for x in live if str(x.get("expiry")) == expiries[0]]
    nearest_dte = min((_i(x.get("dte"), 9999) for x in nearest_rows), default=9999)

    if pre_result and nearest_dte <= PROPOSED_PRE_RESULT_NEXT_MONTH_DTE and len(expiries) >= 2:
        preferred_expiry = expiries[1]
    elif speed_class == "SWING" and nearest_dte <= 12 and len(expiries) >= 2:
        preferred_expiry = expiries[1]

    pool = [x for x in live if str(x.get("expiry")) == preferred_expiry]
    # A currently locked entry contract is allowed to remain on its original
    # expiry for that entry episode.  Expiry is reconsidered only on re-entry.
    locked = next(
        (x for x in live if locked_contract_symbol and str(x.get("symbol") or "") == str(locked_contract_symbol)),
        None,
    )

    spot = _f(spot, 0.0)
    strikes = sorted({_f(x.get("strike")) for x in pool if _f(x.get("strike")) is not None})
    atm_strike = min(strikes, key=lambda s: abs(s - spot)) if strikes else None

    def assess(contract):
        contract = dict(contract)
        mid = _f(contract.get("mid"))
        spread = _f(contract.get("spread_pct"))
        delta = abs(_f(contract.get("delta"), 0.0))
        gamma = max(0.0, _f(contract.get("gamma"), 0.0))
        theta = _f(contract.get("theta"), 0.0)
        vega = _f(contract.get("vega"), 0.0)
        if mid is None or mid <= 0 or spread is None:
            return None, "missing bid/ask"
        friction_pct = estimated_round_trip_cost_pct(mid, _i(contract.get("lot_size"), 1), spread)
        move = abs(_f(expected_underlying_move_abs, 0.0))
        if friction_pct is None or delta <= 0 or move <= 0:
            return None, "cannot estimate option economics"

        horizon_s = max(1.0, _f(expected_horizon_seconds, 0.0) or 0.0)
        favourable_abs = delta * move + 0.5 * gamma * move * move
        theta_abs = theta * (horizon_s / 86400.0)
        iv_change = _expected_iv_change_points(now, horizon_s)
        vega_abs = vega * iv_change
        gross_abs = favourable_abs + theta_abs + vega_abs
        friction_abs = mid * friction_pct / 100.0
        net_abs = gross_abs - friction_abs
        gross_pct = gross_abs / mid * 100.0
        net_roi = net_abs / mid * 100.0
        ratio = friction_abs / gross_abs if gross_abs > 1e-12 else float("inf")

        strike = _f(contract.get("strike"))
        moneyness = None
        if strike is not None and spot:
            moneyness = (strike - spot) / spot * 100.0

        contract.update({
            "estimated_round_trip_friction_pct": round(friction_pct, 4),
            "expected_premium_move_pct": round(gross_pct, 4),
            "expected_premium_gain_abs": round(gross_abs, 4),
            "expected_net_gain_abs": round(net_abs, 4),
            "expected_net_roi_pct": round(net_roi, 4),
            "expected_horizon_seconds": round(horizon_s, 1),
            "expected_iv_change_points": round(iv_change, 5),
            "theta_contribution_abs": round(theta_abs, 4),
            "vega_contribution_abs": round(vega_abs, 4),
            "friction_to_expected_move": round(ratio, 4) if math.isfinite(ratio) else None,
            "moneyness_vs_spot_pct": round(moneyness, 3) if moneyness is not None else None,
            "delta_abs": round(delta, 4),
        })
        if gross_abs <= 0 or net_abs <= 0:
            return None, "expected premium gain does not clear live friction"
        return contract, None

    reroute_reason = None
    if locked is not None:
        assessed, rejected = assess(locked)
        if assessed is not None:
            return {
                "tradeable": True,
                "reason": None,
                "contract": assessed,
                "preferred_expiry": str(locked.get("expiry") or preferred_expiry),
                "pre_result_next_month": bool(pre_result and str(locked.get("expiry")) != expiries[0]),
                "atm_strike": atm_strike,
                "locked": True,
                "locked_contract_requested": locked_contract_symbol,
                "selection_reason": "ACTIONABLE CONTRACT LOCK — selected contract remains net-positive after friction",
                "reroute_reason": None,
            }
        reroute_reason = rejected or "locked contract no longer economically executable"
    elif locked_contract_symbol:
        reroute_reason = "locked contract is no longer in the live quoted universe"

    if not pool:
        return {
            "tradeable": False,
            "reason": "preferred expiry has no quoted contract",
            "preferred_expiry": preferred_expiry,
            "locked_contract_requested": locked_contract_symbol,
            "reroute_reason": reroute_reason,
        }

    # Evaluate every quoted directional contract in the preferred expiry.
    # Selection is the contract with the highest expected net ROI after live
    # spread/statutory friction, theta and the measured intraday IV-drift prior.
    assessed_rows = []
    reject_reasons = []
    for raw in pool:
        contract, rejected = assess(raw)
        if contract is None:
            reject_reasons.append(rejected)
            continue
        assessed_rows.append(contract)

    assessed_rows.sort(
        key=lambda x: (
            _f(x.get("expected_net_roi_pct"), -999.0),
            -_f(x.get("estimated_round_trip_friction_pct"), 999.0),
        ),
        reverse=True,
    )
    chosen = assessed_rows[0] if assessed_rows else None

    if chosen is None:
        return {
            "tradeable": False,
            "reason": reject_reasons[0] if reject_reasons else "no contract passed friction gate",
            "preferred_expiry": preferred_expiry,
            "pre_result_next_month": bool(pre_result and preferred_expiry != expiries[0]),
            "atm_strike": atm_strike,
            "locked": False,
            "locked_contract_requested": locked_contract_symbol,
            "reroute_reason": reroute_reason,
        }

    selection_reason = (
        f"selected {chosen.get('symbol')} by maximum expected net ROI "
        f"{_f(chosen.get('expected_net_roi_pct'), 0.0):.2f}% after friction/theta/IV prior"
    )
    if reroute_reason:
        selection_reason = "RE-ROUTED — " + reroute_reason + "; " + selection_reason

    return {
        "tradeable": True,
        "reason": None,
        "contract": chosen,
        "preferred_expiry": preferred_expiry,
        "pre_result_next_month": bool(pre_result and preferred_expiry != expiries[0]),
        "atm_strike": atm_strike,
        "locked": False,
        "locked_contract_requested": locked_contract_symbol,
        "selection_reason": selection_reason,
        "reroute_reason": reroute_reason,
        "selection_method": "MAX_EXPECTED_NET_ROI_AFTER_FRICTION_THETA_IV",
        "evaluated_contract_count": len(assessed_rows),
        "top_contracts": [
            {
                "symbol": row.get("symbol"),
                "strike": row.get("strike"),
                "expiry": row.get("expiry"),
                "delta_abs": row.get("delta_abs"),
                "expected_net_roi_pct": row.get("expected_net_roi_pct"),
                "estimated_round_trip_friction_pct": row.get("estimated_round_trip_friction_pct"),
            }
            for row in assessed_rows[:5]
        ],
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


def dynamic_trade_plan(
    *,
    direction: str,
    spot: float,
    trigger: float | None,
    invalidation: float | None,
    expected_move_abs: float | None,
    atr: float | None,
    best_favourable_abs: float | None = None,
    worst_adverse_abs: float | None = None,
    completed_bars: list[dict] | None = None,
    contract: dict | None = None,
    entry_underlying: float | None = None,
    option_entry_mid: float | None = None,
    option_entry_delta: float | None = None,
    option_entry_gamma: float | None = None,
    speed_class: str | None = None,
) -> dict:
    """Build an underlying-first dynamic SL/target plan.

    Live decision-support logic:
      * initial SL = actual structural invalidation;
      * T1/T2 = 50% / 100% of the setup's own measured move;
      * until T1 is demonstrated, the original structural SL is retained;
      * after T1, trail only by proven completed 3m structure, never widening
        the original stop.

    Research-only context:
      * completed-bar 3m ATR(14), plus MFE/MAE in 3m-ATR units, is reported for
        future calibration of a volatility buffer.  No fixed ATR multiple is
        allowed to move the live stop.

    Option premium SL/targets are local delta+gamma scenario estimates only.
    Underlying levels remain authoritative because IV/theta/vega/spread and
    discrete jumps can materially change realised option premium.
    """
    sign = _sign(direction)
    spot = _f(spot)
    trigger = _f(trigger)
    invalidation = _f(invalidation)
    expected = abs(_f(expected_move_abs, 0.0))
    atr = abs(_f(atr, 0.0))
    if not sign or spot is None or invalidation is None:
        return {
            "available": False,
            "reason": "direction/spot/structural invalidation unavailable",
            "controls_trading": False,
        }

    entry = _f(entry_underlying, trigger if trigger is not None else spot)
    if entry is None:
        entry = spot

    risk_abs = sign * (entry - invalidation)
    if risk_abs <= 0:
        return {
            "available": False,
            "reason": "structural invalidation is not adverse to the entry reference",
            "controls_trading": False,
        }

    move = expected if expected > 0 else (atr if atr > 0 else None)
    if move is None or move <= 0:
        return {
            "available": False,
            "reason": "setup measured move and ATR unavailable",
            "controls_trading": False,
        }

    t1_dist = RISK_PLAN_TARGET1_FRACTION * move
    t2_dist = move
    target1 = entry + sign * t1_dist
    target2 = entry + sign * t2_dist

    best_favourable = max(0.0, _f(best_favourable_abs, 0.0))
    worst_adverse = max(0.0, _f(worst_adverse_abs, 0.0))
    progress = max(0.0, sign * (spot - entry))

    stage = "INITIAL"
    dynamic_sl = invalidation
    structure_trail = None
    bars = list(completed_bars or [])
    if best_favourable >= t1_dist and t1_dist > 0:
        stage = "RUNNER" if best_favourable >= t2_dist else "PROTECT"
        if len(bars) >= 2:
            recent = bars[-2:]
            if sign > 0:
                structure_trail = min(_f(b.get("low"), spot) for b in recent)
                dynamic_sl = max(invalidation, structure_trail)
            else:
                structure_trail = max(_f(b.get("high"), spot) for b in recent)
                dynamic_sl = min(invalidation, structure_trail)

    rr_t1 = t1_dist / risk_abs if risk_abs > 0 else None
    rr_t2 = t2_dist / risk_abs if risk_abs > 0 else None
    remaining_t1 = max(0.0, t1_dist - progress)
    remaining_t2 = max(0.0, t2_dist - progress)

    atr3 = three_minute_atr(bars, RISK_PLAN_ATR3_LENGTH)
    mfe_atr3 = best_favourable / atr3 if atr3 and atr3 > 0 else None
    mae_atr3 = worst_adverse / atr3 if atr3 and atr3 > 0 else None
    structure_distance_atr3 = None
    if atr3 and atr3 > 0 and structure_trail is not None:
        structure_distance_atr3 = abs(spot - structure_trail) / atr3

    contract = contract or {}
    current_mid = _f(contract.get("mid"))
    delta_now = _f(contract.get("delta"))
    gamma_now = max(0.0, _f(contract.get("gamma"), 0.0))
    premium_ref = _f(option_entry_mid, current_mid)
    delta_ref = _f(option_entry_delta, delta_now)
    gamma_ref = max(0.0, _f(option_entry_gamma, gamma_now))

    def premium_at(level):
        if premium_ref is None or premium_ref <= 0 or delta_ref is None or level is None:
            return None
        ds = level - entry
        est = premium_ref + delta_ref * ds + 0.5 * gamma_ref * ds * ds
        return round(max(0.0, est), 2)

    bars_allowed = PROPOSED_FOLLOWTHROUGH_BARS.get(speed_class)
    time_stop_minutes = int(bars_allowed * 3) if bars_allowed else None

    return {
        "available": True,
        "controls_trading": False,
        "authority": "UNDERLYING",
        "plan_stage": stage,
        "entry_reference_underlying": round(entry, 4),
        "entry_reference_source": "LIVE_TRIGGER" if entry_underlying is not None else "TRIGGER_REFERENCE",
        "initial_sl_underlying": round(invalidation, 4),
        "dynamic_sl_underlying": round(dynamic_sl, 4),
        "target1_underlying": round(target1, 4),
        "target2_underlying": round(target2, 4),
        "target1_reached": bool(progress >= t1_dist),
        "target2_reached": bool(progress >= t2_dist),
        "remaining_to_target1_abs": round(remaining_t1, 4),
        "remaining_to_target2_abs": round(remaining_t2, 4),
        "risk_abs": round(risk_abs, 4),
        "risk_atr": round(risk_abs / atr, 4) if atr > 0 else None,
        "target1_rr": round(rr_t1, 3) if rr_t1 is not None else None,
        "target2_rr": round(rr_t2, 3) if rr_t2 is not None else None,
        "measured_move_abs": round(move, 4),
        "target1_fraction_of_measured_move": RISK_PLAN_TARGET1_FRACTION,
        "structure_trail_underlying": round(structure_trail, 4) if structure_trail is not None else None,
        "best_favourable_abs": round(best_favourable, 4),
        "worst_adverse_abs": round(worst_adverse, 4),
        "atr3_14_shadow": atr3,
        "mfe_atr3_shadow": round(mfe_atr3, 4) if mfe_atr3 is not None else None,
        "mae_atr3_shadow": round(mae_atr3, 4) if mae_atr3 is not None else None,
        "structure_trail_distance_atr3_shadow": round(structure_distance_atr3, 4) if structure_distance_atr3 is not None else None,
        "trail_calibration_status": "SHADOW_LEARN_FROM_NSE_FNO_MAE_MFE",
        "option_contract": contract.get("symbol"),
        "option_entry_reference_mid": round(premium_ref, 2) if premium_ref is not None else None,
        "option_delta_reference": round(delta_ref, 4) if delta_ref is not None else None,
        "option_gamma_reference": round(gamma_ref, 6) if gamma_ref else None,
        "time_stop_minutes_if_no_followthrough": time_stop_minutes,
        "indicative_option_sl": premium_at(dynamic_sl),
        "indicative_option_target1": premium_at(target1),
        "indicative_option_target2": premium_at(target2),
        "premium_projection_note": "delta+gamma local scenario estimate with IV/time held constant; underlying SL/targets are authoritative; theta/vega/spread can change realised premium",
        "method": "STRUCTURE_SL + MEASURED_MOVE_T1_T2 + TIME_BARRIER + PROVEN_3M_STRUCTURE_TRAIL",
    }


def three_minute_atr(completed_bars: list[dict] | None, length: int = RISK_PLAN_ATR3_LENGTH) -> float | None:
    """Wilder ATR from completed 3-minute underlying bars only.

    This is research/normalisation context for risk calibration.  It does not
    alter the live tactical state or move the stop by itself.
    """
    bars = list(completed_bars or [])
    length = max(2, int(length or RISK_PLAN_ATR3_LENGTH))
    if len(bars) < length + 1:
        return None
    trs = []
    prev_close = None
    for bar in bars:
        high = _f(bar.get("high"))
        low = _f(bar.get("low"))
        close = _f(bar.get("close"))
        if high is None or low is None or close is None:
            continue
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(max(0.0, tr))
        prev_close = close
    if len(trs) < length:
        return None
    # Wilder smoothing seeded by the first full simple average.
    atr = sum(trs[:length]) / float(length)
    for tr in trs[length:]:
        atr = ((length - 1) * atr + tr) / float(length)
    return round(atr, 6) if atr > 0 else None


def latency_tax(trigger_option_mid: float | None, fill_price: float | None) -> float | None:
    trigger = _f(trigger_option_mid)
    fill = _f(fill_price)
    if trigger is None or fill is None or trigger <= 0:
        return None
    return round((fill - trigger) / trigger * 100.0, 4)
