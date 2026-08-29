"""Focused research helpers for the F&O early-movement engine."""
from __future__ import annotations

import numpy as np
import pandas as pd

RESEARCH_BUILD_ID = "2026-08-29-DIAG-V4"



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


def institutional_benchmark(stats: dict, mode: str = "intraday") -> dict:
    """Strict promotion gate inspired by institutional research discipline.

    A signal is not called a benchmark because one holdout number looks good.
    It needs adequate sample size, positive net expectancy, profit factor,
    favorable excursion economics, and no failed chronological block.
    """
    mode = (mode or "intraday").lower()
    if mode == "swing":
        min_trades, min_avg, min_pf = 80, 0.18, 1.25
    else:
        min_trades, min_avg, min_pf = 120, 0.12, 1.25
    n = int(stats.get("trade_count") or 0)
    avg = stats.get("avg_return_pct")
    pf = stats.get("profit_factor")
    mfe = stats.get("avg_mfe_atr"); mae = stats.get("avg_mae_atr")
    excursion_ratio = (float(mfe) / float(mae)) if mfe is not None and mae not in (None, 0) else None
    wf = list(stats.get("walkforward") or [])
    wf_valid = [x for x in wf if int(x.get("trade_count") or 0) > 0]
    wf_good = [x for x in wf_valid if (x.get("avg_return_pct") or 0) > 0 and (x.get("profit_factor") or 0) >= 1.10]
    wf_failed = [x for x in wf_valid if (x.get("avg_return_pct") or 0) < 0 or (x.get("profit_factor") or 0) < 0.90]
    checks = {
        "sample": n >= min_trades,
        "expectancy": avg is not None and float(avg) >= min_avg,
        "profit_factor": pf is not None and float(pf) >= min_pf,
        "excursion_quality": excursion_ratio is not None and excursion_ratio >= 1.40,
        "walkforward": len(wf_valid) >= 3 and len(wf_good) >= max(3, len(wf_valid) - 1) and not wf_failed,
    }
    passed = all(checks.values())
    score = sum(bool(v) for v in checks.values())
    status = "Benchmark" if passed else ("Promising" if score >= 4 else "Research")
    return {
        "mode": mode, "status": status, "passed": passed, "checks": checks,
        "excursion_ratio": round(excursion_ratio, 2) if excursion_ratio is not None else None,
        "requirements": {"min_trades": min_trades, "min_avg_return_pct": min_avg, "min_profit_factor": min_pf, "min_mfe_mae": 1.40},
    }


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
        # Keep both sides timezone-aware. ``.values`` strips the timezone and
        # made every intraday comparison False on NSE timestamps, which in turn
        # made all historical 30/60-minute OI velocity NaN.
        current_session = pd.Series(s.index.normalize(), index=s.index)
        prior_session = current_session.shift(bars)
        out = out.where(current_session.eq(prior_session))
    return out


