import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import costs, derivative_intelligence as di, v93_component_lab


def _trial_event(day, ret, *, symbol=None):
    return {
        'symbol': symbol or f'S{day:02d}',
        'signal_time': f'2026-08-{day:02d}T10:00:00+05:30',
        'entry_time': f'2026-08-{day:02d}T10:15:00+05:30',
        'direction': 'Bullish',
        'v93_trial13_candidate': True,
        'swing_returns': {'1D': float(ret), '2D': float(ret) * 0.8},
        'intraday_returns': {'2h': float(ret) * 0.2, '4h': float(ret) * 0.4},
    }


def test_v940_trial13_prefinal_decomposition_never_exposes_final_outcomes():
    # 10 trading days => 6 dev, 2 validation, 2 locked-final days.
    vals = [0.10, 0.20, -0.05, 0.30, 0.15, -0.10, 2.00, -0.20, 99.0, 99.0]
    rows = [_trial_event(i + 1, v) for i, v in enumerate(vals)]
    report = v93_component_lab.build_report(rows, run_context={})
    pre = report['trial13']['prefinal_80']['1D']
    assert pre['trade_count'] == 8
    assert pre['avg_win_pct'] is not None
    assert pre['avg_loss_pct'] is not None
    assert pre['largest_winner_pct'] == pytest.approx(2.0)
    assert pre['avg_return_top1_removed_pct'] is not None
    assert pre['avg_return_top3_removed_pct'] is not None
    assert pre['day_bootstrap_ci95_low_pct'] is not None
    assert pre['day_bootstrap_ci95_high_pct'] is not None
    # Locked-final outcomes (the 99% sentinels) must not leak into any pre-final statistic.
    assert pre['largest_winner_pct'] < 10.0
    assert report['trial13']['final_test']['locked'] is True
    assert 'outcomes' not in report['trial13']['final_test']


def test_v940_vwap_component_accepts_numpy_and_numeric_truth_values():
    rows = []
    for i, flag in enumerate([True, np.bool_(True), 1, 1.0, False, 0.0]):
        rows.append({
            'symbol': f'S{i}', 'signal_time': f'2026-08-{i+1:02d}T10:00:00+05:30',
            'entry_time': f'2026-08-{i+1:02d}T10:15:00+05:30',
            'direction': 'Bullish', 'fresh_breakout': True, 'v93_event_type': 'fresh_breakout',
            'vwap_side_agrees': flag, 'swing_returns': {'1D': 0.1, '2D': 0.2},
            'intraday_returns': {'2h': 0.05, '4h': 0.08},
        })
    report = v93_component_lab.build_report(rows, run_context={})
    assert report['directional_components']['vwap_aligned']['event_count'] == 4


def test_v940_movement_outcomes_use_horizon_scaled_daily_atr_when_available():
    idx = []
    for d in pd.bdate_range('2026-08-24', periods=3):
        base = pd.Timestamp(d.date()).tz_localize('Asia/Kolkata') + pd.Timedelta(hours=9, minutes=15)
        idx.extend([base + pd.Timedelta(minutes=15*i) for i in range(25)])
    idx = pd.DatetimeIndex(idx)
    df = pd.DataFrame({'open':100.0,'high':100.0,'low':100.0,'close':100.0,'volume':1}, index=idx)
    df.loc[idx[30], 'high'] = 110.0
    df.loc[idx[55], 'low'] = 88.0
    out = v93_component_lab.movement_outcomes(df, signal_pos=0, atr=2.0, daily_atr=10.0)
    # Preserve old 15m ATR diagnostic, but add a correctly horizon-scaled metric.
    assert out['1D']['max_abs_move_atr'] == pytest.approx(5.0)
    assert out['1D']['max_abs_move_horizon_atr'] == pytest.approx(1.0)
    assert out['2D']['max_abs_move_horizon_atr'] == pytest.approx(12.0 / (10.0 * np.sqrt(2)), rel=1e-3)


def test_v940_universal_net_return_applies_two_sided_slippage():
    assert costs.net_return_pct(100.0, 101.0, 'Bullish', cost_pct=0.08, slippage_pct=0.05) == pytest.approx(0.82)
    assert costs.round_trip_drag_pct(0.08, 0.05) == pytest.approx(0.18)


