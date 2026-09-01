"""V9.3 component-edge and anticipation research.

Research-only.  Nothing in this module can activate a production playbook.
The purpose is to measure independent evidence streams before combining them.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable

import numpy as np
import pandas as pd

BUILD_ID = "2026-09-01-INSTITUTIONAL-V9.3.4-RESEARCH-WORKER-STABILITY-SLIM"
TRIAL_NUMBER = 13
FAMILYWISE_ALPHA = 0.05
OI_ACCEL_MODERATE_PP = 0.5
TOD_RVOL_MIN = 1.3
COMPRESSION_MIN = 60.0


def trial13_spec() -> dict:
    return {
        "trial_number": TRIAL_NUMBER,
        "name": "Silent OI Build -> Ignition",
        "oi_z_min": 1.5,
        "price_flat_max_atr": 0.5,
        "lead_window_bars": 4,
        "max_entry_extension_atr": 1.25,
        "absolute_regime_gate": "NIFTY 8-bar return sign must agree with breakout direction",
        "primary_horizon": "1D",
        "secondary_horizon": "2D",
        "final_20_locked": True,
        "familywise_alpha": FAMILYWISE_ALPHA,
        "bonferroni_alpha": FAMILYWISE_ALPHA / TRIAL_NUMBER,
        "research_only": True,
        "message": (
            "Pre-registered before outcome inspection: unusual positive intraday OI while 60-minute "
            "price displacement remains within 0.5 ATR; wait at most four 15-minute bars for the "
            "first fresh breakout, reject >1.25 ATR extension, require the breakout direction to "
            "agree with the sign of NIFTY's completed 8-bar return, and evaluate 1D primary / 2D secondary."
        ),
    }


def _same_tz_index(index: pd.DatetimeIndex, tz):
    idx = pd.DatetimeIndex(index)
    if idx.tz is None and tz is not None:
        idx = idx.tz_localize(tz)
    elif idx.tz is not None and tz is None:
        idx = idx.tz_localize(None)
    elif idx.tz is not None and tz is not None:
        idx = idx.tz_convert(tz)
    return idx


def point_in_time_daily_oi_features(daily_oi, intraday_index, *, min_obs=20, window=60) -> pd.DataFrame:
    """Map completed daily continuous-futures OI to intraday bars without look-ahead.

    A daily OI observation is stamped at 15:30 local time, so the same day's
    morning bars cannot see it.  Monday therefore sees Friday's completed OI.
    """
    idx = pd.DatetimeIndex(intraday_index)
    out = pd.DataFrame(index=idx, columns=[
        "daily_oi_level_pti", "daily_oi_chg_pct_pti", "daily_oi_z_pti"
    ], dtype=float)
    if daily_oi is None:
        return out
    s = pd.Series(daily_oi).dropna().astype(float)
    s = s[s > 0]
    if s.empty:
        return out
    target_tz = idx.tz
    didx = _same_tz_index(pd.DatetimeIndex(s.index), target_tz)
    # Kite daily bars can be midnight-stamped.  Treat the observation as known
    # only after the NSE cash/F&O session is complete.
    eod_idx = didx.normalize() + pd.Timedelta(hours=15, minutes=30)
    s = pd.Series(s.to_numpy(), index=eod_idx).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    chg = s.pct_change() * 100.0
    mu = chg.rolling(int(window), min_periods=int(min_obs)).mean().shift(1)
    sd = chg.rolling(int(window), min_periods=int(min_obs)).std(ddof=1).shift(1)
    z = (chg - mu) / sd.where(sd > 1e-9)
    out["daily_oi_level_pti"] = s.reindex(idx, method="ffill")
    out["daily_oi_chg_pct_pti"] = chg.reindex(idx, method="ffill")
    out["daily_oi_z_pti"] = z.reindex(idx, method="ffill")
    return out


def _wilson(wins: int, n: int, z=1.959963984540054) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = wins / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / den
    return max(0.0, center - half) * 100.0, min(1.0, center + half) * 100.0


def _trade_day(row: dict) -> str | None:
    raw = row.get("signal_time") or row.get("entry_time")
    if not raw:
        return None
    try:
        return pd.Timestamp(raw).date().isoformat()
    except Exception:
        return str(raw)[:10] if len(str(raw)) >= 10 else None


def directional_stats(events: Iterable[dict], field: str, key: str) -> dict:
    rows = list(events or [])
    pairs = []
    for row in rows:
        value = (row.get(field) or {}).get(key)
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            pairs.append((row, v))
    vals = [v for _, v in pairs]
    n = len(vals)
    wins = sum(v > 0 for v in vals)
    pos = sum(v for v in vals if v > 0)
    neg = -sum(v for v in vals if v < 0)
    pf = (pos / neg) if neg > 0 else (float("inf") if pos > 0 else None)
    low, high = _wilson(wins, n)
    mfe = []
    mae = []
    for row, _ in pairs:
        for target, container in (("mfe_atr", mfe), ("mae_atr", mae)):
            try:
                value = float((row.get(target) or {}).get(key))
            except (TypeError, ValueError):
                continue
            if np.isfinite(value):
                container.append(value)
    days = {_trade_day(row) for row, _ in pairs}
    days.discard(None)
    daily_returns = defaultdict(list)
    for row, value in pairs:
        day = _trade_day(row)
        if day is not None:
            daily_returns[day].append(value)
    daily_means = [float(np.mean(vs)) for vs in daily_returns.values() if vs]
    if len(daily_means) >= 2:
        daily_center = float(np.mean(daily_means))
        daily_se = float(np.std(daily_means, ddof=1) / math.sqrt(len(daily_means)))
        cluster_low = daily_center - 1.959963984540054 * daily_se
        cluster_high = daily_center + 1.959963984540054 * daily_se
    else:
        cluster_low = cluster_high = None
    return {
        "trade_count": n,
        "distinct_days": len(days),
        "win_rate_pct": round(wins / n * 100.0, 2) if n else None,
        "win_rate_ci95_low_pct": round(low, 2) if low is not None else None,
        "win_rate_ci95_high_pct": round(high, 2) if high is not None else None,
        "avg_return_pct": round(float(np.mean(vals)), 4) if vals else None,
        "median_return_pct": round(float(np.median(vals)), 4) if vals else None,
        "day_cluster_avg_ci95_low_pct": round(cluster_low, 4) if cluster_low is not None else None,
        "day_cluster_avg_ci95_high_pct": round(cluster_high, 4) if cluster_high is not None else None,
        "profit_factor": round(float(pf), 3) if pf is not None and np.isfinite(pf) else pf,
        "avg_mfe_atr": round(float(np.mean(mfe)), 3) if mfe else None,
        "avg_mae_atr": round(float(np.mean(mae)), 3) if mae else None,
    }


def _split_60_20_20(events: Iterable[dict]):
    """Chronologically split by whole trading days, never by individual events.

    Multiple signals from the same session share one market regime/news cycle and
    must stay in the same partition.  Splitting by event count would leak one
    trading day across development/validation/final.
    """
    rows = sorted(list(events or []), key=lambda e: str(e.get("entry_time") or e.get("signal_time") or ""))
    if not rows:
        return [], [], []

    grouped: dict[str, list[dict]] = defaultdict(list)
    undated: list[dict] = []
    for row in rows:
        day = _trade_day(row)
        if day is None:
            undated.append(row)
        else:
            grouped[day].append(row)

    # If timestamps are unavailable, retain the previous deterministic event split
    # rather than silently dropping observations.  Normal research events are dated.
    if not grouped:
        n = len(rows)
        a = int(n * 0.60)
        b = int(n * 0.80)
        return rows[:a], rows[a:b], rows[b:]

    days = sorted(grouped)
    n_days = len(days)
    a = max(1, int(n_days * 0.60)) if n_days >= 3 else min(1, n_days)
    b = max(a, int(n_days * 0.80))
    if n_days >= 3:
        b = min(max(a + 1, b), n_days - 1)
    else:
        b = min(b, n_days)

    dev_days = set(days[:a])
    val_days = set(days[a:b])
    final_days = set(days[b:])
    dev = [row for day in days if day in dev_days for row in grouped[day]]
    validation = [row for day in days if day in val_days for row in grouped[day]]
    final = [row for day in days if day in final_days for row in grouped[day]]

    # Undated observations cannot be safely assigned to a chronological holdout.
    # Keep them development-only so they never contaminate validation/final evidence.
    dev.extend(undated)
    return dev, validation, final


def _four_blocks(events: Iterable[dict], field: str, key: str) -> list[dict]:
    rows = sorted(list(events or []), key=lambda e: str(e.get("entry_time") or e.get("signal_time") or ""))
    if not rows:
        return []
    chunks = np.array_split(np.array(rows, dtype=object), 4)
    out = []
    for i, chunk in enumerate(chunks, 1):
        stats = directional_stats(list(chunk), field, key)
        out.append({"block": i, **stats})
    return out


def _three_way(events: Iterable[dict], field: str, key: str) -> dict:
    dev, validation, _final = _split_60_20_20(events)
    return {
        "development": directional_stats(dev, field, key),
        "validation": directional_stats(validation, field, key),
        "validation_blocks": _four_blocks(validation, field, key),
    }


def _movement_stats(events: Iterable[dict], key: str, baseline_avg=None) -> dict:
    rows = []
    for row in events or []:
        value = ((row.get("movement_outcomes") or {}).get(key) or {}).get("max_abs_move_atr")
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            rows.append((row, v))
    vals = [v for _, v in rows]
    days = {_trade_day(row) for row, _ in rows}
    days.discard(None)
    avg = float(np.mean(vals)) if vals else None
    return {
        "event_count": len(vals),
        "distinct_days": len(days),
        "avg_max_abs_move_atr": round(avg, 3) if avg is not None else None,
        "median_max_abs_move_atr": round(float(np.median(vals)), 3) if vals else None,
        "hit_0_5atr_pct": round(sum(v >= 0.5 for v in vals) / len(vals) * 100.0, 2) if vals else None,
        "hit_1atr_pct": round(sum(v >= 1.0 for v in vals) / len(vals) * 100.0, 2) if vals else None,
        "hit_1_5atr_pct": round(sum(v >= 1.5 for v in vals) / len(vals) * 100.0, 2) if vals else None,
        "lift_vs_baseline": round(avg / baseline_avg, 3) if avg is not None and baseline_avg not in (None, 0) else None,
    }


def _movement_component_report(rows: list[dict], event_type: str, baseline_rows: list[dict]):
    subset = [e for e in rows if e.get("v93_event_type") == event_type]
    out = {}
    for key in ("2h", "4h", "1D", "2D"):
        baseline = _movement_stats(baseline_rows, key)
        out[key] = _movement_stats(subset, key, baseline.get("avg_max_abs_move_atr"))
    return out


def build_report(events: Iterable[dict], run_context: dict | None = None) -> dict:
    rows = list(events or [])
    ctx = dict(run_context or {})
    trial_rows = [e for e in rows if e.get("v93_trial13_candidate") is True]
    long_buildup = [e for e in rows if e.get("v92_accumulation_seed") is True]
    short_buildup = [e for e in rows if e.get("v93_event_type") == "short_buildup"]
    fresh = [e for e in rows if e.get("fresh_breakout") is True and e.get("direction") in ("Bullish", "Bearish")]
    aligned = [e for e in fresh if e.get("v93_absolute_regime_aligned") is True]
    silent_to_ignition = [e for e in fresh if e.get("v93_silent_oi_lead") is True and not e.get("entry_is_extended")]
    baseline_rows = [e for e in rows if e.get("v93_event_type") == "baseline"]

    # Fixed, pre-existing operational thresholds are used here as descriptive
    # component cuts.  They are not optimized against these outcomes.  The
    # point is to learn which independent streams add signal before combining
    # them into any future playbook.
    effective_atr_floor = float(ctx.get("effective_atr_floor_pct") or 0.24)

    def finite_ge(row, key, threshold):
        value = _finite_float(row.get(key))
        return value is not None and value >= float(threshold)

    def relative_aligned(row):
        rs = _finite_float(row.get("rs_pct"))
        if rs is None:
            return False
        return (row.get("direction") == "Bullish" and rs > 0) or (row.get("direction") == "Bearish" and rs < 0)

    oi_accel = [e for e in fresh if finite_ge(e, "oi_acceleration", OI_ACCEL_MODERATE_PP)]
    rvol_hot = [e for e in fresh if finite_ge(e, "tod_rvol", TOD_RVOL_MIN)]
    compressed = [e for e in fresh if finite_ge(e, "compression_score", COMPRESSION_MIN)]
    rs_aligned = [e for e in fresh if relative_aligned(e)]
    vwap_aligned = [e for e in fresh if e.get("vwap_side_agrees") is True]
    atr_floor = [e for e in fresh if finite_ge(e, "atr_pct", effective_atr_floor)]
    not_extended = [e for e in fresh if e.get("entry_is_extended") is False]

    trial = {
        "spec": trial13_spec(),
        "candidate_count": len(trial_rows),
        "primary_horizon": "1D",
        "secondary_horizon": "2D",
        "2h": _three_way(trial_rows, "intraday_returns", "2h"),
        "4h": _three_way(trial_rows, "intraday_returns", "4h"),
        "1D": _three_way(trial_rows, "swing_returns", "1D"),
        "2D": _three_way(trial_rows, "swing_returns", "2D"),
        "final_test": {"locked": True, "message": "Trial 13 final 20% remains untouched until predeclared validation requirements are met."},
        "coverage_status": "EXPLORATORY_INTRADAY_OI" if float((ctx.get("history_coverage") or {}).get("oi_bar_coverage_pct") or 0) < 60 else "MEASURED",
    }

    def directional_component(subset):
        return {
            "event_count": len(subset),
            "2h": directional_stats(subset, "intraday_returns", "2h"),
            "4h": directional_stats(subset, "intraday_returns", "4h"),
            "1D": directional_stats(subset, "swing_returns", "1D"),
            "2D": directional_stats(subset, "swing_returns", "2D"),
        }

    return {
        "build_id": BUILD_ID,
        "research_only": True,
        "protocol": {
            "historical_trials_counted": TRIAL_NUMBER,
            "familywise_alpha": FAMILYWISE_ALPHA,
            "bonferroni_alpha": FAMILYWISE_ALPHA / TRIAL_NUMBER,
            "primary_horizons": ["1D", "2D"],
            "secondary_horizons": ["2h", "4h"],
            "production_activation": False,
            "point_in_time_fno_universe_available": False,
            "intraday_oi_coverage_pct": (ctx.get("history_coverage") or {}).get("oi_bar_coverage_pct"),
            "daily_oi_coverage": dict(ctx.get("daily_oi_coverage") or {}),
        },
        "trial13": trial,
        "component_reference": {
            "oi_acceleration_moderate_pp": OI_ACCEL_MODERATE_PP,
            "tod_rvol_min": TOD_RVOL_MIN,
            "compression_min": COMPRESSION_MIN,
            "effective_atr_floor_pct": effective_atr_floor,
            "note": "Fixed operational/reference cuts; descriptive component tests, not threshold optimization.",
        },
        "directional_components": {
            "long_buildup": directional_component(long_buildup),
            "short_buildup": directional_component(short_buildup),
            "fresh_breakout": directional_component(fresh),
            "fresh_breakout_regime_aligned": directional_component(aligned),
            "oi_acceleration_moderate_plus": directional_component(oi_accel),
            "tod_rvol_1_3_plus": directional_component(rvol_hot),
            "compression_60_plus": directional_component(compressed),
            "relative_direction_aligned": directional_component(rs_aligned),
            "vwap_aligned": directional_component(vwap_aligned),
            "atr_floor_scaled": directional_component(atr_floor),
            "not_extended": directional_component(not_extended),
            "silent_oi_to_ignition_no_chase": directional_component(silent_to_ignition),
        },
        "movement_components": {
            "silent_oi": _movement_component_report(rows, "silent_oi", baseline_rows),
            "compression_onset": _movement_component_report(rows, "compression_onset", baseline_rows),
            "daily_oi_anomaly": _movement_component_report(rows, "daily_oi_anomaly", baseline_rows),
            "baseline": _movement_component_report(rows, "baseline", baseline_rows),
        },
    }


def _finite_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def is_silent_oi_state(row: dict) -> bool:
    spec = trial13_spec()
    oi_z = _finite_float((row or {}).get('oi_z'))
    flat_atr = _finite_float((row or {}).get('price_move_60m_atr'))
    if oi_z is None or flat_atr is None:
        return False
    return bool(oi_z >= spec['oi_z_min'] and abs(flat_atr) <= spec['price_flat_max_atr'])


def absolute_regime_aligned(direction: str | None, index_ret_8_pct) -> bool:
    value = _finite_float(index_ret_8_pct)
    if value is None or value == 0:
        return False
    if direction == 'Bullish':
        return value > 0
    if direction == 'Bearish':
        return value < 0
    return False


def is_trial13_candidate(event: dict) -> bool:
    spec = trial13_spec()
    lead = bool((event or {}).get('v93_silent_oi_lead'))
    bars = (event or {}).get('v93_silent_oi_lead_bars')
    try:
        bars = int(bars)
    except (TypeError, ValueError):
        return False
    if not lead or bars < 1 or bars > int(spec['lead_window_bars']):
        return False
    if bool((event or {}).get('entry_is_extended')):
        return False
    if not bool((event or {}).get('v93_absolute_regime_aligned')):
        return False
    return True


def movement_outcomes(df: pd.DataFrame, signal_pos: int, atr: float) -> dict:
    """Directionless future expansion from the next executable bar.

    This is intentionally different from trade P&L.  It asks whether a
    precursor is followed by a large move at all, before a direction model is
    allowed to claim that move.
    """
    try:
        atr = float(atr)
    except (TypeError, ValueError):
        return {}
    entry_pos = int(signal_pos) + 1
    if atr <= 0 or entry_pos < 0 or entry_pos >= len(df):
        return {}
    entry = float(df['open'].iloc[entry_pos])
    sessions = pd.Series(pd.DatetimeIndex(df.index).normalize(), index=df.index)
    entry_session = sessions.iloc[entry_pos]

    def calc(end_pos):
        if end_pos is None or end_pos < entry_pos:
            return None
        window = df.iloc[entry_pos:end_pos + 1]
        if window.empty:
            return None
        hi = float(pd.to_numeric(window['high'], errors='coerce').max())
        lo = float(pd.to_numeric(window['low'], errors='coerce').min())
        close = float(pd.to_numeric(window['close'], errors='coerce').iloc[-1])
        max_abs = max(abs(hi - entry), abs(entry - lo)) / atr
        close_abs = abs(close / entry - 1.0) * 100.0 if entry else None
        return {
            'max_abs_move_atr': round(float(max_abs), 4),
            'close_abs_return_pct': round(float(close_abs), 4) if close_abs is not None else None,
        }

    out = {}
    for label, bars in (('30m', 2), ('1h', 4), ('2h', 8), ('4h', 16)):
        end_pos = entry_pos + bars - 1
        if end_pos < len(df) and sessions.iloc[end_pos] == entry_session:
            value = calc(end_pos)
            if value:
                out[label] = value

    unique_sessions = list(pd.unique(sessions.iloc[entry_pos:]))
    for n, label in ((1, '1D'), (2, '2D')):
        if len(unique_sessions) <= n:
            continue
        target = unique_sessions[n]
        positions = np.flatnonzero(sessions.eq(target).to_numpy())
        if len(positions):
            value = calc(int(positions[-1]))
            if value:
                out[label] = value
    return out
