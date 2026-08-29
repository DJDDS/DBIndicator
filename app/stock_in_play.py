"""NSE stock-F&O stock-in-play and breakout engine.

Direction is assigned by an actual 15-minute price escape, not by RSI/MACD
voting.  Compression and abnormal participation are directionless radar inputs;
OI/volume/context sponsor the move after price reveals the side.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import numpy as np
import pandas as pd

RECENT_RANGE_BARS = 6          # 90 minutes on the 15-minute engine
OPENING_RANGE_BARS = 2         # 30-minute opening range
COMPRESSION_RADAR_SCORE = 60.0
TOD_RVOL_MIN = 1.30
OPENING_RVOL_MIN = 1.20
TOD_RVOL_STRONG_NO_OI = 1.60
MAX_BREAKOUT_EXTENSION_ATR = 1.25
SWING_EARLIEST_TIME = dt.time(14, 15)



def depth_shadow_metrics(quote: dict | None) -> dict:
    """Top-5 futures order-book microstructure metrics for forward research.

    Kite exposes five levels of depth in full quotes, but not historical depth.
    These fields are therefore explicitly *shadow only*: they are recorded and
    forward-tested, never used to make a Best Entry eligible until live evidence
    clears the research benchmark.
    """
    quote = quote or {}
    depth = quote.get("depth") or {}
    buys = [x for x in (depth.get("buy") or []) if x.get("price") is not None]
    sells = [x for x in (depth.get("sell") or []) if x.get("price") is not None]
    result = {
        "depth_imbalance": None, "spread_bps": None,
        "microprice_bias_bps": None, "shadow_only": True,
    }
    if not buys or not sells:
        return result
    bid = float(buys[0]["price"]); ask = float(sells[0]["price"]); mid = (bid + ask) / 2.0
    if mid <= 0 or ask < bid:
        return result
    buy_qty = float(sum(max(0, x.get("quantity") or 0) for x in buys))
    sell_qty = float(sum(max(0, x.get("quantity") or 0) for x in sells))
    total = buy_qty + sell_qty
    if total > 0:
        result["depth_imbalance"] = (buy_qty - sell_qty) / total
    result["spread_bps"] = (ask - bid) / mid * 10000.0
    bq = float(max(0, buys[0].get("quantity") or 0)); aq = float(max(0, sells[0].get("quantity") or 0))
    if bq + aq > 0:
        micro = (ask * bq + bid * aq) / (bq + aq)
        result["microprice_bias_bps"] = (micro - mid) / mid * 10000.0
    return result


def _live_setting(name, default):
    try:
        from .config import settings
        return float(getattr(settings, name, default))
    except Exception:
        return float(default)


def _series(v, index):
    if isinstance(v, pd.Series):
        return v.reindex(index)
    return pd.Series(v, index=index, dtype=float)


def _session_keys(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(index.normalize(), index=index)


def _opening_range(df: pd.DataFrame):
    sessions = _session_keys(df.index)
    ordinal = sessions.groupby(sessions).cumcount()
    hi = df["high"].groupby(sessions).transform(lambda x: x.iloc[:OPENING_RANGE_BARS].max())
    lo = df["low"].groupby(sessions).transform(lambda x: x.iloc[:OPENING_RANGE_BARS].min())
    # The opening range becomes actionable only after both bars have closed.
    hi = hi.where(ordinal >= OPENING_RANGE_BARS)
    lo = lo.where(ordinal >= OPENING_RANGE_BARS)
    return hi, lo


def _gap_atr(df: pd.DataFrame, atr: pd.Series) -> pd.Series:
    sessions = _session_keys(df.index)
    session_open = df["open"].groupby(sessions).transform("first")
    session_last_close = df["close"].groupby(sessions).last()
    prev_close_by_session = session_last_close.shift(1)
    prev_close = sessions.map(prev_close_by_session)
    return (session_open - prev_close) / atr.replace(0, np.nan)


def build_price_features(df: pd.DataFrame, atr, compression=None, tod_rvol=None, opening_rvol=None) -> pd.DataFrame:
    """Vectorized, no-lookahead 15-minute breakout/radar features.

    The decision range always excludes the current bar. A compression breakout
    means a coil existed in one of the preceding four bars; otherwise an opening
    range or generic recent-range escape can still identify a stock-in-play move.
    """
    if df is None or df.empty:
        return pd.DataFrame(index=getattr(df, "index", None))
    idx = df.index
    atr = _series(atr, idx)
    tod = _series(tod_rvol if tod_rvol is not None else np.nan, idx)
    opening = _series(opening_rvol if opening_rvol is not None else np.nan, idx)
    if compression is None:
        comp_score = pd.Series(np.nan, index=idx)
    elif isinstance(compression, pd.DataFrame):
        comp_score = pd.to_numeric(compression.get("compression_score"), errors="coerce").reindex(idx)
    else:
        comp_score = pd.to_numeric(_series(compression, idx), errors="coerce")

    sessions = _session_keys(idx)
    # The recent decision range is intraday-only. Overnight gaps belong to the
    # gap/stock-in-play layer and must not masquerade as a six-bar breakout.
    recent_hi = df["high"].groupby(sessions).transform(
        lambda x: x.shift(1).rolling(RECENT_RANGE_BARS, min_periods=RECENT_RANGE_BARS).max())
    recent_lo = df["low"].groupby(sessions).transform(
        lambda x: x.shift(1).rolling(RECENT_RANGE_BARS, min_periods=RECENT_RANGE_BARS).min())
    orb_hi, orb_lo = _opening_range(df)

    bull_recent = df["close"] > recent_hi
    bear_recent = df["close"] < recent_lo
    bull_orb = orb_hi.notna() & (df["close"] > orb_hi)
    bear_orb = orb_lo.notna() & (df["close"] < orb_lo)

    compression_threshold = _live_setting("COMPRESSION_RADAR_SCORE", COMPRESSION_RADAR_SCORE)
    compression_recent = comp_score.shift(1).rolling(4, min_periods=1).max().ge(compression_threshold)
    bull = bull_recent | bull_orb
    bear = bear_recent | bear_orb
    direction = pd.Series(np.where(bull & ~bear, "Bullish", np.where(bear & ~bull, "Bearish", None)), index=idx, dtype=object)

    source = pd.Series(None, index=idx, dtype=object)
    source = source.mask(direction.notna() & compression_recent, "Compression")
    source = source.mask(source.isna() & (bull_orb | bear_orb), "Opening Range")
    source = source.mask(source.isna() & direction.notna(), "Recent Range")

    level = pd.Series(np.nan, index=idx, dtype=float)
    is_bull = direction.eq("Bullish")
    is_bear = direction.eq("Bearish")
    # Source-specific trigger levels. Compression uses the same recent range
    # because the current bar must escape a range that existed before it.
    level = level.mask(is_bull & source.eq("Opening Range"), orb_hi)
    level = level.mask(is_bear & source.eq("Opening Range"), orb_lo)
    level = level.mask(is_bull & ~source.eq("Opening Range"), recent_hi)
    level = level.mask(is_bear & ~source.eq("Opening Range"), recent_lo)

    ext = pd.Series(np.nan, index=idx, dtype=float)
    ext = ext.mask(is_bull, (df["close"] - level) / atr.replace(0, np.nan))
    ext = ext.mask(is_bear, (level - df["close"]) / atr.replace(0, np.nan))

    # A breakout event fires only on the first escape in a run. Consecutive
    # new highs/lows are continuation of the same event, not fresh entries.
    same_session_prev = sessions.eq(sessions.shift(1))
    prev_dir = direction.shift(1).where(same_session_prev)
    fresh = direction.notna() & direction.fillna("").ne(prev_dir.fillna(""))

    # Retention is intentionally a one-bar-later fact. It is used by the swing
    # classifier independently of `fresh`, so waiting for confirmation does not
    # make 1–2D candidates impossible.
    retained = pd.Series(False, index=idx)
    prev_level = level.shift(1).where(same_session_prev)
    retained |= prev_dir.eq("Bullish") & (df["close"] > prev_level)
    retained |= prev_dir.eq("Bearish") & (df["close"] < prev_level)
    retained_dir = prev_dir.where(retained)
    retained_source = source.shift(1).where(retained & same_session_prev)
    retained_level = prev_level.where(retained)
    retained_ext = pd.Series(np.nan, index=idx, dtype=float)
    retained_ext = retained_ext.mask(retained_dir.eq("Bullish"), (df["close"] - retained_level) / atr.replace(0, np.nan))
    retained_ext = retained_ext.mask(retained_dir.eq("Bearish"), (retained_level - df["close"]) / atr.replace(0, np.nan))

    # A retest is a one-bar-later confirmation, never a fact available on the
    # breakout bar itself.  The confirmation bar must probe back to within
    # 0.20 ATR of the escaped level and still CLOSE on the breakout side.
    # This makes the research entry executable on the following bar without
    # look-ahead and lets us compare first-escape vs retest entries honestly.
    retest_tolerance = atr * 0.20
    retest_confirmed = pd.Series(False, index=idx)
    retest_confirmed |= (
        retained & retained_dir.eq("Bullish")
        & (df["low"] <= retained_level + retest_tolerance)
        & (df["close"] > retained_level)
    )
    retest_confirmed |= (
        retained & retained_dir.eq("Bearish")
        & (df["high"] >= retained_level - retest_tolerance)
        & (df["close"] < retained_level)
    )

    gap_atr = _gap_atr(df, atr)
    bar_range_atr = (df["high"] - df["low"]) / atr.replace(0, np.nan)
    energy = comp_score.ge(compression_threshold)
    stock_in_play = (
        tod.ge(_live_setting("TOD_RVOL_MIN", TOD_RVOL_MIN))
        | opening.ge(_live_setting("OPENING_RVOL_MIN", OPENING_RVOL_MIN))
        | gap_atr.abs().ge(0.50)
        | bar_range_atr.ge(1.0)
    )

    return pd.DataFrame({
        "recent_range_high": recent_hi,
        "recent_range_low": recent_lo,
        "opening_range_high": orb_hi,
        "opening_range_low": orb_lo,
        "breakout_direction": direction,
        "fresh_breakout": fresh,
        "breakout_retained": retained,
        "retained_breakout_direction": retained_dir,
        "retained_breakout_source": retained_source,
        "retained_breakout_level": retained_level,
        "retained_breakout_extension_atr": retained_ext,
        "breakout_retest_confirmed": retest_confirmed,
        "breakout_source": source,
        "breakout_level": level,
        "breakout_extension_atr": ext,
        "gap_atr": gap_atr,
        "bar_range_atr": bar_range_atr,
        "opening_rvol": opening,
        "stock_in_play": stock_in_play,
        "energy_building": energy,
        "compression_score": comp_score,
    }, index=idx)


def _flag(value):
    """Normalize Python/pandas/NumPy boolean-like values to True/False/None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    # np.where(..., boolean, np.nan) promotes boolean research flags to
    # floating 1.0/0.0. Treat only those exact numeric sentinels as booleans;
    # arbitrary numeric values remain unknown.
    if isinstance(value, (int, float, np.integer, np.floating)):
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return None
        if fv == 1.0:
            return True
        if fv == 0.0:
            return False
    return None


