import datetime as dt

import pytest

from app import v91_goal


def bull_seed(**overrides):
    row = {
        "symbol": "AAA",
        "signal_time": "2026-08-01T10:00:00+05:30",
        "entry_time": "2026-08-01T10:15:00+05:30",
        "direction": "Bullish",
        "v8_direction": "Bullish",
        "v92_accumulation_seed": True,
        "price_chg_60m_pct": 1.2,
        "oi_chg_60m_pct": 4.0,
        "v8_oi_state": "Long Buildup",
        "vwap_side_agrees": True,
        "tod_rvol": 1.4,
        "v8_participation": 82.0,
        "v8_relative": 78.0,
        "v8_derivatives": 80.0,
        "close_position_pct": 75.0,
        "basis_acceleration": 0.01,
    }
    row.update(overrides)
    return row


def test_bull_gate_funnel_counts_exact_cumulative_failures():
    rows = [
        bull_seed(symbol="PASS"),
        bull_seed(symbol="VWAP", vwap_side_agrees=False),
        bull_seed(symbol="TOD", tod_rvol=0.8),
        bull_seed(symbol="PART", v8_participation=65.0),
        bull_seed(symbol="REL", v8_relative=65.0),
        bull_seed(symbol="DERIV", v8_derivatives=60.0),
        bull_seed(symbol="CLV", close_position_pct=50.0),
        bull_seed(symbol="BASIS", basis_acceleration=-0.05),
        bull_seed(symbol="SCORE", v8_participation=70.0, v8_relative=70.0, v8_derivatives=70.0, close_position_pct=70.0),
    ]
    funnel = v91_goal.bull_accumulation_gate_funnel(rows)
    counts = {x["gate"]: x["survivors"] for x in funnel["stages"]}
    assert counts["price_up_oi_up"] == 9
    assert "long_buildup" not in counts
    assert counts["above_vwap"] == 8
    assert counts["tod_rvol_ge_1"] == 7
    assert counts["participation_ge_70"] == 6
    assert counts["relative_strength_ge_70"] == 5
    assert counts["derivatives_ge_65"] == 4
    assert counts["bull_clv_ge_60"] == 3
    assert counts["basis_non_deteriorating"] == 2
    # PASS and SCORE both meet the exact median >=70 threshold.
    assert counts["consensus_ge_70"] == 2
    assert funnel["qualified"] == 2


def bear_row(ts, ret, *, regime="Trend Down", basis=-0.01, lead=-0.5, oi=5.0, index_ret=-0.8, index_vol=0.18, oi_accel=0.5, future_price=-0.4, future_oi=1.5):
    return {
        "symbol": "BBB",
        "signal_time": ts,
        "entry_time": ts,
        "direction": "Bearish",
        "fresh_breakout": True,
        "v8_oi_state": "Fresh Short Buildup",
        "breakout_extension_atr": 0.4,
        "basis_acceleration": basis,
        "v8_participation": 80.0,
        "v8_relative": 75.0,
        "v8_derivatives": 80.0,
        "close_position_pct": 20.0,
        "market_regime": regime,
        "stock_sector_lead_pct": lead,
        "oi_chg_60m_pct": oi,
        "oi_acceleration": oi_accel,
        "index_ret_8_pct": index_ret,
        "index_vol_20bar_pct": index_vol,
        "future_price_chg_60m_pct": future_price,
        "future_oi_chg_60m_pct": future_oi,
        "swing_returns": {"1D": ret},
        "intraday_returns": {"2h": ret / 2},
    }


