import json

import pandas as pd

from app import backtest, early_research, stock_in_play


def _reset_state():
    backtest._early_research_state.clear()
    backtest._early_research_state.update({
        "status": "idle",
        "progress": {"done": 0, "total": 0, "symbol": None, "stage": None, "stage_index": 0, "stage_total": 4, "overall_pct": 0},
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
    })


def test_completed_research_state_round_trips_from_disk(tmp_path, monkeypatch):
    path = tmp_path / "research-state.json"
    monkeypatch.setattr(backtest, "_EARLY_RESEARCH_STATE_PATH", path)
    _reset_state()
    backtest._early_research_state.update({
        "status": "done",
        "result": {"research": {"v9_playbooks": {"combined_status": "RESEARCH"}}},
        "finished_at": "2026-08-30T12:00:00+05:30",
    })

    backtest._persist_early_research_state()
    _reset_state()
    loaded = backtest._load_early_research_state()

    assert loaded["status"] == "done"
    assert loaded["result"]["research"]["v9_playbooks"]["combined_status"] == "RESEARCH"


def test_running_checkpoint_becomes_explicit_interrupted_state_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "research-state.json"
    monkeypatch.setattr(backtest, "_EARLY_RESEARCH_STATE_PATH", path)
    path.write_text(json.dumps({
        "status": "running",
        "progress": {"done": 170, "total": 211, "symbol": "RVNL", "stage": "Fetching F&O history", "stage_index": 1, "stage_total": 4, "overall_pct": 56},
        "result": None,
        "error": None,
        "started_at": "2026-08-30T11:00:00+05:30",
        "finished_at": None,
    }), encoding="utf-8")

    loaded = backtest._load_early_research_state()

    assert loaded["status"] == "error"
    assert "interrupted" in loaded["error"].lower()
    assert "restart" in loaded["error"].lower()
    assert loaded["progress"]["done"] == 170


def test_v9_feature_frame_is_compacted_to_float32():
    idx = pd.date_range("2026-08-01 09:15", periods=3, freq="15min")
    frame = pd.DataFrame({
        "tod_rvol": [1.1, 1.2, 1.3],
        "opening_rvol": [1.0, 1.1, 1.2],
        "bar_range_atr": [0.7, 0.8, 0.9],
        "gap_atr": [0.1, 0.2, 0.3],
        "turnover_notional": [1_000_000, 2_000_000, 3_000_000],
        "oi_chg_60m_pct": [1.0, 2.0, 3.0],
        "rs_pct": [0.2, 0.3, 0.4],
        "stock_sector_lead_pct": [0.1, 0.2, 0.3],
        "unused": [99, 99, 99],
    }, index=idx)

    compact = backtest._compact_v8_feature_frame(frame)

    assert "unused" not in compact.columns
    assert all(str(dtype) == "float32" for dtype in compact.dtypes)


def test_fast_v9_replay_reuses_fresh_breakout_event_instead_of_copying_it():
    closes = [100, 100.1, 100.2, 100.1, 100.2, 100.3, 101.0, 101.2, 101.3, 101.4]
    idx = pd.date_range("2026-08-28 09:15", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "open": closes,
        "high": [c + 0.25 for c in closes],
        "low": [c - 0.25 for c in closes],
        "close": closes,
        "volume": [1000] * len(closes),
    }, index=idx)
    atr = pd.Series(1.0, index=df.index)
    features = stock_in_play.build_price_features(df, atr, timeframe="15minute")
    features["atr"] = atr
    for col, value in {
        "turnover_notional": 1_000_000.0,
        "tod_rvol": 2.0,
        "opening_rvol": 1.8,
        "bar_range_atr": 1.0,
        "gap_atr": 0.0,
        "rs_pct": 1.0,
        "stock_sector_lead_pct": 0.5,
        "oi_chg_60m_pct": 2.0,
        "basis_acceleration": 0.0,
        "vwap_distance_atr": 0.2,
    }.items():
        features[col] = value

    replay = early_research._replay_breakout_feature_frame(df, features, "TEST", fast_v8=True)
    ignition = replay["ignition_events"]
    plays = replay["v9_playbook_events"]

    assert ignition
    assert any(candidate is ignition[0] for candidate in plays)
