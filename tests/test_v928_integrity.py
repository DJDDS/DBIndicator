import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from app import backtest, background, early_research, stock_in_play, v6_edge


def _seed_frame(states):
    idx = pd.date_range("2026-08-28 09:15", periods=len(states), freq="15min", tz="Asia/Kolkata")
    closes = [100 + i * 0.1 for i in range(len(states))]
    df = pd.DataFrame({
        "open": closes,
        "high": [c + 0.2 for c in closes],
        "low": [c - 0.2 for c in closes],
        "close": closes,
        "volume": [1000] * len(states),
    }, index=idx)
    features = pd.DataFrame(index=idx)
    features["atr"] = 1.0
    features["price_chg_60m_pct"] = [0.5 if s else -0.1 for s in states]
    features["oi_chg_60m_pct"] = [2.0 if s else -0.2 for s in states]
    features["oi_chg_30m_pct"] = 1.0
    features["vwap_side_agrees"] = True
    features["tod_rvol"] = 1.4
    features["opening_rvol"] = 1.2
    features["bar_range_atr"] = 0.4
    features["gap_atr"] = 0.0
    features["turnover_notional"] = 100000.0
    features["rs_pct"] = 0.3
    features["stock_sector_lead_pct"] = 0.2
    features["basis_acceleration"] = 0.01
    features["fresh_breakout"] = False
    features["breakout_direction"] = None
    return df, features


def test_v928_net_return_charges_slippage_on_both_entry_and_exit():
    # 1.00% raw move - 0.08% costs - 0.05% slippage each side = 0.82% net.
    got = v6_edge._net_return(100.0, 101.0, "Bullish", 0.08, 0.05)
    assert got == pytest.approx(0.82)


def test_v928_primary_stock_in_play_outcomes_charge_slippage_both_sides():
    got = stock_in_play._net_return(100.0, 101.0, "Bullish", 0.08, 0.05)
    assert got == pytest.approx(0.82)


def test_v928_bull_accumulation_seed_fires_once_per_continuous_episode():
    df, features = _seed_frame([True] * 12)
    replay = early_research._replay_breakout_feature_frame(
        df, features, "ACC", cost_pct=0.08, slippage_pct=0.05, fast_v8=True
    )
    seeds = [e for e in replay["v9_playbook_events"] if e.get("v92_accumulation_seed") is True]
    assert len(seeds) == 1


def test_v928_bull_accumulation_rearms_only_after_state_reset():
    df, features = _seed_frame([True, True, True, False, False, True, True, True])
    replay = early_research._replay_breakout_feature_frame(
        df, features, "ACC", cost_pct=0.08, slippage_pct=0.05, fast_v8=True
    )
    seeds = [e for e in replay["v9_playbook_events"] if e.get("v92_accumulation_seed") is True]
    assert len(seeds) == 2


def test_v928_fetch_history_uses_safe_chunked_downloader(monkeypatch):
    calls = []

    class Kite:
        def historical_data(self, *args, **kwargs):
            raise AssertionError("direct historical_data call must not be used by research")

    def fake_chunked(kite, token, from_date, to_date, interval, oi=False, continuous=False):
        calls.append((token, interval, oi, continuous))
        return [
            {"date": pd.Timestamp("2026-08-28 09:15", tz="Asia/Kolkata"), "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10},
            {"date": pd.Timestamp("2026-08-28 09:30", tz="Asia/Kolkata"), "open": 100.5, "high": 101, "low": 100, "close": 100.7, "volume": 11},
        ]

    monkeypatch.setattr(backtest.scanner_mod, "_fetch_historical_chunked", fake_chunked)
    monkeypatch.setattr(backtest, "now_ist", lambda: dt.datetime(2026, 8, 28, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))))
    df = backtest._fetch_history(123, "15minute", 365, Kite())

    assert calls == [(123, "15minute", False, False)]
    assert len(df) == 2


def test_v928_incomplete_intraday_candle_is_removed():
    idx = pd.DatetimeIndex([
        pd.Timestamp("2026-08-31 13:00", tz="Asia/Kolkata"),
        pd.Timestamp("2026-08-31 13:15", tz="Asia/Kolkata"),
    ])
    df = pd.DataFrame({"close": [100.0, 101.0]}, index=idx)
    now = pd.Timestamp("2026-08-31 13:20", tz="Asia/Kolkata")
    clean = backtest._drop_incomplete_intraday_bars(df, "15minute", now=now)
    assert list(clean.index) == [idx[0]]


def test_v928_history_coverage_reports_price_and_oi_measurement():
    price_idx = pd.date_range("2026-08-01 09:15", periods=8, freq="15min", tz="Asia/Kolkata")
    price = pd.DataFrame({"close": range(8)}, index=price_idx)
    oi = pd.Series([100, 101, 102], index=price_idx[-3:])
    got = backtest._history_coverage_summary(price, oi, requested_days=180)

    assert got["price_bars"] == 8
    assert got["oi_bars"] == 3
    assert got["oi_bar_coverage_pct"] == pytest.approx(37.5)
    assert got["oi_first_timestamp"].startswith("2026-08-01T10:30")


def test_v928_shadow_early_radar_surfaces_research_stages_without_production_rank():
    rows = [{
        "symbol": "COIL",
        "direction": "Bullish",
        "compression_score": 78.0,
        "energy_building": True,
        "entry_trigger": None,
        "entry_trigger_bars_ago": None,
        "oi_recent_agrees": True,
        "oi_chg_60m_pct": 1.4,
        "oi_chg_30m_pct": 0.6,
        "oi_acceleration": 0.2,
        "tod_rvol": 1.25,
        "rs_pct": 0.3,
        "vwap_side_agrees": True,
        "entry_is_extended": False,
        "htf_agrees": True,
    }]

    background._apply_shadow_early_radar(rows)

    assert rows[0]["shadow_radar_rank"] == 1
    assert rows[0]["shadow_movement_stage"] == "Energy Building"
    assert rows[0].get("radar_rank") is None


def test_v928_dashboard_and_backtest_ui_label_shadow_and_legacy_diagnostics():
    index_text = Path("app/templates/index.html").read_text(encoding="utf-8")
    backtest_text = Path("app/templates/backtest.html").read_text(encoding="utf-8")

    assert "Shadow Early Radar" in index_text
    assert "RESEARCH / SHADOW" in index_text
    assert "Legacy Align" in index_text
    assert "Legacy Signal" in index_text
    assert "Stock ADX Regime" in index_text
    assert "Legacy Score" in index_text
    assert "Historical Data Coverage" in backtest_text


def test_v928_4hour_final_session_bucket_closes_at_market_close():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-31 13:15", tz="Asia/Kolkata")])
    df = pd.DataFrame({"close": [100.0]}, index=idx)
    after_close = pd.Timestamp("2026-08-31 15:31", tz="Asia/Kolkata")
    clean = backtest._drop_incomplete_intraday_bars(df, "4hour", now=after_close)
    assert list(clean.index) == list(idx)


def test_v928_4hour_final_session_bucket_is_not_complete_during_market_hours():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-31 13:15", tz="Asia/Kolkata")])
    df = pd.DataFrame({"close": [100.0]}, index=idx)
    before_close = pd.Timestamp("2026-08-31 14:00", tz="Asia/Kolkata")
    clean = backtest._drop_incomplete_intraday_bars(df, "4hour", now=before_close)
    assert clean.empty
