import datetime as dt

import pandas as pd

from app.index_option_stage2 import (
    CONFIRMATION_MODES,
    Stage2Spec,
    analyze_session_confirmation,
    evaluate_confirmation_grid,
    geopolitical_regime,
    summarize_by_geopolitical_regime,
)


def _base_session(day="2026-03-02"):
    idx = pd.date_range(f"{day} 09:15", periods=375, freq="min", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.2,
            "low": 99.8,
            "close": 100.0,
        },
        index=idx,
    )
    return frame


def test_geopolitical_regime_is_exogenous_and_uses_feb_28_boundary():
    assert geopolitical_regime(dt.date(2025, 12, 31)) == "historical_pre_2026"
    assert geopolitical_regime(dt.date(2026, 2, 27)) == "2026_pre_us_iran_war"
    assert geopolitical_regime(dt.date(2026, 2, 28)) == "2026_us_iran_war"
    assert geopolitical_regime(dt.date(2026, 3, 1)) == "2026_us_iran_war"


def test_immediate_confirmation_matches_first_completed_break():
    frame = _base_session()
    ts = pd.Timestamp("2026-03-02 09:30", tz="Asia/Kolkata")
    frame.loc[ts, ["open", "high", "low", "close"]] = [100.1, 100.5, 100.0, 100.4]

    out = analyze_session_confirmation(
        frame,
        Stage2Spec(15, 1, "immediate", buffer_pct=0.0),
    )
    assert out is not None
    assert out["signal_time"] == pd.Timestamp("2026-03-02 09:31", tz="Asia/Kolkata")
    assert out["confirmation_delay_min"] == 0
    assert out["geopolitical_regime"] == "2026_us_iran_war"


def test_double_close_rejects_single_bar_false_break():
    frame = _base_session()
    t0 = pd.Timestamp("2026-03-02 09:30", tz="Asia/Kolkata")
    t1 = pd.Timestamp("2026-03-02 09:31", tz="Asia/Kolkata")
    frame.loc[t0, ["open", "high", "low", "close"]] = [100.0, 100.5, 99.95, 100.4]
    frame.loc[t1, ["open", "high", "low", "close"]] = [100.4, 100.45, 99.9, 100.0]

    immediate = analyze_session_confirmation(
        frame, Stage2Spec(15, 1, "immediate", buffer_pct=0.0)
    )
    double = analyze_session_confirmation(
        frame, Stage2Spec(15, 1, "double_close", buffer_pct=0.0)
    )
    assert immediate is not None
    assert double is None


def test_acceptance_5m_requires_five_consecutive_closes_beyond_boundary():
    frame = _base_session()
    breakout = pd.Timestamp("2026-03-02 09:30", tz="Asia/Kolkata")
    frame.loc[breakout, ["open", "high", "low", "close"]] = [100.0, 100.5, 100.0, 100.4]
    for minute in range(31, 36):
        ts = pd.Timestamp(f"2026-03-02 09:{minute}", tz="Asia/Kolkata")
        frame.loc[ts, ["open", "high", "low", "close"]] = [100.4, 100.6, 100.3, 100.45]

    out = analyze_session_confirmation(
        frame, Stage2Spec(15, 1, "acceptance_5m", buffer_pct=0.0)
    )
    assert out is not None
    assert out["confirmation_delay_min"] == 5
    assert out["signal_time"] == pd.Timestamp("2026-03-02 09:36", tz="Asia/Kolkata")


def test_retest_10m_requires_distinct_touch_then_later_reclaim():
    frame = _base_session()
    t0 = pd.Timestamp("2026-03-02 09:30", tz="Asia/Kolkata")
    t1 = pd.Timestamp("2026-03-02 09:31", tz="Asia/Kolkata")
    t2 = pd.Timestamp("2026-03-02 09:32", tz="Asia/Kolkata")
    frame.loc[t0, ["open", "high", "low", "close"]] = [100.0, 100.5, 100.0, 100.4]
    frame.loc[t1, ["open", "high", "low", "close"]] = [100.4, 100.45, 100.15, 100.18]
    frame.loc[t2, ["open", "high", "low", "close"]] = [100.2, 100.55, 100.1, 100.45]

    out = analyze_session_confirmation(
        frame, Stage2Spec(15, 1, "retest_10m", buffer_pct=0.0)
    )
    assert out is not None
    assert out["signal_time"] == pd.Timestamp("2026-03-02 09:33", tz="Asia/Kolkata")
    assert out["confirmation_delay_min"] == 2


def test_grid_covers_all_18_timing_cells_for_non_retest_confirmations():
    frame = _base_session()
    ts = pd.Timestamp("2026-03-02 11:20", tz="Asia/Kolkata")
    frame.loc[frame.index >= ts, ["open", "high", "low", "close"]] = [100.4, 100.6, 100.3, 100.45]

    trades = evaluate_confirmation_grid(
        frame,
        confirmations=("immediate", "double_close", "acceptance_5m"),
        buffer_pct=0.0,
    )
    assert set(trades["confirmation"]) == {"immediate", "double_close", "acceptance_5m"}
    assert trades[["opening_range_minutes", "trigger_minutes"]].drop_duplicates().shape[0] == 18


def test_retest_mode_is_available_in_grid_when_path_retests():
    frame = _base_session()
    t0 = pd.Timestamp("2026-03-02 11:20", tz="Asia/Kolkata")
    t1 = pd.Timestamp("2026-03-02 11:21", tz="Asia/Kolkata")
    t2 = pd.Timestamp("2026-03-02 11:22", tz="Asia/Kolkata")
    frame.loc[t0, ["open", "high", "low", "close"]] = [100.0, 100.5, 100.25, 100.45]
    frame.loc[t1, ["open", "high", "low", "close"]] = [100.4, 100.42, 100.15, 100.18]
    frame.loc[t2, ["open", "high", "low", "close"]] = [100.2, 100.55, 100.1, 100.45]

    trades = evaluate_confirmation_grid(
        frame,
        opening_ranges=(15,),
        trigger_windows=(1,),
        confirmations=("retest_10m",),
        buffer_pct=0.0,
    )
    assert len(trades) == 1
    assert trades.iloc[0]["confirmation"] == "retest_10m"


def test_geopolitical_summary_is_diagnostic_not_a_ranking():
    frame1 = _base_session("2026-02-27")
    frame2 = _base_session("2026-03-02")
    for frame in (frame1, frame2):
        ts = frame.index[15]
        frame.loc[frame.index >= ts, ["open", "high", "low", "close"]] = [100.4, 100.6, 100.3, 100.45]
    bars = pd.concat([frame1, frame2]).sort_index()

    trades = evaluate_confirmation_grid(
        bars,
        opening_ranges=(15,),
        trigger_windows=(1,),
        confirmations=("immediate",),
        buffer_pct=0.0,
    )
    summary = summarize_by_geopolitical_regime(trades, horizon_minutes=120)
    assert set(summary["geopolitical_regime"]) == {
        "2026_pre_us_iran_war",
        "2026_us_iran_war",
    }
    assert "rank" not in summary.columns
    assert "winner" not in summary.columns
