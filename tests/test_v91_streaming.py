import time
import pickle

import pandas as pd

from app import backtest, early_research, v91_goal


def _event(symbol="AAA", direction="Bullish", signal="2026-08-30T10:00:00"):
    return {
        "symbol": symbol,
        "signal_time": signal,
        "entry_time": signal,
        "direction": direction,
        "v91_accumulation_probe": direction == "Bullish",
        "fresh_breakout": direction == "Bearish",
        "breakout_source": "Recent Range" if direction == "Bearish" else None,
        "breakout_extension_atr": 0.4,
        "price_chg_60m_pct": 1.2 if direction == "Bullish" else -1.2,
        "oi_chg_60m_pct": 8.0,
        "basis_acceleration": 0.01 if direction == "Bullish" else -0.01,
        "vwap_side_agrees": True,
        "tod_rvol": 1.8,
        "opening_rvol": 1.5,
        "bar_range_atr": 1.0,
        "gap_atr": 0.1,
        "turnover_notional": 1_000_000.0,
        "rs_pct": 1.0 if direction == "Bullish" else -1.0,
        "stock_sector_lead_pct": 0.5 if direction == "Bullish" else -0.5,
        "close_position_pct": 82.0 if direction == "Bullish" else 15.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.5 if direction == "Bullish" else 99.4,
        "intraday_returns": {"30m": 0.1, "1h": 0.2, "2h": 0.3, "eod": 0.4},
        "swing_returns": {"1D": 0.5, "2D": 0.6},
        "mfe_atr": {"1D": 1.8, "2D": 2.0},
        "mae_atr": {"1D": 0.7, "2D": 0.8},
        "huge_unused_payload": [1] * 10_000,
    }


def _compact_frame(values, idx):
    return pd.DataFrame({
        "tod_rvol": pd.Series(values, index=idx, dtype="float32"),
        "opening_rvol": pd.Series(values, index=idx, dtype="float32"),
        "bar_range_atr": pd.Series(values, index=idx, dtype="float32"),
        "gap_atr": pd.Series([0.1] * len(idx), index=idx, dtype="float32"),
        "turnover_notional": pd.Series([100.0 * v for v in values], index=idx, dtype="float32"),
        "oi_chg_60m_pct": pd.Series([2.0 * v for v in values], index=idx, dtype="float32"),
        "rs_pct": pd.Series(values, index=idx, dtype="float32"),
        "stock_sector_lead_pct": pd.Series([v / 2 for v in values], index=idx, dtype="float32"),
    })


def test_compact_v91_events_keep_goal_fields_and_drop_bulk():
    replay = {
        "v9_playbook_events": [_event()],
        "ignition_events": [{"oi_status": "Confirmed", "oi_chg_60m_pct": 2.0, "htf_agrees": True, "vwap_side_agrees": True}],
    }

    compact = backtest._compact_v91_events(replay)
    summary = backtest._v91_confirmation_summary(replay)

    assert len(compact) == 1
    row = compact[0]
    assert row["symbol"] == "AAA"
    assert row["intraday_returns"]["2h"] == 0.3
    assert row["swing_returns"]["1D"] == 0.5
    assert row["v91_accumulation_probe"] is True
    assert "huge_unused_payload" not in row
    assert summary["events"] == 1
    assert summary["oi_confirmed"] == 1


def test_v91_symbol_shard_can_omit_full_replay(tmp_path):
    idx = pd.date_range("2026-08-30 10:00", periods=2, freq="15min")
    path = backtest._write_research_symbol_shard(
        tmp_path, 0, "AAA", compact_frame=_compact_frame([1.0, 2.0], idx),
        replay=None, note=None, v91_events=[_event()], v91_confirmation={"events": 1},
    )
    loaded = backtest._load_research_symbol_shard(path)
    assert loaded["replay"] is None
    assert loaded["v91_events"][0]["symbol"] == "AAA"
    assert loaded["v91_confirmation"]["events"] == 1


