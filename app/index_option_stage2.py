"""Stage-2 breakout confirmation research for Index Option Buying V1.

This stage keeps the Stage-1 opening-range/trigger grid intact and changes only the
confirmation/entry mechanism. It remains UNDERLYING-only research; no option contract,
order placement, or live recommendation is produced.

Confirmation modes are deliberately mechanical:
- immediate: Stage-1 completed trigger close outside the buffered OR.
- double_close: two consecutive completed trigger bars close beyond the same boundary.
- acceptance_5m: after the initial trigger, five consecutive completed 1-minute closes
  remain beyond the breakout boundary.
- retest_10m: after the initial trigger, price retests the OR boundary within 10 minutes
  and then a completed 1-minute close re-establishes beyond the buffered trigger.

The U.S.-Iran war regime beginning 2026-02-28 is recorded as EXOGENOUS DIAGNOSTIC
metadata only. It is never used to choose a signal or tune a threshold.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from .index_option_research import (
    DEFAULT_ENTRY_DEADLINE,
    SESSION_CLOSE,
    SESSION_OPEN,
    TimingSpec,
    _at_clock,
    _complete_minute_block,
    _forward_metrics,
    _trigger_bars,
    _validate_1m_ohlc,
)


CONFIRMATION_MODES = ("immediate", "double_close", "acceptance_5m", "retest_10m")
WAR_START = dt.date(2026, 2, 28)


@dataclass(frozen=True)
class Stage2Spec:
    opening_range_minutes: int
    trigger_minutes: int
    confirmation: str
    buffer_pct: float = 0.0004
    entry_deadline: dt.time = DEFAULT_ENTRY_DEADLINE

    def __post_init__(self):
        if self.confirmation not in CONFIRMATION_MODES:
            raise ValueError(f"confirmation must be one of {CONFIRMATION_MODES}")


def geopolitical_regime(session_date: dt.date) -> str:
    """Exogenous diagnostic label; never a trading filter in Stage 2."""
    if session_date < dt.date(2026, 1, 1):
        return "historical_pre_2026"
    if session_date < WAR_START:
        return "2026_pre_us_iran_war"
    return "2026_us_iran_war"


def _beyond(direction: str, close: float, long_trigger: float, short_trigger: float) -> bool:
    return close >= long_trigger if direction == "Bullish" else close <= short_trigger


def _confirm_double_close(
    trigger_bars: list[dict],
    first_index: int,
    *,
    direction: str,
    long_trigger: float,
    short_trigger: float,
):
    if first_index + 1 >= len(trigger_bars):
        return None
    second = trigger_bars[first_index + 1]
    if _beyond(direction, float(second["close"]), long_trigger, short_trigger):
        return second
    return None


def _confirm_acceptance_5m(
    session: pd.DataFrame,
    first_bar: dict,
    *,
    direction: str,
    long_trigger: float,
    short_trigger: float,
    deadline: pd.Timestamp,
):
    start = first_bar["end"]
    end = start + pd.Timedelta(minutes=5)
    if end > deadline:
        return None
    block = session[(session.index >= start) & (session.index < end)]
    if not _complete_minute_block(block, start, 5):
        return None
    closes = block["close"].astype(float)
    if direction == "Bullish":
        ok = bool((closes >= long_trigger).all())
    else:
        ok = bool((closes <= short_trigger).all())
    if not ok:
        return None
    return {
        "start": start,
        "end": end,
        "open": float(block["open"].iloc[0]),
        "high": float(block["high"].max()),
        "low": float(block["low"].min()),
        "close": float(block["close"].iloc[-1]),
    }


def _confirm_retest_10m(
    session: pd.DataFrame,
    first_bar: dict,
    *,
    direction: str,
    range_high: float,
    range_low: float,
    long_trigger: float,
    short_trigger: float,
    deadline: pd.Timestamp,
):
    start = first_bar["end"]
    end = min(start + pd.Timedelta(minutes=10), deadline)
    block = session[(session.index >= start) & (session.index < end)]
    if block.empty:
        return None

    retested = False
    for ts, row in block.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        if direction == "Bullish":
            if low <= range_high:
                retested = True
            if retested and close >= long_trigger:
                return {
                    "start": ts,
                    "end": ts + pd.Timedelta(minutes=1),
                    "open": float(row["open"]),
                    "high": high,
                    "low": low,
                    "close": close,
                }
        else:
            if high >= range_low:
                retested = True
            if retested and close <= short_trigger:
                return {
                    "start": ts,
                    "end": ts + pd.Timedelta(minutes=1),
                    "open": float(row["open"]),
                    "high": high,
                    "low": low,
                    "close": close,
                }
    return None


def analyze_session_confirmation(
    session_1m: pd.DataFrame,
    spec: Stage2Spec,
    *,
    range_pct_bounds: tuple[float, float] | None = None,
) -> dict | None:
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
    if width <= 0 or session_open <= 0:
        return None

    range_pct = width / session_open
    if range_pct_bounds is not None:
        lo, hi = range_pct_bounds
        if range_pct < lo or range_pct > hi:
            return None

    buffer_points = session_open * spec.buffer_pct
    long_trigger = range_high + buffer_points
    short_trigger = range_low - buffer_points

    trigger_bars = list(
        _trigger_bars(
            session,
            start=range_end,
            deadline=deadline,
            trigger_minutes=spec.trigger_minutes,
        )
    )

    for i, bar in enumerate(trigger_bars):
        direction = None
        if float(bar["close"]) >= long_trigger:
            direction = "Bullish"
        elif float(bar["close"]) <= short_trigger:
            direction = "Bearish"
        if direction is None:
            continue

        confirmed = None
        if spec.confirmation == "immediate":
            confirmed = bar
        elif spec.confirmation == "double_close":
            confirmed = _confirm_double_close(
                trigger_bars,
                i,
                direction=direction,
                long_trigger=long_trigger,
                short_trigger=short_trigger,
            )
        elif spec.confirmation == "acceptance_5m":
            confirmed = _confirm_acceptance_5m(
                session,
                bar,
                direction=direction,
                long_trigger=long_trigger,
                short_trigger=short_trigger,
                deadline=deadline,
            )
        elif spec.confirmation == "retest_10m":
            confirmed = _confirm_retest_10m(
                session,
                bar,
                direction=direction,
                range_high=range_high,
                range_low=range_low,
                long_trigger=long_trigger,
                short_trigger=short_trigger,
                deadline=deadline,
            )

        if confirmed is None:
            continue

        signal_time = confirmed["end"]
        if signal_time > deadline:
            continue

        signal_close = float(confirmed["close"])
        result = {
            "session": session_start.date().isoformat(),
            "geopolitical_regime": geopolitical_regime(session_start.date()),
            "opening_range_minutes": spec.opening_range_minutes,
            "trigger_minutes": spec.trigger_minutes,
            "confirmation": spec.confirmation,
            "direction": direction,
            "range_high": range_high,
            "range_low": range_low,
            "range_width": width,
            "range_pct": range_pct,
            "session_open": session_open,
            "buffer_points": buffer_points,
            "trigger_level": long_trigger if direction == "Bullish" else short_trigger,
            "initial_break_time": bar["end"],
            "signal_time": signal_time,
            "confirmation_delay_min": int((signal_time - bar["end"]).total_seconds() // 60),
            "signal_close": signal_close,
        }
        result.update(
            _forward_metrics(
                session,
                signal_time=signal_time,
                signal_close=signal_close,
                direction=direction,
                range_width=width,
            )
        )
        return result

    return None


def evaluate_confirmation_grid(
    bars_1m: pd.DataFrame,
    *,
    opening_ranges=(15, 30, 45, 60, 90, 120),
    trigger_windows=(1, 3, 5),
    confirmations=CONFIRMATION_MODES,
    buffer_pct: float = 0.0004,
    entry_deadline: dt.time = DEFAULT_ENTRY_DEADLINE,
    range_pct_bounds: tuple[float, float] | None = None,
) -> pd.DataFrame:
    bars = _validate_1m_ohlc(bars_1m)
    if bars.empty:
        return pd.DataFrame()

    records = []
    for _, session in bars.groupby(bars.index.normalize(), sort=True):
        for opening_range in opening_ranges:
            for trigger_window in trigger_windows:
                for confirmation in confirmations:
                    row = analyze_session_confirmation(
                        session,
                        Stage2Spec(
                            opening_range_minutes=int(opening_range),
                            trigger_minutes=int(trigger_window),
                            confirmation=str(confirmation),
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
        ["session", "opening_range_minutes", "trigger_minutes", "confirmation"]
    ).reset_index(drop=True)


def summarize_confirmation_grid(trades: pd.DataFrame, *, horizon_minutes: int = 120) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()
    ret_col = f"return_{horizon_minutes}m_r"
    mfe_col = f"mfe_{horizon_minutes}m_r"
    mae_col = f"mae_{horizon_minutes}m_r"

    rows = []
    grouped = trades.groupby(
        ["opening_range_minutes", "trigger_minutes", "confirmation"], sort=True
    )
    for (opening_range, trigger_window, confirmation), group in grouped:
        usable = group.dropna(subset=[ret_col])
        returns = usable[ret_col].astype(float)
        mfe = usable[mfe_col].dropna().astype(float)
        mae = usable[mae_col].dropna().astype(float)
        rows.append({
            "opening_range_minutes": int(opening_range),
            "trigger_minutes": int(trigger_window),
            "confirmation": confirmation,
            "trade_count": int(len(usable)),
            "win_rate_pct": float((returns > 0).mean() * 100.0) if len(returns) else None,
            "mean_return_r": float(returns.mean()) if len(returns) else None,
            "median_return_r": float(returns.median()) if len(returns) else None,
            "median_mfe_r": float(mfe.median()) if len(mfe) else None,
            "median_mae_r": float(mae.median()) if len(mae) else None,
            "mean_confirmation_delay_min": float(usable["confirmation_delay_min"].mean())
            if len(usable)
            else None,
        })
    return pd.DataFrame(rows).sort_values(
        ["opening_range_minutes", "trigger_minutes", "confirmation"]
    ).reset_index(drop=True)


def summarize_by_geopolitical_regime(
    trades: pd.DataFrame, *, horizon_minutes: int = 120
) -> pd.DataFrame:
    """Diagnostic only; never used by the signal generator."""
    if trades is None or trades.empty:
        return pd.DataFrame()
    ret_col = f"return_{horizon_minutes}m_r"
    rows = []
    grouped = trades.groupby(
        ["geopolitical_regime", "confirmation"], sort=True
    )
    for (regime, confirmation), group in grouped:
        usable = group.dropna(subset=[ret_col])
        r = usable[ret_col].astype(float)
        rows.append({
            "geopolitical_regime": regime,
            "confirmation": confirmation,
            "trade_count": int(len(usable)),
            "win_rate_pct": float((r > 0).mean() * 100.0) if len(r) else None,
            "mean_return_r": float(r.mean()) if len(r) else None,
            "median_return_r": float(r.median()) if len(r) else None,
        })
    return pd.DataFrame(rows).reset_index(drop=True)
