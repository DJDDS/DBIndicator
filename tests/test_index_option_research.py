import pandas as pd
import pytest

from app.index_option_research import (
    OR_WINDOWS_MIN,
    TRIGGER_WINDOWS_MIN,
    TimingSpec,
    analyze_session_timing,
    evaluate_timing_grid,
    summarize_timing_grid,
)


def _session(breakout_at="11:20", breakout_close=100.50):
    idx = pd.date_range("2026-09-01 09:15", periods=375, freq="min", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {
            "open": 100.00,
            "high": 100.20,
            "low": 99.80,
            "close": 100.00,
        },
        index=idx,
    )
    ts = pd.Timestamp(f"2026-09-01 {breakout_at}", tz="Asia/Kolkata")
    frame.loc[frame.index >= ts, "open"] = breakout_close
    frame.loc[frame.index >= ts, "close"] = breakout_close
    frame.loc[frame.index >= ts, "high"] = breakout_close + 0.10
    frame.loc[frame.index >= ts, "low"] = breakout_close - 0.10
    return frame


def test_stage1_grid_contains_all_18_timing_families_when_all_trigger():
    trades = evaluate_timing_grid(_session(), buffer_pct=0.0)
    assert len(trades) == len(OR_WINDOWS_MIN) * len(TRIGGER_WINDOWS_MIN) == 18
    assert set(trades["opening_range_minutes"]) == set(OR_WINDOWS_MIN)
    assert set(trades["trigger_minutes"]) == set(TRIGGER_WINDOWS_MIN)


def test_one_minute_trigger_can_fire_while_three_minute_close_rejects():
    frame = _session(breakout_at="12:59", breakout_close=100.0)
    # 15-minute OR ends 09:30. A one-minute close escapes, but the aligned
    # three-minute bar closes back inside the range.
    for stamp, close, high in (
        ("09:30", 100.30, 100.35),
        ("09:31", 100.05, 100.15),
        ("09:32", 100.00, 100.10),
    ):
        ts = pd.Timestamp(f"2026-09-01 {stamp}", tz="Asia/Kolkata")
        frame.loc[ts, ["open", "close", "high", "low"]] = [100.0, close, high, 99.90]

    one = analyze_session_timing(frame, TimingSpec(15, 1, buffer_pct=0.0))
    three = analyze_session_timing(frame, TimingSpec(15, 3, buffer_pct=0.0))

    assert one is not None
    assert one["direction"] == "Bullish"
    assert one["signal_time"] == pd.Timestamp("2026-09-01 09:31", tz="Asia/Kolkata")
    assert three is None


def test_signal_time_does_not_change_when_future_bars_are_mutated():
    frame = _session(breakout_at="10:00", breakout_close=100.45)
    spec = TimingSpec(30, 1, buffer_pct=0.0)
    before = analyze_session_timing(frame, spec)

    changed = frame.copy()
    future = changed.index >= pd.Timestamp("2026-09-01 12:00", tz="Asia/Kolkata")
    changed.loc[future, ["open", "high", "low", "close"]] = [80.0, 81.0, 79.0, 80.0]
    after = analyze_session_timing(changed, spec)

    assert before is not None and after is not None
    assert after["signal_time"] == before["signal_time"]
    assert after["signal_close"] == before["signal_close"]
    assert after["range_high"] == before["range_high"]
    assert after["range_low"] == before["range_low"]


def test_pdf_range_width_gate_is_optional_and_reproducible():
    frame = _session(breakout_at="10:00", breakout_close=100.45)
    spec = TimingSpec(30, 1, buffer_pct=0.0)

    accepted = analyze_session_timing(frame, spec, range_pct_bounds=(0.0015, 0.0060))
    rejected = analyze_session_timing(frame, spec, range_pct_bounds=(0.0041, 0.0060))

    assert accepted is not None
    assert accepted["range_pct"] == pytest.approx(0.004)
    assert rejected is None


def test_forward_metrics_are_measured_after_completed_signal_bar():
    frame = _session(breakout_at="10:00", breakout_close=100.30)
    signal_ts = pd.Timestamp("2026-09-01 10:00", tz="Asia/Kolkata")
    # Signal bar closes at 10:01. From 10:01 onward make a known favorable path.
    frame.loc[signal_ts, ["open", "high", "low", "close"]] = [100.0, 100.35, 99.95, 100.30]
    for minute, close, high, low in (
        ("10:01", 100.35, 100.40, 100.25),
        ("10:02", 100.40, 100.45, 100.30),
        ("10:03", 100.45, 100.50, 100.35),
        ("10:04", 100.50, 100.55, 100.40),
        ("10:05", 100.55, 100.60, 100.45),
    ):
        ts = pd.Timestamp(f"2026-09-01 {minute}", tz="Asia/Kolkata")
        frame.loc[ts, ["open", "high", "low", "close"]] = [close, high, low, close]

    out = analyze_session_timing(frame, TimingSpec(30, 1, buffer_pct=0.0))
    assert out is not None
    assert out["signal_time"] == pd.Timestamp("2026-09-01 10:01", tz="Asia/Kolkata")
    # R = 0.40, and first 5 minutes after the signal reach 100.60 high.
    assert out["mfe_5m_r"] == pytest.approx((100.60 - 100.30) / 0.40)
    assert out["mae_5m_r"] == pytest.approx((100.30 - 100.25) / 0.40)


def test_summary_is_descriptive_and_does_not_rank_or_select_a_winner():
    trades = evaluate_timing_grid(_session(), buffer_pct=0.0)
    summary = summarize_timing_grid(trades, horizon_minutes=60)

    assert len(summary) == 18
    assert "trade_count" in summary
    assert "win_rate_pct" in summary
    assert "median_mfe_r" in summary
    assert "median_mae_r" in summary
    assert "rank" not in summary
    assert "winner" not in summary
