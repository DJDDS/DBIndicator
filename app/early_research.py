"""Focused research helpers for the F&O early-movement engine."""
from __future__ import annotations

import numpy as np


def summarize_energy_events(events, horizons=(4, 8), move_atr=1.0):
    """How often a directionless Energy Building event produces expansion.

    `future_abs_move_atr[h]` is the largest absolute excursion, measured in
    signal-bar ATRs, during the next h bars. This deliberately does not score
    direction; compression is an expansion forecast, not a long/short call.
    """
    out = {}
    for h in horizons:
        vals = [e.get("future_abs_move_atr", {}).get(h) for e in events]
        vals = [float(v) for v in vals if v is not None and np.isfinite(v)]
        if not vals:
            out[str(h)] = {"event_count": 0}
            continue
        hits = sum(v >= move_atr for v in vals)
        out[str(h)] = {
            "event_count": len(vals),
            "move_hit_rate_pct": round(hits / len(vals) * 100.0, 1),
            "avg_abs_move_atr": round(float(np.mean(vals)), 3),
            "median_abs_move_atr": round(float(np.median(vals)), 3),
        }
    return out


def rank_component_results(rows):
    """Rank research variants by untouched holdout expectancy, then PF."""
    return sorted(list(rows or []), key=lambda r: (
        r.get("holdout_avg_return_pct") is None,
        -(r.get("holdout_avg_return_pct") or 0.0),
        -(r.get("holdout_profit_factor") or 0.0),
    ))


def chronological_split(events, holdout_pct=30.0):
    """Chronological event split; later observations are the untouched holdout."""
    rows = sorted(list(events or []), key=lambda e: e.get("entry_time", ""))
    if not rows or not holdout_pct:
        return rows, []
    pct = min(max(float(holdout_pct), 0.0), 90.0)
    cut = max(1, min(len(rows), int(np.ceil(len(rows) * (1.0 - pct / 100.0)))))
    return rows[:cut], rows[cut:]


def _rolling_last_percentile(series, lookback=60, min_periods=20):
    def _f(x):
        vals = x[np.isfinite(x)]
        if len(vals) < min_periods or not np.isfinite(x[-1]):
            return np.nan
        return float((vals <= x[-1]).mean() * 100.0)
    return series.rolling(lookback, min_periods=min_periods).apply(_f, raw=True)


def _session_pct_change(series, bars):
    import pandas as pd
    s = pd.to_numeric(series, errors="coerce")
    out = s.pct_change(bars) * 100.0
    if isinstance(s.index, pd.DatetimeIndex):
        same = s.index.normalize() == s.index.to_series().shift(bars).dt.normalize().values
        out = out.where(same)
    return out


