import pytest

from app.early_onset import assess_early_onset, derive_price_move_60m_atr
from app.oi_view import live_opportunity_radar, swing_research_console
from app.v12_trade_console import build_trade_console


def _base(symbol="EARLY", **extra):
    row = {
        "symbol": symbol,
        "close": 100.0,
        "atr": 2.0,
        "price_chg_60m_pct": 0.20,
        "price_chg_today_pct": 0.25,
        "oi_structure": "Long Buildup",
        "oi_chg_15m_pct": 0.9,
        "oi_chg_30m_pct": 1.2,
        "oi_chg_60m_pct": 1.4,
        "oi_acceleration": 0.6,
        "tod_rvol": 1.45,
        "tod_rvol_accel": 0.35,
        "vol_rising": True,
        "bar_range_atr": 0.35,
        "compression_score": 72.0,
        "prior_high_20d": 100.4,
        "vs_vwap": "above",
        "htf_direction": "Bullish",
        "v8_relative": 76.0,
        "rs_pct": 0.8,
        "rs_acceleration": 0.2,
    }
    row.update(extra)
    return row


def test_preignition_is_visible_before_big_price_move():
    onset = assess_early_onset(_base(), "Bullish", fallback_score=20)
    assert onset["usable"] is True
    assert onset["phase"] == "PRE-IGNITION"
    assert onset["action_stage"] in ("FORMING", "ARMED")
    assert abs(onset["price_move_60m_atr"]) < 0.35


def test_extended_move_is_late_even_with_large_cumulative_values():
    row = _base(
        symbol="DONE",
        close=104.0,
        price_chg_60m_pct=4.0,
        price_chg_today_pct=5.0,
        oi_day_chg_pct=12.0,
        oi_chg_15m_pct=0.0,
        oi_chg_30m_pct=1.0,
        oi_chg_60m_pct=4.0,
        oi_acceleration=-0.5,
        tod_rvol=0.9,
        prior_high_20d=101.0,
    )
    onset = assess_early_onset(row, "Bullish", fallback_score=95)
    assert onset["phase"] in ("EXTENDED", "FADING")
    assert onset["action_stage"] in ("LATE", "FADING")


def test_radar_prefers_forming_move_to_completed_move():
    early = _base("EARLY")
    done = _base(
        "DONE",
        close=104.0,
        price_chg_60m_pct=4.0,
        price_chg_today_pct=4.5,
        oi_day_chg_pct=10.0,
        oi_chg_15m_pct=0.05,
        oi_chg_30m_pct=1.0,
        oi_chg_60m_pct=4.0,
        oi_acceleration=-0.4,
        tod_rvol=0.9,
        prior_high_20d=101.0,
    )
    radar = live_opportunity_radar([done, early], limit=5)
    assert radar["bullish"]
    assert radar["bullish"][0]["symbol"] == "EARLY"


def test_60m_atr_is_derived_live_for_2d_route():
    row = _base(price_chg_60m_pct=0.20, atr=2.0, close=100.0)
    assert derive_price_move_60m_atr(row) == pytest.approx(0.10)


def test_trade_console_uses_trigger_state_not_score_only():
    result = _base(
        close=100.5,
        prior_high_20d=100.4,
        price_chg_60m_pct=0.6,
        option_contract="EARLY24SEP100CE",
        option_spread_pct=2.0,
        option_intelligence={
            "contract": {
                "symbol": "EARLY24SEP100CE",
                "bid": 10.0,
                "ask": 10.2,
                "spread_pct": 2.0,
            }
        },
    )
    radar = live_opportunity_radar([result], limit=5)
    console = build_trade_console(radar, swing_research_console(radar), [result], limit=5)
    assert console["intraday"]
    assert console["intraday"][0]["trade_state"] in ("TRIGGERED", "ARMED", "FORMING")
    assert console["intraday"][0]["trade_state"] not in ("LATE", "FADING")