def _is_finite_number(value):
    """True only for real finite numeric observations; NaN is missing data."""
    if value is None:
        return False
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _directional(value, direction):
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    return float(value) if direction == "Bullish" else -float(value)


def _parse_time(value):
    if value is None:
        return None
    try:
        return pd.Timestamp(value).time()
    except Exception:
        return None


def classify_live_candidate(row: dict) -> dict:
    """Classify one latest live row using the evidence-led recent-range funnel.

    Generic opening/compression escapes remain visible for research/radar, but
    the strongest live labels are deliberately narrower because the current
    holdout data shows Recent Range materially outperforming those sources.
    Intraday Best Entry therefore requires a sponsored Recent-Range escape; a
    1–2D Swing candidate additionally requires bullish retention and explicit
    4H agreement.
    """
    direction = row.get("breakout_direction") or row.get("retained_breakout_direction")
    energy = bool(row.get("energy_building"))
    in_play = bool(row.get("stock_in_play"))
    fresh = bool(row.get("fresh_breakout"))
    blockers = []
    tod_min = _live_setting("TOD_RVOL_MIN", TOD_RVOL_MIN)
    tod_strong_no_oi = _live_setting("TOD_RVOL_STRONG_NO_OI", TOD_RVOL_STRONG_NO_OI)
    max_extension = _live_setting("MAX_ENTRY_EXTENSION_ATR", MAX_BREAKOUT_EXTENSION_ATR)

    oi60 = row.get("oi_chg_60m_pct")
    oi30 = row.get("oi_chg_30m_pct")
    accel = row.get("oi_acceleration")
    oi_available = (
        any(_is_finite_number(v) for v in (oi60, oi30, accel))
        or _flag(row.get("oi_recent_agrees")) is not None
    )
    oi_confirmed = bool(
        _flag(row.get("oi_recent_agrees")) is True
        and _is_finite_number(oi60) and float(oi60) > 0
        and (not _is_finite_number(accel) or float(accel) >= -0.15)
    )
    oi_status = "Confirmed" if oi_confirmed else ("Not Confirming" if oi_available else "Unavailable")

    tod = row.get("tod_rvol")
    tod_ok = _is_finite_number(tod) and float(tod) >= tod_min
    source = row.get("breakout_source") or row.get("retained_breakout_source")
    recent_range = source == "Recent Range"
    rs_dir = _directional(row.get("rs_pct"), direction) if direction else None
    strong_alt = bool(
        not oi_available
        and _is_finite_number(tod) and float(tod) >= tod_strong_no_oi
        and rs_dir is not None and rs_dir >= 0.50
        and _flag(row.get("sector_agrees")) is True
    )

    if not direction:
        stage = "Energy Building" if energy else ("Stock in Play" if in_play else None)
        return {
            "direction": None, "stage": stage, "intraday_eligible": False,
            "swing_eligible": False, "oi_status": oi_status, "score": None,
            "blockers": ["waiting for price breakout"] if stage else [],
            "edge_priority": 0,
        }

    if not fresh:
        blockers.append("breakout not fresh")
    if not recent_range:
        blockers.append("generic breakout kept in research only")
    if _flag(row.get("vwap_side_agrees")) is not True:
        blockers.append("wrong side of VWAP")
    if not tod_ok:
        blockers.append(f"time-of-day participation below {tod_min:.2f}x")
    ext = row.get("breakout_extension_atr")
    if _flag(row.get("entry_is_extended")) is True or (_is_finite_number(ext) and float(ext) > max_extension):
        blockers.append("breakout already extended")
    # The validated interaction table shows volume + OI is the best current
    # sponsorship combination.  Keep alternate sponsorship visible in score,
    # but do not call it a Best Entry until the Recent-Range lab proves it.
    if not oi_confirmed:
        blockers.append("OI not confirming" if oi_available else "OI unavailable")
    if _flag(row.get("sector_agrees")) is False and _flag(row.get("htf_agrees")) is False:
        blockers.append("sector and 4H context both oppose")

    intraday = recent_range and not blockers
    retest = _flag(row.get("breakout_retest_confirmed")) is True

    score = 0.0
    if fresh:
        score += 25
    if recent_range:
        score += 15
    if _flag(row.get("vwap_side_agrees")) is True:
        score += 10
    if _is_finite_number(tod):
        score += 20 if float(tod) >= tod_min else 0
    if oi_confirmed:
        score += 20
    elif strong_alt:
        score += 8
    if _flag(row.get("htf_agrees")) is True:
        score += 5
    if _flag(row.get("sector_agrees")) is True:
        score += 3
    if _is_finite_number(ext) and float(ext) <= 0.75:
        score += 2
    if retest:
        score += 5
    score = min(100.0, score)

    timestamp_time = _parse_time(row.get("timestamp"))
    swing = bool(
        timestamp_time is not None and timestamp_time >= SWING_EARLIEST_TIME
        and direction == "Bullish"
        and recent_range
        and _flag(row.get("breakout_retained")) is True
        and _flag(row.get("vwap_side_agrees")) is True
        and tod_ok
        and not (_flag(row.get("entry_is_extended")) is True or (_is_finite_number(ext) and float(ext) > max_extension))
        and _flag(row.get("htf_agrees")) is True
        and _flag(row.get("sector_agrees")) is not False
        and oi_confirmed
    )

    if swing:
        stage = "High-Quality Swing 1-2D"
    elif intraday:
        stage = "Intraday Best Entry"
    elif recent_range and tod_ok and oi_confirmed:
        stage = "Sponsored Recent-Range"
    elif recent_range:
        stage = "Recent-Range Breakout"
    else:
        stage = "Breakout Research"

    edge_priority = (
        (4 if swing else 0)
        + (3 if intraday else 0)
        + (2 if recent_range and tod_ok and oi_confirmed else 0)
        + (1 if retest else 0)
    )
    return {
        "direction": direction,
        "stage": stage,
        "intraday_eligible": intraday,
        "swing_eligible": swing,
        "oi_status": oi_status,
        "score": round(score, 1),
        "blockers": blockers,
        "edge_priority": edge_priority,
        "retest_confirmed": retest,
    }