def build_feature_frame(df, timeframe="15minute", oi_series=None, index_df=None, sector_df=None):
    """Build the live early-movement evidence axes on every historical bar.

    No future value is used. This is the canonical research feature frame so
    compression, participation, momentum and relative-strength thresholds can
    be ablated without reimplementing a different strategy in the backtester.
    """
    import pandas as pd
    from . import early_signal
    from . import indicators
    from .config import settings

    series = indicators.compute_series(df, timeframe)
    if "error" in series:
        return pd.DataFrame(index=df.index)
    out = pd.DataFrame(index=df.index)
    comp = series.get("compression")
    if comp is not None:
        out = out.join(comp)

    # Directional state is context only; entry timing comes from fresh crosses.
    align = (
        (series["rsi_line"] > series["rsi_smooth"]).astype(int)
        + (series["macd_line"] > series["signal_line"]).astype(int)
        + (series["cmf"] > 0).astype(int)
    )
    direction = pd.Series(np.where(align >= 2, "Bullish", "Bearish"), index=df.index, dtype=object)
    out["direction"] = direction
    out["trend_state"] = np.where(series["ema9"] > series["bb_mid"], "Bullish", "Bearish")

    bull_trigger = series["rsi_up"] | series["macd_up"]
    bear_trigger = series["rsi_dn"] | series["macd_dn"]
    trigger_now = (direction.eq("Bullish") & bull_trigger) | (direction.eq("Bearish") & bear_trigger)
    trigger_prev = (direction.eq("Bullish") & bull_trigger.shift(1, fill_value=False)) | (direction.eq("Bearish") & bear_trigger.shift(1, fill_value=False))
    out["entry_trigger"] = np.where(trigger_now | trigger_prev, direction, None)
    out["entry_trigger_bars_ago"] = np.where(trigger_now, 0, np.where(trigger_prev, 1, np.nan))

    rsi_spread = series["rsi_line"] - series["rsi_smooth"]
    rsi_slope = rsi_spread.diff()
    hist_slope = series["macd_hist"].diff()
    out["rsi_spread_slope"] = rsi_slope
    out["macd_hist_slope"] = hist_slope
    out["macd_agrees"] = np.where(direction.eq("Bullish"), series["macd_line"] > series["signal_line"], series["macd_line"] < series["signal_line"])
    out["macd_hist_agrees"] = np.where(direction.eq("Bullish"), hist_slope > 0, hist_slope < 0)
    out["momentum_inflection_agrees"] = np.where(
        direction.eq("Bullish"), (rsi_slope > 0) & (hist_slope > 0), (rsi_slope < 0) & (hist_slope < 0)
    )

    # Intraday participation versus the same clock slot on prior sessions.
    tod = indicators.time_of_day_rvol(df, lookback_sessions=20) if timeframe == "15minute" else pd.Series(np.nan, index=df.index)
    out["tod_rvol"] = tod
    prev_med = tod.shift(1).rolling(4, min_periods=2).median()
    out["tod_rvol_accel"] = tod / prev_med.replace(0, np.nan)
    out["vol_rising"] = (df["volume"] > df["volume"].shift(1)) & (df["volume"].shift(1) > df["volume"].shift(2))

    # VWAP/anti-chase entry location.
    vwap = indicators.session_vwap_series(df, timeframe)
    atr = series["atr"]
    raw_dist = (df["close"] - vwap) / atr.replace(0, np.nan)
    signed_dist = raw_dist.where(direction.eq("Bullish"), -raw_dist)
    out["entry_extension_atr"] = signed_dist
    out["entry_is_extended"] = signed_dist > settings.MAX_ENTRY_EXTENSION_ATR
    out["vwap_side_agrees"] = np.where(direction.eq("Bullish"), df["close"] > vwap, df["close"] < vwap)
    out["breakout_state"] = np.where(df["close"] > series["bb_upper"], "Breakout", np.where(df["close"] < series["bb_lower"], "Breakdown", None))

    # Recent OI velocity is more important than the whole-day anomaly for an
    # early entry. 15-minute is the live research surface: 2 bars=30m, 4=60m.
    if oi_series is not None:
        oi = pd.Series(oi_series).dropna().reindex(df.index, method="ffill", limit=2)
        oi30 = _session_pct_change(oi, 2) if timeframe == "15minute" else oi.pct_change() * 100.0
        oi60 = _session_pct_change(oi, 4) if timeframe == "15minute" else oi.pct_change() * 100.0
        out["oi_chg_30m_pct"] = oi30
        out["oi_chg_60m_pct"] = oi60
        out["oi_acceleration"] = oi30 - oi30.shift(2 if timeframe == "15minute" else 1)
        onebar_px = df["close"].pct_change() * 100.0
        out["oi_recent_agrees"] = (oi60 > 0) & np.where(direction.eq("Bullish"), onebar_px > 0, onebar_px < 0)

        changes = _session_pct_change(oi, 1) if timeframe == "15minute" else oi.pct_change() * 100.0
        mu = changes.rolling(early_signal.INTRADAY_BASELINE_OBS if timeframe == "15minute" else early_signal.BASELINE_DAYS,
                             min_periods=early_signal.MIN_BASELINE_OBS).mean().shift(1)
        sd = changes.rolling(early_signal.INTRADAY_BASELINE_OBS if timeframe == "15minute" else early_signal.BASELINE_DAYS,
                             min_periods=early_signal.MIN_BASELINE_OBS).std(ddof=1).shift(1)
        out["oi_z"] = (changes - mu) / sd.where(sd > 1e-6)
        out["oi_agrees"] = out["oi_recent_agrees"]
    else:
        for c in ("oi_chg_30m_pct", "oi_chg_60m_pct", "oi_acceleration", "oi_z", "oi_agrees", "oi_recent_agrees"):
            out[c] = np.nan

    # Relative strength versus NIFTY, with an acceleration term rather than
    # only a static lead that may have been earned many bars ago.
    if index_df is not None and not index_df.empty:
        idx_close = pd.Series(index_df["close"]).reindex(df.index, method="ffill", limit=2)
        stock20 = df["close"].pct_change(20) * 100.0
        idx20 = idx_close.pct_change(20) * 100.0
        stock10 = df["close"].pct_change(10) * 100.0
        idx10 = idx_close.pct_change(10) * 100.0
        rs20 = stock20 - idx20
        rs10 = stock10 - idx10
        out["rs_pct"] = rs20
        out["rs_improving"] = rs10 > 0
        out["rs_acceleration"] = rs10 - rs20
    else:
        out["rs_pct"] = np.nan
        out["rs_improving"] = np.nan
        out["rs_acceleration"] = np.nan

    # Sector context: replay the same 3-vote directional majority on the
    # sector index at each completed fine-timeframe bar. Missing sector
    # history stays missing rather than receiving free confirmation points.
    if sector_df is not None and not sector_df.empty:
        sector_series = indicators.compute_series(sector_df, timeframe)
        if "error" not in sector_series:
            warm = (
                sector_series["rsi_smooth"].notna()
                & sector_series["macd_line"].notna()
                & sector_series["signal_line"].notna()
                & sector_series["cmf"].notna()
            )
            votes = (
                (sector_series["rsi_line"] > sector_series["rsi_smooth"]).astype(int)
                + (sector_series["macd_line"] > sector_series["signal_line"]).astype(int)
                + (sector_series["cmf"] > 0).astype(int)
            )
            sector_dir = pd.Series(
                np.where(votes >= 2, "Bullish", "Bearish"),
                index=sector_df.index, dtype=object,
            ).where(warm, other=None)
            sector_dir = sector_dir.reindex(df.index, method="ffill", limit=2)
            out["sector_agrees"] = pd.Series(
                np.where(sector_dir.notna(), sector_dir.eq(direction), np.nan),
                index=df.index,
            )
        else:
            out["sector_agrees"] = np.nan
    else:
        out["sector_agrees"] = np.nan

    # Higher-timeframe context: a historical replay must never let a 15m bar
    # see the eventual close of its still-forming 4h bucket. Use only the
    # previous fully closed HTF bucket, then align it back to the 15m bars.
    spec = getattr(indicators, "_HTF_RESAMPLE", {}).get(timeframe)
    if spec is not None and not df.empty:
        htf_df = df.resample(spec["rule"], **spec["kwargs"]).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        htf_series = indicators.compute_series(htf_df, spec.get("label", "4hour"))
        if "error" not in htf_series:
            warm = (
                htf_series["rsi_smooth"].notna()
                & htf_series["macd_line"].notna()
                & htf_series["signal_line"].notna()
                & htf_series["cmf"].notna()
            )
            votes = (
                (htf_series["rsi_line"] > htf_series["rsi_smooth"]).astype(int)
                + (htf_series["macd_line"] > htf_series["signal_line"]).astype(int)
                + (htf_series["cmf"] > 0).astype(int)
            )
            htf_dir = pd.Series(
                np.where(votes >= 2, "Bullish", "Bearish"),
                index=htf_df.index, dtype=object,
            ).where(warm, other=None).shift(1)
            fine = pd.DataFrame({"ts": df.index}).sort_values("ts")
            lookup = pd.DataFrame({"ts": htf_dir.index, "htf_dir": htf_dir.values}).sort_values("ts")
            aligned = pd.merge_asof(fine, lookup, on="ts", direction="backward")["htf_dir"]
            aligned.index = df.index
            out["htf_agrees"] = pd.Series(
                np.where(aligned.notna(), aligned.eq(direction), np.nan),
                index=df.index,
            )
        else:
            out["htf_agrees"] = np.nan
    else:
        out["htf_agrees"] = np.nan
    return out


