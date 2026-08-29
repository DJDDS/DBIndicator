import math

import numpy as np
import pandas as pd
import pytest

from app import v6_edge


def test_cross_section_percentile_ranks_highest_value_near_100():
    vals = pd.Series([10.0, 20.0, 40.0], index=["A", "B", "C"])
    ranks = v6_edge.percentile_rank(vals)
    assert ranks["C"] == pytest.approx(100.0)
    assert ranks["A"] < ranks["B"] < ranks["C"]


def test_catalyst_proxy_rewards_gap_opening_activity_and_range_shock():
    quiet = v6_edge.catalyst_proxy_score(
        gap_atr=0.05, opening_rvol=0.8, tod_rvol=0.9,
        bar_range_atr=0.4, turnover_percentile=35.0,
    )
    active = v6_edge.catalyst_proxy_score(
        gap_atr=0.8, opening_rvol=2.0, tod_rvol=1.8,
        bar_range_atr=1.2, turnover_percentile=92.0,
    )
    assert quiet < 25
    assert active >= 80


def test_market_regime_uses_index_breadth_and_dispersion():
    assert v6_edge.classify_market_regime(0.7, 69.0, 31.0, 0.9) == "Trend Up"
    assert v6_edge.classify_market_regime(-0.8, 28.0, 72.0, 0.8) == "Trend Down"
    assert v6_edge.classify_market_regime(0.1, 52.0, 48.0, 1.4) == "Rotation"
    assert v6_edge.classify_market_regime(0.05, 51.0, 49.0, 0.35) == "Chop"


def test_price_location_prefers_long_near_previous_highs():
    long_high = v6_edge.price_location_score(
        direction="Bullish", close=99.0, high20=100.0, low20=80.0,
        high50=102.0, low50=72.0,
    )
    long_middle = v6_edge.price_location_score(
        direction="Bullish", close=88.0, high20=100.0, low20=80.0,
        high50=102.0, low50=72.0,
    )
    assert long_high["score"] > long_middle["score"]
    assert long_high["near_20d_high"] is True


def test_sponsorship_is_soft_and_basis_can_substitute_for_missing_oi():
    no_oi_basis = v6_edge.sponsorship_score(
        direction="Bullish", tod_rvol=1.6,
        oi_confirmed=None, basis_pct=0.25, basis_acceleration=0.12,
    )
    oi_only = v6_edge.sponsorship_score(
        direction="Bullish", tod_rvol=1.6,
        oi_confirmed=True, basis_pct=None, basis_acceleration=None,
    )
    neither = v6_edge.sponsorship_score(
        direction="Bullish", tod_rvol=1.6,
        oi_confirmed=False, basis_pct=-0.1, basis_acceleration=-0.05,
    )
    assert no_oi_basis["sponsored"] is True
    assert oi_only["sponsored"] is True
    assert neither["sponsored"] is False
    assert no_oi_basis["score"] > 0


def test_live_candidate_does_not_require_oi_when_other_evidence_is_strong():
    row = {
        "direction": "Bullish",
        "breakout_source": "Recent Range",
        "fresh_breakout": True,
        "breakout_retained": True,
        "retest_confirmed": True,
        "tod_rvol": 1.8,
        "turnover_percentile": 95.0,
        "catalyst_score": 85.0,
        "sector_rank_percentile": 90.0,
        "stock_sector_lead_pct": 0.8,
        "price_location_score": 90.0,
        "market_regime": "Trend Up",
        "vwap_side_agrees": True,
        "breakout_extension_atr": 0.45,
        "oi_confirmed": False,
        "basis_pct": 0.30,
        "basis_acceleration": 0.15,
        "execution_5m_quality": 80.0,
        "htf_agrees": True,
        "sector_agrees": True,
    }
    result = v6_edge.classify_v6_candidate(row)
    assert result["intraday_eligible"] is True
    assert result["score"] >= 70
    assert "OI" not in " ".join(result["blockers"])


def test_swing_short_is_research_only_even_when_strong():
    row = {
        "direction": "Bearish",
        "breakout_source": "Recent Range",
        "fresh_breakout": True,
        "breakout_retained": True,
        "retest_confirmed": True,
        "tod_rvol": 2.0,
        "turnover_percentile": 98.0,
        "catalyst_score": 90.0,
        "sector_rank_percentile": 95.0,
        "stock_sector_lead_pct": -1.0,
        "price_location_score": 90.0,
        "market_regime": "Trend Down",
        "vwap_side_agrees": True,
        "breakout_extension_atr": 0.4,
        "oi_confirmed": True,
        "basis_pct": -0.25,
        "basis_acceleration": -0.12,
        "execution_5m_quality": 85.0,
        "htf_agrees": True,
        "sector_agrees": True,
        "timestamp": "2026-08-29T14:30:00+05:30",
    }
    result = v6_edge.classify_v6_candidate(row)
    assert result["intraday_eligible"] is True
    assert result["swing_eligible"] is False
    assert result["short_research_only"] is True


