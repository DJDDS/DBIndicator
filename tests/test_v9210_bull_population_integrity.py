import datetime as dt
from pathlib import Path

import pytest

from app import early_research, v91_goal


def _qualified_seed(i=0, **overrides):
    day = 1 + i
    row = {
        "symbol": f"BULL{i:02d}",
        "signal_time": f"2026-08-{day:02d}T10:00:00+05:30",
        "entry_time": f"2026-08-{day:02d}T10:15:00+05:30",
        "direction": "Bullish",
        "v8_direction": "Bullish",
        "v92_accumulation_seed": True,
        # Deliberately false: V9.2 broad seeds that survive the exact funnel
        # must not disappear merely because the older V9.1 pre-gate flag is off.
        "v91_accumulation_probe": False,
        "price_chg_60m_pct": 1.0,
        "oi_chg_60m_pct": 3.0,
        "bull_vwap_available": True,
        "bull_above_vwap": True,
        "vwap_side_agrees": True,
        "tod_rvol": 1.4,
        "v8_participation": 80.0,
        "v8_relative": 78.0,
        "v8_derivatives": 76.0,
        "v8_alpha": 80.0,
        "close_position_pct": 75.0,
        "basis_acceleration": 0.0,
        "intraday_returns": {"30m": 0.1, "1h": 0.2, "2h": 0.3, "eod": 0.4},
        "swing_returns": {"1D": 0.5, "2D": 0.6},
    }
    row.update(overrides)
    return row


def test_v9210_funnel_qualified_population_is_the_bull_backtest_population():
    rows = [_qualified_seed(i) for i in range(10)]
    report = early_research.v91_goal_report(rows, run_context={"history_coverage": {}})

    funnel = report["bull_gate_funnel"]
    bull = report["bull_institutional_accumulation"]

    assert funnel["qualified"] == 10
    assert len(funnel["qualified_event_keys"]) == 10
    assert bull["trade_count"] == 10
    assert bull["population_integrity"]["status"] == "OK"
    # 60/20/20 chronological split => 2 validation events out of 10.
    assert bull["2h"]["validation"]["trade_count"] == 2
    assert bull["1D"]["validation"]["trade_count"] == 2


def test_v9210_population_integrity_mismatch_raises_data_logic_error():
    funnel = {"qualified": 3, "qualified_event_keys": ["a", "b", "c"]}
    with pytest.raises(RuntimeError, match="DATA/LOGIC ERROR"):
        v91_goal.assert_bull_population_integrity(funnel, [{"symbol": "ONLY_ONE"}])


def test_v9210_v92_report_exposes_history_coverage_inside_goal_report():
    coverage = {
        "price_bars": 1000,
        "oi_bars": 400,
        "oi_bar_coverage_pct": 40.0,
        "symbols_with_oi": 150,
        "symbols_measured": 210,
        "price_first_timestamp": "2026-03-01T09:15:00+05:30",
        "price_last_timestamp": "2026-08-31T15:30:00+05:30",
        "oi_first_timestamp": "2026-06-01T09:15:00+05:30",
        "oi_last_timestamp": "2026-08-31T15:30:00+05:30",
        "note": "coverage note",
    }
    report = early_research.v91_goal_report([], run_context={"history_coverage": coverage})
    assert report["history_coverage"] == coverage


def test_v9210_backtest_ui_keeps_v92_history_coverage_visible_in_fast_mode():
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert 'id="er-v91-history-coverage"' in text
    assert "const v91Coverage = ((r.v91_goal || {}).history_coverage || hc);" in text
    assert "Bull population integrity" in text
    assert "DATA/LOGIC ERROR" in text

def test_v9210_651_funnel_survivors_cannot_collapse_to_zero_validation():
    start = dt.datetime(2026, 1, 1, 9, 15, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    rows = []
    for i in range(651):
        signal = start + dt.timedelta(minutes=15 * i)
        row = _qualified_seed(0)
        row["symbol"] = f"S{i:03d}"
        row["signal_time"] = signal.isoformat()
        row["entry_time"] = (signal + dt.timedelta(minutes=15)).isoformat()
        rows.append(row)

    report = early_research.v91_goal_report(rows, run_context={"history_coverage": {}})
    bull = report["bull_institutional_accumulation"]
    assert report["bull_gate_funnel"]["qualified"] == 651
    assert bull["trade_count"] == 651
    assert bull["2h"]["validation"]["trade_count"] == 130
    assert bull["1D"]["validation"]["trade_count"] == 130