def build_feature_frame(df, timeframe="15minute", oi_series=None, index_df=None, sector_df=None):
    """Build historical features with live breakout semantics and no look-ahead.

    RSI/MACD slopes remain available as diagnostics for old research panels, but
    they no longer assign direction. Direction comes only from an actual price
    escape generated by :mod:`app.stock_in_play`.
    """
    import pandas as pd
    from . import early_signal, indicators, stock_in_play

    series = indicators.compute_series(df, timeframe)
    if "error" in series:
        return pd.DataFrame(index=df.index)

    out = pd.DataFrame(index=df.index)
    comp = series.get("compression")
    if comp is not None:
        out = out.join(comp)

    tod = indicators.time_of_day_rvol(df, lookback_sessions=20) if timeframe == "15minute" else pd.Series(np.nan, index=df.index)
    opening_rvol = indicators.opening_relative_volume(df, opening_bars=2, lookback_sessions=14) if timeframe == "15minute" else pd.Series(np.nan, index=df.index)
    out["tod_rvol"] = tod
    out["opening_rvol"] = opening_rvol
    prev_med = tod.shift(1).rolling(4, min_periods=2).median()
    out["tod_rvol_accel"] = tod / prev_med.replace(0, np.nan)
    out["vol_rising"] = (df["volume"] > df["volume"].shift(1)) & (df["volume"].shift(1) > df["volume"].shift(2))

    price = stock_in_play.build_price_features(df, series["atr"], comp, tod, opening_rvol=opening_rvol)
    for col in price.columns:
        # compression_score / energy_building are already present from comp;
        # assigning again is harmless and guarantees live/research parity.
        out[col] = price[col]
    direction = out["breakout_direction"]
    out["direction"] = direction
    out["entry_trigger"] = np.where(out["fresh_breakout"], direction, None)
    out["entry_trigger_bars_ago"] = np.where(out["fresh_breakout"], 0, np.nan)

    # Momentum diagnostics only. They do not create direction or eligibility.
    rsi_spread = series["rsi_line"] - series["rsi_smooth"]
    rsi_slope = rsi_spread.diff()
    hist_slope = series["macd_hist"].diff()
    out["rsi_spread_slope"] = rsi_slope
    out["macd_hist_slope"] = hist_slope
    out["macd_agrees"] = np.where(
        direction.eq("Bullish"), series["macd_line"] > series["signal_line"],
        np.where(direction.eq("Bearish"), series["macd_line"] < series["signal_line"], np.nan),
    )
    out["macd_hist_agrees"] = np.where(
        direction.eq("Bullish"), hist_slope > 0,
        np.where(direction.eq("Bearish"), hist_slope < 0, np.nan),
    )
    out["momentum_inflection_agrees"] = np.where(
        direction.eq("Bullish"), (rsi_slope > 0) & (hist_slope > 0),
        np.where(direction.eq("Bearish"), (rsi_slope < 0) & (hist_slope < 0), np.nan),
    )

    # VWAP/anti-chase are evaluated relative to breakout direction.
    vwap = indicators.session_vwap_series(df, timeframe)
    out["vwap_side_agrees"] = np.where(
        direction.eq("Bullish"), df["close"] > vwap,
        np.where(direction.eq("Bearish"), df["close"] < vwap, np.nan),
    )
    out["breakout_vwap_agrees"] = out["vwap_side_agrees"]
    out["entry_is_extended"] = out["breakout_extension_atr"] > stock_in_play._live_setting("MAX_ENTRY_EXTENSION_ATR", stock_in_play.MAX_BREAKOUT_EXTENSION_ATR)
    out["breakout_entry_extended"] = out["entry_is_extended"]
    out["breakout_state"] = np.where(direction.eq("Bullish"), "Breakout", np.where(direction.eq("Bearish"), "Breakdown", None))

    # Intraday OI. The timezone-safe _session_pct_change is critical: previous
    # code stripped timezone from one side of the same-session comparison and
    # made all 30/60-minute historical OI changes NaN.
    if oi_series is not None:
        oi = pd.Series(oi_series).dropna().sort_index()
        oi = oi[~oi.index.duplicated(keep="last")]
        oi = oi.reindex(df.index, method="ffill", limit=2)
        oi30 = _session_pct_change(oi, 2) if timeframe == "15minute" else oi.pct_change() * 100.0
        oi60 = _session_pct_change(oi, 4) if timeframe == "15minute" else oi.pct_change() * 100.0
        out["oi_chg_30m_pct"] = oi30
        out["oi_chg_60m_pct"] = oi60
        out["oi_acceleration"] = oi30 - oi30.shift(2 if timeframe == "15minute" else 1)
        out["oi_recent_agrees"] = np.where(direction.notna(), oi60 > 0, np.nan)

        changes = _session_pct_change(oi, 1) if timeframe == "15minute" else oi.pct_change() * 100.0
        window = early_signal.INTRADAY_BASELINE_OBS if timeframe == "15minute" else early_signal.BASELINE_DAYS
        mu = changes.rolling(window, min_periods=early_signal.MIN_BASELINE_OBS).mean().shift(1)
        sd = changes.rolling(window, min_periods=early_signal.MIN_BASELINE_OBS).std(ddof=1).shift(1)
        out["oi_z"] = (changes - mu) / sd.where(sd > 1e-6)
        out["oi_agrees"] = out["oi_recent_agrees"]
    else:
        for c in ("oi_chg_30m_pct", "oi_chg_60m_pct", "oi_acceleration", "oi_z", "oi_agrees", "oi_recent_agrees"):
            out[c] = np.nan

    # Static market-relative participation is context, not a gate. Acceleration
    # remains exposed for diagnostics because the user's research already showed
    # that making it mandatory worsened expectancy.
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

    # Sector context uses price structure (8-bar return sign), not a second
    # RSI/MACD voting system.
    if sector_df is not None and not sector_df.empty:
        sec_close = pd.Series(sector_df["close"]).reindex(df.index, method="ffill", limit=2)
        sec_ret = sec_close.pct_change(8)
        out["sector_agrees"] = pd.Series(
            np.where(direction.eq("Bullish"), sec_ret > 0,
                     np.where(direction.eq("Bearish"), sec_ret < 0, np.nan)),
            index=df.index,
        )
    else:
        out["sector_agrees"] = np.nan

    # 4H context is structure only: previous fully closed 4H close relative to
    # a rising/falling EMA20. It confirms, but does not create the entry.
    if timeframe == "15minute" and not df.empty:
        htf = df.resample("4h", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        if len(htf) >= 22:
            ema20 = htf["close"].ewm(span=20, adjust=False).mean()
            bull = (htf["close"] > ema20) & (ema20 > ema20.shift(1))
            bear = (htf["close"] < ema20) & (ema20 < ema20.shift(1))
            htf_dir = pd.Series(np.where(bull, "Bullish", np.where(bear, "Bearish", None)), index=htf.index, dtype=object).shift(1)
            fine = pd.DataFrame({"ts": df.index}).sort_values("ts")
            lookup = pd.DataFrame({"ts": htf_dir.index, "htf_dir": htf_dir.values}).sort_values("ts")
            aligned = pd.merge_asof(fine, lookup, on="ts", direction="backward")["htf_dir"]
            aligned.index = df.index
            out["htf_agrees"] = pd.Series(
                np.where(direction.eq("Bullish"), aligned.eq("Bullish"),
                         np.where(direction.eq("Bearish"), aligned.eq("Bearish"), np.nan)),
                index=df.index,
            )
        else:
            out["htf_agrees"] = np.nan
    else:
        out["htf_agrees"] = np.nan

    out["atr"] = series["atr"]
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



def _future_abs_move_atr(df, pos, atr, horizons=(4, 8, 16, 25)):
    out = {}
    if atr is None or not np.isfinite(atr) or atr <= 0:
        return out
    ref = float(df["close"].iloc[pos])
    for h in horizons:
        end = min(len(df), pos + int(h) + 1)
        if end <= pos + 1:
            continue
        hi = float(df["high"].iloc[pos + 1:end].max())
        lo = float(df["low"].iloc[pos + 1:end].min())
        out[str(h)] = max(abs(hi - ref), abs(lo - ref)) / float(atr)
    return out


def _replay_breakout_feature_frame(df, features, symbol, cost_pct=0.05, slippage_pct=0.02):
    """Replay actual price breakouts with intraday and 1–2D outcomes.

    Intraday entries are measured from the first fresh escape. Swing candidates
    are measured separately from the later retention bar used by the live
    1–2D classifier, so the backtest pays the same confirmation delay as live.
    """
    from . import stock_in_play

    energy_events, baseline_events, ignition_events, best_events, swing_events = [], [], [], [], []
    energy = features.get("energy_building")
    if energy is None:
        energy = pd.Series(False, index=features.index)
    energy = pd.Series(energy, index=features.index).fillna(False).astype(bool)
    energy_rise = energy & ~energy.shift(1, fill_value=False)

    def _event_from_row(row, pos, direction, classified):
        atr = _py(features["atr"].iloc[pos]) if "atr" in features.columns else None
        if atr is None or atr <= 0 or pos + 1 >= len(df):
            return None
        outcomes = stock_in_play.compute_trade_outcomes(
            df, signal_pos=pos, direction=direction, atr=float(atr),
            cost_pct=cost_pct, slippage_pct=slippage_pct,
        )
        entry_pos = outcomes.get("entry_pos")
        return {
            "symbol": symbol,
            "signal_time": df.index[pos].isoformat(),
            "entry_time": df.index[entry_pos].isoformat() if entry_pos is not None else None,
            "direction": direction,
            "breakout_source": row.get("breakout_source") or row.get("retained_breakout_source"),
            "breakout_level": row.get("breakout_level") if row.get("breakout_level") is not None else row.get("retained_breakout_level"),
            "breakout_extension_atr": row.get("breakout_extension_atr") if row.get("breakout_extension_atr") is not None else row.get("retained_breakout_extension_atr"),
            "compression_score": row.get("compression_score"),
            "tod_rvol": row.get("tod_rvol"),
            "oi_chg_30m_pct": row.get("oi_chg_30m_pct"),
            "oi_chg_60m_pct": row.get("oi_chg_60m_pct"),
            "oi_acceleration": row.get("oi_acceleration"),
            "oi_status": classified.get("oi_status"),
            "htf_agrees": row.get("htf_agrees"),
            "sector_agrees": row.get("sector_agrees"),
            "rs_pct": row.get("rs_pct"),
            "vwap_side_agrees": row.get("vwap_side_agrees"),
            "entry_is_extended": row.get("entry_is_extended"),
            "intraday_returns": outcomes.get("intraday", {}),
            "swing_returns": outcomes.get("swing", {}),
            "mfe_atr": outcomes.get("mfe_atr", {}),
            "mae_atr": outcomes.get("mae_atr", {}),
            "time_to_0_5atr_bars": outcomes.get("time_to_0_5atr_bars"),
            "time_to_1atr_bars": outcomes.get("time_to_1atr_bars"),
            "intraday_eligible": classified.get("intraday_eligible", False),
            "swing_eligible": classified.get("swing_eligible", False),
            "stage": classified.get("stage"),
            "movement_score": classified.get("score"),
            "blockers": classified.get("blockers", []),
        }

    # A sampled non-coil baseline is enough to estimate lift without storing
    # every bar from the entire 211-stock universe in memory.
    for pos in range(len(df)):
        atr = _py(features["atr"].iloc[pos]) if "atr" in features.columns else None
        if atr is not None and atr > 0:
            future = _future_abs_move_atr(df, pos, atr)
            if energy_rise.iloc[pos]:
                energy_events.append({
                    "symbol": symbol, "entry_time": df.index[pos].isoformat(),
                    "signal_time": df.index[pos].isoformat(),
                    "compression_score": _py(features.get("compression_score", pd.Series(index=features.index)).iloc[pos]) if "compression_score" in features.columns else None,
                    "future_abs_move_atr": future,
                })
            elif pos % 8 == 0 and future:
                baseline_events.append({
                    "symbol": symbol, "entry_time": df.index[pos].isoformat(),
                    "future_abs_move_atr": future,
                })

        row = {k: _py(v) for k, v in features.iloc[pos].to_dict().items()}
        row["timestamp"] = df.index[pos].isoformat()

        # First escape: research the actual directional breakout and intraday
        # Best Entry eligibility.
        direction = row.get("breakout_direction") or row.get("direction")
        if direction in ("Bullish", "Bearish") and row.get("fresh_breakout") is True and pos + 1 < len(df):
            row["breakout_direction"] = direction
            classified = stock_in_play.classify_live_candidate(row)
            event = _event_from_row(row, pos, direction, classified)
            if event is not None:
                mapping = {2: "30m", 4: "1h", 8: "2h", 16: "4h"}
                event["returns_pct"] = {h: event["intraday_returns"][label] for h, label in mapping.items() if label in event["intraday_returns"]}
                ignition_events.append(event)
                if classified.get("intraday_eligible"):
                    best_events.append(dict(event))

        # One-bar-later retention: this is the live swing confirmation point.
        retained_direction = row.get("retained_breakout_direction")
        if (row.get("breakout_retained") is True and retained_direction in ("Bullish", "Bearish")
                and pos + 1 < len(df)):
            row["breakout_direction"] = None
            row["retained_breakout_direction"] = retained_direction
            if row.get("breakout_source") is None:
                row["breakout_source"] = row.get("retained_breakout_source")
            if row.get("breakout_level") is None:
                row["breakout_level"] = row.get("retained_breakout_level")
            if row.get("breakout_extension_atr") is None:
                row["breakout_extension_atr"] = row.get("retained_breakout_extension_atr")
            classified = stock_in_play.classify_live_candidate(row)
            if classified.get("swing_eligible"):
                event = _event_from_row(row, pos, retained_direction, classified)
                if event is not None:
                    swing_events.append(event)

    return {
        "energy_events": energy_events,
        "baseline_energy_events": baseline_events,
        "ignition_events": ignition_events,
        "best_entry_events": best_events,
        "swing_events": swing_events,
    }


def summarize_named_returns(events, field, keys):
    out = {}
    for key in keys:
        vals = [e.get(field, {}).get(key) for e in (events or [])]
        vals = [float(v) for v in vals if v is not None and np.isfinite(v)]
        if not vals:
            out[str(key)] = {"trade_count": 0}
            continue
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v < 0]
        gp, gl = sum(wins), abs(sum(losses))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else None)
        out[str(key)] = {
            "trade_count": len(vals),
            "win_rate_pct": round(len(wins) / len(vals) * 100.0, 1),
            "avg_return_pct": round(float(np.mean(vals)), 3),
            "median_return_pct": round(float(np.median(vals)), 3),
            "profit_factor": round(float(pf), 2) if pf is not None and np.isfinite(pf) else pf,
        }
    return out