def _five_minute_frame():
    idx = pd.date_range("2026-08-29 10:45", periods=8, freq="5min")
    return pd.DataFrame({
        "open": [100.2, 100.4, 100.5, 100.7, 100.8, 101.0, 101.1, 101.2],
        "high": [100.6, 100.7, 100.9, 101.0, 101.2, 101.3, 101.4, 101.5],
        "low":  [100.0, 100.2, 100.3, 100.5, 100.6, 100.8, 100.9, 101.0],
        "close":[100.4, 100.5, 100.8, 100.9, 101.1, 101.2, 101.3, 101.4],
        "volume":[100, 120, 220, 180, 240, 210, 190, 200],
    }, index=idx)


def test_five_minute_execution_quality_rewards_retention_and_volume_burst():
    q = v6_edge.five_minute_execution_quality(
        _five_minute_frame(), direction="Bullish", breakout_level=100.0,
        atr=1.0, signal_time=pd.Timestamp("2026-08-29 10:45"),
    )
    assert q["available"] is True
    assert q["quality"] >= 65
    assert q["retained"] is True
    assert q["extended"] is False


def test_first_touch_exit_is_conservative_when_target_and_stop_hit_same_bar():
    idx = pd.date_range("2026-08-29 10:45", periods=3, freq="15min")
    df = pd.DataFrame({
        "open": [100, 100, 100],
        "high": [100, 101.2, 100.5],
        "low": [100, 99.4, 99.8],
        "close": [100, 100.5, 100.2],
    }, index=idx)
    result = v6_edge.first_touch_exit(
        df, entry_pos=0, direction="Bullish", entry_price=100,
        atr=1.0, target_atr=1.0, stop_atr=0.5,
        cost_pct=0.05, slippage_pct=0.02,
    )
    assert result["outcome"] == "stop"
    assert result["net_return_pct"] < 0


def test_three_way_split_is_chronological_60_20_20():
    events = [{"entry_time": f"2026-01-{i:02d}T10:00:00", "x": i} for i in range(1, 11)]
    dev, val, final = v6_edge.three_way_split(events)
    assert [x["x"] for x in dev] == [1, 2, 3, 4, 5, 6]
    assert [x["x"] for x in val] == [7, 8]
    assert [x["x"] for x in final] == [9, 10]


def test_final_test_payload_is_locked_by_default(monkeypatch):
    monkeypatch.delenv("V6_UNLOCK_FINAL_TEST", raising=False)
    payload = v6_edge.final_test_payload({"trade_count": 50, "profit_factor": 1.4})
    assert payload["locked"] is True
    assert "profit_factor" not in payload


def test_first_touch_grid_returns_named_target_stop_variants():
    idx = pd.date_range("2026-08-29 10:45", periods=6, freq="15min")
    df = pd.DataFrame({
        "open": [100, 100, 100.5, 101.0, 101.2, 101.4],
        "high": [100, 100.6, 101.1, 101.4, 101.7, 101.8],
        "low": [100, 99.8, 100.3, 100.8, 101.0, 101.2],
        "close": [100, 100.5, 101.0, 101.2, 101.5, 101.6],
    }, index=idx)
    grid = v6_edge.first_touch_grid(
        df, entry_pos=0, direction="Bullish", entry_price=100,
        atr=1.0, pairs=((0.5, 0.5), (1.0, 0.5)), max_bars=5,
    )
    assert set(grid) == {"T0.50_S0.50", "T1.00_S0.50"}
    assert grid["T0.50_S0.50"]["outcome"] == "target"


def test_breakeven_exit_moves_stop_after_trigger():
    idx = pd.date_range("2026-08-29 10:45", periods=5, freq="15min")
    df = pd.DataFrame({
        "open": [100, 100, 100.6, 100.2, 100.0],
        "high": [100, 100.8, 100.9, 100.5, 100.2],
        "low": [100, 99.7, 100.1, 99.8, 99.6],
        "close": [100, 100.6, 100.3, 100.0, 99.8],
    }, index=idx)
    out = v6_edge.breakeven_after_trigger_exit(
        df, entry_pos=0, direction="Bullish", entry_price=100,
        atr=1.0, trigger_atr=0.5, initial_stop_atr=0.5, target_atr=1.25,
        max_bars=4,
    )
    assert out["breakeven_armed"] is True
    assert out["outcome"] == "breakeven"


def test_validation_benchmark_uses_validation_not_final_test(monkeypatch):
    events = []
    for i in range(20):
        events.append({
            "entry_time": f"2026-01-{i+1:02d}T10:00:00",
            "returns": {"1D": 0.30 if i < 16 else -5.0},
        })
    report = v6_edge.three_way_research_report(events, field="returns", key="1D")
    assert report["development"]["trade_count"] == 12
    assert report["validation"]["trade_count"] == 4
    assert report["final_test"]["locked"] is True
    # Validation is positive even though deliberately awful final rows are hidden.
    assert report["validation"]["avg_return_pct"] > 0