def test_v940_option_chain_exposes_executable_atm_call_put():
    expiry = dt.date(2026, 9, 10)
    contracts = [
        {'tradingsymbol':'ABC100CE','strike':100.0,'instrument_type':'CE','expiry':expiry},
        {'tradingsymbol':'ABC100PE','strike':100.0,'instrument_type':'PE','expiry':expiry},
    ]
    quotes = {
        'NFO:ABC100CE': {'last_price':5.0,'depth':{'buy':[{'price':4.9}],'sell':[{'price':5.1}]}},
        'NFO:ABC100PE': {'last_price':4.0,'depth':{'buy':[{'price':3.9}],'sell':[{'price':4.1}]}},
    }
    out = di.analyze_option_quotes('ABC','Bullish',100.0,contracts,quotes,now=dt.datetime(2026,9,1,10,0))
    assert out['atm_call']['symbol'] == 'ABC100CE'
    assert out['atm_put']['symbol'] == 'ABC100PE'
    assert out['atm_straddle_ask'] == pytest.approx(9.2)
    assert out['atm_straddle_bid'] == pytest.approx(8.8)


def test_v940_long_vol_signal_uses_ask_entry_and_bid_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(di, 'SHADOW_STATE_FILE', str(tmp_path / 'shadow_state.json'))
    chain = {
        'atm_strike':100.0, 'expiry':'2026-09-10', 'dte':9, 'atm_iv_pct':28.0,
        'straddle_move_pct':9.0,
        'atm_call': {'symbol':'ABC100CE','ask':5.1,'bid':4.9,'mid':5.0},
        'atm_put': {'symbol':'ABC100PE','ask':4.1,'bid':3.9,'mid':4.0},
    }
    sid = di.register_long_vol_signal({'symbol':'ABC','close':100.0,'timestamp':'2026-09-01T10:00:00'}, chain, now=dt.datetime(2026,9,1,10,0))
    assert sid
    class K:
        def quote(self, keys):
            return {
                'NFO:ABC100CE': {'last_price':6.0,'depth':{'buy':[{'price':5.8}],'sell':[{'price':6.2}]}},
                'NFO:ABC100PE': {'last_price':5.0,'depth':{'buy':[{'price':4.8}],'sell':[{'price':5.2}]}},
            }
    di.resolve_shadow_outcomes(K(), now=dt.datetime(2026,9,2,10,1))
    sig = di.load_shadow_state()['signals'][0]
    out = sig['outcomes']['1D']
    # Entry cost = 5.1 + 4.1 = 9.2; executable exit = 5.8 + 4.8 = 10.6.
    assert out['entry_ask_total'] == pytest.approx(9.2)
    assert out['exit_bid_total'] == pytest.approx(10.6)
    assert out['premium_return_pct'] == pytest.approx((10.6 / 9.2 - 1) * 100, abs=0.001)
    stats = di.get_shadow_stats('magnitude')
    assert stats['1D']['count'] == 1
    assert stats['1D']['profit_factor'] == float('inf')
    assert stats['1D']['expectancy_pct'] == pytest.approx(out['premium_return_pct'])


def test_v940_trial14_is_preregistered_as_magnitude_only():
    spec = v93_component_lab.trial14_spec()
    assert spec['trial_number'] == 14
    assert spec['name'] == 'Daily OI Anomaly + Compression -> Expansion'
    assert spec['directional_prediction'] is False
    assert spec['daily_oi_z_min'] == pytest.approx(1.5)
    assert spec['compression_onset_min'] == pytest.approx(60.0)
    assert spec['primary_horizon'] == '1D'
    assert spec['secondary_horizon'] == '2D'
    assert spec['research_only'] is True


def test_v940_ui_explains_trial13_resolution_and_trial14_pivot():
    text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'V9.4 Measurement Repair' in text
    assert 'Trial 13 pre-final 80%' in text
    assert 'Trial 14' in text
    assert 'Daily OI Anomaly + Compression' in text