def summarize_directional_events(events, horizons=(1, 2, 3, 5, 10)):
    """Net directional outcome statistics for Ignition/Best Entry events."""
    out = {}
    for h in horizons:
        vals = [e.get("returns_pct", {}).get(h) for e in (events or [])]
        vals = [float(v) for v in vals if v is not None and np.isfinite(v)]
        if not vals:
            out[str(h)] = {"trade_count": 0}
            continue
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v < 0]
        gp, gl = sum(wins), abs(sum(losses))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else None)
        out[str(h)] = {
            "trade_count": len(vals),
            "win_rate_pct": round(len(wins) / len(vals) * 100.0, 1),
            "avg_return_pct": round(float(np.mean(vals)), 3),
            "median_return_pct": round(float(np.median(vals)), 3),
            "profit_factor": round(float(pf), 2) if pf is not None and np.isfinite(pf) else pf,
            "avg_winner_pct": round(float(np.mean(wins)), 3) if wins else None,
            "avg_loser_pct": round(float(np.mean(losses)), 3) if losses else None,
        }
    return out


def sensitivity_table(events, field, thresholds, horizon=3, holdout_pct=30.0, mode="ge"):
    """One-factor threshold study using only the chronological holdout stats.

    This intentionally changes one threshold at a time. It is not a Cartesian
    grid search, so a pretty row cannot emerge merely from trying thousands of
    combinations on the same sample.
    """
    rows = []
    for threshold in thresholds:
        subset = []
        for e in events or []:
            v = e.get(field)
            if v is None or not np.isfinite(v):
                continue
            keep = v >= threshold if mode == "ge" else v <= threshold
            if keep:
                subset.append(e)
        train, hold = chronological_split(subset, holdout_pct=holdout_pct)
        train_stats = summarize_directional_events(train, horizons=(horizon,))[str(horizon)]
        hold_stats = summarize_directional_events(hold, horizons=(horizon,))[str(horizon)]
        rows.append({
            "field": field,
            "threshold": threshold,
            "train_trade_count": train_stats.get("trade_count", 0),
            "train_avg_return_pct": train_stats.get("avg_return_pct"),
            "holdout_trade_count": hold_stats.get("trade_count", 0),
            "holdout_win_rate_pct": hold_stats.get("win_rate_pct"),
            "holdout_avg_return_pct": hold_stats.get("avg_return_pct"),
            "holdout_profit_factor": hold_stats.get("profit_factor"),
        })
    return rows


