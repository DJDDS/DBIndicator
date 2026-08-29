import math

from app import v8_dual


def test_directional_clv_is_asymmetric():
    row = {"high": 110, "low": 100, "close": 109}
    assert v8_dual.directional_clv(row, "Bullish") == 90.0
    assert v8_dual.directional_clv(row, "Bearish") == 10.0


def test_oi_state_and_directional_sponsorship_are_asymmetric():
    assert v8_dual.classify_oi_state(1.0, 2.0) == "Long Buildup"
    assert v8_dual.classify_oi_state(-1.0, 2.0) == "Fresh Short Buildup"
    assert v8_dual.classify_oi_state(1.0, -2.0) == "Short Covering"
    assert v8_dual.classify_oi_state(-1.0, -2.0) == "Long Unwinding"

    bull = v8_dual.directional_derivatives_score(
        "Bullish", price_chg_pct=1.0, oi_chg_pct=2.0, oi_strength_percentile=90
    )
    bear = v8_dual.directional_derivatives_score(
        "Bearish", price_chg_pct=-1.0, oi_chg_pct=2.0, oi_strength_percentile=90
    )
    assert bull["oi_state"] == "Long Buildup"
    assert bull["score"] > 80
    assert bear["oi_state"] == "Fresh Short Buildup"
    assert bear["score"] > 80


def test_consensus_uses_median_not_weighted_sum():
    score = v8_dual.consensus_score([95, 90, 20, 85])
    assert score == 87.5


def test_trade_candidate_requires_alpha_participation_recent_range_and_not_chased():
    row = {
        "breakout_source": "Recent Range",
        "breakout_extension_atr": 0.6,
    }
    out = v8_dual.classify_opportunity(row, alpha=88, participation=74)
    assert out["state"] == "TRADE CANDIDATE"
    assert out["eligible"] is True

    assert v8_dual.classify_opportunity(row, alpha=88, participation=65)["state"] == "WATCH"
    chased = dict(row, breakout_extension_atr=1.3)
    assert v8_dual.classify_opportunity(chased, alpha=92, participation=90)["state"] == "WATCH"
    generic = dict(row, breakout_source="Opening Range")
    assert v8_dual.classify_opportunity(generic, alpha=95, participation=95)["state"] == "WATCH"


def test_missing_evidence_is_ignored_in_consensus_not_scored_as_zero():
    assert v8_dual.consensus_score([90, None, float("nan"), 70]) == 80.0
    assert v8_dual.consensus_score([None, float("nan")]) is None


def test_rank_cross_section_builds_independent_bull_and_bear_scores():
    rows = [
        {
            "symbol": "BULL",
            "direction": "Bullish",
            "breakout_source": "Recent Range",
            "breakout_extension_atr": 0.25,
            "high": 101.0,
            "low": 99.0,
            "close": 100.9,
            "tod_rvol": 2.2,
            "opening_rvol": 1.8,
            "bar_range_atr": 1.2,
            "gap_atr": 0.8,
            "turnover_notional": 500,
            "rs_pct": 2.0,
            "stock_sector_lead_pct": 1.5,
            "price_chg_60m_pct": 1.0,
            "oi_chg_60m_pct": 3.0,
            "basis_acceleration": 0.08,
        },
        {
            "symbol": "BEAR",
            "direction": "Bearish",
            "breakout_source": "Recent Range",
            "breakout_extension_atr": 0.30,
            "high": 101.0,
            "low": 99.0,
            "close": 99.1,
            "tod_rvol": 2.0,
            "opening_rvol": 1.7,
            "bar_range_atr": 1.1,
            "gap_atr": -0.7,
            "turnover_notional": 450,
            "rs_pct": -1.8,
            "stock_sector_lead_pct": -1.4,
            "price_chg_60m_pct": -1.2,
            "oi_chg_60m_pct": 2.5,
            "basis_acceleration": -0.07,
        },
        {
            "symbol": "DULL",
            "direction": "Bullish",
            "breakout_source": "Recent Range",
            "breakout_extension_atr": 0.1,
            "high": 101.0,
            "low": 99.0,
            "close": 100.1,
            "tod_rvol": 0.8,
            "opening_rvol": 0.9,
            "bar_range_atr": 0.4,
            "gap_atr": 0.1,
            "turnover_notional": 50,
            "rs_pct": -0.5,
            "stock_sector_lead_pct": -0.4,
            "price_chg_60m_pct": 0.1,
            "oi_chg_60m_pct": 0.1,
            "basis_acceleration": 0.0,
        },
    ]
    ranked = v8_dual.rank_cross_section(rows)
    by = {r["symbol"]: r for r in ranked}

    assert by["BULL"]["v8_direction"] == "Bullish"
    assert by["BEAR"]["v8_direction"] == "Bearish"
    assert by["BULL"]["v8_alpha"] > by["DULL"]["v8_alpha"]
    assert by["BEAR"]["v8_derivatives"] > 70
    assert by["BULL"]["v8_oi_state"] == "Long Buildup"
    assert by["BEAR"]["v8_oi_state"] == "Fresh Short Buildup"


def test_build_live_leaderboards_keeps_sides_separate_and_sorted():
    rows = [
        {"symbol": "A", "v8_direction": "Bullish", "v8_alpha": 91, "v8_state": "TRADE CANDIDATE"},
        {"symbol": "B", "v8_direction": "Bearish", "v8_alpha": 95, "v8_state": "TRADE CANDIDATE"},
        {"symbol": "C", "v8_direction": "Bullish", "v8_alpha": 80, "v8_state": "WATCH"},
    ]
    boards = v8_dual.build_live_leaderboards(rows, limit=10)
    assert [r["symbol"] for r in boards["bullish"]] == ["A", "C"]
    assert [r["symbol"] for r in boards["bearish"]] == ["B"]

def test_score_preranked_row_uses_full_universe_percentiles():
    row = {
        "symbol": "X",
        "direction": "Bearish",
        "breakout_source": "Recent Range",
        "breakout_extension_atr": 0.4,
        "high": 110,
        "low": 100,
        "close": 100.2,
        "price_chg_60m_pct": -1.2,
        "oi_chg_60m_pct": 3.0,
        "basis_acceleration": -0.05,
        "v8_tod_rvol_percentile": 95,
        "v8_opening_rvol_percentile": 90,
        "v8_range_shock_percentile": 92,
        "v8_gap_shock_percentile": 88,
        "v8_turnover_percentile": 94,
        "v8_breakout_strength_percentile": 91,
        "v8_oi_strength_percentile": 96,
        "v8_relative_percentile": 93,
    }
    out = v8_dual.score_preranked_row(row)
    assert out["v8_participation"] >= 90
    assert out["v8_structure"] >= 90
    assert out["v8_derivatives"] >= 80
    assert out["v8_alpha"] >= 85
    assert out["v8_state"] == "TRADE CANDIDATE"
    assert out["v8_oi_state"] == "Fresh Short Buildup"


def test_swing_state_is_separate_and_requires_late_session_persistence():
    row = {
        "breakout_source": "Recent Range",
        "breakout_extension_atr": 0.4,
        "breakout_retained": True,
        "close_position_pct": 92,
    }
    late = v8_dual.classify_swing_opportunity(
        row, direction="Bullish", alpha=90, participation=90, derivatives=88,
        now_time="14:30"
    )
    assert late["state"] == "TRADE CANDIDATE"
    assert late["eligible"] is True
    early = v8_dual.classify_swing_opportunity(
        row, direction="Bullish", alpha=90, participation=90, derivatives=88,
        now_time="11:00"
    )
    assert early["state"] == "WATCH"
    assert early["eligible"] is False