def _movement_event(day, value, event_type='trial14_daily_oi_compression'):
    return {
        'symbol': f'M{day:02d}',
        'signal_time': f'2026-08-{day:02d}T10:00:00+05:30',
        'entry_time': f'2026-08-{day:02d}T10:15:00+05:30',
        'v93_event_type': event_type,
        'movement_outcomes': {
            '1D': {'max_abs_move_atr': value, 'max_abs_move_horizon_atr': value},
            '2D': {'max_abs_move_atr': value * 1.1, 'max_abs_move_horizon_atr': value * 1.1},
        },
    }


def test_v940_trial14_report_never_reads_locked_final_movement_outcomes():
    rows = []
    # baseline each day, kept deliberately stable
    for d in range(1, 11):
        rows.append(_movement_event(d, 1.0, 'baseline'))
    vals = [1.2, 1.1, 1.3, 1.4, 1.2, 1.5, 1.25, 1.35, 99.0, 99.0]
    rows.extend(_movement_event(i + 1, v) for i, v in enumerate(vals))
    report = v93_component_lab.build_report(rows, run_context={})
    t14 = report['trial14']
    assert t14['candidate_count'] == 10
    assert t14['prefinal_80_candidate_count'] == 8
    assert t14['locked_final_candidate_count'] == 2
    assert t14['final_test']['locked'] is True
    assert 'outcomes' not in t14['final_test']
    assert t14['1D']['avg_max_abs_move_horizon_atr'] < 10.0
    assert t14['measurement_scope'].startswith('Development + validation')


def test_v940_v91_protocol_validates_computed_drag_not_only_parameters():
    from app import v91_goal
    ctx = {
        'setup_timeframe': '15minute', 'execution_timeframe': '15minute', 'days': 180,
        'cost_pct': 0.08, 'slippage_pct': 0.05, 'universe_is_full_fno': True,
    }
    assert v91_goal.validate_protocol(ctx)['valid'] is True
    bad = dict(ctx, slippage_pct=0.04)
    out = v91_goal.validate_protocol(bad)
    assert out['valid'] is False
    assert any('slippage assumption' in x or 'round-trip drag' in x for x in out['mismatches'])


def test_v940_daily_oi_live_cache_streams_from_research_shards(tmp_path, monkeypatch):
    import pickle
    from app import v94_magnitude
    monkeypatch.setattr(v94_magnitude, 'CACHE_FILE', tmp_path / 'v94-daily-oi-live.json')
    idx = pd.date_range('2026-08-28 09:15', periods=3, freq='15min', tz='Asia/Kolkata')
    frame = pd.DataFrame({
        'daily_oi_z_pti': [1.2, 1.7, 1.7],
        'daily_oi_chg_pct_pti': [2.0, 3.5, 3.5],
    }, index=idx)
    shard = tmp_path / '0000-ABC.pkl'
    with shard.open('wb') as fh:
        pickle.dump({'symbol':'ABC','compact_frame':frame}, fh)
    out = v94_magnitude.persist_daily_oi_snapshot_from_shards({'ABC': shard})
    assert out['symbols']['ABC']['daily_oi_z'] == pytest.approx(1.7)
    assert out['symbols']['ABC']['daily_oi_chg_pct'] == pytest.approx(3.5)
    assert v94_magnitude.load_daily_oi_snapshot()['symbols']['ABC']['daily_oi_z'] == pytest.approx(1.7)


def test_v940_trial14_live_candidate_requires_fresh_compression_and_daily_oi(monkeypatch):
    from app import v94_magnitude
    v94_magnitude.reset_live_state_for_tests()
    monkeypatch.setattr(v94_magnitude, 'load_daily_oi_snapshot', lambda: {
        'generated_at':'2026-09-01T09:00:00+05:30',
        'symbols': {'ABC': {'daily_oi_z':1.8, 'daily_oi_chg_pct':4.2, 'feature_ts':'2026-08-31T15:30:00+05:30'}}
    })
    now = dt.datetime(2026, 9, 1, 10, 0)
    # First observation initializes the edge state and must not manufacture an onset.
    assert v94_magnitude.fresh_trial14_candidates([{'symbol':'ABC','compression_score':55,'close':100}], now=now) == []
    rows = [{'symbol':'ABC','compression_score':65,'close':100,'timestamp':'2026-09-01T10:15:00+05:30'}]
    cands = v94_magnitude.fresh_trial14_candidates(rows, now=dt.datetime(2026,9,1,10,15))
    assert len(cands) == 1
    assert cands[0]['v94_trial14_live_candidate'] is True
    assert cands[0]['trial14_daily_oi_z'] == pytest.approx(1.8)
    # Remaining compressed does not fire again.
    assert v94_magnitude.fresh_trial14_candidates(rows, now=dt.datetime(2026,9,1,10,30)) == []


