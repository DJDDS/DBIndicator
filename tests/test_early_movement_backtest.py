import numpy as np
import pandas as pd
import pytest

from app import indicators
from app.early_movement import score_candidate


def _frame(periods=140, freq='15min', squeeze=True):
    idx = pd.date_range('2026-08-20 09:15', periods=periods, freq=freq)
    # Keep one continuous synthetic session-like series; feature helpers do
    # not require exchange calendars for these unit tests.
    base = 100 + np.linspace(0, 1.2, periods)
    if squeeze:
        amp = np.r_[np.full(periods - 20, 1.2), np.linspace(0.35, 0.18, 20)]
    else:
        amp = np.r_[np.full(periods - 20, 0.6), np.linspace(1.5, 2.2, 20)]
    wave = np.sin(np.arange(periods) / 2.2) * amp
    close = base + wave
    open_ = close - np.sin(np.arange(periods)) * 0.05
    high = np.maximum(open_, close) + amp * 0.35
    low = np.minimum(open_, close) - amp * 0.35
    volume = np.full(periods, 1000.0)
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume}, index=idx)


def test_compression_metrics_reward_true_coil_and_penalize_expansion():
    squeezed = indicators.compute_compression_metrics(_frame(squeeze=True))
    expanded = indicators.compute_compression_metrics(_frame(squeeze=False))
    assert squeezed['compression_score'].iloc[-1] >= 55
    assert squeezed['bb_width_percentile'].iloc[-1] < expanded['bb_width_percentile'].iloc[-1]
    assert squeezed['atr_compression_ratio'].iloc[-1] < expanded['atr_compression_ratio'].iloc[-1]
    assert squeezed['compression_score'].iloc[-1] > expanded['compression_score'].iloc[-1]


def test_energy_building_stage_can_exist_before_directional_trigger():
    row = {
        'direction': 'Bullish',
        'entry_trigger': None, 'entry_trigger_bars_ago': None,
        'trend_state': 'Bullish', 'macd_agrees': True, 'macd_hist_agrees': True,
        'htf_agrees': True, 'vwap_side_agrees': True, 'entry_is_extended': False,
        'oi_agrees': True, 'oi_chg_30m_pct': 0.7, 'oi_chg_60m_pct': 1.1,
        'oi_acceleration': 0.35, 'tod_rvol': 1.15, 'tod_rvol_accel': 1.18,
        'rs_pct': 0.2, 'rs_improving': True, 'sector_agrees': True,
        'compression_score': 82, 'energy_building': True,
        'momentum_inflection_agrees': True,
    }
    out = score_candidate(row)
    assert out['eligible'] is False
    assert out['stage'] == 'Energy Building'
    assert out['compression_score'] == 82


def test_best_entry_uses_compression_and_momentum_inflection_as_independent_evidence():
    row = {
        'direction': 'Bullish',
        'entry_trigger': 'Bullish', 'entry_trigger_bars_ago': 0,
        'trend_state': 'Bullish', 'macd_agrees': True, 'macd_hist_agrees': True,
        'htf_agrees': True, 'vwap_side_agrees': True, 'entry_is_extended': False,
        'oi_agrees': True, 'oi_chg_30m_pct': 1.0, 'oi_chg_60m_pct': 1.8,
        'oi_acceleration': 0.5, 'tod_rvol': 1.5, 'tod_rvol_accel': 1.2,
        'rs_pct': 0.7, 'rs_improving': True, 'rs_acceleration': 0.25,
        'sector_agrees': True,
        'compression_score': 74, 'energy_building': True,
        'momentum_inflection_agrees': True,
        'rsi_spread_slope': 0.8, 'macd_hist_slope': 0.04,
    }
    out = score_candidate(row)
    assert out['eligible'] is True
    assert out['stage'] == 'Best Entry'
    ids = {p['id'] for p in out['parts']}
    assert 'compression' in ids
    assert 'momentum' in ids


