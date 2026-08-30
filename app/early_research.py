"""Focused research helpers for the F&O early-movement engine."""
from __future__ import annotations

import numpy as np
import pandas as pd

RESEARCH_BUILD_ID = "2026-08-30-INSTITUTIONAL-V9.1.1-RESUMABLE-BACKTEST"



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



def _prior_session_ranges(df: pd.DataFrame):
    """Prior-completed-session 20/50 day ranges, mapped to intraday bars."""
    sessions = pd.Series(df.index.normalize(), index=df.index)
    daily = df.groupby(df.index.normalize()).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    )
    out = {}
    for n in (20, 50):
        minp = min(n, 10 if n == 20 else 20)
        hi = daily["high"].shift(1).rolling(n, min_periods=minp).max()
        lo = daily["low"].shift(1).rolling(n, min_periods=minp).min()
        out[f"high{n}"] = sessions.map(hi)
        out[f"low{n}"] = sessions.map(lo)
    return out


def _historical_market_regime(index_close: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Point-in-time research regime when breadth history is unavailable."""
    idx = pd.to_numeric(index_close, errors="coerce").reindex(index, method="ffill", limit=2)
    ret8 = idx.pct_change(8) * 100.0
    vol = idx.pct_change().rolling(20, min_periods=10).std() * 100.0
    return pd.Series(
        np.where(ret8 >= 0.35, "Trend Up",
                 np.where(ret8 <= -0.35, "Trend Down",
                          np.where((ret8.abs() < 0.20) & (vol >= 0.20), "Rotation", "Chop"))),
        index=index, dtype=object,
    )


def build_feature_frame(df, timeframe="15minute", oi_series=None, index_df=None, sector_df=None, sector_rank_series=None, futures_df=None):
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

    if timeframe in ("15minute", "4hour"):
        interval_minutes = 15 if timeframe == "15minute" else 240
        tod = indicators.time_of_day_rvol(
            df, lookback_sessions=20, interval_minutes=interval_minutes
        )
        # On 4H this is deliberately the first completed 4H bucket versus the
        # same bucket on prior sessions, not a pretend 30-minute opening read.
        opening_bars = 2 if timeframe == "15minute" else 1
        opening_rvol = indicators.opening_relative_volume(
            df, opening_bars=opening_bars, lookback_sessions=14
        )
    else:
        tod = pd.Series(np.nan, index=df.index)
        opening_rvol = pd.Series(np.nan, index=df.index)
    out["tod_rvol"] = tod
    out["opening_rvol"] = opening_rvol
    prev_med = tod.shift(1).rolling(4, min_periods=2).median()
    out["tod_rvol_accel"] = tod / prev_med.replace(0, np.nan)
    out["vol_rising"] = (df["volume"] > df["volume"].shift(1)) & (df["volume"].shift(1) > df["volume"].shift(2))

    price = stock_in_play.build_price_features(
        df, series["atr"], comp, tod, opening_rvol=opening_rvol, timeframe=timeframe
    )
    for col in price.columns:
        # compression_score / energy_building are already present from comp;
        # assigning again is harmless and guarantees live/research parity.
        out[col] = price[col]
    direction = out["breakout_direction"]
    out["direction"] = direction
    out["entry_trigger"] = np.where(out["fresh_breakout"], direction, None)
    out["entry_trigger_bars_ago"] = np.where(out["fresh_breakout"], 0, np.nan)
    # Context must follow the breakout currently being evaluated.  On the
    # one-bar-later retention/retest bar there is no fresh breakout direction,
    # so use the retained direction instead of turning OI/4H/VWAP into NaN.
    context_direction = direction.where(direction.notna(), out.get("retained_breakout_direction"))

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

    # VWAP/anti-chase are evaluated relative to the fresh OR retained breakout
    # direction.  Simple VWAP-side agreement was true for ~94% of historical
    # events, so also measure directional distance in ATRs for a stricter,
    # testable proximity condition rather than pretending all "above VWAP"
    # observations are equally informative.
    vwap = indicators.session_vwap_series(df, timeframe)
    atr_safe = pd.to_numeric(series["atr"], errors="coerce").replace(0, np.nan)
    out["vwap_side_agrees"] = np.where(
        context_direction.eq("Bullish"), df["close"] > vwap,
        np.where(context_direction.eq("Bearish"), df["close"] < vwap, np.nan),
    )
    out["vwap_distance_atr"] = np.where(
        context_direction.eq("Bullish"), (df["close"] - vwap) / atr_safe,
        np.where(context_direction.eq("Bearish"), (vwap - df["close"]) / atr_safe, np.nan),
    )
    out["vwap_proximity_quality"] = (pd.to_numeric(out["vwap_distance_atr"], errors="coerce") >= 0) & (pd.to_numeric(out["vwap_distance_atr"], errors="coerce") <= 0.75)
    out["breakout_vwap_agrees"] = out["vwap_side_agrees"]
    failed_dir = out.get("failed_breakout_direction", pd.Series(None, index=df.index, dtype=object))
    out["failed_breakout_vwap_reject"] = np.where(
        pd.Series(failed_dir, index=df.index).eq("Bearish"), df["close"] < vwap,
        np.where(pd.Series(failed_dir, index=df.index).eq("Bullish"), df["close"] > vwap, np.nan),
    )
    effective_extension = pd.to_numeric(out["breakout_extension_atr"], errors="coerce").fillna(
        pd.to_numeric(out.get("retained_breakout_extension_atr"), errors="coerce")
    )
    out["entry_is_extended"] = effective_extension > stock_in_play._live_setting("MAX_ENTRY_EXTENSION_ATR", stock_in_play.MAX_BREAKOUT_EXTENSION_ATR)
    out["breakout_entry_extended"] = out["entry_is_extended"]
    out["breakout_state"] = np.where(context_direction.eq("Bullish"), "Breakout", np.where(context_direction.eq("Bearish"), "Breakdown", None))

    # V8 pairs price and OI over the SAME 60-minute window.  This is kept
    # separate from the older 20-bar relative-strength return so the OI
    # quadrant cannot silently compare different horizons.
    close_num = pd.to_numeric(df["close"], errors="coerce")
    out["price_chg_60m_pct"] = (
        _session_pct_change(close_num, 4)
        if timeframe == "15minute" else close_num.pct_change() * 100.0
    )

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
        out["oi_recent_agrees"] = np.where(context_direction.notna(), oi60 > 0, np.nan)

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
            np.where(context_direction.eq("Bullish"), sec_ret > 0,
                     np.where(context_direction.eq("Bearish"), sec_ret < 0, np.nan)),
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
                np.where(context_direction.eq("Bullish"), aligned.eq("Bullish"),
                         np.where(context_direction.eq("Bearish"), aligned.eq("Bearish"), np.nan)),
                index=df.index,
            )
        else:
            out["htf_agrees"] = np.nan
    else:
        out["htf_agrees"] = np.nan

    # ------------------------------------------------------------------
    # V6 research features. These are observable, point-in-time proxies for
    # information-driven participation and continuation regime. OI is only one
    # sponsorship input; it is not a universal veto.
    # ------------------------------------------------------------------
    from . import v6_edge
    prior = _prior_session_ranges(df)
    out["prior_high_20d"] = prior["high20"]
    out["prior_low_20d"] = prior["low20"]
    out["prior_high_50d"] = prior["high50"]
    out["prior_low_50d"] = prior["low50"]

    denom20 = (out["prior_high_20d"] - out["prior_low_20d"]).replace(0, np.nan)
    denom50 = (out["prior_high_50d"] - out["prior_low_50d"]).replace(0, np.nan)
    p20 = ((df["close"] - out["prior_low_20d"]) / denom20 * 100.0).clip(0, 100)
    p50 = ((df["close"] - out["prior_low_50d"]) / denom50 * 100.0).clip(0, 100)
    out["price_position_20d_pct"] = p20
    out["price_position_50d_pct"] = p50
    loc_mean = pd.concat([p20, p50], axis=1).mean(axis=1, skipna=True)
    out["price_location_score"] = np.where(
        context_direction.eq("Bullish"), loc_mean,
        np.where(context_direction.eq("Bearish"), 100.0 - loc_mean, np.nan),
    )
    near_hi = out["prior_high_20d"].gt(0) & (df["close"] / out["prior_high_20d"] >= 0.985)
    near_lo = out["prior_low_20d"].gt(0) & (df["close"] <= out["prior_low_20d"] * 1.015)
    out["near_20d_high"] = near_hi
    out["near_20d_low"] = near_lo
    out.loc[context_direction.eq("Bullish") & near_hi, "price_location_score"] = out.loc[context_direction.eq("Bullish") & near_hi, "price_location_score"].clip(lower=90)
    out.loc[context_direction.eq("Bearish") & near_lo, "price_location_score"] = out.loc[context_direction.eq("Bearish") & near_lo, "price_location_score"].clip(lower=90)

    turnover = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(df["volume"], errors="coerce")
    out["turnover_notional"] = turnover
    # Historical self-percentile is an initial stock-in-play proxy. Aggregate
    # research later adds a cross-sectional candidate percentile at each signal.
    out["turnover_percentile"] = _rolling_last_percentile(turnover, lookback=80, min_periods=20)

    out["catalyst_score"] = [
        v6_edge.catalyst_proxy_score(
            gap_atr=g, opening_rvol=o, tod_rvol=t, bar_range_atr=r,
            turnover_percentile=pct,
        )
        for g, o, t, r, pct in zip(
            out.get("gap_atr"), out.get("opening_rvol"), out.get("tod_rvol"),
            out.get("bar_range_atr"), out.get("turnover_percentile")
        )
    ]

    if index_df is not None and not index_df.empty:
        idx_close_v6 = pd.Series(index_df["close"]).reindex(df.index, method="ffill", limit=2)
        out["market_regime"] = _historical_market_regime(idx_close_v6, df.index)
        out["index_ret_8_pct"] = idx_close_v6.pct_change(8) * 100.0
    else:
        out["market_regime"] = "Unknown"
        out["index_ret_8_pct"] = np.nan

    if sector_df is not None and not sector_df.empty:
        sec_close_v6 = pd.Series(sector_df["close"]).reindex(df.index, method="ffill", limit=2)
        out["sector_ret_8_pct"] = sec_close_v6.pct_change(8) * 100.0
        out["stock_sector_lead_pct"] = df["close"].pct_change(8) * 100.0 - out["sector_ret_8_pct"]
    else:
        out["sector_ret_8_pct"] = np.nan
        out["stock_sector_lead_pct"] = np.nan

    if sector_rank_series is not None:
        out["sector_rank_percentile"] = pd.to_numeric(pd.Series(sector_rank_series), errors="coerce").reindex(df.index, method="ffill", limit=2)
    else:
        out["sector_rank_percentile"] = np.nan

    # Intraday futures basis has necessarily partial historical coverage because
    # Kite cannot reconstruct every expired single-stock futures contract at
    # 15-minute granularity. Missing basis is therefore NaN, never a failed gate.
    if futures_df is not None and not futures_df.empty and "close" in futures_df:
        fut_close = pd.to_numeric(pd.Series(futures_df["close"]), errors="coerce").reindex(df.index, method="ffill", limit=2)
        out["basis_pct"] = (fut_close / pd.to_numeric(df["close"], errors="coerce") - 1.0) * 100.0
        out["basis_acceleration"] = out["basis_pct"] - out["basis_pct"].shift(2)
    else:
        out["basis_pct"] = np.nan
        out["basis_acceleration"] = np.nan

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



def _setup_bar_available_at(ts, setup_timeframe="15minute"):
    """Timestamp when a completed setup candle can first be acted on."""
    stamp = pd.Timestamp(ts)
    if setup_timeframe == "4hour":
        theoretical = stamp + pd.Timedelta(hours=4)
        session_close = stamp.normalize() + pd.Timedelta(hours=15, minutes=30)
        return min(theoretical, session_close)
    if setup_timeframe == "15minute":
        return stamp + pd.Timedelta(minutes=15)
    if setup_timeframe == "60minute":
        return stamp + pd.Timedelta(hours=1)
    return stamp


def _align_timestamp_to_index(ts, index):
    stamp = pd.Timestamp(ts)
    if not isinstance(index, pd.DatetimeIndex):
        return stamp
    try:
        if index.tz is not None and stamp.tzinfo is None:
            stamp = stamp.tz_localize(index.tz)
        elif index.tz is None and stamp.tzinfo is not None:
            stamp = stamp.tz_localize(None)
        elif index.tz is not None and stamp.tzinfo is not None:
            stamp = stamp.tz_convert(index.tz)
    except Exception:
        pass
    return stamp


def _first_execution_pos(execution_df, available_at):
    if execution_df is None or execution_df.empty:
        return None
    target = _align_timestamp_to_index(available_at, execution_df.index)
    pos = int(execution_df.index.searchsorted(target, side="left"))
    return pos if 0 <= pos < len(execution_df) else None


def _future_abs_move_atr_from_available(execution_df, available_at, ref_price, atr,
                                        horizons=(4, 8, 16, 25)):
    """Clock-consistent expansion path on the 15m execution stream."""
    out = {}
    if atr is None or not np.isfinite(atr) or atr <= 0:
        return out
    start = _first_execution_pos(execution_df, available_at)
    if start is None:
        return out
    for h in horizons:
        end = min(len(execution_df), start + int(h))
        if end <= start:
            continue
        hi = float(execution_df["high"].iloc[start:end].max())
        lo = float(execution_df["low"].iloc[start:end].min())
        out[str(h)] = max(abs(hi - float(ref_price)), abs(lo - float(ref_price))) / float(atr)
    return out


def _replay_breakout_feature_frame(df, features, symbol, cost_pct=0.05, slippage_pct=0.02,
                                   execution_df=None, setup_timeframe="15minute", fast_v8=False):
    """Replay actual price breakouts with intraday and 1–2D outcomes.

    Intraday entries are measured from the first fresh escape. Swing candidates
    are measured separately from the later retention bar used by the live
    1–2D classifier, so the backtest pays the same confirmation delay as live.
    """
    from . import stock_in_play

    execution = execution_df if execution_df is not None else df
    execution_timeframe = "15minute" if execution_df is not None else setup_timeframe
    energy_events, baseline_events, ignition_events, best_events, swing_events, recent_range_confirmation_events = [], [], [], [], [], []
    v9_playbook_events = []
    energy = features.get("energy_building")
    if energy is None:
        energy = pd.Series(False, index=features.index)
    energy = pd.Series(energy, index=features.index).fillna(False).astype(bool)
    energy_rise = energy & ~energy.shift(1, fill_value=False)

    def _event_from_row(row, pos, direction, classified):
        atr = _py(features["atr"].iloc[pos]) if "atr" in features.columns else None
        if atr is None or atr <= 0:
            return None
        signal_available_at = _setup_bar_available_at(df.index[pos], setup_timeframe)
        if execution_df is None:
            if pos + 1 >= len(df):
                return None
            outcomes = stock_in_play.compute_trade_outcomes(
                df, signal_pos=pos, direction=direction, atr=float(atr),
                cost_pct=cost_pct, slippage_pct=slippage_pct,
            )
        else:
            resolved_entry_pos = _first_execution_pos(execution, signal_available_at)
            if resolved_entry_pos is None:
                return None
            outcomes = stock_in_play.compute_trade_outcomes_from_entry(
                execution, entry_pos=resolved_entry_pos, direction=direction, atr=float(atr),
                cost_pct=cost_pct, slippage_pct=slippage_pct,
            )
        entry_pos = outcomes.get("entry_pos")
        event = {
            "symbol": symbol,
            "signal_time": df.index[pos].isoformat(),
            "signal_available_at": signal_available_at.isoformat(),
            "entry_time": execution.index[entry_pos].isoformat() if entry_pos is not None else None,
            "setup_timeframe": setup_timeframe,
            "execution_timeframe": execution_timeframe,
            "direction": direction,
            # Raw signal-candle geometry is carried into V8 so Bull and Bear
            # price-acceptance scores are genuinely directional rather than
            # inferred later from a generic breakout label.
            "high": float(df["high"].iloc[pos]),
            "low": float(df["low"].iloc[pos]),
            "close": float(df["close"].iloc[pos]),
            "price_chg_60m_pct": row.get("price_chg_60m_pct"),
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
            "vwap_distance_atr": row.get("vwap_distance_atr"),
            "vwap_proximity_quality": row.get("vwap_proximity_quality"),
            "entry_is_extended": row.get("entry_is_extended"),
            "breakout_retained": row.get("breakout_retained"),
            "retest_confirmed": row.get("breakout_retest_confirmed"),
            "fresh_breakout": row.get("fresh_breakout"),
            "retained_breakout_direction": row.get("retained_breakout_direction"),
            "retained_breakout_source": row.get("retained_breakout_source"),
            "retained_breakout_level": row.get("retained_breakout_level"),
            "retained_breakout_extension_atr": row.get("retained_breakout_extension_atr"),
            "failed_breakout_direction": row.get("failed_breakout_direction"),
            "failed_breakout_source": row.get("failed_breakout_source"),
            "failed_breakout_level": row.get("failed_breakout_level"),
            "failed_breakout_extension_atr": row.get("failed_breakout_extension_atr"),
            "failed_breakout_vwap_reject": row.get("failed_breakout_vwap_reject"),
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
            "entry_pos": entry_pos,
            "entry_price": outcomes.get("entry_price"),
            "atr_value": float(atr),
            "turnover_notional": row.get("turnover_notional"),
            "turnover_percentile": row.get("turnover_percentile"),
            "gap_atr": row.get("gap_atr"),
            "opening_rvol": row.get("opening_rvol"),
            "bar_range_atr": row.get("bar_range_atr"),
            "catalyst_score": row.get("catalyst_score"),
            "market_regime": row.get("market_regime"),
            "sector_rank_percentile": row.get("sector_rank_percentile"),
            "stock_sector_lead_pct": row.get("stock_sector_lead_pct"),
            "price_location_score": row.get("price_location_score"),
            "basis_pct": row.get("basis_pct"),
            "basis_acceleration": row.get("basis_acceleration"),
        }
        # Legacy V6 classification and path-exit grids are intentionally
        # bypassed by the V8 fast path. They are expensive and have no role in
        # V8.1/V8.2 Top-K selection; the Legacy / 4H Diagnostic button still
        # runs them through the full research path.
        if not fast_v8:
            try:
                from . import v6_edge
                v6_row = dict(row)
                v6_row["direction"] = direction
                v6_row["oi_confirmed"] = classified.get("oi_status") == "Confirmed"
                v6_classified = v6_edge.classify_v6_candidate(v6_row)
                event["v6_stage"] = v6_classified.get("stage")
                event["v6_score"] = v6_classified.get("score")
                event["v6_intraday_eligible"] = v6_classified.get("intraday_eligible", False)
                event["v6_swing_eligible"] = v6_classified.get("swing_eligible", False)
                event["v6_sponsorship"] = v6_classified.get("sponsorship")
                event["v6_blockers"] = v6_classified.get("blockers", [])
            except Exception:
                event["v6_stage"] = None
                event["v6_score"] = None
                event["v6_intraday_eligible"] = False
                event["v6_swing_eligible"] = False
                event["v6_sponsorship"] = {}
                event["v6_blockers"] = []
            if event.get("breakout_source") == "Recent Range":
                attach_v6_path_exits(execution, event, max_bars=50)
        return event

    # A sampled non-coil baseline is enough to estimate lift without storing
    # every bar from the entire 211-stock universe in memory.
    for pos in range(len(df)):
        if not fast_v8:
            atr = _py(features["atr"].iloc[pos]) if "atr" in features.columns else None
            if atr is not None and atr > 0:
                if execution_df is None:
                    future = _future_abs_move_atr(df, pos, atr)
                else:
                    available_at = _setup_bar_available_at(df.index[pos], setup_timeframe)
                    future = _future_abs_move_atr_from_available(
                        execution, available_at, float(df["close"].iloc[pos]), atr
                    )
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

        # V9.1 Bull Institutional Accumulation probe. This is intentionally
        # independent of a price-range breakout: it starts from new long futures
        # positioning (price up + OI up), above-VWAP acceptance and at least normal
        # time-of-day participation. Cross-sectional ranks attached later decide
        # whether the activity is exceptional enough to become a trade candidate.
        price_60 = row.get("price_chg_60m_pct")
        oi_60 = row.get("oi_chg_60m_pct")
        tod = row.get("tod_rvol")
        if (fast_v8 and setup_timeframe == "15minute" and pos + 1 < len(df)
                and price_60 is not None and np.isfinite(price_60) and float(price_60) > 0
                and oi_60 is not None and np.isfinite(oi_60) and float(oi_60) > 0
                and row.get("vwap_side_agrees") is True
                and tod is not None and np.isfinite(tod) and float(tod) >= 1.0):
            accumulation_event = _event_from_row(row, pos, "Bullish", {})
            if accumulation_event is not None:
                accumulation_event["v91_accumulation_probe"] = True
                accumulation_event["fresh_breakout"] = False
                accumulation_event["breakout_source"] = None
                v9_playbook_events.append(accumulation_event)

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
                # In the V9 fast path the same immutable-at-this-stage event is
                # referenced by both families instead of duplicating a large dict.
                v9_playbook_events.append(event if fast_v8 else dict(event))
                if classified.get("intraday_eligible") and not fast_v8:
                    best_events.append(dict(event))

        # One-bar-later retention/retest: V9 needs this point-in-time event even
        # on the fast path for Pullback/Reclaim and VWAP Retest Failure. Legacy
        # V6 swing diagnostics remain disabled when fast_v8=True.
        retained_direction = row.get("retained_breakout_direction")
        if (row.get("breakout_retained") is True and retained_direction in ("Bullish", "Bearish")
                and pos + 1 < len(df)):
            retained_row = dict(row)
            retained_row["breakout_direction"] = None
            retained_row["retained_breakout_direction"] = retained_direction
            if retained_row.get("breakout_source") is None:
                retained_row["breakout_source"] = retained_row.get("retained_breakout_source")
            if retained_row.get("breakout_level") is None:
                retained_row["breakout_level"] = retained_row.get("retained_breakout_level")
            if retained_row.get("breakout_extension_atr") is None:
                retained_row["breakout_extension_atr"] = retained_row.get("retained_breakout_extension_atr")
            classified = stock_in_play.classify_live_candidate(retained_row)
            event = _event_from_row(retained_row, pos, retained_direction, classified)
            if event is not None and bool(retained_row.get("breakout_retest_confirmed")):
                v9_playbook_events.append(event if fast_v8 else dict(event))
            if not fast_v8:
                if event is not None and event.get("breakout_source") == "Recent Range":
                    recent_range_confirmation_events.append(dict(event))
                if classified.get("swing_eligible") and event is not None:
                    swing_events.append(event)

        # Failed breakout is known only after the next completed bar. V9 treats
        # a failed bullish escape as an independent bearish reversal playbook.
        failed_direction = row.get("failed_breakout_direction")
        if failed_direction in ("Bullish", "Bearish") and pos + 1 < len(df):
            failed_row = dict(row)
            failed_row["breakout_direction"] = None
            failed_row["retained_breakout_direction"] = None
            failed_row["breakout_source"] = failed_row.get("failed_breakout_source")
            failed_row["breakout_level"] = failed_row.get("failed_breakout_level")
            failed_row["breakout_extension_atr"] = failed_row.get("failed_breakout_extension_atr")
            event = _event_from_row(failed_row, pos, failed_direction, {})
            if event is not None:
                v9_playbook_events.append(event)

    return {
        "energy_events": energy_events,
        "baseline_energy_events": baseline_events,
        "ignition_events": ignition_events,
        "best_entry_events": best_events,
        "swing_events": swing_events,
        "recent_range_confirmation_events": recent_range_confirmation_events,
        "v9_playbook_events": v9_playbook_events,
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


def recent_range_edge_variants(events, confirmation_events=None):
    """Motivated Recent-Range variants, not a Cartesian optimisation grid.

    The current holdout evidence puts Recent Range materially ahead of Opening
    Range and compression breakouts, with bullish 1D behavior also much less
    negative than bearish.  This lab therefore asks a narrow sequence of
    falsifiable questions: does volume help, does OI help, do they help
    together, does 4H context help, and is a one-bar retention/retest entry
    better than buying the first escape?
    """
    from . import stock_in_play

    events = list(events or [])
    confirmations = list(confirmation_events or [])
    recent = [e for e in events if e.get("breakout_source") == "Recent Range"]
    bull = [e for e in recent if e.get("direction") == "Bullish"]
    bear = [e for e in recent if e.get("direction") == "Bearish"]
    tod_min = stock_in_play._live_setting("TOD_RVOL_MIN", stock_in_play.TOD_RVOL_MIN)

    def vol(e):
        v = e.get("tod_rvol")
        return stock_in_play._is_finite_number(v) and float(v) >= tod_min

    def oi(e):
        return e.get("oi_status") == "Confirmed"

    def htf(e):
        return stock_in_play._flag(e.get("htf_agrees")) is True

    def no_chase(e):
        return stock_in_play._flag(e.get("entry_is_extended")) is False

    def vwap_proximity(e):
        v = e.get("vwap_distance_atr")
        return stock_in_play._is_finite_number(v) and 0 <= float(v) <= 0.75

    retained = [e for e in confirmations
                if e.get("breakout_source") == "Recent Range"
                and e.get("direction") == "Bullish"
                and stock_in_play._flag(e.get("breakout_retained")) is True]
    retest = [e for e in retained if stock_in_play._flag(e.get("retest_confirmed")) is True]

    return {
        "recent_range_all": recent,
        "recent_range_bullish": bull,
        "recent_range_bearish": bear,
        "recent_range_plus_volume_oi": [e for e in recent if vol(e) and oi(e)],
        "bullish_plus_volume": [e for e in bull if vol(e)],
        "bullish_plus_oi": [e for e in bull if oi(e)],
        "bullish_plus_volume_oi": [e for e in bull if vol(e) and oi(e)],
        "bearish_plus_volume_oi": [e for e in bear if vol(e) and oi(e)],
        "bullish_plus_4h": [e for e in bull if htf(e)],
        "bullish_plus_volume_oi_4h": [e for e in bull if vol(e) and oi(e) and htf(e)],
        "bullish_plus_volume_oi_4h_no_chase": [e for e in bull if vol(e) and oi(e) and htf(e) and no_chase(e)],
        "bullish_plus_volume_oi_vwap_proximity": [e for e in bull if vol(e) and oi(e) and vwap_proximity(e)],
        "bullish_retained": retained,
        "bullish_retest": retest,
        "bullish_retained_volume_oi_4h": [e for e in retained if vol(e) and oi(e) and htf(e)],
        "bullish_retest_volume_oi_4h": [e for e in retest if vol(e) and oi(e) and htf(e)],
    }


def _recent_range_edge_report(events, confirmation_events, holdout_pct):
    from . import stock_in_play

    variants = recent_range_edge_variants(events, confirmation_events)
    confirmation_names = {
        "bullish_retained", "bullish_retest",
        "bullish_retained_volume_oi_4h", "bullish_retest_volume_oi_4h",
    }
    rows = []
    for name, subset in variants.items():
        subset = sorted(list(subset or []), key=lambda e: e.get("entry_time", ""))
        _train, hold = chronological_split(subset, holdout_pct=holdout_pct)
        i = summarize_named_returns(hold, "intraday_returns", ("2h", "eod"))
        sw = summarize_named_returns(hold, "swing_returns", ("1D", "2D"))
        primary = sw.get("1D", {})
        rows.append({
            "variant": name,
            "entry_type": "1-bar confirmation" if name in confirmation_names else "first escape",
            "total_events": len(subset),
            "holdout_events": len(hold),
            "intraday_2h": i.get("2h", {}),
            "intraday_eod": i.get("eod", {}),
            "swing_1D": sw.get("1D", {}),
            "swing_2D": sw.get("2D", {}),
            "holdout_1D_avg": primary.get("avg_return_pct"),
            "holdout_1D_pf": primary.get("profit_factor"),
            "promotion": _promotion_benchmark(
                subset, "swing_returns", "1D", "swing", holdout_pct
            ) if subset else {
                "status": "Research", "passed": False,
                "checks": {}, "requirements": {},
            },
        })
    rows.sort(key=lambda r: (
        r.get("holdout_1D_avg") is None,
        -(r.get("holdout_1D_avg") or -999),
        -(r.get("holdout_1D_pf") or 0),
    ))
    best = next((r for r in rows if (r.get("swing_1D") or {}).get("trade_count", 0) >= 30), None)
    return {
        "rows": rows,
        "best_1D_variant": best,
        "tod_rvol_threshold": stock_in_play._live_setting("TOD_RVOL_MIN", stock_in_play.TOD_RVOL_MIN),
        "vwap_proximity_max_atr": 0.75,
    }


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
                         cost_pct=0.05, slippage_pct=0.02, execution_df=None,
                         setup_timeframe="15minute", fast_v8=False):
    if "fresh_breakout" in features.columns and "breakout_direction" in features.columns:
        return _replay_breakout_feature_frame(
            df, features, symbol, cost_pct=cost_pct, slippage_pct=slippage_pct,
            execution_df=execution_df, setup_timeframe=setup_timeframe, fast_v8=fast_v8,
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


def aggregate_research(replays, holdout_pct=30.0, ref_horizon=3, horizons=(1, 2, 3, 5, 10), run_context=None):
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
    recent_range_confirmation_events = []
    for replay in replays or []:
        baseline.extend(replay.get("baseline_energy_events") or [])
        swing_events.extend(replay.get("swing_events") or [])
        recent_range_confirmation_events.extend(replay.get("recent_range_confirmation_events") or [])
    baseline.sort(key=lambda e: e.get("entry_time", ""))
    swing_events.sort(key=lambda e: e.get("entry_time", ""))
    recent_range_confirmation_events.sort(key=lambda e: e.get("entry_time", ""))

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
        result["recent_range_edge_lab"] = _recent_range_edge_report(
            ignition, recent_range_confirmation_events, holdout_pct
        )
        result["v8_dual"] = v8_dual_report(ignition)
        result["v6_edge_lab"] = v6_edge_report(ignition)
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



def aggregate_v8_research_fast(replays, holdout_pct=30.0, run_context=None):
    """Minimal V9 playbook aggregation for the primary backtest button.

    The historical function name is kept for API compatibility, but the V9
    primary path deliberately skips retired V8 Top-K tables as well as all V6
    labs. Cross-sectional V8 component ranks remain attached upstream only as
    transparent evidence features consumed by the six V9 playbooks.
    """
    ignition = []
    v9_candidates = []
    for replay in replays or []:
        ignition.extend(replay.get("ignition_events") or [])
        v9_candidates.extend(replay.get("v9_playbook_events") or [])
    ignition.sort(key=lambda e: e.get("entry_time", ""))
    v9_candidates.sort(key=lambda e: e.get("entry_time", ""))
    available = sum(1 for e in ignition if e.get("oi_status") != "Unavailable")
    confirmed = sum(1 for e in ignition if e.get("oi_status") == "Confirmed")
    mode = (run_context or {}).get("research_mode") or "v9_fast"
    result = {
        "research_build_id": RESEARCH_BUILD_ID,
        "holdout_pct": float(holdout_pct),
        "fast_v8": True,
        "fast_v9": True,
        "run_context": dict(run_context or {}),
        "oi_coverage": {
            "total": len(ignition),
            "available": available,
            "unavailable": len(ignition) - available,
            "confirmed": confirmed,
            "coverage_pct": round(available / len(ignition) * 100.0, 1) if ignition else 0.0,
        },
        "confirmation_diagnostics": confirmation_diagnostics(ignition),
    }
    if mode in ("v91_fast", "v91_bear_final"):
        result["v91_goal"] = v91_goal_report(
            v9_candidates,
            run_context=run_context,
            reveal_bear_final=(mode == "v91_bear_final"),
        )
    else:
        result["v9_playbooks"] = v9_playbook_report(v9_candidates)
    return result


def _v9_three_way(events, field, key):
    """V9 development/validation split with a permanently locked final 20%."""
    from . import v6_edge
    dev, validation, _final = v6_edge.three_way_split(events)
    return {
        "development": _v8_return_stats(dev, field, key),
        "validation": _v8_return_stats(validation, field, key),
        "validation_blocks": _v8_validation_blocks(events, field, key),
        "final_test": {
            "locked": True,
            "message": "V9 final 20% stays locked until an individual playbook is frozen after validation.",
        },
        "split": {"development_pct": 60, "validation_pct": 20, "final_pct": 20},
    }


def v9_playbook_report(events):
    """Evaluate V9 playbooks independently; never average Bull and Bear together."""
    from . import v9_playbooks
    rows = []
    for raw in events or []:
        row = dict(raw)
        # Pre-tagged events are accepted for isolated report tests and future
        # event archives. Normal replay rows are classified below from raw facts.
        if row.get("v9_playbook") in v9_playbooks.PLAYBOOKS:
            rows.append(row)
            continue
        try:
            now = pd.Timestamp(row.get("signal_time") or row.get("entry_time")).to_pydatetime()
        except Exception:
            now = None
        for play in v9_playbooks.evaluate_row(row, now=now):
            # Historical report tests only the exact TRADE rule. WATCH rows are
            # retained live for monitoring but must not inflate backtest N.
            if play.get("state") != "TRADE CANDIDATE":
                continue
            item = dict(row)
            item["v9_playbook"] = play.get("playbook")
            item["v9_score"] = play.get("score")
            item["v9_reasons"] = play.get("reasons") or []
            item["v9_modes"] = play.get("modes") or []
            rows.append(item)

    playbooks = {}
    for name in v9_playbooks.PLAYBOOKS:
        if name == v9_playbooks.BULL_CATALYST_CONTINUATION:
            playbooks[name] = {
                "historical_status": "LIVE_SHADOW",
                "message": "Real catalyst/news history is not available point-in-time; V9 refuses to fabricate a historical catalyst backtest.",
                "trade_count": 0,
            }
            continue
        subset = [e for e in rows if e.get("v9_playbook") == name]
        playbooks[name] = {
            "historical_status": "BACKTESTABLE",
            "side": "Bullish" if name.startswith("Bull ") else "Bearish",
            "trade_count": len(subset),
            "30m": _v9_three_way(subset, "intraday_returns", "30m"),
            "1h": _v9_three_way(subset, "intraday_returns", "1h"),
            "2h": _v9_three_way(subset, "intraday_returns", "2h"),
            "eod": _v9_three_way(subset, "intraday_returns", "eod"),
            "1D": _v9_three_way(subset, "swing_returns", "1D"),
            "2D": _v9_three_way(subset, "swing_returns", "2D"),
            "benchmark_2h": _v8_benchmark(subset, "intraday_returns", "2h"),
            "benchmark_1D": _v8_benchmark(subset, "swing_returns", "1D"),
        }
    promotable_bull = [n for n, p in playbooks.items() if n.startswith("Bull ") and p.get("benchmark_1D", {}).get("status") == "PROMOTABLE"]
    promotable_bear = [n for n, p in playbooks.items() if n.startswith("Bear ") and p.get("benchmark_1D", {}).get("status") == "PROMOTABLE"]
    return {
        "build_id": v9_playbooks.V9_BUILD_ID,
        "protocol": {
            "setup_timeframe": "15minute",
            "selection": "independent professional playbooks",
            "final_20_locked": True,
            "real_catalyst_history": False,
        },
        "playbooks": playbooks,
        "promotable_bull": promotable_bull,
        "promotable_bear": promotable_bear,
        "combined_status": "PROMOTABLE" if promotable_bull and promotable_bear else "RESEARCH",
        "event_count": len(events or []),
        "trade_event_count": len(rows),
    }


def attach_v6_path_exits(df: pd.DataFrame, event: dict, max_bars=50):
    """Attach conservative target/stop and breakeven-path outcomes to one event."""
    from . import v6_edge
    entry_pos = event.get("entry_pos")
    entry_price = event.get("entry_price")
    atr = event.get("atr_value")
    direction = event.get("direction")
    if entry_pos is None or not all(v is not None for v in (entry_price, atr, direction)):
        event["path_exits"] = {}
        return event
    grid = v6_edge.first_touch_grid(
        df, entry_pos=int(entry_pos), direction=direction, entry_price=float(entry_price),
        atr=float(atr), max_bars=max_bars,
    )
    grid["breakeven_0.50"] = v6_edge.breakeven_after_trigger_exit(
        df, entry_pos=int(entry_pos), direction=direction, entry_price=float(entry_price),
        atr=float(atr), trigger_atr=0.50, initial_stop_atr=0.50, target_atr=1.25,
        max_bars=max_bars,
    )
    grid["breakeven_0.75"] = v6_edge.breakeven_after_trigger_exit(
        df, entry_pos=int(entry_pos), direction=direction, entry_price=float(entry_price),
        atr=float(atr), trigger_atr=0.75, initial_stop_atr=0.50, target_atr=1.50,
        max_bars=max_bars,
    )
    event["path_exits"] = grid
    return event


def _v6_variant_report(events, predicate, field="swing_returns", key="1D"):
    from . import v6_edge
    subset = [e for e in (events or []) if predicate(e)]
    return v6_edge.three_way_research_report(subset, field=field, key=key)


def _v6_path_exit_report(events):
    """60/20/20 validation for path-aware target/stop variants."""
    from . import v6_edge
    rows = list(events or [])
    keys = sorted({
        key for e in rows for key in (e.get("path_exits") or {}).keys()
    })

    def stats(subset, key):
        vals = []
        targets = stops = breakevens = timeouts = 0
        for e in subset:
            payload = (e.get("path_exits") or {}).get(key) or {}
            v = payload.get("net_return_pct")
            try:
                if v is not None and np.isfinite(float(v)):
                    vals.append(float(v))
            except (TypeError, ValueError):
                continue
            outcome = payload.get("outcome")
            targets += int(outcome == "target")
            stops += int(outcome == "stop")
            breakevens += int(outcome == "breakeven")
            timeouts += int(outcome == "timeout")
        if not vals:
            return {"trade_count": 0, "win_rate_pct": None, "avg_return_pct": None, "profit_factor": None}
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v < 0]
        gp, gl = sum(wins), abs(sum(losses))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else None)
        return {
            "trade_count": len(vals),
            "win_rate_pct": round(len(wins) / len(vals) * 100.0, 1),
            "avg_return_pct": round(float(np.mean(vals)), 3),
            "median_return_pct": round(float(np.median(vals)), 3),
            "profit_factor": round(float(pf), 2) if pf is not None and np.isfinite(pf) else pf,
            "targets": targets, "stops": stops, "breakevens": breakevens, "timeouts": timeouts,
        }

    out = {}
    for key in keys:
        dev, val, final = v6_edge.three_way_split(rows)
        out[key] = {
            "development": stats(dev, key),
            "validation": stats(val, key),
            "final_test": v6_edge.final_test_payload(stats(final, key)),
        }
    return out


def _v8_return_stats(events, field, key):
    vals = []
    for e in events or []:
        payload = e.get(field) or {}
        value = payload.get(key) if isinstance(payload, dict) else None
        try:
            if value is not None and np.isfinite(float(value)):
                vals.append(float(value))
        except (TypeError, ValueError):
            continue
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


def _v8_three_way(events, field, key):
    from . import v6_edge
    dev, validation, _final = v6_edge.three_way_split(events)
    return {
        "development": _v8_return_stats(dev, field, key),
        "validation": _v8_return_stats(validation, field, key),
        "validation_blocks": _v8_validation_blocks(events, field, key),
        "final_test": {
            "locked": True,
            "message": "V8.1 final 20% is locked until Bull Top-3 and Bear Pressure Top-3 are frozen.",
        },
        "split": {"development_pct": 60, "validation_pct": 20, "final_pct": 20},
    }


def _v8_validation_blocks(events, field, key):
    """Four chronological blocks inside the validation 20%, never final data."""
    from . import v6_edge
    _dev, validation, _final = v6_edge.three_way_split(events)
    rows = list(validation)
    if not rows:
        return []
    chunks = np.array_split(np.arange(len(rows)), 4)
    out = []
    for i, idxs in enumerate(chunks, 1):
        subset = [rows[int(j)] for j in idxs]
        stats = _v8_return_stats(subset, field, key)
        out.append({"block": i, **stats, "positive": bool(
            stats.get("avg_return_pct") is not None and stats["avg_return_pct"] > 0
        )})
    return out


def _v8_excursion_ratio(events, key="1D"):
    mfes, maes = [], []
    for e in events or []:
        mfe = (e.get("mfe_atr") or {}).get(key)
        mae = (e.get("mae_atr") or {}).get(key)
        try:
            if mfe is not None and mae is not None and np.isfinite(float(mfe)) and np.isfinite(float(mae)):
                mfes.append(float(mfe)); maes.append(float(mae))
        except (TypeError, ValueError):
            continue
    if not mfes:
        return None
    mean_mfe = float(np.mean(mfes)); mean_mae = float(np.mean(maes))
    if mean_mae <= 1e-9:
        return float("inf") if mean_mfe > 0 else None
    return round(mean_mfe / mean_mae, 2)


def _v8_benchmark(events, field, key):
    from . import v6_edge
    _dev, validation, _final = v6_edge.three_way_split(events)
    stats = _v8_return_stats(validation, field, key)
    blocks = _v8_validation_blocks(events, field, key)
    positive_blocks = sum(1 for b in blocks if b.get("positive"))
    excursion = _v8_excursion_ratio(validation, "1D" if field == "swing_returns" else key)
    pf = stats.get("profit_factor")
    checks = {
        "sample": stats.get("trade_count", 0) >= 100,
        "expectancy": stats.get("avg_return_pct") is not None and stats["avg_return_pct"] > 0,
        "profit_factor": pf is not None and (pf == float("inf") or pf >= 1.25),
        "excursion_quality": None if excursion is None else excursion >= 1.40,
        "chronological_stability": len(blocks) == 4 and positive_blocks >= 3,
    }
    mandatory = [checks["sample"], checks["expectancy"], checks["profit_factor"], checks["chronological_stability"]]
    if checks["excursion_quality"] is not None:
        mandatory.append(checks["excursion_quality"])
    return {
        "status": "PROMOTABLE" if all(mandatory) else "RESEARCH",
        "validation": stats,
        "checks": checks,
        "mfe_mae_ratio": excursion,
        "positive_blocks": positive_blocks,
        "blocks": blocks,
        "requirements": {"n": 100, "avg_net_gt": 0.0, "pf": 1.25, "mfe_mae": 1.40, "positive_blocks": 3},
    }


def _ensure_v8_event_scores(events):
    """Score events missing V8 fields, grouped at their signal timestamp.

    Backtest.py can attach full-universe percentiles before aggregation. This
    fallback keeps unit tests and partial research runs usable by ranking the
    contemporaneous event cross-section only; it never overwrites pre-ranked
    full-universe scores.
    """
    from . import v8_dual
    rows = [dict(e) for e in (events or [])]
    missing = [i for i, e in enumerate(rows) if e.get("v8_alpha") is None]
    if not missing:
        return rows
    groups = {}
    for i in missing:
        groups.setdefault(rows[i].get("signal_time") or rows[i].get("entry_time") or "", []).append(i)
    for idxs in groups.values():
        scored = v8_dual.rank_cross_section([rows[i] for i in idxs])
        for i, scored_row in zip(idxs, scored):
            for key, value in scored_row.items():
                if key.startswith("v8_") or key.startswith("v81_"):
                    rows[i][key] = value
    for row in rows:
        if row.get("direction") == "Bearish" and row.get("v81_bear_pressure") is None:
            row["v81_bear_pressure"] = v8_dual.bear_pressure_score(row)
    return rows


def v8_dual_report_fast(events):
    """Primary V8.2.2 report without audit-only ablations or duplicate Top-K scans.

    The operational logic is identical to V8.1/V8.2.1: Bullish Recent-Range
    Top-K and independent Bear Pressure Top-K with fixed quality floors.  The
    fast report computes K=1/3/5 in one pass per side and reuses Top-3 for the
    full-horizon benchmark.  Legacy ablations stay available only through the
    explicit diagnostic path.
    """
    from . import v8_dual
    rows = _ensure_v8_event_scores(events)

    def fin(e, key):
        try:
            v = e.get(key)
            return float(v) if v is not None and np.isfinite(float(v)) else None
        except (TypeError, ValueError):
            return None

    def not_chased(e):
        ext = fin(e, "breakout_extension_atr")
        return ext is None or ext <= v8_dual.MAX_EXTENSION_ATR

    bull_base = [e for e in rows if e.get("direction") == "Bullish" and e.get("breakout_source") == "Recent Range" and not_chased(e)]
    bear_base = [e for e in rows if e.get("direction") == "Bearish" and not_chased(e)]

    report = {
        "protocol": {
            "setup_timeframe": "15minute",
            "entry": "next executable 15-minute bar",
            "weights_fitted": False,
            "parameter_grid": False,
            "final_locked": True,
            "selection": "point-in-time Top-K, predefined K=1/3/5",
            "operational_breadth": 3,
            "watch_quality_floor": 70.0,
            "participation_floor": 70.0,
            "max_extension_atr": v8_dual.MAX_EXTENSION_ATR,
            "bull_origin": "Bullish Recent-Range escape",
            "bear_origin": "Any bearish breakout source; pressure-led ranking",
            "bear_pressure": "median(Participation, Relative Weakness, Derivatives, Bear CLV)",
            "fast_stage3": True,
        }
    }

    bull_sets = v8_dual.select_top_k_breadths(
        bull_base, score_field="v8_alpha", ks=(1, 3, 5), direction="Bullish",
        participation_floor=70.0, score_floor=70.0, allowed_sources={"Recent Range"},
    )
    bear_sets = v8_dual.select_top_k_breadths(
        bear_base, score_field="v81_bear_pressure", ks=(1, 3, 5), direction="Bearish",
        participation_floor=70.0, score_floor=70.0, allowed_sources=None,
    )

    report["bullish"] = {"primary_variants": {}}
    report["bearish"] = {"primary_variants": {}}
    for k in (1, 3, 5):
        subset = bull_sets[k]
        report["bullish"]["primary_variants"][f"top{k}"] = {
            "2h": _v8_three_way(subset, "intraday_returns", "2h"),
            "1D": _v8_three_way(subset, "swing_returns", "1D"),
        }
        subset = bear_sets[k]
        report["bearish"]["primary_variants"][f"pressure_top{k}"] = {
            "2h": _v8_three_way(subset, "intraday_returns", "2h"),
            "1D": _v8_three_way(subset, "swing_returns", "1D"),
        }

    for name, full in (("bullish", bull_sets[3]), ("bearish", bear_sets[3])):
        report[name]["full_horizons"] = {
            "30m": _v8_three_way(full, "intraday_returns", "30m"),
            "1h": _v8_three_way(full, "intraday_returns", "1h"),
            "2h": _v8_three_way(full, "intraday_returns", "2h"),
            "eod": _v8_three_way(full, "intraday_returns", "eod"),
            "1D": _v8_three_way(full, "swing_returns", "1D"),
            "2D": _v8_three_way(full, "swing_returns", "2D"),
        }
        report[name]["benchmark"] = {
            "intraday_2h": _v8_benchmark(full, "intraday_returns", "2h"),
            "swing_1D": _v8_benchmark(full, "swing_returns", "1D"),
        }

    bull_ok = report["bullish"]["benchmark"]["swing_1D"]["status"] == "PROMOTABLE"
    bear_ok = report["bearish"]["benchmark"]["swing_1D"]["status"] == "PROMOTABLE"
    report["combined_status"] = "PROMOTABLE" if bull_ok and bear_ok else "RESEARCH"
    report["total_recent_range_events"] = sum(1 for e in rows if e.get("breakout_source") == "Recent Range")
    report["total_bearish_breakout_events"] = len(bear_base)
    return report


def v8_dual_report(events):
    """Evidence-locked V8.1 Bull/Bear report with predefined Top-K breadth.

    Bull keeps the validated 15-minute Recent-Range mechanism but chooses the
    strongest point-in-time names instead of requiring Alpha >= 85. Bear is
    independent: it ranks selling pressure across all bearish breakout sources
    using participation, relative weakness, derivatives and close-near-low
    acceptance. K=1/3/5 is declared portfolio breadth, not a score grid.
    """
    from . import v8_dual
    rows = _ensure_v8_event_scores(events)

    def fin(e, key):
        try:
            v = e.get(key)
            return float(v) if v is not None and np.isfinite(float(v)) else None
        except (TypeError, ValueError):
            return None

    def not_chased(e):
        ext = fin(e, "breakout_extension_atr")
        return ext is None or ext <= v8_dual.MAX_EXTENSION_ATR

    bull_base = [e for e in rows if e.get("direction") == "Bullish" and e.get("breakout_source") == "Recent Range" and not_chased(e)]
    bear_base = [e for e in rows if e.get("direction") == "Bearish" and not_chased(e)]

    # Old fixed-cutoff ablations remain machine-readable for audit continuity,
    # but they are no longer the primary selection surface.
    legacy_variants = {
        "raw_recent_range": lambda e: not_chased(e),
        "structure_only": lambda e: not_chased(e) and (fin(e, "v8_structure") or -1) >= 85,
        "participation_only": lambda e: not_chased(e) and (fin(e, "v8_participation") or -1) >= 85,
        "relative_only": lambda e: not_chased(e) and (fin(e, "v8_relative") or -1) >= 85,
        "derivatives_only": lambda e: not_chased(e) and (fin(e, "v8_derivatives") or -1) >= 85,
        "full_consensus": lambda e: bool(e.get("v8_eligible")),
    }

    report = {
        "protocol": {
            "setup_timeframe": "15minute",
            "entry": "next executable 15-minute bar",
            "weights_fitted": False,
            "parameter_grid": False,
            "final_locked": True,
            "selection": "point-in-time Top-K, predefined K=1/3/5",
            "operational_breadth": 3,
            "watch_quality_floor": 70.0,
            "participation_floor": 70.0,
            "max_extension_atr": v8_dual.MAX_EXTENSION_ATR,
            "bull_origin": "Bullish Recent-Range escape",
            "bear_origin": "Any bearish breakout source; pressure-led ranking",
            "bear_pressure": "median(Participation, Relative Weakness, Derivatives, Bear CLV)",
        }
    }

    # Bull primary: top names among Recent-Range bullish escapes.
    bull_primary = {}
    for k in (1, 3, 5):
        subset = v8_dual.select_top_k(
            bull_base, score_field="v8_alpha", k=k, direction="Bullish",
            participation_floor=70.0, score_floor=70.0, allowed_sources={"Recent Range"},
        )
        bull_primary[f"top{k}"] = {
            "2h": _v8_three_way(subset, "intraday_returns", "2h"),
            "1D": _v8_three_way(subset, "swing_returns", "1D"),
        }

    # Bear primary: independent selling-pressure ranking; Recent Range is not
    # a mandatory origin and mirrored bullish Structure is not in the score.
    bear_primary = {}
    for k in (1, 3, 5):
        subset = v8_dual.select_top_k(
            bear_base, score_field="v81_bear_pressure", k=k, direction="Bearish",
            participation_floor=70.0, score_floor=70.0, allowed_sources=None,
        )
        bear_primary[f"pressure_top{k}"] = {
            "2h": _v8_three_way(subset, "intraday_returns", "2h"),
            "1D": _v8_three_way(subset, "swing_returns", "1D"),
        }

    report["bullish"] = {"primary_variants": bull_primary, "legacy_ablations": {}}
    report["bearish"] = {"primary_variants": bear_primary, "legacy_ablations": {}}

    recent = [e for e in rows if e.get("breakout_source") == "Recent Range"]
    for direction, name in (("Bullish", "bullish"), ("Bearish", "bearish")):
        side_recent = [e for e in recent if e.get("direction") == direction]
        for variant_name, pred in legacy_variants.items():
            subset = [e for e in side_recent if pred(e)]
            payload = {
                "2h": _v8_three_way(subset, "intraday_returns", "2h"),
                "1D": _v8_three_way(subset, "swing_returns", "1D"),
            }
            report[name]["legacy_ablations"][variant_name] = payload
            # Backward-compatible aliases for existing API consumers/tests.
            report[name][variant_name] = payload

    bull_operational = v8_dual.select_top_k(
        bull_base, score_field="v8_alpha", k=3, direction="Bullish",
        participation_floor=70.0, score_floor=70.0, allowed_sources={"Recent Range"},
    )
    bear_operational = v8_dual.select_top_k(
        bear_base, score_field="v81_bear_pressure", k=3, direction="Bearish",
        participation_floor=70.0, score_floor=70.0, allowed_sources=None,
    )
    for name, full in (("bullish", bull_operational), ("bearish", bear_operational)):
        report[name]["full_horizons"] = {
            "30m": _v8_three_way(full, "intraday_returns", "30m"),
            "1h": _v8_three_way(full, "intraday_returns", "1h"),
            "2h": _v8_three_way(full, "intraday_returns", "2h"),
            "eod": _v8_three_way(full, "intraday_returns", "eod"),
            "1D": _v8_three_way(full, "swing_returns", "1D"),
            "2D": _v8_three_way(full, "swing_returns", "2D"),
        }
        report[name]["benchmark"] = {
            "intraday_2h": _v8_benchmark(full, "intraday_returns", "2h"),
            "swing_1D": _v8_benchmark(full, "swing_returns", "1D"),
        }

    bull_ok = report["bullish"]["benchmark"]["swing_1D"]["status"] == "PROMOTABLE"
    bear_ok = report["bearish"]["benchmark"]["swing_1D"]["status"] == "PROMOTABLE"
    report["combined_status"] = "PROMOTABLE" if bull_ok and bear_ok else "RESEARCH"
    report["total_recent_range_events"] = len(recent)
    report["total_bearish_breakout_events"] = len(bear_base)
    return report

def v6_edge_report(events):
    """Focused 60/20/20 V6 variants; final 20% stays locked by default."""
    from . import stock_in_play
    recent = [e for e in (events or []) if e.get("breakout_source") == "Recent Range"]
    long = [e for e in recent if e.get("direction") == "Bullish"]
    short = [e for e in recent if e.get("direction") == "Bearish"]

    def fin(e, key, default=None):
        v = e.get(key, default)
        try:
            return float(v) if v is not None and np.isfinite(float(v)) else None
        except (TypeError, ValueError):
            return None

    def high_turnover(e):
        v = fin(e, "turnover_percentile")
        return v is not None and v >= 80

    def catalyst(e):
        v = fin(e, "catalyst_score")
        return v is not None and v >= 60

    def leadership_location(e):
        lead = fin(e, "stock_sector_lead_pct")
        loc = fin(e, "price_location_score")
        return lead is not None and lead >= 0.20 and loc is not None and loc >= 75

    def sponsored(e):
        s = e.get("v6_sponsorship") or {}
        if isinstance(s, dict):
            return bool(s.get("sponsored"))
        return False

    def retained_or_retest(e):
        return stock_in_play._flag(e.get("breakout_retained")) is True or stock_in_play._flag(e.get("retest_confirmed")) is True

    report = {
        "recent_range_all": _v6_variant_report(recent, lambda _e: True),
        "recent_range_long": _v6_variant_report(long, lambda _e: True),
        "recent_range_short_research": _v6_variant_report(short, lambda _e: True),
        "long_high_turnover": _v6_variant_report(long, high_turnover),
        "long_catalyst": _v6_variant_report(long, catalyst),
        "long_leadership_location": _v6_variant_report(long, leadership_location),
        "long_sponsored": _v6_variant_report(long, sponsored),
        "long_retained_or_retest": _v6_variant_report(long, retained_or_retest),
        "long_full_v6": _v6_variant_report(
            long,
            lambda e: high_turnover(e) and catalyst(e) and leadership_location(e)
                      and sponsored(e) and retained_or_retest(e),
        ),
    }
    report["path_exit_lab"] = _v6_path_exit_report(long)
    return report


def v91_goal_report(events, run_context=None, *, reveal_bear_final=False):
    """V9.1 goal-focused report: one new Bull research play + frozen Bear final."""
    from . import v9_playbooks, v91_goal

    rows = _ensure_v8_event_scores(events)

    bull = []
    for row in rows:
        if not row.get("v91_accumulation_probe"):
            continue
        try:
            now = pd.Timestamp(row.get("signal_time") or row.get("entry_time")).to_pydatetime()
        except Exception:
            now = None
        for play in v9_playbooks.evaluate_row(row, now=now):
            if play.get("playbook") != v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION:
                continue
            if play.get("state") != "TRADE CANDIDATE":
                continue
            item = dict(row)
            item["v9_playbook"] = play.get("playbook")
            item["v9_score"] = play.get("score")
            item["v9_reasons"] = play.get("reasons") or []
            bull.append(item)

    bull_report = {
        "historical_status": "BACKTESTABLE",
        "trade_count": len(bull),
        "30m": _v9_three_way(bull, "intraday_returns", "30m"),
        "1h": _v9_three_way(bull, "intraday_returns", "1h"),
        "2h": _v9_three_way(bull, "intraday_returns", "2h"),
        "eod": _v9_three_way(bull, "intraday_returns", "eod"),
        "1D": _v9_three_way(bull, "swing_returns", "1D"),
        "2D": _v9_three_way(bull, "swing_returns", "2D"),
        "benchmark_2h": _v8_benchmark(bull, "intraday_returns", "2h"),
        "benchmark_1D": _v8_benchmark(bull, "swing_returns", "1D"),
    }

    bear_final = v91_goal.bear_fsb_final_report(
        rows, run_context or {}, reveal_final=bool(reveal_bear_final)
    )
    bear_candidates = v91_goal.select_frozen_bear_fsb(rows)
    bear_1d = _v9_three_way(bear_candidates, "swing_returns", "1D")
    bear_2h = _v9_three_way(bear_candidates, "intraday_returns", "2h")
    val = bear_1d.get("validation") or {}
    blocks = bear_1d.get("validation_blocks") or []
    positive_blocks = sum(1 for b in blocks if b.get("positive"))
    pf = val.get("profit_factor")
    validation_qualified = bool(
        int(val.get("trade_count") or 0) >= 80
        and val.get("avg_return_pct") is not None and float(val["avg_return_pct"]) >= 0.18
        and pf is not None and (pf == float("inf") or float(pf) >= 1.25)
        and len(blocks) == 4 and positive_blocks >= 3
    )
    bear_report = {
        "historical_status": "FROZEN_FINAL_CANDIDATE",
        "trade_count": len(bear_candidates),
        "2h": bear_2h,
        "1D": bear_1d,
        "validation_status": (
            "VALIDATION QUALIFIED — FINAL TEST LOCKED"
            if validation_qualified and not reveal_bear_final
            else ("FINAL TEST RUN" if reveal_bear_final else "RESEARCH")
        ),
        "validation_qualified": validation_qualified,
    }

    return {
        "build_id": v91_goal.BUILD_ID,
        "protocol": {
            "setup_timeframe": "15minute",
            "days": 180,
            "final_20_locked_for_bull": True,
            "bear_rule_fingerprint": v91_goal.frozen_bear_fsb_spec()["fingerprint"],
            "bear_final_revealed": bool(reveal_bear_final),
        },
        "bull_institutional_accumulation": bull_report,
        "bull_catalyst_continuation": {
            "historical_status": "LIVE_SHADOW",
            "message": "Real point-in-time catalyst/news history is unavailable; V9.1 keeps this live/shadow only.",
        },
        "bear_fresh_short_buildup": bear_report,
        "bear_final": bear_final,
        "retired_playbooks": [
            "Bull Opening Drive", "Bull Pullback/Reclaim",
            "Bear Failed Breakout", "Bear VWAP Retest Failure",
        ],
    }