def _net_return(entry, exit_px, direction, cost_pct, slippage_pct):
    raw = (float(exit_px) / float(entry) - 1.0) * 100.0
    if direction == "Bearish":
        raw = -raw
    return raw - max(0.0, float(cost_pct)) - max(0.0, float(slippage_pct))


def compute_trade_outcomes(df: pd.DataFrame, signal_pos: int, direction: str, atr: float,
                           cost_pct=0.05, slippage_pct=0.02) -> dict:
    """Return intraday and 1–2 session outcomes from next executable bar."""
    entry_pos = int(signal_pos) + 1
    if entry_pos >= len(df):
        return {"entry_pos": None, "intraday": {}, "swing": {}}
    entry = float(df["open"].iloc[entry_pos])
    sessions = pd.Series(df.index.normalize(), index=df.index)
    entry_session = sessions.iloc[entry_pos]
    intraday = {}
    for label, bars in (("30m", 2), ("1h", 4), ("2h", 8), ("4h", 16)):
        exit_pos = entry_pos + bars - 1
        if exit_pos < len(df) and sessions.iloc[exit_pos] == entry_session:
            intraday[label] = _net_return(entry, df["close"].iloc[exit_pos], direction, cost_pct, slippage_pct)
    same_positions = np.flatnonzero(sessions.eq(entry_session).to_numpy())
    same_positions = same_positions[same_positions >= entry_pos]
    if len(same_positions):
        intraday["eod"] = _net_return(entry, df["close"].iloc[same_positions[-1]], direction, cost_pct, slippage_pct)

    unique_sessions = list(pd.unique(sessions.iloc[entry_pos:]))
    swing = {}
    session_exit_pos = {}
    for n, label in ((1, "1D"), (2, "2D")):
        if len(unique_sessions) > n:
            target = unique_sessions[n]
            pos = np.flatnonzero(sessions.eq(target).to_numpy())
            if len(pos):
                exit_pos = int(pos[-1])
                swing[label] = _net_return(entry, df["close"].iloc[exit_pos], direction, cost_pct, slippage_pct)
                session_exit_pos[label] = exit_pos

    # Excursions through the longest available (2D preferred) evaluation window.
    final_pos = session_exit_pos.get("2D", session_exit_pos.get("1D"))
    if final_pos is None:
        final_pos = int(same_positions[-1]) if len(same_positions) else entry_pos
    highs = df["high"].iloc[entry_pos:final_pos + 1].astype(float)
    lows = df["low"].iloc[entry_pos:final_pos + 1].astype(float)
    if direction == "Bullish":
        fav = (highs - entry) / float(atr)
        adv = (entry - lows) / float(atr)
    else:
        fav = (entry - lows) / float(atr)
        adv = (highs - entry) / float(atr)
    mfe_atr = {"2D": max(0.0, float(fav.max())) if len(fav) else 0.0}
    mae_atr = {"2D": max(0.0, float(adv.max())) if len(adv) else 0.0}
    if "1D" in session_exit_pos:
        p = session_exit_pos["1D"]
        hh = df["high"].iloc[entry_pos:p + 1].astype(float)
        ll = df["low"].iloc[entry_pos:p + 1].astype(float)
        f = (hh - entry) / atr if direction == "Bullish" else (entry - ll) / atr
        a = (entry - ll) / atr if direction == "Bullish" else (hh - entry) / atr
        mfe_atr["1D"] = max(0.0, float(f.max()))
        mae_atr["1D"] = max(0.0, float(a.max()))

    def _time_to(target):
        hits = np.flatnonzero(fav.values >= target)
        return int(hits[0] + 1) if len(hits) else None

    return {
        "entry_pos": entry_pos,
        "entry_price": entry,
        "intraday": intraday,
        "swing": swing,
        "mfe_atr": mfe_atr,
        "mae_atr": mae_atr,
        "time_to_0_5atr_bars": _time_to(0.5),
        "time_to_1atr_bars": _time_to(1.0),
    }