def _py(v):
    """Convert pandas/NumPy scalars to the live score's Python semantics."""
    import pandas as pd
    if v is None or (not isinstance(v, (bool, np.bool_)) and pd.isna(v)):
        return None
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.generic):
        return v.item()
    return v


def replay_feature_frame(df, features, symbol, horizons=(1, 2, 3, 5, 10),
                         cost_pct=0.05, slippage_pct=0.02):
    """Replay Energy Building, Ignition and Best Entry on a prepared frame.

    Directional trades enter at the NEXT bar's open after a completed signal
    bar. That is intentionally more conservative than using the signal bar's
    close and avoids look-ahead/execution optimism in research.
    """
    from .early_movement import score_candidate

    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0}))
    energy_events, ignition_events, best_events = [], [], []
    energy = features.get("energy_building")
    if energy is None:
        energy = np.zeros(len(features), dtype=bool)
    energy = np.asarray(energy, dtype=bool)
    energy_rise = energy & ~np.r_[False, energy[:-1]]

    for pos in range(len(df)):
        row = {k: _py(v) for k, v in features.iloc[pos].to_dict().items()}
        atr = row.get("atr")
        if atr is None and "atr" in features.columns:
            atr = _py(features["atr"].iloc[pos])

        if energy_rise[pos] and atr is not None and atr > 0:
            future = {}
            ref = float(df["close"].iloc[pos])
            for h in (4, 8):
                end = min(len(df), pos + h + 1)
                if end <= pos + 1:
                    continue
                hi = float(df["high"].iloc[pos + 1:end].max())
                lo = float(df["low"].iloc[pos + 1:end].min())
                future[h] = max(abs(hi - ref), abs(lo - ref)) / float(atr)
            energy_events.append({
                "symbol": symbol,
                "signal_time": df.index[pos].isoformat(),
                "entry_time": df.index[pos].isoformat(),
                "compression_score": row.get("compression_score"),
                "future_abs_move_atr": future,
            })

        direction = row.get("direction")
        trigger = row.get("entry_trigger")
        bars_ago = row.get("entry_trigger_bars_ago")
        ignition = direction in ("Bullish", "Bearish") and trigger == direction and bars_ago == 0
        if not ignition or pos + 1 >= len(df):
            continue

        scored = score_candidate(row)
        entry_pos = pos + 1
        entry = float(df["open"].iloc[entry_pos])
        returns = {}
        for h in horizons:
            exit_pos = entry_pos + h - 1
            if exit_pos >= len(df):
                continue
            exit_px = float(df["close"].iloc[exit_pos])
            raw = (exit_px / entry - 1.0) * 100.0
            if direction == "Bearish":
                raw = -raw
            returns[h] = round(raw - max(0.0, float(cost_pct)) - max(0.0, float(slippage_pct)), 3)

        event = {
            "symbol": symbol,
            "signal_time": df.index[pos].isoformat(),
            "entry_time": df.index[entry_pos].isoformat(),
            "direction": direction,
            "returns_pct": returns,
            "movement_score": scored.get("score"),
            "movement_stage": scored.get("stage"),
            "compression_score": row.get("compression_score"),
            "oi_chg_60m_pct": row.get("oi_chg_60m_pct"),
            "oi_acceleration": row.get("oi_acceleration"),
            "tod_rvol": row.get("tod_rvol"),
            "tod_rvol_accel": row.get("tod_rvol_accel"),
            "momentum_inflection_agrees": row.get("momentum_inflection_agrees"),
            "rs_acceleration_directional": (
                row.get("rs_acceleration") if direction == "Bullish" else
                (-row.get("rs_acceleration") if row.get("rs_acceleration") is not None else None)
            ),
            "vwap_side_agrees": row.get("vwap_side_agrees"),
            "entry_is_extended": row.get("entry_is_extended"),
            "eligible": scored.get("eligible", False),
            "blockers": scored.get("blockers", []),
        }
        ignition_events.append(event)
        if scored.get("eligible"):
            best_events.append(dict(event))

    return {
        "energy_events": energy_events,
        "ignition_events": ignition_events,
        "best_entry_events": best_events,
    }