def _named_stage_with_holdout(events, field, keys, holdout_pct):
    train, hold = chronological_split(events, holdout_pct=holdout_pct)
    return {
        "all": summarize_named_returns(events, field, keys),
        "train": summarize_named_returns(train, field, keys),
        "holdout": summarize_named_returns(hold, field, keys),
        "total_events": len(events), "train_events": len(train), "holdout_events": len(hold),
    }


def walkforward_named_returns(events, field, key, blocks=4):
    """Chronological block stability for one executable return horizon.

    The blocks are never shuffled.  This is deliberately simple and visible:
    a candidate rule must survive several sequential market periods instead of
    relying on one favorable aggregate sample.
    """
    rows = sorted(list(events or []), key=lambda e: e.get("entry_time", ""))
    if not rows or int(blocks) <= 0:
        return []
    blocks = min(int(blocks), len(rows))
    index_chunks = np.array_split(np.arange(len(rows)), blocks)
    out = []
    for block_no, indexes in enumerate(index_chunks, start=1):
        chunk = [rows[int(i)] for i in indexes]
        stat = summarize_named_returns(chunk, field, (key,))[str(key)]
        out.append({"block": block_no, **stat})
    return out


def _promotion_benchmark(events, field, key, mode, holdout_pct):
    """Apply the strict promotion standard to the untouched holdout sample."""
    _train, hold = chronological_split(events, holdout_pct=holdout_pct)
    hold_stat = summarize_named_returns(hold, field, (key,))[str(key)]
    excursion = _excursion_summary(hold)
    stats = dict(hold_stat)
    stats["avg_mfe_atr"] = excursion.get("avg_mfe_1D_atr")
    stats["avg_mae_atr"] = excursion.get("avg_mae_1D_atr")
    stats["walkforward"] = walkforward_named_returns(events, field, key, blocks=4)
    return institutional_benchmark(stats, mode=mode)