def test_bear_regime_decomposition_compares_validation_and_consumed_final():
    rows = []
    start = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    # 10 qualifying events -> split 6/2/2. Validation is trend-down/weak sector;
    # final switches to trend-up/not-weaker, so the diagnostic should expose it.
    for i in range(6):
        ts = (start + dt.timedelta(days=i)).isoformat()
        rows.append(bear_row(ts, 0.1, regime="Trend Down", lead=-0.8))
    for i in range(2):
        ts = (start + dt.timedelta(days=6+i)).isoformat()
        rows.append(bear_row(ts, 0.5, regime="Trend Down", lead=-1.0))
    for i in range(2):
        ts = (start + dt.timedelta(days=8+i)).isoformat()
        rows.append(bear_row(ts, -0.5, regime="Trend Up", lead=0.4, basis=0.01, index_ret=0.9, index_vol=0.32, oi_accel=-0.7, future_price=0.6, future_oi=-1.2))

    report = v91_goal.bear_fsb_regime_decomposition(rows)
    assert report["validation"]["overall"]["trade_count"] == 2
    # The consumed one-shot final is immutable; current rolling rows must not
    # manufacture a replacement final cohort.
    assert report["final"]["overall"]["trade_count"] == 68
    assert report["final"]["overall"]["avg_return_pct"] == pytest.approx(-0.208)
    assert report["final"]["overall"]["profit_factor"] == pytest.approx(0.68)
    assert report["final_sample_source"] == "IMMUTABLE_CONSUMED_FINAL_SUMMARY"
    assert report["final_cohort_analysis_available"] is False
    assert report["validation"]["market_regime"]["Trend Down"]["trade_count"] == 2
    assert report["validation"]["sector_relative"]["weaker_than_sector"]["trade_count"] == 2
    assert report["validation"]["index_trend"]["index_down"]["trade_count"] == 2
    assert report["validation"]["market_volatility"]["normal_vol"]["trade_count"] == 2
    assert report["validation"]["oi_persistence"]["accelerating_oi"]["trade_count"] == 2
    assert report["validation"]["post_60m_positioning"]["shorts_persisting"]["trade_count"] == 2
    assert report["breadth_history"] == "UNAVAILABLE_IN_CURRENT_HISTORICAL_DATASET"
    assert report["diagnostic_only"] is True
    assert "new_rule" not in report


def test_v92_replay_captures_broad_long_buildup_seed_before_vwap_and_tod_gates():
    import pandas as pd
    from app import early_research

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
    features["vwap_side_agrees"] = False
    features["tod_rvol"] = 0.7
    features["opening_rvol"] = 0.8
    features["bar_range_atr"] = 0.4
    features["gap_atr"] = 0.0
    features["turnover_notional"] = 100000.0
    features["rs_pct"] = 0.3
    features["stock_sector_lead_pct"] = 0.2
    features["basis_acceleration"] = -0.05
    features["fresh_breakout"] = False
    features["breakout_direction"] = None
    replay = early_research._replay_breakout_feature_frame(df, features, "ACC", fast_v8=True)
    seeds = [e for e in replay["v9_playbook_events"] if e.get("v92_accumulation_seed") is True]
    assert seeds
    assert all(e.get("v91_accumulation_probe") is not True for e in seeds)


def test_v92_compactor_keeps_broad_bull_seed_even_when_old_pre_gates_fail():
    from app import backtest
    row = bull_seed(v91_accumulation_probe=False, v92_accumulation_seed=True,
                    vwap_side_agrees=False, tod_rvol=0.7, basis_acceleration=-0.05,
                    close_position_pct=45.0)
    replay = {"v9_playbook_events": [row]}
    compact = backtest._compact_v91_events(replay)
    assert len(compact) == 1
    assert compact[0]["v92_accumulation_seed"] is True
    assert compact[0]["vwap_side_agrees"] is False


def test_v91_goal_report_includes_v92_diagnostics_without_changing_bear_final_lock():
    from app import early_research
    rows = [bull_seed(symbol="A"), bull_seed(symbol="B", vwap_side_agrees=False)]
    # Add enough frozen Bear events to create validation/final cohorts.
    start = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    for i in range(20):
        ts = (start + dt.timedelta(days=i)).isoformat()
        rows.append(bear_row(ts, 0.3 if i < 16 else -0.2,
                             regime="Trend Down" if i < 16 else "Trend Up",
                             lead=-0.5 if i < 16 else 0.2))
    ctx = {
        "setup_timeframe": "15minute", "execution_timeframe": "15minute", "days": 180,
        "cost_pct": 0.08, "slippage_pct": 0.05, "universe_is_full_fno": True,
        "research_mode": "v91_fast",
    }
    report = early_research.v91_goal_report(rows, run_context=ctx, reveal_bear_final=False)
    assert report["bull_gate_funnel"]["seed_count"] == 2
    assert report["bear_regime_decomposition"]["diagnostic_only"] is True
    assert report["bear_final"]["final_test"]["locked"] is True