def test_research_energy_summary_measures_move_probability_not_directional_return():
    from app.early_research import summarize_energy_events
    events = [
        {'future_abs_move_atr': {4: 1.2, 8: 1.5}},
        {'future_abs_move_atr': {4: 0.7, 8: 1.1}},
        {'future_abs_move_atr': {4: 0.4, 8: 0.6}},
    ]
    out = summarize_energy_events(events, horizons=(4, 8), move_atr=1.0)
    assert out['4']['event_count'] == 3
    assert out['4']['move_hit_rate_pct'] == pytest.approx(33.3, abs=0.1)
    assert out['8']['move_hit_rate_pct'] == pytest.approx(66.7, abs=0.1)


def test_component_research_ranks_by_holdout_expectancy_not_win_rate():
    from app.early_research import rank_component_results
    rows = [
        {'component': 'compression', 'holdout_avg_return_pct': 0.12, 'holdout_profit_factor': 1.3, 'holdout_win_rate_pct': 45.0},
        {'component': 'macd_state', 'holdout_avg_return_pct': -0.02, 'holdout_profit_factor': 0.95, 'holdout_win_rate_pct': 61.0},
    ]
    ranked = rank_component_results(rows)
    assert ranked[0]['component'] == 'compression'


def test_feature_frame_contains_backtestable_early_axes():
    from app.early_research import build_feature_frame
    df = _frame(periods=180, squeeze=True)
    oi = pd.Series(np.linspace(1000, 1250, len(df)), index=df.index)
    idx = df.copy()
    idx['close'] = np.linspace(100, 100.5, len(df))
    feat = build_feature_frame(df, '15minute', oi_series=oi, index_df=idx)
    for col in (
        'compression_score', 'energy_building', 'oi_chg_30m_pct', 'oi_chg_60m_pct',
        'oi_acceleration', 'tod_rvol', 'rsi_spread_slope', 'macd_hist_slope',
        'momentum_inflection_agrees', 'rs_pct', 'rs_acceleration', 'vwap_side_agrees',
    ):
        assert col in feat.columns
    assert feat['compression_score'].notna().any()


def test_chronological_split_keeps_later_events_only_in_holdout():
    from app.early_research import chronological_split
    events = [
        {'entry_time': '2026-08-01T10:00:00'},
        {'entry_time': '2026-08-02T10:00:00'},
        {'entry_time': '2026-08-03T10:00:00'},
        {'entry_time': '2026-08-04T10:00:00'},
    ]
    train, hold = chronological_split(events, holdout_pct=25)
    assert [e['entry_time'] for e in train] == [e['entry_time'] for e in events[:3]]
    assert [e['entry_time'] for e in hold] == [events[-1]['entry_time']]


def test_directional_summary_reports_expectancy_and_profit_factor():
    from app.early_research import summarize_directional_events
    events = [
        {'returns_pct': {3: 1.0}, 'direction': 'Bullish'},
        {'returns_pct': {3: -0.5}, 'direction': 'Bullish'},
        {'returns_pct': {3: 0.5}, 'direction': 'Bearish'},
    ]
    out = summarize_directional_events(events, horizons=(3,))['3']
    assert out['trade_count'] == 3
    assert out['avg_return_pct'] == pytest.approx(0.333, abs=0.001)
    assert out['profit_factor'] == pytest.approx(3.0, abs=0.01)


def test_sensitivity_table_changes_only_one_threshold_and_uses_holdout():
    from app.early_research import sensitivity_table
    events = []
    for i in range(100):
        events.append({
            'entry_time': f'2026-08-{1 + i//10:02d}T{9 + (i%10)//4:02d}:{15*((i%4)+1):02d}:00',
            'returns_pct': {3: 0.5 if i >= 70 else -0.1},
            'compression_score': float(i),
        })
    rows = sensitivity_table(events, 'compression_score', [50, 70, 80], horizon=3, holdout_pct=30)
    assert [r['threshold'] for r in rows] == [50, 70, 80]
    assert all('holdout_avg_return_pct' in r for r in rows)


