from app import early_research


def _event(i, direction, alpha=90, participation=90, structure=90, relative=90, derivatives=90, ret2h=0.3, ret1d=0.4):
    return {
        "symbol": f"S{i}",
        "signal_time": f"2026-01-{1 + i // 4:02d} {9 + (i % 4)}:15:00",
        "entry_time": f"2026-01-{1 + i // 4:02d} {9 + (i % 4)}:30:00",
        "direction": direction,
        "breakout_source": "Recent Range",
        "breakout_extension_atr": 0.4,
        "v8_alpha": alpha,
        "v8_participation": participation,
        "v8_structure": structure,
        "v8_relative": relative,
        "v8_derivatives": derivatives,
        "v8_state": "TRADE CANDIDATE" if alpha >= 85 and participation >= 70 else "WATCH",
        "v8_eligible": alpha >= 85 and participation >= 70,
        "intraday_returns": {"30m": ret2h / 2, "1h": ret2h * 0.75, "2h": ret2h, "eod": ret2h},
        "swing_returns": {"1D": ret1d, "2D": ret1d * 1.1},
        "mfe_atr": {"1D": 1.6},
        "mae_atr": {"1D": 0.8},
    }


def test_v8_dual_report_keeps_bull_and_bear_separate_and_final_locked():
    events = []
    # 120 each direction gives meaningful 60/20/20 splits.
    for i in range(120):
        events.append(_event(i, "Bullish", ret2h=0.25, ret1d=0.35))
        events.append(_event(200 + i, "Bearish", ret2h=0.22, ret1d=0.30))
    report = early_research.v8_dual_report(events)
    assert report["bullish"]["full_consensus"]["2h"]["validation"]["trade_count"] == 24
    assert report["bearish"]["full_consensus"]["1D"]["validation"]["trade_count"] == 24
    assert report["bullish"]["full_consensus"]["1D"]["final_test"]["locked"] is True
    assert report["bearish"]["full_consensus"]["1D"]["final_test"]["locked"] is True


def test_v8_report_has_fixed_ablations_not_parameter_grid():
    events = [_event(i, "Bullish") for i in range(120)]
    report = early_research.v8_dual_report(events)
    keys = set(report["bullish"])
    assert {"raw_recent_range", "structure_only", "participation_only", "relative_only", "derivatives_only", "full_consensus"}.issubset(keys)
    assert "threshold_grid" not in report
    assert report["protocol"]["weights_fitted"] is False
    assert report["protocol"]["final_locked"] is True


def test_v8_promotion_benchmark_requires_pf_sample_expectancy_excursion_and_stability():
    events = [_event(i, "Bullish", ret1d=0.4) for i in range(600)]
    report = early_research.v8_dual_report(events)
    bench = report["bullish"]["benchmark"]["swing_1D"]
    assert bench["checks"]["sample"] is True
    assert bench["checks"]["expectancy"] is True
    assert bench["checks"]["profit_factor"] is True
    assert bench["checks"]["excursion_quality"] is True
    assert bench["checks"]["chronological_stability"] is True
    assert bench["status"] == "PROMOTABLE"


def test_v8_report_does_not_hide_failed_bear_side_behind_bull_side():
    events = []
    for i in range(600):
        events.append(_event(i, "Bullish", ret1d=0.4))
        events.append(_event(1000 + i, "Bearish", ret1d=-0.3))
    report = early_research.v8_dual_report(events)
    assert report["bullish"]["benchmark"]["swing_1D"]["status"] == "PROMOTABLE"
    assert report["bearish"]["benchmark"]["swing_1D"]["status"] == "RESEARCH"
    assert report["combined_status"] == "RESEARCH"


def test_breakout_replay_carries_directional_price_fields_for_v8():
    import pandas as pd
    import numpy as np

    idx = pd.date_range("2026-08-20 09:15", periods=30, freq="15min")
    close = pd.Series(np.linspace(100, 110, len(idx)), index=idx)
    df = pd.DataFrame({
        "open": close - 0.1,
        "high": close + 0.4,
        "low": close - 0.4,
        "close": close,
        "volume": 1000,
    }, index=idx)
    features = pd.DataFrame(index=idx)
    features["atr"] = 1.0
    features["energy_building"] = False
    features["breakout_direction"] = None
    features["direction"] = None
    features["fresh_breakout"] = False
    pos = 10
    features.loc[idx[pos], "breakout_direction"] = "Bullish"
    features.loc[idx[pos], "direction"] = "Bullish"
    features.loc[idx[pos], "fresh_breakout"] = True
    features["breakout_source"] = None
    features.loc[idx[pos], "breakout_source"] = "Recent Range"
    features["breakout_level"] = np.nan
    features.loc[idx[pos], "breakout_level"] = float(close.iloc[pos] - 0.3)
    features["breakout_extension_atr"] = np.nan
    features.loc[idx[pos], "breakout_extension_atr"] = 0.3
    features["tod_rvol"] = 2.0
    features["oi_chg_30m_pct"] = 1.0
    features["oi_chg_60m_pct"] = 2.0
    features["oi_acceleration"] = 0.1
    features["oi_recent_agrees"] = True
    features["vwap_side_agrees"] = True
    features["entry_is_extended"] = False
    features["rs_pct"] = 1.0
    features["sector_agrees"] = True
    features["htf_agrees"] = True
    features["price_chg_60m_pct"] = 1.25

    replay = early_research._replay_breakout_feature_frame(df, features, "TEST")
    event = replay["ignition_events"][0]
    assert event["price_chg_60m_pct"] == 1.25
    assert event["high"] == float(df["high"].iloc[pos])
    assert event["low"] == float(df["low"].iloc[pos])
    assert event["close"] == float(df["close"].iloc[pos])


def test_build_feature_frame_computes_same_window_60m_price_change():
    import pandas as pd
    import numpy as np

    idx = pd.date_range("2026-08-20 09:15", periods=80, freq="15min")
    close = pd.Series(np.linspace(100, 120, len(idx)), index=idx)
    df = pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 1000,
    }, index=idx)
    features = early_research.build_feature_frame(df, "15minute")
    assert "price_chg_60m_pct" in features.columns
    pos = 20
    expected = (close.iloc[pos] / close.iloc[pos - 4] - 1.0) * 100.0
    assert abs(float(features["price_chg_60m_pct"].iloc[pos]) - expected) < 1e-9