def _interaction_report(events, holdout_pct):
    from .stock_in_play import interaction_variants
    rows = {}
    for name, subset in interaction_variants(events).items():
        _, hold = chronological_split(subset, holdout_pct=holdout_pct)
        rows[name] = {
            "intraday_2h": summarize_named_returns(hold, "intraday_returns", ("2h",))["2h"],
            "swing_1D": summarize_named_returns(hold, "swing_returns", ("1D",))["1D"],
            "holdout_events": len(hold),
        }
    return rows

def replay_feature_frame(df, features, symbol, horizons=(1, 2, 3, 5, 10),
                         cost_pct=0.05, slippage_pct=0.02):
    if "fresh_breakout" in features.columns and "breakout_direction" in features.columns:
        return _replay_breakout_feature_frame(
            df, features, symbol, cost_pct=cost_pct, slippage_pct=slippage_pct
        )
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




def _excursion_summary(events):
    rows = list(events or [])
    def vals(path1, path2=None):
        out = []
        for e in rows:
            v = e.get(path1)
            if path2 is not None and isinstance(v, dict):
                v = v.get(path2)
            if v is not None and np.isfinite(v):
                out.append(float(v))
        return out
    mfe1 = vals("mfe_atr", "1D")
    mae1 = vals("mae_atr", "1D")
    mfe2 = vals("mfe_atr", "2D")
    mae2 = vals("mae_atr", "2D")
    t05 = vals("time_to_0_5atr_bars")
    t10 = vals("time_to_1atr_bars")
    return {
        "events": len(rows),
        "avg_mfe_1D_atr": round(float(np.mean(mfe1)), 3) if mfe1 else None,
        "avg_mae_1D_atr": round(float(np.mean(mae1)), 3) if mae1 else None,
        "avg_mfe_2D_atr": round(float(np.mean(mfe2)), 3) if mfe2 else None,
        "avg_mae_2D_atr": round(float(np.mean(mae2)), 3) if mae2 else None,
        "hit_0_5atr_pct": round(len(t05) / len(rows) * 100.0, 1) if rows else None,
        "hit_1atr_pct": round(len(t10) / len(rows) * 100.0, 1) if rows else None,
        "median_bars_to_0_5atr": round(float(np.median(t05)), 1) if t05 else None,
        "median_bars_to_1atr": round(float(np.median(t10)), 1) if t10 else None,
    }

