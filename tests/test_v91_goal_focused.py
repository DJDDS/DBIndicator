import datetime as dt

from app import early_research, v9_playbooks
from app import v91_goal


def accumulation_row():
    return {
        "symbol": "ACC",
        "direction": "Bullish",
        "v8_direction": "Bullish",
        "v91_accumulation_probe": True,
        "price_chg_60m_pct": 1.2,
        "oi_chg_60m_pct": 8.0,
        "v8_oi_state": "Long Buildup",
        "v8_participation": 82.0,
        "v8_relative": 84.0,
        "v8_derivatives": 88.0,
        "v8_alpha": 84.0,
        "close_position_pct": 82.0,
        "basis_acceleration": 0.01,
        "vwap_side_agrees": True,
        "tod_rvol": 1.8,
    }


def bear_fsb_row(i=0, ret=0.4):
    return {
        "symbol": f"B{i}",
        "entry_time": f"2026-01-{(i % 28) + 1:02d}T10:{i % 60:02d}:00+05:30",
        "signal_time": f"2026-01-{(i % 28) + 1:02d}T10:{i % 60:02d}:00+05:30",
        "direction": "Bearish",
        "v8_direction": "Bearish",
        "fresh_breakout": True,
        "breakout_extension_atr": 0.4,
        "v8_oi_state": "Fresh Short Buildup",
        "v8_participation": 82.0,
        "v8_relative": 78.0,
        "v8_derivatives": 76.0,
        "v8_alpha": 78.0,
        "close_position_pct": 15.0,
        "basis_acceleration": -0.01,
        "swing_returns": {"1D": ret, "2D": ret},
        "intraday_returns": {"30m": 0.0, "1h": 0.0, "2h": 0.1, "eod": 0.2},
    }


def exact_context():
    return {
        "setup_timeframe": "15minute",
        "execution_timeframe": "15minute",
        "days": 180,
        "cost_pct": 0.08,
        "slippage_pct": 0.05,
        "universe_is_full_fno": True,
    }


def test_v91_bull_accumulation_is_independent_long_buildup_playbook():
    plays = v9_playbooks.evaluate_row(accumulation_row(), now=dt.datetime(2026, 8, 30, 13, 0))
    play = next(p for p in plays if p["playbook"] == v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION)
    assert play["state"] == "TRADE CANDIDATE"
    assert set(play["modes"]) == {"intraday", "swing"}


def test_v91_bull_accumulation_requires_true_long_buildup_and_vwap_acceptance():
    row = accumulation_row()
    row["v8_oi_state"] = "Short Covering"
    assert not any(p["playbook"] == v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION for p in v9_playbooks.evaluate_row(row))
    row = accumulation_row()
    row["vwap_side_agrees"] = False
    assert not any(p["playbook"] == v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION for p in v9_playbooks.evaluate_row(row))


def test_v91_frozen_bear_rule_fingerprint_and_acceptance_are_predeclared():
    spec = v91_goal.frozen_bear_fsb_spec()
    assert spec["rule_id"] == "BEAR_FSB_15M_NEXTBAR_1D_V91"
    assert spec["fingerprint"]
    assert spec["acceptance"]["min_profit_factor"] == 1.25
    assert spec["acceptance"]["min_avg_return_pct"] == 0.15
    assert spec["acceptance"]["required_positive_blocks"] == 3


def test_v91_bear_final_stays_locked_for_protocol_mismatch():
    events = [bear_fsb_row(i, 0.5) for i in range(100)]
    report = v91_goal.bear_fsb_final_report(events, {**exact_context(), "days": 90})
    assert report["final_test"]["locked"] is True
    assert report["verdict"]["verdict"] == "NOT_RUN"


def test_v91_bear_final_reveals_only_exact_frozen_rule_on_exact_protocol():
    events = [bear_fsb_row(i, 0.5 if i % 5 else -0.1) for i in range(100)]
    # noise that must not enter the frozen final population
    bad = bear_fsb_row(101, 9.0)
    bad["v8_oi_state"] = "Long Unwinding"
    events.append(bad)
    report = v91_goal.bear_fsb_final_report(events, exact_context())
    assert report["final_test"]["locked"] is False
    assert report["qualifying_events"] == 100
    assert report["final_test"]["trade_count"] == 20
    assert len(report["chronological_blocks"]) == 4


