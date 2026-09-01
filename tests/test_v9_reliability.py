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


def test_research_symbol_shards_round_trip_and_report_completed_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "_EARLY_RESEARCH_WORK_ROOT", tmp_path / "work")
    run_dir = backtest._early_research_run_dir(
        symbols=["AAA", "BBB"], timeframe="15minute", days=180,
        holdout_pct=20.0, cost_pct=0.08, slippage_pct=0.05,
        research_mode="v91_fast",
    )
    frame = pd.DataFrame({"tod_rvol": pd.Series([1.2, 1.4], dtype="float32")})
    replay = {"ignition_events": [{"symbol": "AAA", "signal_time": "2026-08-30T10:00:00+05:30"}]}
    backtest._write_research_symbol_shard(run_dir, 0, "AAA", compact_frame=frame, replay=replay, note=None)

    completed = backtest._completed_research_symbol_shards(run_dir)
    loaded = backtest._load_research_symbol_shard(completed["AAA"])

    assert set(completed) == {"AAA"}
    assert loaded["symbol"] == "AAA"
    assert loaded["replay"]["ignition_events"][0]["symbol"] == "AAA"
    assert str(loaded["compact_frame"]["tod_rvol"].dtype) == "float32"


def test_interrupted_restart_without_checkpoint_does_not_promise_resume(tmp_path, monkeypatch):
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
    assert "no durable checkpoint" in loaded["error"].lower()
    assert "resume" not in loaded["error"].lower()


def test_disk_backed_cross_sectional_ranks_match_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "_EARLY_RESEARCH_WORK_ROOT", tmp_path / "work")
    idx = pd.date_range("2026-08-30 10:00", periods=2, freq="15min")
    frames = {
        "AAA": pd.DataFrame({
            "tod_rvol": pd.Series([1.0, 2.0], index=idx, dtype="float32"),
            "opening_rvol": pd.Series([1.0, 2.0], index=idx, dtype="float32"),
            "bar_range_atr": pd.Series([1.0, 2.0], index=idx, dtype="float32"),
            "gap_atr": pd.Series([0.1, 0.2], index=idx, dtype="float32"),
            "turnover_notional": pd.Series([100.0, 200.0], index=idx, dtype="float32"),
            "oi_chg_60m_pct": pd.Series([1.0, 2.0], index=idx, dtype="float32"),
            "rs_pct": pd.Series([0.5, 1.0], index=idx, dtype="float32"),
            "stock_sector_lead_pct": pd.Series([0.3, 0.6], index=idx, dtype="float32"),
        }),
        "BBB": pd.DataFrame({
            "tod_rvol": pd.Series([2.0, 1.0], index=idx, dtype="float32"),
            "opening_rvol": pd.Series([2.0, 1.0], index=idx, dtype="float32"),
            "bar_range_atr": pd.Series([2.0, 1.0], index=idx, dtype="float32"),
            "gap_atr": pd.Series([0.2, 0.1], index=idx, dtype="float32"),
            "turnover_notional": pd.Series([200.0, 100.0], index=idx, dtype="float32"),
            "oi_chg_60m_pct": pd.Series([2.0, 1.0], index=idx, dtype="float32"),
            "rs_pct": pd.Series([1.0, 0.5], index=idx, dtype="float32"),
            "stock_sector_lead_pct": pd.Series([0.6, 0.3], index=idx, dtype="float32"),
        }),
    }
    def make_replays():
        return [{"ignition_events": [{
            "symbol": symbol, "signal_time": idx[0].isoformat(), "direction": "Bullish",
            "breakout_source": "Recent Range", "breakout_extension_atr": 0.5,
        }]} for symbol in ("AAA", "BBB")]

    expected = make_replays()
    backtest._attach_v8_full_universe_scores(expected, frames)

    run_dir = backtest._early_research_run_dir(
        symbols=["AAA", "BBB"], timeframe="15minute", days=180,
        holdout_pct=20.0, cost_pct=0.08, slippage_pct=0.05, research_mode="v91_fast",
    )
    shards = {}
    for i, symbol in enumerate(("AAA", "BBB")):
        shards[symbol] = backtest._write_research_symbol_shard(
            run_dir, i, symbol, compact_frame=frames[symbol], replay=make_replays()[i], note=None
        )
    actual = make_replays()
    backtest._attach_v8_full_universe_scores_from_shards(actual, shards)

    e = expected[0]["ignition_events"][0]
    a = actual[0]["ignition_events"][0]
    assert a["v8_tod_rvol_percentile"] == e["v8_tod_rvol_percentile"]
    assert a["v8_relative_percentile"] == e["v8_relative_percentile"]
    assert a["v8_alpha"] == e["v8_alpha"]