def _stage_with_holdout(events, horizons, holdout_pct):
    train, hold = chronological_split(events, holdout_pct=holdout_pct)
    return {
        "all": summarize_directional_events(events, horizons=horizons),
        "train": summarize_directional_events(train, horizons=horizons),
        "holdout": summarize_directional_events(hold, horizons=horizons),
        "total_events": len(events),
        "train_events": len(train),
        "holdout_events": len(hold),
    }


def _component_rows(events, ref_horizon=3, holdout_pct=30.0):
    checks = {
        "compression": lambda e: (e.get("compression_score") or 0) >= 60,
        "oi_velocity": lambda e: e.get("oi_chg_60m_pct") is not None and e.get("oi_chg_60m_pct") > 0 and (e.get("oi_acceleration") or 0) >= 0,
        "tod_participation": lambda e: e.get("tod_rvol") is not None and e.get("tod_rvol") >= 1.10 and (e.get("tod_rvol_accel") or 0) >= 1.0,
        "momentum_inflection": lambda e: e.get("momentum_inflection_agrees") is True,
        "rs_acceleration": lambda e: e.get("rs_acceleration_directional") is not None and e.get("rs_acceleration_directional") > 0,
        "vwap_acceptance": lambda e: e.get("vwap_side_agrees") is True,
        "anti_chase": lambda e: e.get("entry_is_extended") is False,
    }
    rows = []
    for name, fn in checks.items():
        subset = [e for e in events if fn(e)]
        _, hold = chronological_split(subset, holdout_pct=holdout_pct)
        hs = summarize_directional_events(hold, horizons=(ref_horizon,))[str(ref_horizon)]
        rows.append({
            "component": name,
            "events": len(subset),
            "holdout_trade_count": hs.get("trade_count", 0),
            "holdout_win_rate_pct": hs.get("win_rate_pct"),
            "holdout_avg_return_pct": hs.get("avg_return_pct"),
            "holdout_profit_factor": hs.get("profit_factor"),
        })
    return rank_component_results(rows)