def expansion_lift_table(event_vals: Iterable[dict], baseline_vals: Iterable[dict],
                         horizons=("4", "8", "16", "25"),
                         thresholds=(0.5, 0.75, 1.0, 1.5)):
    rows = []
    events = list(event_vals or [])
    baseline = list(baseline_vals or [])
    for h in horizons:
        for threshold in thresholds:
            ev = [e.get("future_abs_move_atr", {}).get(str(h), e.get("future_abs_move_atr", {}).get(int(h))) for e in events]
            bl = [e.get("future_abs_move_atr", {}).get(str(h), e.get("future_abs_move_atr", {}).get(int(h))) for e in baseline]
            ev = [float(v) for v in ev if v is not None and np.isfinite(v)]
            bl = [float(v) for v in bl if v is not None and np.isfinite(v)]
            er = sum(v >= threshold for v in ev) / len(ev) * 100.0 if ev else None
            br = sum(v >= threshold for v in bl) / len(bl) * 100.0 if bl else None
            lift = (er / br) if er is not None and br not in (None, 0) else None
            rows.append({
                "horizon": str(h), "threshold_atr": float(threshold),
                "event_n": len(ev), "baseline_n": len(bl),
                "event_hit_rate_pct": round(er, 1) if er is not None else None,
                "baseline_hit_rate_pct": round(br, 1) if br is not None else None,
                "lift": round(lift, 2) if lift is not None else None,
            })
    return rows


def interaction_variants(events):
    events = list(events or [])
    tod_min = _live_setting("TOD_RVOL_MIN", TOD_RVOL_MIN)
    return {
        "breakout_only": events,
        "breakout_plus_volume": [e for e in events if (e.get("tod_rvol") or 0) >= tod_min],
        "breakout_plus_oi": [e for e in events if e.get("oi_status") == "Confirmed"],
        "breakout_plus_volume_oi": [e for e in events if (e.get("tod_rvol") or 0) >= tod_min and e.get("oi_status") == "Confirmed"],
        "breakout_plus_4h": [e for e in events if _flag(e.get("htf_agrees")) is True],
        "live_quality_stack": [e for e in events if _flag(e.get("vwap_side_agrees")) is True and _flag(e.get("entry_is_extended")) is False and (e.get("tod_rvol") or 0) >= tod_min],
    }