def test_fast_run_resumes_saved_symbol_without_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "_EARLY_RESEARCH_WORK_ROOT", tmp_path / "work")
    symbols = ["AAA", "BBB"]
    run_dir = backtest._early_research_run_dir(
        symbols=symbols, timeframe="15minute", days=180,
        holdout_pct=30.0, cost_pct=0.08, slippage_pct=0.05,
        research_mode="v91_fast",
    )
    idx = pd.date_range("2026-08-29 09:15", periods=80, freq="15min")
    compact = pd.DataFrame({"tod_rvol": pd.Series([1.2] * len(idx), index=idx, dtype="float32")})
    backtest._write_research_symbol_shard(
        run_dir, 0, "AAA", compact_frame=compact,
        replay={"ignition_events": [], "v9_playbook_events": []}, note=None,
    )

    monkeypatch.setattr(backtest, "_load_instrument_map", lambda _kite: {"AAA": 1, "BBB": 2})
    monkeypatch.setattr(backtest, "_load_index_token", lambda *_a, **_k: None)
    monkeypatch.setattr(backtest.scanner_mod, "SYMBOL_SECTOR_MAP", {})
    fetch_calls = []
    price = pd.DataFrame({
        "open": [100.0] * len(idx), "high": [101.0] * len(idx),
        "low": [99.0] * len(idx), "close": [100.0] * len(idx),
        "volume": [1000] * len(idx),
    }, index=idx)
    def fake_fetch(token, *_a, **_k):
        fetch_calls.append(token)
        return price.copy()
    monkeypatch.setattr(backtest, "_fetch_history", fake_fetch)
    monkeypatch.setattr(backtest, "_fetch_oi_history_for_backtest", lambda *_a, **_k: None)
    monkeypatch.setattr(backtest, "_fetch_near_futures_history_for_research", lambda *_a, **_k: None)
    feat = pd.DataFrame({
        "tod_rvol": [1.5] * len(idx), "opening_rvol": [1.4] * len(idx),
        "bar_range_atr": [0.5] * len(idx), "gap_atr": [0.0] * len(idx),
        "turnover_notional": [100000.0] * len(idx), "oi_chg_60m_pct": [2.0] * len(idx),
        "rs_pct": [0.3] * len(idx), "stock_sector_lead_pct": [0.2] * len(idx),
    }, index=idx)
    monkeypatch.setattr(backtest.early_research, "build_feature_frame", lambda *_a, **_k: feat.copy())
    monkeypatch.setattr(backtest, "compute_series", lambda *_a, **_k: {"atr": pd.Series([1.0] * len(idx), index=idx)})
    monkeypatch.setattr(backtest.early_research, "replay_feature_frame", lambda *_a, **_k: {"ignition_events": [], "v9_playbook_events": []})
    monkeypatch.setattr(backtest, "_attach_v8_full_universe_scores_from_shards", lambda replays, *_a, **_k: replays)
    monkeypatch.setattr(backtest.early_research, "aggregate_v8_research_fast", lambda *_a, **_k: {"v91_goal": {"build_id": "TEST"}})
    monkeypatch.setattr(backtest.time, "sleep", lambda *_a, **_k: None)

    result = backtest.run_early_movement_research(
        object(), symbols=symbols, timeframe="15minute", days=180,
        holdout_pct=30.0, cost_pct=0.08, slippage_pct=0.05,
        fast_v8=True, research_mode="v91_fast", resume_run_dir=run_dir,
    )

    assert fetch_calls == [2]
    assert result["symbols_scanned"] == 2
    assert result["symbols_completed"] == 2
