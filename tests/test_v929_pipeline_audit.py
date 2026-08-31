import datetime as dt
import time
from pathlib import Path

import pandas as pd
import pytest

from app import backtest, early_research, opportunity_forward, v91_goal


def _compact_frame(rows=32, seed=1.0):
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


def _event(symbol, ts, direction='Bullish'):
    return {
        'symbol': symbol,
        'signal_time': ts.isoformat(),
        'entry_time': ts.isoformat(),
        'direction': direction,
        'v92_accumulation_seed': direction == 'Bullish',
        'price_chg_60m_pct': 0.5 if direction == 'Bullish' else -0.5,
        'oi_chg_60m_pct': 2.0,
        'intraday_returns': {'30m': 0.1, '1h': 0.2, '2h': 0.3},
        'swing_returns': {'1D': 0.2},
    }


def test_v929_stage2_reads_each_symbol_shard_only_once(tmp_path, monkeypatch):
    symbols = ['AAA', 'BBB', 'CCC']
    shard_map = {}
    ts = pd.Timestamp('2026-08-28 10:00', tz='Asia/Kolkata')
    for i, symbol in enumerate(symbols):
        path = backtest._write_research_symbol_shard(
            tmp_path, i, symbol,
            compact_frame=_compact_frame(seed=i + 1), replay=None, note=None,
            v91_events=[_event(symbol, ts)], v91_confirmation={},
        )
        shard_map[symbol] = path

    original = backtest._load_research_symbol_shard
    calls = []

    def counted(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(backtest, '_load_research_symbol_shard', counted)
    backtest._build_v91_ranked_events_checkpoint(tmp_path, shard_map)

    counts = {path: calls.count(path) for path in set(calls)}
    assert max(counts.values()) == 1, counts


def test_v929_stage2_progress_reports_symbol_loading_and_rank_work(tmp_path):
    symbols = ['AAA', 'BBB']
    shard_map = {}
    ts = pd.Timestamp('2026-08-28 10:00', tz='Asia/Kolkata')
    for i, symbol in enumerate(symbols):
        shard_map[symbol] = backtest._write_research_symbol_shard(
            tmp_path, i, symbol,
            compact_frame=_compact_frame(seed=i + 1), replay=None, note=None,
            v91_events=[_event(symbol, ts)], v91_confirmation={},
        )
    messages = []
    backtest._build_v91_ranked_events_checkpoint(
        tmp_path, shard_map,
        stage_cb=lambda _a, _b, message, _pct: messages.append(message),
    )
    text = '\n'.join(messages)
    assert 'Loading Stage-2 inputs' in text
    assert 'OI strength' in text
    assert 'ranking' in text.lower()
    assert 'attaching' in text.lower()


def test_v929_stage2_rank_progress_is_resumable(tmp_path, monkeypatch):
    symbols = ['AAA', 'BBB']
    shard_map = {}
    ts = pd.Timestamp('2026-08-28 10:00', tz='Asia/Kolkata')
    for i, symbol in enumerate(symbols):
        shard_map[symbol] = backtest._write_research_symbol_shard(
            tmp_path, i, symbol,
            compact_frame=_compact_frame(seed=i + 1), replay=None, note=None,
            v91_events=[_event(symbol, ts)], v91_confirmation={},
        )

    original_save = backtest._save_v91_rank_progress
    saves = []

    def crash_after_first(*args, **kwargs):
        result = original_save(*args, **kwargs)
        saves.append(1)
        if len(saves) == 1:
            raise RuntimeError('simulated worker restart')
        return result

    monkeypatch.setattr(backtest, '_save_v91_rank_progress', crash_after_first)
    with pytest.raises(RuntimeError, match='simulated worker restart'):
        backtest._build_v91_ranked_events_checkpoint(tmp_path, shard_map)

    monkeypatch.setattr(backtest, '_save_v91_rank_progress', original_save)
    messages = []
    path = backtest._build_v91_ranked_events_checkpoint(
        tmp_path, shard_map,
        stage_cb=lambda _a, _b, message, _pct: messages.append(message),
    )
    payload = backtest._load_v91_ranked_events_checkpoint(path)
    assert payload['events']
    assert any('Resuming Stage 2 after 1/7 ranks' in m for m in messages)


def test_v929_forward_summary_reports_expectancy_pf_and_wilson_interval():
    state = opportunity_forward.empty_state()
    state['events'] = []
    for i, ret in enumerate([1.0, 0.5, -0.25, -0.5]):
        state['events'].append({
            'key': str(i), 'trade_date': '2026-08-31', 'direction': 'Bullish',
            'score_band': '70+',
            'outcomes': {'30m': {'directional_return_pct': ret, 'win': ret > 0}},
        })
    stats = opportunity_forward.summarize(state, today=dt.date(2026, 8, 31))['horizons']['30m']
    assert stats['avg_directional_return_pct'] == pytest.approx(0.1875)
    assert stats['profit_factor'] == pytest.approx(2.0)
    assert stats['win_rate_pct'] == 50.0
    assert stats['win_rate_ci95_low_pct'] < 50 < stats['win_rate_ci95_high_pct']
    assert stats['avg_win_pct'] == pytest.approx(0.75)
    assert stats['avg_loss_pct'] == pytest.approx(-0.375)
    summary = opportunity_forward.summarize(state, today=dt.date(2026, 8, 31))
    assert summary['distinct_trade_days'] == 1


def test_v929_bull_gate_funnel_does_not_present_duplicate_long_buildup_as_new_evidence():
    report = v91_goal.bull_accumulation_gate_funnel([])
    gates = [row['gate'] for row in report['stages']]
    assert 'price_up_oi_up' in gates
    assert 'long_buildup' not in gates
    assert report['independent_streams'] == ['price', 'volume', 'futures_oi', 'relative_strength', 'basis_when_available']


def test_v929_bear_compactor_keeps_broad_fresh_short_seed_for_single_freeze_boundary():
    raw = {
        'direction': 'Bearish', 'fresh_breakout': True,
        'price_chg_60m_pct': -1.0, 'oi_chg_60m_pct': 3.0,
        'breakout_extension_atr': 2.0,  # frozen rule will reject later
        'basis_acceleration': 0.05,     # frozen rule will reject later
        'close_position_pct': 60.0,     # frozen rule will reject later
        'signal_time': '2026-08-28T10:00:00+05:30',
    }
    rows = backtest._compact_v91_events({'v9_playbook_events': [raw]})
    assert len(rows) == 1


def test_v929_v92_protocol_declares_multiple_testing_and_survivorship_limits():
    report = early_research.v91_goal_report([], run_context={
        'setup_timeframe': '15minute', 'execution_timeframe': '15minute', 'days': 180,
        'cost_pct': 0.08, 'slippage_pct': 0.05, 'universe_is_full_fno': True,
    })
    protocol = report['protocol']
    assert protocol['historical_trials_counted'] >= 12
    assert protocol['familywise_alpha'] == pytest.approx(0.05)
    assert protocol['bonferroni_alpha'] < 0.005
    assert protocol['power_reference_55pct_vs_50pct_n'] == 782
    assert protocol['point_in_time_fno_universe_available'] is False
    assert 'survivorship' in protocol['universe_warning'].lower()


def test_v929_stage2_full_universe_210x5000_finishes_within_budget(tmp_path):
    import numpy as np

    rows = 5000
    idx = pd.date_range('2026-01-01 09:15', periods=rows, freq='15min', tz='Asia/Kolkata')
    shard_map = {}
    for i in range(210):
        symbol = f'S{i:03d}'
        frame = pd.DataFrame({
            'tod_rvol': np.full(rows, 1.0 + i / 500, dtype='float32'),
            'opening_rvol': np.full(rows, 1.1 + i / 1000, dtype='float32'),
            'bar_range_atr': np.full(rows, 0.3 + i / 2000, dtype='float32'),
            'gap_atr': np.full(rows, i / 5000, dtype='float32'),
            'turnover_notional': np.full(rows, 100000 + i, dtype='float32'),
            'oi_chg_60m_pct': np.full(rows, i / 100, dtype='float32'),
            'rs_pct': np.full(rows, i / 1000, dtype='float32'),
            'stock_sector_lead_pct': np.full(rows, i / 2000, dtype='float32'),
        }, index=idx)
        events = [_event(symbol, idx[100 + k * 300]) for k in range(10)]
        shard_map[symbol] = backtest._write_research_symbol_shard(
            tmp_path, i, symbol, compact_frame=frame, replay=None, note=None,
            v91_events=events, v91_confirmation={},
        )

    started = time.perf_counter()
    path = backtest._build_v91_ranked_events_checkpoint(tmp_path, shard_map)
    elapsed = time.perf_counter() - started
    payload = backtest._load_v91_ranked_events_checkpoint(path)

    assert payload['symbols_completed'] == 210
    assert len(payload['events']) == 2100
    # This is intentionally generous versus the local ~2-4 second runtime;
    # it catches accidental return to the 1,680-deserialization design while
    # allowing slower CI/container hosts plenty of headroom.
    assert elapsed < 30.0


def test_v929_ui_headlines_net_forward_metrics_and_audit_protocol():
    index_text = Path('app/templates/index.html').read_text(encoding='utf-8')
    backtest_text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'avg_net_return_pct' in index_text
    assert 'net_profit_factor' in index_text
    assert 'net_win_rate_ci95_low_pct' in index_text
    assert 'trading day' in index_text
    assert 'Bonferroni' in backtest_text
    assert 'reference power' in backtest_text