def aggregate_research(replays, holdout_pct=30.0, ref_horizon=3, horizons=(1, 2, 3, 5, 10)):
    """Aggregate many symbol replays into one improvement-oriented report."""
    energy, ignition, best = [], [], []
    for replay in replays or []:
        energy.extend(replay.get("energy_events") or [])
        ignition.extend(replay.get("ignition_events") or [])
        best.extend(replay.get("best_entry_events") or [])
    energy.sort(key=lambda e: e.get("entry_time", ""))
    ignition.sort(key=lambda e: e.get("entry_time", ""))
    best.sort(key=lambda e: e.get("entry_time", ""))

    energy_train, energy_hold = chronological_split(energy, holdout_pct=holdout_pct)
    result = {
        "ref_horizon": int(ref_horizon),
        "holdout_pct": float(holdout_pct),
        "energy": {
            "all": summarize_energy_events(energy, horizons=(4, 8), move_atr=1.0),
            "train": summarize_energy_events(energy_train, horizons=(4, 8), move_atr=1.0),
            "holdout": summarize_energy_events(energy_hold, horizons=(4, 8), move_atr=1.0),
            "total_events": len(energy),
        },
        "ignition": _stage_with_holdout(ignition, horizons, holdout_pct),
        "best_entry": _stage_with_holdout(best, horizons, holdout_pct),
        "component_lifts": _component_rows(ignition, ref_horizon=ref_horizon, holdout_pct=holdout_pct),
        "sensitivity": {
            "compression_score": sensitivity_table(ignition, "compression_score", [60, 70, 80], ref_horizon, holdout_pct),
            "tod_rvol": sensitivity_table(ignition, "tod_rvol", [1.0, 1.1, 1.3, 1.5], ref_horizon, holdout_pct),
            "oi_chg_60m_pct": sensitivity_table(ignition, "oi_chg_60m_pct", [0.0, 0.5, 1.0, 2.0], ref_horizon, holdout_pct),
            "movement_score": sensitivity_table(ignition, "movement_score", [65, 70, 72, 75, 80], ref_horizon, holdout_pct),
            "rs_acceleration": sensitivity_table(ignition, "rs_acceleration_directional", [0.0, 0.1, 0.25, 0.5], ref_horizon, holdout_pct),
        },
    }
    return result