def test_v92_backtest_ui_mentions_gate_funnel_and_regime_decomposition():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V9.2 Diagnostic Reset" in text
    assert "Bull Gate Funnel" in text
    assert "Bear FSB Validation vs Final Regime" in text
    assert "Index trend" in text
    assert "Market volatility" in text
    assert "OI persistence" in text
    assert "Post-signal positioning" in text
    assert "Bear FSB Final: REJECTED" in text
    assert "Run Frozen Bear FSB Final Test" not in text
    assert "diagnostic only" in text.lower()


def test_stage3_reuses_already_ranked_event_objects_without_copying():
    from app import early_research

    bull = bull_seed(v8_alpha=82.0)
    bear = bear_row("2026-08-01T10:00:00+05:30", 0.3)
    bear.update({"v8_alpha": 80.0, "v81_bear_pressure": 79.0})
    rows = [bull, bear]

    scored = early_research._ensure_v8_event_scores(rows)

    assert scored is rows
    assert scored[0] is bull
    assert scored[1] is bear


def test_bull_gate_funnel_does_not_clone_seed_mappings():
    class NoCloneDict(dict):
        def keys(self):
            raise AssertionError("gate funnel must not clone candidate dictionaries")

    row = NoCloneDict(bull_seed(v8_alpha=82.0))
    funnel = v91_goal.bull_accumulation_gate_funnel([row])

    assert funnel["seed_count"] == 1
    assert funnel["qualified"] == 1


def test_v91_goal_report_emits_stage3_subprogress():
    from app import early_research

    rows = [bull_seed(symbol="A", v8_alpha=82.0)]
    start = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    for i in range(20):
        ts = (start + dt.timedelta(days=i)).isoformat()
        row = bear_row(ts, 0.2)
        row.update({"v8_alpha": 80.0, "v81_bear_pressure": 80.0})
        rows.append(row)
    ctx = {
        "setup_timeframe": "15minute", "execution_timeframe": "15minute", "days": 180,
        "cost_pct": 0.08, "slippage_pct": 0.05, "universe_is_full_fno": True,
        "research_mode": "v91_fast",
    }
    updates = []

    early_research.v91_goal_report(
        rows, run_context=ctx, reveal_bear_final=False,
        progress_cb=lambda message, pct: updates.append((message, pct)),
    )

    assert [m for m, _ in updates] == [
        "Bull accumulation + gate funnel",
        "Bear FSB regime decomposition",
        "Finalizing V9.2 diagnostic report",
    ]
    assert [p for _, p in updates] == [88, 92, 96]


def test_bull_gate_funnel_separates_vwap_availability_from_bull_above_vwap():
    row = bull_seed(vwap_side_agrees=None)
    row['bull_vwap_available'] = True
    row['bull_above_vwap'] = True
    funnel = v91_goal.bull_accumulation_gate_funnel([row])
    counts = {x['gate']: x['survivors'] for x in funnel['stages']}
    assert counts['vwap_available'] == 1
    assert counts['above_vwap'] == 1


def test_consumed_bear_final_summary_is_immutable_and_not_resplit_from_current_events():
    rows = []
    start = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    for i in range(100):
        ts = (start + dt.timedelta(days=i)).isoformat()
        rows.append(bear_row(ts, 9.0 if i >= 80 else 0.2))

    report = v91_goal.bear_fsb_regime_decomposition(rows)

    final = report['final']['overall']
    assert final['trade_count'] == 68
    assert final['avg_return_pct'] == pytest.approx(-0.208)
    assert final['profit_factor'] == pytest.approx(0.68)
    assert report['final_sample_source'] == 'IMMUTABLE_CONSUMED_FINAL_SUMMARY'
    assert report['final_cohort_analysis_available'] is False


def test_15minute_backtest_default_is_180_days_for_primary_research():
    from app import backtest
    assert backtest.backtest_day_bounds('15minute')[2] == 180
