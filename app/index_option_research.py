"""Stage-1 intraday timing research for index option buying.

This module deliberately studies the UNDERLYING index only. It does not choose an
option contract, use V12/V12.1 forward recorder data, or generate a live trade.
The purpose is to answer one narrow question without parameter leakage:

    Which opening-range duration and minute trigger clock produce the most robust
    post-break directional follow-through?

Input bars are expected to be 1-minute OHLC data with timestamps at bar START.
All decisions use completed bars only. The opening range excludes the trigger
bar, and forward-path metrics begin with the first 1-minute bar after the signal
close, so the implementation is explicitly no-lookahead.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


OR_WINDOWS_MIN = (15, 30, 45, 60, 90, 120)
TRIGGER_WINDOWS_MIN = (1, 3, 5)
FORWARD_HORIZONS_MIN = (5, 10, 15, 30, 60, 90, 120)

SESSION_OPEN = dt.time(9, 15)
DEFAULT_ENTRY_DEADLINE = dt.time(13, 0)
SESSION_CLOSE = dt.time(15, 30)


@dataclass(frozen=True)
class TimingSpec:
    opening_range_minutes: int
    trigger_minutes: int
    buffer_pct: float = 0.0004
    entry_deadline: dt.time = DEFAULT_ENTRY_DEADLINE

    def __post_init__(self):
        if self.opening_range_minutes <= 0:
            raise ValueError("opening_range_minutes must be positive")
        if self.trigger_minutes <= 0:
            raise ValueError("trigger_minutes must be positive")
        if self.buffer_pct < 0:
            raise ValueError("buffer_pct cannot be negative")


def _validate_1m_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    required = {"open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {sorted(missing)}")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError("1-minute bars must use a DatetimeIndex")
    out = frame.sort_index().copy()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _at_clock(ts: pd.Timestamp, clock: dt.time) -> pd.Timestamp:
    base = pd.Timestamp(ts).normalize()
    return base + pd.Timedelta(hours=clock.hour, minutes=clock.minute, seconds=clock.second)


def _expected_minute_index(start: pd.Timestamp, periods: int) -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=periods, freq="min")


def _complete_minute_block(block: pd.DataFrame, start: pd.Timestamp, minutes: int) -> bool:
    if len(block) != minutes:
        return False
    expected = _expected_minute_index(start, minutes)
    return block.index.equals(expected)


def _trigger_bars(
    session: pd.DataFrame,
    *,
    start: pd.Timestamp,
    deadline: pd.Timestamp,
    trigger_minutes: int,
):
    """Yield completed m-minute trigger bars aligned to the end of the OR window."""
    eligible = session[(session.index >= start) & (session.index < deadline)]
    if eligible.empty:
        return
    offsets = ((eligible.index - start).total_seconds() // 60).astype(int)
    bucket_ids = offsets // int(trigger_minutes)
    for bucket_id in sorted(set(int(x) for x in bucket_ids)):
        block_start = start + pd.Timedelta(minutes=bucket_id * trigger_minutes)
        block_end = block_start + pd.Timedelta(minutes=trigger_minutes)
        if block_end > deadline:
            continue
        block = eligible[bucket_ids == bucket_id]
        if not _complete_minute_block(block, block_start, trigger_minutes):
            continue
        yield {
            "start": block_start,
            "end": block_end,
            "open": float(block["open"].iloc[0]),
            "high": float(block["high"].max()),
            "low": float(block["low"].min()),
            "close": float(block["close"].iloc[-1]),
        }


def _directional_move(direction: str, reference: float, value: float) -> float:
    return (value - reference) if direction == "Bullish" else (reference - value)


def _forward_metrics(
    session: pd.DataFrame,
    *,
    signal_time: pd.Timestamp,
    signal_close: float,
    direction: str,
    range_width: float,
) -> dict:
    session_close = _at_clock(signal_time, SESSION_CLOSE)
    post = session[(session.index >= signal_time) & (session.index < session_close)]
    out: dict[str, float | int | None] = {}

    for horizon in FORWARD_HORIZONS_MIN:
        end = signal_time + pd.Timedelta(minutes=horizon)
        window = post[post.index < end]
        key = f"{horizon}m"
        if window.empty:
            out[f"return_{key}_r"] = None
            out[f"mfe_{key}_r"] = None
            out[f"mae_{key}_r"] = None
            continue
        last_close = float(window["close"].iloc[-1])
        if direction == "Bullish":
            favorable = float(window["high"].max()) - signal_close
            adverse = signal_close - float(window["low"].min())
        else:
            favorable = signal_close - float(window["low"].min())
            adverse = float(window["high"].max()) - signal_close
        out[f"return_{key}_r"] = _directional_move(direction, signal_close, last_close) / range_width
        out[f"mfe_{key}_r"] = favorable / range_width
        out[f"mae_{key}_r"] = adverse / range_width

    if post.empty:
        out["return_eod_r"] = None
        out["mfe_eod_r"] = None
        out["mae_eod_r"] = None
        out["time_to_0_5r_min"] = None
        out["time_to_1_0r_min"] = None
        return out

    last_close = float(post["close"].iloc[-1])
    if direction == "Bullish":
        favorable_path = post["high"].astype(float) - signal_close
        adverse_path = signal_close - post["low"].astype(float)
    else:
        favorable_path = signal_close - post["low"].astype(float)
        adverse_path = post["high"].astype(float) - signal_close
    out["return_eod_r"] = _directional_move(direction, signal_close, last_close) / range_width
    out["mfe_eod_r"] = float(favorable_path.max()) / range_width
    out["mae_eod_r"] = float(adverse_path.max()) / range_width

    for threshold, name in ((0.5, "time_to_0_5r_min"), (1.0, "time_to_1_0r_min")):
        reached = favorable_path[favorable_path >= threshold * range_width]
        if reached.empty:
            out[name] = None
        else:
            # Timestamps are bar starts, so +1 minute is when the observation is complete.
            reached_at = reached.index[0] + pd.Timedelta(minutes=1)
            out[name] = int((reached_at - signal_time).total_seconds() // 60)
    return out


def analyze_session_timing(
    session_1m: pd.DataFrame,
    spec: TimingSpec,
    *,
    range_pct_bounds: tuple[float, float] | None = None,
) -> dict | None:
    """Return the first qualifying directional break for one session/spec.

    range_pct_bounds, when supplied, are decimal fractions of session open.
    Example: (0.0015, 0.0060) reproduces the PDF's 0.15%-0.60% control gate.
    """
    session = _validate_1m_ohlc(session_1m)
    if session.empty:
        return None

    first_ts = session.index[0]
    session_start = _at_clock(first_ts, SESSION_OPEN)
    range_end = session_start + pd.Timedelta(minutes=spec.opening_range_minutes)
    deadline = _at_clock(first_ts, spec.entry_deadline)

    range_bars = session[(session.index >= session_start) & (session.index < range_end)]
    if not _complete_minute_block(range_bars, session_start, spec.opening_range_minutes):
        return None

    session_open = float(range_bars["open"].iloc[0])
    range_high = float(range_bars["high"].max())
    range_low = float(range_bars["low"].min())
    width = range_high - range_low
    if not np.isfinite(width) or width <= 0 or not np.isfinite(session_open) or session_open <= 0:
        return None

    range_pct = width / session_open
    if range_pct_bounds is not None:
        lo, hi = range_pct_bounds
        if range_pct < lo or range_pct > hi:
            return None

    buffer_points = session_open * spec.buffer_pct
    long_trigger = range_high + buffer_points
    short_trigger = range_low - buffer_points

    signal = None
    for bar in _trigger_bars(
        session, start=range_end, deadline=deadline, trigger_minutes=spec.trigger_minutes
    ):
        if bar["close"] >= long_trigger:
            signal = (bar, "Bullish", long_trigger)
            break
        if bar["close"] <= short_trigger:
            signal = (bar, "Bearish", short_trigger)
            break

    if signal is None:
        return None

    bar, direction, trigger_level = signal
    signal_close = float(bar["close"])
    result = {
        "session": session_start.date().isoformat(),
        "opening_range_minutes": spec.opening_range_minutes,
        "trigger_minutes": spec.trigger_minutes,
        "direction": direction,
        "range_high": range_high,
        "range_low": range_low,
        "range_width": width,
        "range_pct": range_pct,
        "session_open": session_open,
        "buffer_points": buffer_points,
        "trigger_level": trigger_level,
        "signal_time": bar["end"],
        "signal_close": signal_close,
    }
    result.update(
        _forward_metrics(
            session,
            signal_time=bar["end"],
            signal_close=signal_close,
            direction=direction,
            range_width=width,
        )
    )
    return result


def evaluate_timing_grid(
    bars_1m: pd.DataFrame,
    *,
    opening_ranges: Iterable[int] = OR_WINDOWS_MIN,
    trigger_windows: Iterable[int] = TRIGGER_WINDOWS_MIN,
    buffer_pct: float = 0.0004,
    entry_deadline: dt.time = DEFAULT_ENTRY_DEADLINE,
    range_pct_bounds: tuple[float, float] | None = None,
) -> pd.DataFrame:
    """Evaluate all timing pairs independently, one first-break signal per session."""
    bars = _validate_1m_ohlc(bars_1m)
    if bars.empty:
        return pd.DataFrame()

    records: list[dict] = []
    for _, session in bars.groupby(bars.index.normalize(), sort=True):
        for opening_range in opening_ranges:
            for trigger_window in trigger_windows:
                row = analyze_session_timing(
                    session,
                    TimingSpec(
                        opening_range_minutes=int(opening_range),
                        trigger_minutes=int(trigger_window),
                        buffer_pct=float(buffer_pct),
                        entry_deadline=entry_deadline,
                    ),
                    range_pct_bounds=range_pct_bounds,
                )
                if row is not None:
                    records.append(row)
    if not records:
        return pd.DataFrame()
    return pd.DataFrame.from_records(records).sort_values(
        ["session", "opening_range_minutes", "trigger_minutes"]
    ).reset_index(drop=True)


def summarize_timing_grid(trades: pd.DataFrame, *, horizon_minutes: int = 60) -> pd.DataFrame:
    """Descriptive timing summary. It intentionally does NOT pick or rank a winner."""
    if trades is None or trades.empty:
        return pd.DataFrame()
    if horizon_minutes not in FORWARD_HORIZONS_MIN:
        raise ValueError(f"horizon_minutes must be one of {FORWARD_HORIZONS_MIN}")

    ret_col = f"return_{horizon_minutes}m_r"
    mfe_col = f"mfe_{horizon_minutes}m_r"
    mae_col = f"mae_{horizon_minutes}m_r"
    required = {"opening_range_minutes", "trigger_minutes", ret_col, mfe_col, mae_col}
    missing = required.difference(trades.columns)
    if missing:
        raise ValueError(f"trades missing columns: {sorted(missing)}")

    rows = []
    grouped = trades.groupby(["opening_range_minutes", "trigger_minutes"], sort=True)
    for (opening_range, trigger_window), group in grouped:
        usable = group.dropna(subset=[ret_col])
        returns = usable[ret_col].astype(float)
        mfe = usable[mfe_col].dropna().astype(float)
        mae = usable[mae_col].dropna().astype(float)
        rows.append({
            "opening_range_minutes": int(opening_range),
            "trigger_minutes": int(trigger_window),
            "trade_count": int(len(usable)),
            "win_rate_pct": float((returns > 0).mean() * 100.0) if len(returns) else None,
            "mean_return_r": float(returns.mean()) if len(returns) else None,
            "median_return_r": float(returns.median()) if len(returns) else None,
            "median_mfe_r": float(mfe.median()) if len(mfe) else None,
            "median_mae_r": float(mae.median()) if len(mae) else None,
            "p75_mfe_r": float(mfe.quantile(0.75)) if len(mfe) else None,
            "p75_mae_r": float(mae.quantile(0.75)) if len(mae) else None,
        })
    return pd.DataFrame(rows).sort_values(
        ["opening_range_minutes", "trigger_minutes"]
    ).reset_index(drop=True)
