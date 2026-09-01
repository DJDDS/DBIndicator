import pandas as pd
import pytest

from app import backtest


def _compact_frame(rows=64, seed=1.0):
    idx = pd.date_range('2026-08-28 09:15', periods=rows, freq='15min', tz='Asia/Kolkata')
    return pd.DataFrame({
        'tod_rvol': [1.0 + seed * 0.01] * rows,
        'opening_rvol': [1.1] * rows,
        'bar_range_atr': [0.4] * rows,
        'gap_atr': [0.1] * rows,
        'turnover_notional': [100000.0 + seed] * rows,
        'oi_chg_60m_pct': [seed] * rows,
        'rs_pct': [seed * 0.1] * rows,
        'stock_sector_lead_pct': [seed * 0.05] * rows,
    }, index=idx).astype('float32')


def _event(symbol, ts):
    return {
        'symbol': symbol,
        'signal_time': ts.isoformat(),
        'entry_time': ts.isoformat(),
        'direction': 'Bullish',
        'v92_accumulation_seed': True,
        'price_chg_60m_pct': 0.5,
        'oi_chg_60m_pct': 2.0,
        'intraday_returns': {'30m': 0.1, '1h': 0.2, '2h': 0.3},
        'swing_returns': {'1D': 0.2, '2D': 0.3},
    }


def test_v935_stage2_uses_lean_rank_feature_shards_instead_of_retaining_all_frames(tmp_path, monkeypatch):
    symbols = ['AAA', 'BBB', 'CCC']
    shard_map = {}
    ts = pd.Timestamp('2026-08-28 10:00', tz='Asia/Kolkata')
    for i, symbol in enumerate(symbols):
        shard_map[symbol] = backtest._write_research_symbol_shard(
            tmp_path, i, symbol,
            compact_frame=_compact_frame(seed=i + 1), replay=None, note=None,
            v91_events=[_event(symbol, ts)], v91_confirmation={},
        )

    full_loads = []
    lean_loads = []
    original_full = backtest._load_research_symbol_shard
    original_lean = backtest._load_v91_rank_feature_shard

    def counted_full(path):
        full_loads.append(str(path))
        return original_full(path)

    def counted_lean(path):
        lean_loads.append(str(path))
        return original_lean(path)

    monkeypatch.setattr(backtest, '_load_research_symbol_shard', counted_full)
    monkeypatch.setattr(backtest, '_load_v91_rank_feature_shard', counted_lean)

    path = backtest._build_v91_ranked_events_checkpoint(tmp_path, shard_map)
    payload = backtest._load_v91_ranked_events_checkpoint(path)

    assert payload['symbols_completed'] == 3
    assert len(full_loads) == 3  # one heavy deserialisation per symbol
    assert len(lean_loads) >= 3 * 7  # ranks are built from compact rank-only shards
    assert backtest._v91_rank_feature_dir(tmp_path).exists()


def test_v935_stage2_input_checkpoint_resumes_without_reloading_heavy_symbol_shards(tmp_path, monkeypatch):
    symbols = ['AAA', 'BBB']
    shard_map = {}
    ts = pd.Timestamp('2026-08-28 10:00', tz='Asia/Kolkata')
    for i, symbol in enumerate(symbols):
        shard_map[symbol] = backtest._write_research_symbol_shard(
            tmp_path, i, symbol,
            compact_frame=_compact_frame(seed=i + 1), replay=None, note=None,
            v91_events=[_event(symbol, ts)], v91_confirmation={},
        )

    original_save = backtest._save_v91_input_progress
    calls = []

    def crash_after_input_checkpoint(*args, **kwargs):
        result = original_save(*args, **kwargs)
        calls.append(kwargs.copy())
        if not kwargs.get('completed_rank_keys'):
            raise RuntimeError('simulated restart after lean-input preparation')
        return result

    monkeypatch.setattr(backtest, '_save_v91_input_progress', crash_after_input_checkpoint)
    with pytest.raises(RuntimeError, match='simulated restart'):
        backtest._build_v91_ranked_events_checkpoint(tmp_path, shard_map)

    monkeypatch.setattr(backtest, '_save_v91_input_progress', original_save)

    def forbidden_heavy_load(_path):
        raise AssertionError('heavy Stage-1 shard should not be reloaded after input checkpoint')

    monkeypatch.setattr(backtest, '_load_research_symbol_shard', forbidden_heavy_load)
    path = backtest._build_v91_ranked_events_checkpoint(tmp_path, shard_map)
    payload = backtest._load_v91_ranked_events_checkpoint(path)
    assert payload['events']


def test_v935_keeps_v934_resume_schema_so_saved_210_symbol_shards_are_reusable():
    assert backtest._RESEARCH_RESUME_SCHEMA == 'v934-resume-shards-1'