def confirmation_diagnostics(events):
    """Expose raw research-data availability before strategy filtering."""
    from . import stock_in_play
    events = list(events or [])

    def finite(v):
        if v is None:
            return False
        try:
            return bool(np.isfinite(float(v)))
        except (TypeError, ValueError):
            return False

    def flag_counts(key):
        vals = [stock_in_play._flag(e.get(key)) for e in events]
        return (sum(v is not None for v in vals),
                sum(v is True for v in vals),
                sum(v is False for v in vals))

    htf_avail, htf_true, htf_false = flag_counts("htf_agrees")
    vwap_avail, vwap_true, vwap_false = flag_counts("vwap_side_agrees")
    ext_avail, ext_true, ext_false = flag_counts("entry_is_extended")
    oi60_vals = [e.get("oi_chg_60m_pct") for e in events]
    accel_vals = [e.get("oi_acceleration") for e in events]
    return {
        "events": len(events),
        "oi_60m_finite": sum(finite(v) for v in oi60_vals),
        "oi_60m_positive": sum(finite(v) and float(v) > 0 for v in oi60_vals),
        "oi_accel_finite": sum(finite(v) for v in accel_vals),
        "oi_confirmed": sum(e.get("oi_status") == "Confirmed" for e in events),
        "oi_unavailable": sum(e.get("oi_status") == "Unavailable" for e in events),
        "htf_available": htf_avail, "htf_true": htf_true, "htf_false": htf_false,
        "vwap_available": vwap_avail, "vwap_true": vwap_true, "vwap_false": vwap_false,
        "entry_extended_available": ext_avail, "entry_extended_true": ext_true, "entry_extended_false": ext_false,
    }


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
        "research_build_id": RESEARCH_BUILD_ID,
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
    # New stock-in-play research surfaces. Keep legacy keys above so older
    # diagnostics/tests remain readable while the primary UI uses real horizons.
    baseline = []
    swing_events = []
    for replay in replays or []:
        baseline.extend(replay.get("baseline_energy_events") or [])
        swing_events.extend(replay.get("swing_events") or [])
    baseline.sort(key=lambda e: e.get("entry_time", ""))
    swing_events.sort(key=lambda e: e.get("entry_time", ""))

    if ignition and any("intraday_returns" in e for e in ignition):
        from .stock_in_play import expansion_lift_table
        result["intraday"] = _named_stage_with_holdout(
            ignition, "intraday_returns", ("30m", "1h", "2h", "4h", "eod"), holdout_pct
        )
        result["swing"] = _named_stage_with_holdout(
            ignition, "swing_returns", ("1D", "2D"), holdout_pct
        )
        result["intraday_best_entry"] = _named_stage_with_holdout(
            best, "intraday_returns", ("30m", "1h", "2h", "4h", "eod"), holdout_pct
        )
        result["swing_candidates"] = _named_stage_with_holdout(
            swing_events, "swing_returns", ("1D", "2D"), holdout_pct
        )
        result["compression_lift"] = expansion_lift_table(energy, baseline)
        result["interactions"] = _interaction_report(ignition, holdout_pct)
        available = sum(1 for e in ignition if e.get("oi_status") != "Unavailable")
        confirmed = sum(1 for e in ignition if e.get("oi_status") == "Confirmed")
        result["oi_coverage"] = {
            "total": len(ignition), "available": available,
            "unavailable": len(ignition) - available, "confirmed": confirmed,
            "coverage_pct": round(available / len(ignition) * 100.0, 1) if ignition else 0.0,
        }
        result["confirmation_diagnostics"] = confirmation_diagnostics(ignition)
        _, excursion_hold = chronological_split(ignition, holdout_pct=holdout_pct)
        result["excursions"] = {
            "all": _excursion_summary(ignition),
            "holdout": _excursion_summary(excursion_hold),
        }
        result["promotion_benchmark"] = {
            "intraday": _promotion_benchmark(best, "intraday_returns", "2h", "intraday", holdout_pct),
            "swing": _promotion_benchmark(swing_events, "swing_returns", "1D", "swing", holdout_pct),
        }
        result["by_breakout_source"] = {}
        for source in sorted({e.get("breakout_source") for e in ignition if e.get("breakout_source")}):
            subset = [e for e in ignition if e.get("breakout_source") == source]
            result["by_breakout_source"][source] = {
                "intraday": summarize_named_returns(subset, "intraday_returns", ("2h", "eod")),
                "swing": summarize_named_returns(subset, "swing_returns", ("1D", "2D")),
                "events": len(subset),
            }
        result["by_direction"] = {}
        for direction in ("Bullish", "Bearish"):
            subset = [e for e in ignition if e.get("direction") == direction]
            result["by_direction"][direction] = {
                "intraday": summarize_named_returns(subset, "intraday_returns", ("2h", "eod")),
                "swing": summarize_named_returns(subset, "swing_returns", ("1D", "2D")),
                "events": len(subset),
            }
    return result