def test_replay_feature_frame_separates_energy_ignition_and_best_entry():
    from app.early_research import replay_feature_frame
    idx = pd.date_range('2026-08-28 10:00', periods=7, freq='15min')
    df = pd.DataFrame({
        'open': [100, 100, 101, 103, 104, 105, 106],
        'high': [101, 101, 102, 104.5, 105.5, 106.5, 107],
        'low':  [99, 99.5, 100.5, 102.5, 103.5, 104.5, 105.5],
        'close':[100, 100.5, 101.5, 104, 105, 106, 106.5],
        'volume':[1000]*7,
    }, index=idx)
    feat = pd.DataFrame(index=idx)
    feat['direction'] = 'Bullish'
    feat['compression_score'] = [0, 80, 75, 50, 40, 30, 20]
    feat['energy_building'] = [False, True, True, False, False, False, False]
    feat['entry_trigger'] = [None, None, 'Bullish', None, None, None, None]
    feat['entry_trigger_bars_ago'] = [np.nan, np.nan, 0, np.nan, np.nan, np.nan, np.nan]
    feat['trend_state'] = 'Bullish'
    feat['macd_agrees'] = True
    feat['macd_hist_agrees'] = True
    feat['momentum_inflection_agrees'] = True
    feat['htf_agrees'] = True
    feat['vwap_side_agrees'] = True
    feat['entry_is_extended'] = False
    feat['oi_agrees'] = True
    feat['oi_recent_agrees'] = True
    feat['oi_chg_30m_pct'] = 1.0
    feat['oi_chg_60m_pct'] = 1.5
    feat['oi_acceleration'] = 0.4
    feat['oi_z'] = 1.8
    feat['tod_rvol'] = 1.5
    feat['tod_rvol_accel'] = 1.2
    feat['vol_rising'] = True
    feat['rs_pct'] = 0.7
    feat['rs_improving'] = True
    feat['rs_acceleration'] = 0.3
    feat['sector_agrees'] = True
    feat['breakout_state'] = None
    feat['atr'] = 1.0
    out = replay_feature_frame(df, feat, 'AAA', horizons=(1, 2, 3), cost_pct=0.05, slippage_pct=0.02)
    assert len(out['energy_events']) == 1
    assert len(out['ignition_events']) == 1
    assert len(out['best_entry_events']) == 1
    ev = out['best_entry_events'][0]
    assert ev['entry_time'] == idx[3].isoformat()
    assert ev['returns_pct'][1] > 0
    assert ev['movement_score'] >= 72


def test_aggregate_research_exposes_stage_holdout_and_threshold_sensitivity():
    from app.early_research import aggregate_research
    replay = {
        'energy_events': [
            {'entry_time': f'2026-08-{d:02d}T10:00:00', 'future_abs_move_atr': {4: 1.2, 8: 1.4}, 'compression_score': 70}
            for d in range(1, 6)
        ],
        'ignition_events': [], 'best_entry_events': [],
    }
    for i in range(100):
        e = {
            'entry_time': f'2026-08-{1 + i//10:02d}T10:{(i%10)*5:02d}:00',
            'direction': 'Bullish', 'returns_pct': {3: 0.3 if i >= 70 else -0.05},
            'movement_score': 65 + (i % 20), 'compression_score': 50 + (i % 40),
            'oi_chg_60m_pct': (i % 5) * 0.5, 'oi_acceleration': 0.2,
            'tod_rvol': 1.0 + (i % 5) * 0.1, 'tod_rvol_accel': 1.1,
            'momentum_inflection_agrees': True, 'rs_acceleration_directional': 0.2,
            'vwap_side_agrees': True, 'entry_is_extended': False,
        }
        replay['ignition_events'].append(e)
        if e['movement_score'] >= 72:
            replay['best_entry_events'].append(e)
    out = aggregate_research([replay], holdout_pct=30, ref_horizon=3)
    assert 'energy' in out and 'ignition' in out and 'best_entry' in out
    assert 'holdout' in out['best_entry']
    assert 'compression_score' in out['sensitivity']
    assert 'tod_rvol' in out['sensitivity']
    assert out['ref_horizon'] == 3