def test_ranked_event_checkpoint_scores_compact_rows_and_is_reused(tmp_path, monkeypatch):
    idx = pd.date_range("2026-08-30 10:00", periods=2, freq="15min")
    shards = {}
    for i, (symbol, values, direction) in enumerate((
        ("AAA", [2.0, 1.0], "Bullish"),
        ("BBB", [1.0, 2.0], "Bearish"),
    )):
        shards[symbol] = backtest._write_research_symbol_shard(
            tmp_path, i, symbol, compact_frame=_compact_frame(values, idx), replay=None, note=None,
            v91_events=[_event(symbol=symbol, direction=direction, signal=idx[0].isoformat())],
            v91_confirmation={"events": 1},
        )

    checkpoint = backtest._build_v91_ranked_events_checkpoint(tmp_path, shards)
    payload = backtest._load_v91_ranked_events_checkpoint(checkpoint)
    rows = payload["events"]
    assert len(rows) == 2
    bull = next(r for r in rows if r["symbol"] == "AAA")
    bear = next(r for r in rows if r["symbol"] == "BBB")
    assert bull["v8_tod_rvol_percentile"] > bear["v8_tod_rvol_percentile"]
    assert bull["v8_alpha"] is not None
    assert bear["v8_oi_state"] == "Fresh Short Buildup"

    monkeypatch.setattr(backtest, "_load_research_symbol_shard", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must reuse checkpoint")))
    same = backtest._build_v91_ranked_events_checkpoint(tmp_path, shards)
    assert same == checkpoint


def test_compact_aggregate_preserves_v91_goal_contract_and_fingerprint():
    rows = []
    for i in range(100):
        row = _event(symbol=f"B{i}", direction="Bearish", signal=f"2026-01-{(i % 28)+1:02d}T10:{i%60:02d}:00+05:30")
        row.update({
            "v8_direction": "Bearish",
            "v8_oi_state": "Fresh Short Buildup",
            "v8_participation": 82.0,
            "v8_relative": 78.0,
            "v8_derivatives": 76.0,
            "v8_alpha": 78.0,
        })
        rows.append(row)
    ctx = {
        "setup_timeframe": "15minute", "execution_timeframe": "15minute", "days": 180,
        "cost_pct": 0.08, "slippage_pct": 0.05, "universe_is_full_fno": True,
        "research_mode": "v91_fast",
    }
    result = early_research.aggregate_v91_compact_events(rows, {"events": 100, "oi_confirmed": 100}, run_context=ctx)
    assert result["v91_goal"]["bear_final"]["final_test"]["locked"] is True
    assert result["v91_goal"]["protocol"]["bear_rule_fingerprint"] == v91_goal.frozen_bear_fsb_spec()["fingerprint"]


def test_v91_runner_never_uses_full_replay_loader(tmp_path, monkeypatch):
    symbols = ["AAA"]
    idx = pd.date_range("2026-08-29 09:15", periods=80, freq="15min")
    price = pd.DataFrame({
        "open": [100.0] * len(idx), "high": [101.0] * len(idx),
        "low": [99.0] * len(idx), "close": [100.0] * len(idx), "volume": [1000] * len(idx),
    }, index=idx)
    feat = _compact_frame([1.5] * len(idx), idx)
    feat["price_chg_60m_pct"] = 0.5
    feat["vwap_side_agrees"] = True
    feat["basis_acceleration"] = 0.0

    monkeypatch.setattr(backtest, "_EARLY_RESEARCH_WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(backtest, "_load_instrument_map", lambda _kite: {"AAA": 1})
    monkeypatch.setattr(backtest, "_load_index_token", lambda *_a, **_k: None)
    monkeypatch.setattr(backtest.scanner_mod, "SYMBOL_SECTOR_MAP", {})
    monkeypatch.setattr(backtest, "_fetch_history", lambda *_a, **_k: price.copy())
    monkeypatch.setattr(backtest, "_fetch_oi_history_for_backtest", lambda *_a, **_k: None)
    monkeypatch.setattr(backtest, "_fetch_near_futures_history_for_research", lambda *_a, **_k: None)
    monkeypatch.setattr(backtest.early_research, "build_feature_frame", lambda *_a, **_k: feat.copy())
    monkeypatch.setattr(backtest, "compute_series", lambda *_a, **_k: {"atr": pd.Series([1.0] * len(idx), index=idx)})
    monkeypatch.setattr(backtest.early_research, "replay_feature_frame", lambda *_a, **_k: {"ignition_events": [], "v9_playbook_events": [_event()]})
    monkeypatch.setattr(backtest, "_load_research_replays_from_shards", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("full replay loader used")))
    monkeypatch.setattr(backtest.time, "sleep", lambda *_a, **_k: None)

    result = backtest.run_early_movement_research(
        object(), symbols=symbols, timeframe="15minute", days=180,
        holdout_pct=30.0, cost_pct=0.08, slippage_pct=0.05,
        universe_is_full_fno=True, fast_v8=True, research_mode="v91_fast",
    )
    assert result["research"]["v91_goal"]["build_id"]
    assert result["symbols_completed"] == 1


def test_resume_summary_reports_saved_symbols_and_stage2_checkpoint(tmp_path):
    idx = pd.date_range("2026-08-30 10:00", periods=1, freq="15min")
    for i, symbol in enumerate(("AAA", "BBB")):
        backtest._write_research_symbol_shard(
            tmp_path, i, symbol, compact_frame=_compact_frame([1.0], idx), replay=None, note=None,
            v91_events=[], v91_confirmation={},
        )
    assert backtest._research_resume_summary(tmp_path, 3) == "2/3 symbols saved"
    backtest._atomic_pickle(backtest._v91_ranked_events_path(tmp_path), {
        "schema": backtest._V91_RANKED_EVENTS_SCHEMA,
        "events": [], "confirmation": {}, "notes": {}, "symbols_completed": 2,
    })
    assert backtest._research_resume_summary(tmp_path, 2) == "2/2 symbols saved · Stage 2 checkpoint available"


def test_backtest_ui_renders_resume_summary_in_progress_label():
    text = open("app/templates/backtest.html", encoding="utf-8").read()
    assert "p.resume_summary" in text
    assert "Resume data" in text


def test_compact_v91_events_excludes_rows_that_cannot_match_active_goal_models():
    bull = _event(direction="Bullish")
    bear = _event(symbol="BEAR", direction="Bearish")
    retired = _event(symbol="RET", direction="Bullish")
    retired["v91_accumulation_probe"] = False
    weak_bear = _event(symbol="WEAK", direction="Bearish")
    weak_bear["price_chg_60m_pct"] = 0.5  # cannot be fresh short buildup
    replay = {"v9_playbook_events": [bull, bear, retired, weak_bear], "ignition_events": []}
    rows = backtest._compact_v91_events(replay)
    assert {r["symbol"] for r in rows} == {"AAA", "BEAR"}


def test_v91_stage2_checkpoint_skips_all_history_fetch_on_resume(tmp_path, monkeypatch):
    symbols = ["AAA", "BBB"]
    run_dir = backtest._early_research_run_dir(
        symbols=symbols, timeframe="15minute", days=180,
        holdout_pct=30.0, cost_pct=0.08, slippage_pct=0.05, research_mode="v91_fast",
    )
    # point the helper at this test's root after deterministic dir creation
    monkeypatch.setattr(backtest, "_EARLY_RESEARCH_WORK_ROOT", tmp_path / "work")
    run_dir = backtest._early_research_run_dir(
        symbols=symbols, timeframe="15minute", days=180,
        holdout_pct=30.0, cost_pct=0.08, slippage_pct=0.05, research_mode="v91_fast",
    )
    backtest._atomic_pickle(backtest._v91_ranked_events_path(run_dir), {
        "schema": backtest._V91_RANKED_EVENTS_SCHEMA,
        "events": [], "confirmation": {}, "notes": {}, "symbols_completed": 2,
    })
    monkeypatch.setattr(backtest, "_load_instrument_map", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("history setup must be skipped")))
    result = backtest.run_early_movement_research(
        object(), symbols=symbols, timeframe="15minute", days=180,
        holdout_pct=30.0, cost_pct=0.08, slippage_pct=0.05,
        universe_is_full_fno=True, fast_v8=True, research_mode="v91_fast", resume_run_dir=run_dir,
    )
    assert result["symbols_completed"] == 2
    assert result["research"]["streaming_v91"] is True