def test_v91_goal_report_keeps_bull_final_locked_and_marks_bear_validation_qualified():
    events = []
    for i in range(120):
        row = accumulation_row()
        row.update({
            "entry_time": f"2026-02-{(i % 28) + 1:02d}T11:{i % 60:02d}:00+05:30",
            "signal_time": f"2026-02-{(i % 28) + 1:02d}T11:{i % 60:02d}:00+05:30",
            "intraday_returns": {"30m": 0.1, "1h": 0.1, "2h": 0.2, "eod": 0.2},
            "swing_returns": {"1D": 0.3, "2D": 0.35},
        })
        events.append(row)
    events.extend(bear_fsb_row(i, 0.5 if i % 4 else -0.1) for i in range(400))
    report = early_research.v91_goal_report(events, run_context=exact_context(), reveal_bear_final=False)
    bull = report["bull_institutional_accumulation"]
    bear = report["bear_fresh_short_buildup"]
    assert bull["1D"]["final_test"]["locked"] is True
    assert bear["validation_status"] == "VALIDATION QUALIFIED — FINAL TEST LOCKED"
    assert report["bear_final"]["final_test"]["locked"] is True


def test_v91_fast_replay_emits_accumulation_probe_without_requiring_breakout():
    import pandas as pd
    idx = pd.date_range("2026-08-28 09:15", periods=16, freq="15min", tz="Asia/Kolkata")
    closes = [100 + i * 0.05 for i in range(16)]
    df = pd.DataFrame({
        "open": closes, "high": [c + 0.2 for c in closes],
        "low": [c - 0.2 for c in closes], "close": closes,
        "volume": [1000] * 16,
    }, index=idx)
    features = pd.DataFrame(index=idx)
    features["atr"] = 1.0
    features["price_chg_60m_pct"] = 0.5
    features["oi_chg_60m_pct"] = 2.0
    features["oi_chg_30m_pct"] = 1.0
    features["vwap_side_agrees"] = True
    features["tod_rvol"] = 1.4
    features["opening_rvol"] = 1.3
    features["bar_range_atr"] = 0.4
    features["gap_atr"] = 0.0
    features["turnover_notional"] = 100000.0
    features["rs_pct"] = 0.3
    features["stock_sector_lead_pct"] = 0.2
    features["basis_acceleration"] = 0.01
    features["fresh_breakout"] = False
    features["breakout_direction"] = None
    replay = early_research._replay_breakout_feature_frame(df, features, "ACC", fast_v8=True)
    assert any(e.get("v91_accumulation_probe") is True for e in replay["v9_playbook_events"])


def test_v91_live_accumulation_can_trigger_from_live_long_buildup_facts_without_replay_probe():
    row = accumulation_row()
    row.pop("v91_accumulation_probe")
    plays = v9_playbooks.evaluate_row(row, now=dt.datetime(2026, 8, 30, 13, 0))
    assert any(p["playbook"] == v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION and p["state"] == "TRADE CANDIDATE" for p in plays)


def test_v91_active_playbook_set_retires_failed_v9_models():
    assert v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION in v9_playbooks.ACTIVE_PLAYBOOKS
    assert v9_playbooks.BEAR_FRESH_SHORT_BUILDUP in v9_playbooks.ACTIVE_PLAYBOOKS
    assert v9_playbooks.BULL_OPENING_DRIVE not in v9_playbooks.ACTIVE_PLAYBOOKS
    assert v9_playbooks.BEAR_FAILED_BREAKOUT not in v9_playbooks.ACTIVE_PLAYBOOKS


def test_v91_fast_aggregate_keeps_bear_final_locked_but_final_mode_reveals_it():
    events = [bear_fsb_row(i, 0.5 if i % 5 else -0.1) for i in range(100)]
    base_ctx = exact_context()
    locked = early_research.aggregate_v8_research_fast([
        {"ignition_events": events, "v9_playbook_events": events}
    ], run_context={**base_ctx, "research_mode": "v91_fast"})
    assert locked["v91_goal"]["bear_final"]["final_test"]["locked"] is True
    final = early_research.aggregate_v8_research_fast([
        {"ignition_events": events, "v9_playbook_events": events}
    ], run_context={**base_ctx, "research_mode": "v91_bear_final"})
    assert final["v91_goal"]["bear_final"]["final_test"]["locked"] is False
    assert "v9_playbooks" not in final


def test_v91_live_seed_can_be_ranked_bullish_without_breakout_direction():
    from app import v8_dual
    row = {
        "v91_accumulation_seed_direction": "Bullish",
        "price_chg_60m_pct": 1.0,
        "oi_chg_60m_pct": 4.0,
        "tod_rvol": 2.0,
        "opening_rvol": 1.5,
        "bar_range_atr": 1.0,
        "gap_atr": 0.1,
        "turnover_notional": 1000000,
        "rs_pct": 1.0,
        "stock_sector_lead_pct": 0.5,
        "close_position_pct": 80,
        "basis_acceleration": 0.01,
    }
    got = v8_dual.rank_cross_section([row])[0]
    assert got["v8_direction"] == "Bullish"
    assert got["v8_oi_state"] == "Long Buildup"