def test_v940_live_trial14_registers_executable_straddle(tmp_path, monkeypatch):
    from app import v94_magnitude
    monkeypatch.setattr(di, 'SHADOW_STATE_FILE', str(tmp_path / 'shadow_state.json'))
    v94_magnitude.reset_live_state_for_tests()
    monkeypatch.setattr(v94_magnitude, 'load_daily_oi_snapshot', lambda: {
        'generated_at':'2026-09-01T09:00:00+05:30',
        'symbols': {'ABC': {'daily_oi_z':1.8, 'daily_oi_chg_pct':4.2, 'feature_ts':'2026-08-31T15:30:00+05:30'}}
    })
    expiry = dt.date(2026, 9, 10)
    cmap = {'ABC': [
        {'tradingsymbol':'ABC100CE','strike':100.0,'instrument_type':'CE','expiry':expiry},
        {'tradingsymbol':'ABC100PE','strike':100.0,'instrument_type':'PE','expiry':expiry},
    ]}
    monkeypatch.setattr(di, 'get_option_contracts_map', lambda kite: cmap)
    class K:
        def quote(self, keys):
            return {
                'NFO:ABC100CE': {'last_price':5.0,'depth':{'buy':[{'price':4.9}],'sell':[{'price':5.1}]}},
                'NFO:ABC100PE': {'last_price':4.0,'depth':{'buy':[{'price':3.9}],'sell':[{'price':4.1}]}},
            }
    # initialize below compression then create the onset
    v94_magnitude.fresh_trial14_candidates([{'symbol':'ABC','compression_score':55,'close':100}], now=dt.datetime(2026,9,1,10,0))
    rows = [{'symbol':'ABC','compression_score':65,'close':100,'timestamp':'2026-09-01T10:15:00+05:30'}]
    out = v94_magnitude.register_live_trial14_straddles(K(), rows, now=dt.datetime(2026,9,1,10,15))
    assert out['registered'] == 1
    state = di.load_shadow_state()
    sig = state['signals'][0]
    assert sig['signal_kind'] == 'magnitude'
    assert sig['entry_ask_total'] == pytest.approx(9.2)
    assert rows[0]['v94_trial14_shadow_registered'] is True


def test_v940_research_cache_and_live_registration_are_wired_without_production_gate_changes():
    backtest_text = Path('app/backtest.py').read_text(encoding='utf-8')
    bg_text = Path('app/background.py').read_text(encoding='utf-8')
    assert 'v94_magnitude.persist_daily_oi_snapshot_from_shards(completed_shards)' in backtest_text
    assert 'v94_magnitude.register_live_trial14_straddles' in bg_text
    assert 'daily_oi_z_pti' in backtest_text
    # Magnitude research must not be used as a production shortlist bridge.
    assert 'v94_trial14_live_candidate' not in bg_text


def test_v940_trial14_generic_movement_table_also_excludes_locked_final():
    rows = []
    for d in range(1, 11):
        rows.append(_movement_event(d, 1.0, 'baseline'))
    vals = [1.2, 1.1, 1.3, 1.4, 1.2, 1.5, 1.25, 1.35, 99.0, 99.0]
    rows.extend(_movement_event(i + 1, v) for i, v in enumerate(vals))
    report = v93_component_lab.build_report(rows, run_context={})
    generic = report['movement_components']['trial14_daily_oi_compression']['1D']
    assert generic['event_count'] == 8
    assert generic['avg_max_abs_move_horizon_atr'] < 10.0
