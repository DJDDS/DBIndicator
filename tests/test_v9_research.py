import pandas as pd

from app import stock_in_play, early_research, v9_playbooks


def frame(closes, highs=None, lows=None):
    idx = pd.date_range("2026-08-28 09:15", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    highs = highs or [c + 0.3 for c in closes]
    lows = lows or [c - 0.3 for c in closes]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1000] * len(closes)}, index=idx)


def test_failed_bullish_breakout_is_only_known_on_following_bar():
    closes = [100, 100.1, 100.2, 100.1, 100.2, 100.3, 101.0, 100.15, 99.9]
    highs = [100.3,100.4,100.5,100.4,100.5,100.6,101.2,100.5,100.2]
    lows = [99.7,99.8,99.9,99.8,99.9,100.0,100.5,99.9,99.6]
    df = frame(closes, highs, lows)
    atr = pd.Series(1.0, index=df.index)
    f = stock_in_play.build_price_features(df, atr, timeframe="15minute")
    assert bool(f["fresh_breakout"].iloc[6]) is True
    assert pd.isna(f["failed_breakout_direction"].iloc[6])
    assert f["failed_breakout_direction"].iloc[7] == "Bearish"
    assert f["failed_breakout_source"].iloc[7] in ("Recent Range", "Opening Range")


def test_v9_fast_replay_emits_confirmed_pullback_reclaim_event():
    closes = [100,100.1,100.2,100.1,100.2,100.3,101.0,100.75,101.2,101.4,101.5,101.7,101.9,102.0,102.2]
    highs = [c + 0.25 for c in closes]
    lows = [c - 0.25 for c in closes]
    # breakout level is around 100.55; next bar probes it and closes above.
    lows[7] = 100.48
    df = frame(closes, highs, lows)
    atr = pd.Series(1.0, index=df.index)
    price = stock_in_play.build_price_features(df, atr, timeframe="15minute")
    features = price.copy()
    features["atr"] = atr
    features["v8_participation"] = 80.0
    features["v8_relative"] = 75.0
    features["v8_derivatives"] = 70.0
    features["v8_structure"] = 80.0
    features["v8_oi_state"] = "Long Buildup"
    features["vwap_side_agrees"] = True
    features["vwap_distance_atr"] = 0.2
    features["price_chg_60m_pct"] = 1.0
    features["oi_chg_60m_pct"] = 2.0
    features["basis_acceleration"] = 0.0
    features["close_position_pct"] = 80.0
    features["turnover_notional"] = 1_000_000.0
    features["turnover_percentile"] = 80.0
    features["tod_rvol"] = 2.0
    features["opening_rvol"] = 1.8
    features["bar_range_atr"] = 1.0
    features["gap_atr"] = 0.0
    features["rs_pct"] = 1.0
    features["stock_sector_lead_pct"] = 0.5
    replay = early_research._replay_breakout_feature_frame(df, features, "TEST", fast_v8=True)
    plays = replay.get("v9_playbook_events") or []
    assert any(e.get("retained_breakout_direction") == "Bullish" and e.get("retest_confirmed") is True for e in plays)


def test_v9_report_keeps_final_locked_and_catalyst_shadow():
    events = []
    for i in range(30):
        events.append({
            "v9_playbook": v9_playbooks.BULL_OPENING_DRIVE,
            "direction": "Bullish",
            "signal_time": f"2026-01-{(i%28)+1:02d}T10:00:00+05:30",
            "intraday_returns": {"30m": 0.1, "1h": 0.15, "2h": 0.2, "eod": 0.25},
            "swing_returns": {"1D": 0.3, "2D": 0.35},
            "mfe_atr": {"2h": 1.2, "1D": 1.5},
            "mae_atr": {"2h": 0.5, "1D": 0.7},
        })
    report = early_research.v9_playbook_report(events)
    opening = report["playbooks"][v9_playbooks.BULL_OPENING_DRIVE]
    assert opening["2h"]["final_test"]["locked"] is True
    assert len(opening["2h"]["validation_blocks"]) == 4
    assert report["playbooks"][v9_playbooks.BULL_CATALYST_CONTINUATION]["historical_status"] == "LIVE_SHADOW"


def test_v9_fast_primary_report_skips_retired_v8_topk_audit(monkeypatch):
    """The V9 primary button must not spend Stage 3 rebuilding retired V8 Top-K tables."""
    from app import early_research
    monkeypatch.setattr(early_research, "v8_dual_report_fast", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("retired V8 Top-K audit called")))
    event = {
        "symbol": "ABC",
        "signal_time": "2026-08-01 10:00:00",
        "entry_time": "2026-08-01 10:15:00",
        "direction": "Bullish",
        "breakout_direction": "Bullish",
        "breakout_source": "Opening Range",
        "fresh_breakout": True,
        "v8_participation": 90,
        "v8_relative": 85,
        "v8_structure": 88,
        "v8_derivatives": 80,
        "close_position_pct": 90,
        "breakout_extension_atr": 0.3,
        "intraday_returns": {"30m": 0.1, "1h": 0.2, "2h": 0.3, "eod": 0.4},
        "swing_returns": {"1D": 0.4, "2D": 0.5},
        "oi_status": "Unavailable",
    }
    result = early_research.aggregate_v8_research_fast([
        {"ignition_events": [event], "v9_playbook_events": [event]}
    ], run_context={"setup_timeframe": "15minute", "days": 180})
    assert "v9_playbooks" in result
    assert "v8_dual" not in result