def test_run_early_movement_research_returns_holdout_and_sensitivity(monkeypatch):
    from app import backtest
    monkeypatch.setattr(backtest, '_load_instrument_map', lambda _k: {'AAA': 1})
    monkeypatch.setattr(backtest, '_load_index_token', lambda _k, _s: 99)
    df = _frame(periods=180, squeeze=True)
    monkeypatch.setattr(backtest, '_fetch_history', lambda token, timeframe, days, kite: df.copy())
    monkeypatch.setattr(backtest, '_fetch_oi_history_for_backtest', lambda *a, **k: pd.Series(np.linspace(1000, 1300, len(df)), index=df.index))
    fake_replay = {
        'energy_events': [{'entry_time': '2026-08-28T10:00:00', 'future_abs_move_atr': {4: 1.2, 8: 1.4}, 'compression_score': 70}],
        'ignition_events': [], 'best_entry_events': [],
    }
    monkeypatch.setattr(backtest.early_research, 'replay_feature_frame', lambda *a, **k: fake_replay)
    out = backtest.run_early_movement_research(object(), symbols=['AAA'], days=30)
    assert out['timeframe'] == '15minute'
    assert out['symbols_scanned'] == 1
    assert out['research']['ref_horizon'] == 3
    assert 'sensitivity' in out['research']


def test_dashboard_exposes_early_radar_before_best_entries():
    text = open('app/templates/index.html', encoding='utf-8').read()
    assert 'Early Radar' in text
    assert 'Energy Building' in text
    assert text.index('Early Radar') < text.index('Best Entries')


def test_feature_frame_backtests_htf_and_sector_context_when_history_is_supplied():
    from app.early_research import build_feature_frame
    df = _frame(periods=720, squeeze=False)
    # A sector series with the same timestamps but a steadier upward path.
    sector = df.copy()
    sector['close'] = 90 + np.linspace(0, 8, len(df)) + np.sin(np.arange(len(df)) / 8) * 0.2
    sector['open'] = sector['close'].shift(1).fillna(sector['close'].iloc[0])
    sector['high'] = np.maximum(sector['open'], sector['close']) + 0.2
    sector['low'] = np.minimum(sector['open'], sector['close']) - 0.2
    oi = pd.Series(np.linspace(1000, 1800, len(df)), index=df.index)
    feat = build_feature_frame(df, '15minute', oi_series=oi, index_df=df, sector_df=sector)
    assert feat['htf_agrees'].notna().any()
    assert feat['sector_agrees'].notna().any()


def test_run_early_movement_research_supplies_sector_history(monkeypatch):
    from app import backtest
    monkeypatch.setattr(backtest, '_load_instrument_map', lambda _k: {'AAA': 1})
    monkeypatch.setattr(backtest, '_load_index_token', lambda _k, _s: 99)
    monkeypatch.setattr(backtest.scanner_mod, 'SYMBOL_SECTOR_MAP', {'AAA': 'NIFTY IT'})
    df = _frame(periods=180, squeeze=True)
    monkeypatch.setattr(backtest, '_fetch_history', lambda token, timeframe, days, kite: df.copy())
    monkeypatch.setattr(backtest, '_fetch_oi_history_for_backtest', lambda *a, **k: pd.Series(np.linspace(1000, 1300, len(df)), index=df.index))
    seen = {'sector': False}
    original = backtest.early_research.build_feature_frame
    def capture(*args, **kwargs):
        seen['sector'] = kwargs.get('sector_df') is not None
        return original(*args, **kwargs)
    monkeypatch.setattr(backtest.early_research, 'build_feature_frame', capture)
    monkeypatch.setattr(backtest.early_research, 'replay_feature_frame', lambda *a, **k: {'energy_events': [], 'ignition_events': [], 'best_entry_events': []})
    backtest.run_early_movement_research(object(), symbols=['AAA'], days=30)
    assert seen['sector'] is True
