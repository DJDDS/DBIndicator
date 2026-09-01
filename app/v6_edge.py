"""V6 evidence-led F&O edge helpers.

The module is deliberately pure where practical so the same logic can be used
by the live scanner and the historical research engine.  OI is sponsorship,
not direction.  Direction comes from a real Recent-Range escape; regime,
participation, leadership, price location, futures basis and 5-minute execution
then decide whether the move is worth trading.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

from . import costs


def _finite(value) -> bool:
    try:
        return value is not None and bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _flag(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        fv = float(value)
        if fv == 1.0:
            return True
        if fv == 0.0:
            return False
    return None


def percentile_rank(values: pd.Series, higher_better: bool = True) -> pd.Series:
    """Cross-sectional percentile rank, preserving missing observations."""
    ser = pd.to_numeric(pd.Series(values), errors="coerce")
    ranks = ser.rank(pct=True, method="average") * 100.0
    if not higher_better:
        ranks = 100.0 - ranks + (100.0 / max(1, int(ser.notna().sum())))
    return ranks.clip(0, 100)


def catalyst_proxy_score(*, gap_atr=None, opening_rvol=None, tod_rvol=None,
                         bar_range_atr=None, turnover_percentile=None) -> float:
    """Information-shock / stock-in-play proxy, 0..100.

    This intentionally does not claim a news event occurred.  It measures the
    observable footprint that often accompanies new information: a meaningful
    gap/range shock and participation that is abnormal for that stock/time.
    """
    score = 0.0
    if _finite(gap_atr):
        g = abs(float(gap_atr))
        score += 25 if g >= 0.75 else (15 if g >= 0.40 else (6 if g >= 0.20 else 0))
    if _finite(opening_rvol):
        v = float(opening_rvol)
        score += 25 if v >= 1.75 else (17 if v >= 1.30 else (7 if v >= 1.05 else 0))
    if _finite(tod_rvol):
        v = float(tod_rvol)
        score += 20 if v >= 1.60 else (12 if v >= 1.30 else (5 if v >= 1.05 else 0))
    if _finite(bar_range_atr):
        r = float(bar_range_atr)
        score += 20 if r >= 1.00 else (12 if r >= 0.70 else (5 if r >= 0.50 else 0))
    if _finite(turnover_percentile):
        p = float(turnover_percentile)
        score += 10 if p >= 90 else (7 if p >= 80 else (3 if p >= 65 else 0))
    return round(min(100.0, score), 1)


def classify_market_regime(index_chg_pct=None, bullish_pct=None, bearish_pct=None,
                           dispersion_pct=None) -> str:
    """Simple, explicit market-state classifier used by live V6 ranking."""
    idx = float(index_chg_pct) if _finite(index_chg_pct) else 0.0
    bull = float(bullish_pct) if _finite(bullish_pct) else 50.0
    bear = float(bearish_pct) if _finite(bearish_pct) else 50.0
    disp = float(dispersion_pct) if _finite(dispersion_pct) else 0.0
    if idx >= 0.35 and bull >= 60.0:
        return "Trend Up"
    if idx <= -0.35 and bear >= 60.0:
        return "Trend Down"
    if abs(idx) >= 1.0 and disp >= 1.0:
        return "High Volatility"
    if abs(idx) < 0.35 and disp >= 0.90:
        return "Rotation"
    return "Chop"


def price_location_score(*, direction: str, close, high20=None, low20=None,
                         high50=None, low50=None) -> dict:
    """Direction-aware location within prior completed-session ranges."""
    if not _finite(close):
        return {"score": 0.0, "near_20d_high": False, "near_20d_low": False,
                "position_20d_pct": None, "position_50d_pct": None}
    c = float(close)

    def pos(lo, hi):
        if not (_finite(lo) and _finite(hi)):
            return None
        lo, hi = float(lo), float(hi)
        if hi <= lo:
            return None
        return float(np.clip((c - lo) / (hi - lo) * 100.0, 0.0, 100.0))

    p20, p50 = pos(low20, high20), pos(low50, high50)
    near_hi = bool(_finite(high20) and float(high20) > 0 and c / float(high20) >= 0.985)
    near_lo = bool(_finite(low20) and c <= float(low20) * 1.015)
    directional = []
    for p in (p20, p50):
        if p is not None:
            directional.append(p if direction == "Bullish" else 100.0 - p)
    score = float(np.mean(directional)) if directional else 50.0
    # Reward being close to the relevant edge, but cap so location cannot
    # overwhelm actual participation/sponsorship.
    if direction == "Bullish" and near_hi:
        score = max(score, 90.0)
    if direction == "Bearish" and near_lo:
        score = max(score, 90.0)
    return {
        "score": round(float(np.clip(score, 0, 100)), 1),
        "near_20d_high": near_hi,
        "near_20d_low": near_lo,
        "position_20d_pct": round(p20, 1) if p20 is not None else None,
        "position_50d_pct": round(p50, 1) if p50 is not None else None,
    }


def sponsorship_score(*, direction: str, tod_rvol=None, oi_confirmed=None,
                      basis_pct=None, basis_acceleration=None) -> dict:
    """Soft sponsorship score; OI is never a universal veto in V6."""
    score = 0.0
    volume_ok = _finite(tod_rvol) and float(tod_rvol) >= 1.30
    if volume_ok:
        score += 10.0 if float(tod_rvol) < 1.6 else 15.0

    oi_flag = _flag(oi_confirmed)
    if oi_flag is True:
        score += 12.0

    sign = 1.0 if direction == "Bullish" else -1.0
    basis_ok = False
    if _finite(basis_pct):
        directed_basis = sign * float(basis_pct)
        if directed_basis >= 0.08:
            score += 6.0
            basis_ok = True
    if _finite(basis_acceleration):
        directed_accel = sign * float(basis_acceleration)
        if directed_accel >= 0.05:
            score += 7.0
            basis_ok = True
    sponsored = bool(volume_ok and (oi_flag is True or basis_ok))
    return {
        "score": round(min(30.0, score), 1),
        "volume_ok": bool(volume_ok),
        "oi_ok": oi_flag is True,
        "basis_ok": bool(basis_ok),
        "sponsored": sponsored,
    }


def _regime_score(direction: str, regime: str | None) -> float:
    if regime == "Trend Up":
        return 10.0 if direction == "Bullish" else -6.0
    if regime == "Trend Down":
        return 10.0 if direction == "Bearish" else -6.0
    if regime == "Rotation":
        return 4.0
    if regime == "High Volatility":
        return 2.0
    return 0.0


def classify_v6_candidate(row: dict) -> dict:
    """Evidence-weighted V6 live classifier.

    Recent Range remains the validated setup source.  Long/short intraday are
    scored separately; swing is deliberately long-only until the short model
    clears the independent research benchmark.
    """
    direction = row.get("direction") or row.get("breakout_direction") or row.get("retained_breakout_direction")
    source = row.get("breakout_source") or row.get("retained_breakout_source")
    blockers = []
    if direction not in ("Bullish", "Bearish"):
        return {
            "stage": "Stock in Play" if row.get("stock_in_play") else ("Energy Building" if row.get("energy_building") else None),
            "score": None, "intraday_eligible": False, "swing_eligible": False,
            "short_research_only": False, "blockers": ["waiting for price direction"],
        }

    recent = source == "Recent Range"
    if not recent:
        blockers.append("generic breakout remains research-only")
    if _flag(row.get("vwap_side_agrees")) is False:
        blockers.append("VWAP opposes breakout")
    ext = row.get("breakout_extension_atr")
    extended = _flag(row.get("entry_is_extended")) is True or (_finite(ext) and float(ext) > 1.25)
    if extended:
        blockers.append("entry extended")

    catalyst = float(row.get("catalyst_score") or 0.0)
    turnover = float(row.get("turnover_percentile") or 0.0)
    loc = float(row.get("price_location_score") or 50.0)
    sector_rank = float(row.get("sector_rank_percentile") or 50.0)
    stock_sector_lead = row.get("stock_sector_lead_pct")
    leadership = 0.0
    if turnover >= 80:
        leadership += 8
    elif turnover >= 65:
        leadership += 4
    if sector_rank >= 80:
        leadership += 5
    if _finite(stock_sector_lead):
        directed = float(stock_sector_lead) if direction == "Bullish" else -float(stock_sector_lead)
        if directed >= 0.50:
            leadership += 7
        elif directed >= 0.20:
            leadership += 3

    sponsorship = sponsorship_score(
        direction=direction,
        tod_rvol=row.get("tod_rvol"),
        oi_confirmed=row.get("oi_confirmed", row.get("oi_recent_agrees")),
        basis_pct=row.get("basis_pct"),
        basis_acceleration=row.get("basis_acceleration"),
    )

    setup = 0.0
    if recent:
        setup += 12
    if _flag(row.get("fresh_breakout")) is True:
        setup += 5
    if _flag(row.get("breakout_retained")) is True:
        setup += 6
    if _flag(row.get("retest_confirmed")) is True or _flag(row.get("breakout_retest_confirmed")) is True:
        setup += 5
    if _flag(row.get("vwap_side_agrees")) is True:
        setup += 4
    if _finite(ext) and float(ext) <= 0.75:
        setup += 3

    catalyst_component = min(15.0, catalyst * 0.15)
    location_component = min(10.0, max(0.0, loc) * 0.10)
    regime_component = _regime_score(direction, row.get("market_regime"))
    execution_quality = row.get("execution_5m_quality")
    execution_component = 0.0
    if _finite(execution_quality):
        execution_component = min(8.0, max(0.0, float(execution_quality)) * 0.08)

    score = setup + catalyst_component + leadership + location_component + sponsorship["score"] + regime_component + execution_component
    if _flag(row.get("htf_agrees")) is True:
        score += 4
    elif _flag(row.get("htf_agrees")) is False:
        score -= 4
    score = float(np.clip(score, 0, 100))

    # Eligibility focuses on setup/participation/quality; OI is NOT a hard gate.
    if catalyst < 35:
        blockers.append("stock-in-play evidence weak")
    if turnover < 55:
        blockers.append("cross-sectional turnover not strong")
    if not sponsorship["sponsored"] and catalyst < 70:
        blockers.append("no strong sponsorship/catalyst substitute")
    if _finite(execution_quality) and float(execution_quality) < 50:
        blockers.append("5-minute execution weak")

    intraday = bool(recent and not blockers and score >= 68)
    # Unknown 5m is neutral.  Once measured, a weak 5m execution can reject.
    short_research_only = direction == "Bearish"
    swing = bool(
        direction == "Bullish" and recent and score >= 76
        and _flag(row.get("breakout_retained")) is True
        and (_flag(row.get("retest_confirmed")) is True or _flag(row.get("breakout_retest_confirmed")) is True)
        and _flag(row.get("htf_agrees")) is True
        and _flag(row.get("sector_agrees")) is not False
        and row.get("market_regime") not in ("Trend Down",)
        and not extended
        and (sponsorship["sponsored"] or catalyst >= 75)
    )

    if swing:
        stage = "V6 Swing 1-2D"
    elif intraday:
        stage = "V6 Intraday Entry"
    elif recent and sponsorship["sponsored"]:
        stage = "Sponsored Recent-Range"
    elif recent:
        stage = "Recent-Range Setup"
    else:
        stage = "Breakout Research"
    return {
        "stage": stage,
        "score": round(score, 1),
        "intraday_eligible": intraday,
        "swing_eligible": swing,
        "short_research_only": short_research_only,
        "blockers": blockers,
        "sponsorship": sponsorship,
        "edge_priority": (5 if swing else 0) + (4 if intraday else 0) + (2 if sponsorship["sponsored"] else 0),
    }


def five_minute_execution_quality(df: pd.DataFrame, *, direction: str, breakout_level,
                                  atr, signal_time) -> dict:
    """Score post-15m execution using only 5m bars at/after signal close."""
    if df is None or df.empty or not (_finite(breakout_level) and _finite(atr)):
        return {"available": False, "quality": None, "retained": None, "extended": None}
    frame = df.copy()
    frame.index = pd.to_datetime(frame.index)
    start = pd.Timestamp(signal_time)
    # Normalise timezone mismatch conservatively.
    try:
        if frame.index.tz is not None and start.tzinfo is None:
            start = start.tz_localize(frame.index.tz)
        elif frame.index.tz is None and start.tzinfo is not None:
            start = start.tz_localize(None)
    except Exception:
        pass
    post = frame.loc[frame.index >= start].head(4)
    if post.empty:
        return {"available": False, "quality": None, "retained": None, "extended": None}
    level, atr = float(breakout_level), float(atr)
    sign = 1 if direction == "Bullish" else -1
    directed_close = sign * (post["close"].astype(float) - level)
    retained = bool((directed_close > 0).iloc[-1])
    # A probe toward the level followed by a close on the correct side.
    tol = 0.20 * atr
    if direction == "Bullish":
        retest = bool(((post["low"].astype(float) <= level + tol) & (post["close"].astype(float) > level)).any())
        extension = max(0.0, (float(post["close"].iloc[-1]) - level) / atr)
    else:
        retest = bool(((post["high"].astype(float) >= level - tol) & (post["close"].astype(float) < level)).any())
        extension = max(0.0, (level - float(post["close"].iloc[-1])) / atr)
    extended = extension > 0.95
    vols = pd.to_numeric(post.get("volume"), errors="coerce") if "volume" in post else pd.Series(dtype=float)
    vol_burst = False
    if not vols.empty and vols.notna().sum() >= 3:
        med = float(vols.median())
        vol_burst = bool(med > 0 and float(vols.max()) >= 1.35 * med)
    quality = 25.0
    if retained:
        quality += 30
    if retest:
        quality += 20
    if vol_burst:
        quality += 15
    if not extended:
        quality += 10
    return {
        "available": True,
        "quality": round(min(100.0, quality), 1),
        "retained": retained,
        "retest": retest,
        "volume_burst": vol_burst,
        "extended": extended,
        "extension_atr": round(extension, 3),
    }


def _net_return(entry: float, exit_px: float, direction: str, cost_pct: float, slippage_pct: float) -> float:
    return costs.net_return_pct(
        entry, exit_px, direction, cost_pct=cost_pct, slippage_pct=slippage_pct
    )


def first_touch_exit(df: pd.DataFrame, *, entry_pos: int, direction: str, entry_price: float,
                     atr: float, target_atr: float, stop_atr: float,
                     cost_pct=0.05, slippage_pct=0.02, max_bars: int | None = None) -> dict:
    """Conservative target/stop first-touch simulation.

    When both target and stop are inside the same OHLC bar, the stop wins.  This
    avoids the optimistic intrabar ordering assumption that would otherwise
    flatter the strategy.
    """
    if df is None or df.empty or atr <= 0 or entry_pos >= len(df):
        return {"outcome": "unavailable", "net_return_pct": None, "bars": None}
    sign = 1.0 if direction == "Bullish" else -1.0
    target_px = entry_price + sign * float(target_atr) * atr
    stop_px = entry_price - sign * float(stop_atr) * atr
    end = len(df) if max_bars is None else min(len(df), entry_pos + 1 + int(max_bars))
    for pos in range(entry_pos + 1, end):
        hi, lo = float(df["high"].iloc[pos]), float(df["low"].iloc[pos])
        if direction == "Bullish":
            stop_hit, target_hit = lo <= stop_px, hi >= target_px
        else:
            stop_hit, target_hit = hi >= stop_px, lo <= target_px
        if stop_hit:  # conservative if both touched on same bar
            return {"outcome": "stop", "net_return_pct": round(_net_return(entry_price, stop_px, direction, cost_pct, slippage_pct), 4), "bars": pos - entry_pos}
        if target_hit:
            return {"outcome": "target", "net_return_pct": round(_net_return(entry_price, target_px, direction, cost_pct, slippage_pct), 4), "bars": pos - entry_pos}
    if end <= entry_pos + 1:
        return {"outcome": "unavailable", "net_return_pct": None, "bars": None}
    exit_px = float(df["close"].iloc[end - 1])
    return {"outcome": "timeout", "net_return_pct": round(_net_return(entry_price, exit_px, direction, cost_pct, slippage_pct), 4), "bars": end - 1 - entry_pos}


def three_way_split(events: Iterable[dict], dev_pct: float = 60.0, validation_pct: float = 20.0):
    rows = sorted(list(events or []), key=lambda e: e.get("entry_time", ""))
    n = len(rows)
    i = int(np.floor(n * dev_pct / 100.0))
    j = int(np.floor(n * (dev_pct + validation_pct) / 100.0))
    return rows[:i], rows[i:j], rows[j:]


def final_test_payload(stats: dict) -> dict:
    # V7 permanently locks every legacy V6 final-test surface.  The only final
    # sample allowed to reveal is the separately fingerprinted frozen rule in
    # app.v7_frozen; this prevents post-hoc fishing across old variants.
    return {"locked": True, "message": "Legacy V6 final tests remain locked in V7; only the frozen V7 rule may reveal the final 20%."}


def first_touch_grid(df: pd.DataFrame, *, entry_pos: int, direction: str, entry_price: float,
                     atr: float, pairs=((0.5, 0.5), (0.75, 0.5), (1.0, 0.5),
                                       (1.0, 0.75), (1.25, 0.75), (1.5, 1.0)),
                     cost_pct=0.05, slippage_pct=0.02, max_bars: int | None = None) -> dict:
    out = {}
    for target, stop in pairs:
        key = f"T{float(target):.2f}_S{float(stop):.2f}"
        out[key] = first_touch_exit(
            df, entry_pos=entry_pos, direction=direction, entry_price=entry_price,
            atr=atr, target_atr=float(target), stop_atr=float(stop),
            cost_pct=cost_pct, slippage_pct=slippage_pct, max_bars=max_bars,
        )
    return out


def breakeven_after_trigger_exit(df: pd.DataFrame, *, entry_pos: int, direction: str,
                                 entry_price: float, atr: float, trigger_atr: float = 0.5,
                                 initial_stop_atr: float = 0.5, target_atr: float = 1.25,
                                 cost_pct=0.05, slippage_pct=0.02,
                                 max_bars: int | None = None) -> dict:
    if df is None or df.empty or atr <= 0 or entry_pos >= len(df):
        return {"outcome": "unavailable", "net_return_pct": None, "bars": None, "breakeven_armed": False}
    sign = 1.0 if direction == "Bullish" else -1.0
    target_px = entry_price + sign * target_atr * atr
    stop_px = entry_price - sign * initial_stop_atr * atr
    trigger_px = entry_price + sign * trigger_atr * atr
    armed = False
    end = len(df) if max_bars is None else min(len(df), entry_pos + 1 + int(max_bars))
    for pos in range(entry_pos + 1, end):
        hi, lo = float(df["high"].iloc[pos]), float(df["low"].iloc[pos])
        if direction == "Bullish":
            target_hit = hi >= target_px
            trigger_hit = hi >= trigger_px
            active_stop = entry_price if armed else stop_px
            stop_hit = lo <= active_stop
        else:
            target_hit = lo <= target_px
            trigger_hit = lo <= trigger_px
            active_stop = entry_price if armed else stop_px
            stop_hit = hi >= active_stop

        # Same-bar ambiguity remains conservative: an already-active stop wins.
        if stop_hit:
            label = "breakeven" if armed else "stop"
            return {
                "outcome": label,
                "net_return_pct": round(_net_return(entry_price, active_stop, direction, cost_pct, slippage_pct), 4),
                "bars": pos - entry_pos,
                "breakeven_armed": armed,
            }
        if target_hit:
            return {
                "outcome": "target",
                "net_return_pct": round(_net_return(entry_price, target_px, direction, cost_pct, slippage_pct), 4),
                "bars": pos - entry_pos,
                "breakeven_armed": armed or trigger_hit,
            }
        if trigger_hit:
            armed = True

    if end <= entry_pos + 1:
        return {"outcome": "unavailable", "net_return_pct": None, "bars": None, "breakeven_armed": armed}
    exit_px = float(df["close"].iloc[end - 1])
    return {
        "outcome": "timeout",
        "net_return_pct": round(_net_return(entry_price, exit_px, direction, cost_pct, slippage_pct), 4),
        "bars": end - 1 - entry_pos,
        "breakeven_armed": armed,
    }


def _return_stats(events: Iterable[dict], field: str, key: str) -> dict:
    vals = []
    for e in events or []:
        payload = e.get(field) or {}
        value = payload.get(key) if isinstance(payload, dict) else None
        if _finite(value):
            vals.append(float(value))
    if not vals:
        return {"trade_count": 0, "win_rate_pct": None, "avg_return_pct": None,
                "median_return_pct": None, "profit_factor": None}
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    gp, gl = float(sum(wins)), abs(float(sum(losses)))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else None)
    return {
        "trade_count": len(vals),
        "win_rate_pct": round(len(wins) / len(vals) * 100.0, 1),
        "avg_return_pct": round(float(np.mean(vals)), 3),
        "median_return_pct": round(float(np.median(vals)), 3),
        "profit_factor": round(float(pf), 2) if pf is not None and np.isfinite(pf) else pf,
    }


def three_way_research_report(events: Iterable[dict], *, field: str, key: str) -> dict:
    dev, validation, final = three_way_split(events)
    dev_stats = _return_stats(dev, field, key)
    val_stats = _return_stats(validation, field, key)
    final_stats = _return_stats(final, field, key)
    return {
        "development": dev_stats,
        "validation": val_stats,
        "final_test": final_test_payload(final_stats),
        "split": {"development_pct": 60, "validation_pct": 20, "final_pct": 20},
    }