def test_successful_v91_background_run_cleans_checkpoint_dir_without_error(tmp_path, monkeypatch):
    """A completed streaming run must clean its shard directory and stay done."""
    state_path = tmp_path / "research-state.json"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "sentinel.txt").write_text("saved", encoding="utf-8")

    monkeypatch.setattr(backtest, "_EARLY_RESEARCH_STATE_PATH", state_path)
    monkeypatch.setattr(backtest, "_early_research_run_dir", lambda **_kwargs: run_dir)
    monkeypatch.setattr(backtest, "_research_resume_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backtest, "_completed_research_symbol_shards", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        backtest,
        "run_early_movement_research",
        lambda *_args, **_kwargs: {"research": {"v91_goal": {"build_id": "TEST"}}, "symbols_completed": 1},
    )

    with backtest._early_research_lock:
        backtest._early_research_state.clear()
        backtest._early_research_state.update(backtest._default_early_research_state())

    started = backtest.start_early_movement_research(
        object(), symbols=["AAA"], timeframe="15minute", days=180,
        holdout_pct=30.0, cost_pct=0.08, slippage_pct=0.05,
        universe_is_full_fno=True, fast_v8=True, research_mode="v91_fast",
    )
    assert started["started"] is True

    deadline = time.time() + 2.0
    state = backtest.get_early_research_state()
    while time.time() < deadline:
        state = backtest.get_early_research_state()
        if state["status"] == "error" or not run_dir.exists():
            break
        time.sleep(0.01)

    state = backtest.get_early_research_state()
    assert state["status"] == "done"
    assert state["error"] is None
    assert not run_dir.exists()
