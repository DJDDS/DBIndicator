import math
from pathlib import Path

import pandas as pd
import pytest

from app import v93_component_lab


def _event(i, ret1d=0.2, ret2d=0.3, *, direction='Bullish', trial=True, event_type=None):
    day = 1 + (i % 20)
    row = {
        'symbol': f'S{i:03d}',
        'signal_time': f'2026-08-{day:02d}T10:00:00+05:30',
        'entry_time': f'2026-08-{day:02d}T10:15:00+05:30',
        'direction': direction,
        'intraday_returns': {'2h': ret1d / 2.0, '4h': ret1d * 0.75},
        'swing_returns': {'1D': ret1d, '2D': ret2d},
        'mfe_atr': {'1D': 1.2, '2D': 1.8},
        'mae_atr': {'1D': 0.5, '2D': 0.7},
        'v93_trial13_candidate': trial,
        'v93_event_type': event_type,
    }
    return row


def test_v930_trial13_is_preregistered_before_results():
    spec = v93_component_lab.trial13_spec()
    assert spec['trial_number'] == 13
    assert spec['name'] == 'Silent OI Build -> Ignition'
    assert spec['oi_z_min'] == pytest.approx(1.5)
    assert spec['price_flat_max_atr'] == pytest.approx(0.5)
    assert spec['lead_window_bars'] == 4
    assert spec['max_entry_extension_atr'] == pytest.approx(1.25)
    assert spec['absolute_regime_gate'] == 'NIFTY 8-bar return sign must agree with breakout direction'
    assert spec['primary_horizon'] == '1D'
    assert spec['secondary_horizon'] == '2D'
    assert spec['final_20_locked'] is True
    assert spec['bonferroni_alpha'] == pytest.approx(0.05 / 13.0)


def test_v930_daily_oi_point_in_time_mapping_never_uses_same_morning_close():
    daily_idx = pd.to_datetime(['2026-08-27', '2026-08-28', '2026-08-31']).tz_localize('Asia/Kolkata')
    daily = pd.Series([100.0, 110.0, 132.0], index=daily_idx)
    intraday_idx = pd.DatetimeIndex([
        pd.Timestamp('2026-08-28 09:15', tz='Asia/Kolkata'),
        pd.Timestamp('2026-08-28 15:15', tz='Asia/Kolkata'),
        pd.Timestamp('2026-08-31 09:15', tz='Asia/Kolkata'),
    ])
    mapped = v93_component_lab.point_in_time_daily_oi_features(daily, intraday_idx, min_obs=1, window=2)
    # 28-Aug intraday can only see 27-Aug completed daily OI, never 28-Aug OI.
    assert mapped.loc[intraday_idx[0], 'daily_oi_level_pti'] == pytest.approx(100.0)
    assert mapped.loc[intraday_idx[1], 'daily_oi_level_pti'] == pytest.approx(100.0)
    # Monday can see Friday's completed OI.
    assert mapped.loc[intraday_idx[2], 'daily_oi_level_pti'] == pytest.approx(110.0)


def test_v930_trial13_report_keeps_final_20_locked_and_uses_1d_2d_primary():
    rows = [_event(i, ret1d=0.25 if i % 3 else -0.10, ret2d=0.35 if i % 3 else -0.15) for i in range(50)]
    report = v93_component_lab.build_report(rows, run_context={
        'history_coverage': {'oi_bar_coverage_pct': 35.2},
        'daily_oi_coverage': {'symbols_with_daily_oi': 210, 'symbols_measured': 210},
    })
    trial = report['trial13']
    assert trial['candidate_count'] == 50
    assert trial['1D']['validation']['trade_count'] == 8
    assert trial['2D']['validation']['trade_count'] == 8
    assert trial['final_test']['locked'] is True
    assert trial['primary_horizon'] == '1D'
    assert trial['secondary_horizon'] == '2D'
    assert report['protocol']['historical_trials_counted'] == 13
    assert report['protocol']['bonferroni_alpha'] == pytest.approx(0.05 / 13.0)


def test_v930_directional_stats_report_expectancy_pf_ci_days_and_excursions():
    rows = [
        _event(0, 0.50, 0.80), _event(1, 0.25, 0.40),
        _event(2, -0.20, -0.30), _event(3, -0.10, -0.20),
    ]
    stats = v93_component_lab.directional_stats(rows, 'swing_returns', '1D')
    assert stats['trade_count'] == 4
    assert stats['avg_return_pct'] == pytest.approx(0.1125)
    assert stats['profit_factor'] == pytest.approx(0.75 / 0.30)
    assert stats['win_rate_pct'] == 50.0
    assert stats['win_rate_ci95_low_pct'] < 50 < stats['win_rate_ci95_high_pct']
    assert stats['distinct_days'] == 4
    assert stats['avg_mfe_atr'] == pytest.approx(1.2)
    assert stats['avg_mae_atr'] == pytest.approx(0.5)


def test_v930_directionless_component_reports_expansion_lift_vs_baseline():
    baseline = []
    silent = []
    for i in range(20):
        baseline.append({
            'symbol': f'B{i}', 'signal_time': f'2026-08-{1 + i%20:02d}T11:00:00+05:30',
            'v93_event_type': 'baseline',
            'movement_outcomes': {'1D': {'max_abs_move_atr': 0.8}, '2D': {'max_abs_move_atr': 1.0}},
        })
        silent.append({
            'symbol': f'O{i}', 'signal_time': f'2026-08-{1 + i%20:02d}T10:00:00+05:30',
            'v93_event_type': 'silent_oi',
            'movement_outcomes': {'1D': {'max_abs_move_atr': 1.6}, '2D': {'max_abs_move_atr': 2.0}},
        })
    report = v93_component_lab.build_report(baseline + silent, run_context={})
    comp = report['movement_components']['silent_oi']
    assert comp['1D']['avg_max_abs_move_atr'] == pytest.approx(1.6)
    assert comp['1D']['lift_vs_baseline'] == pytest.approx(2.0)
    assert comp['2D']['lift_vs_baseline'] == pytest.approx(2.0)


def test_v930_ui_has_component_lab_button_and_trial13_language():
    text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'Run V9.3 Anticipation Lab' in text
    assert 'Silent OI Build' in text
    assert 'Component Edge Laboratory' in text
    assert 'final 20% locked' in text.lower()

def test_v930_silent_oi_state_and_trial13_candidate_logic_are_fixed_not_tuned():
    assert v93_component_lab.is_silent_oi_state({'oi_z': 1.6, 'price_move_60m_atr': 0.4}) is True
    assert v93_component_lab.is_silent_oi_state({'oi_z': 1.4, 'price_move_60m_atr': 0.4}) is False
    assert v93_component_lab.is_silent_oi_state({'oi_z': 1.6, 'price_move_60m_atr': 0.6}) is False
    assert v93_component_lab.absolute_regime_aligned('Bullish', 0.01) is True
    assert v93_component_lab.absolute_regime_aligned('Bearish', -0.01) is True
    assert v93_component_lab.absolute_regime_aligned('Bearish', 0.01) is False
    event = {
        'v93_silent_oi_lead': True,
        'v93_silent_oi_lead_bars': 3,
        'entry_is_extended': False,
        'v93_absolute_regime_aligned': True,
    }
    assert v93_component_lab.is_trial13_candidate(event) is True
    event['entry_is_extended'] = True
    assert v93_component_lab.is_trial13_candidate(event) is False

def test_v930_movement_outcomes_measure_directionless_expansion_from_next_bar():
    idx = pd.date_range('2026-08-25 09:15', periods=25*3, freq='15min', tz='Asia/Kolkata')
    # Force each 25 bars to be a separate trading session for the helper.
    idx = pd.DatetimeIndex(
        [pd.Timestamp('2026-08-25 09:15', tz='Asia/Kolkata') + pd.Timedelta(minutes=15*i) for i in range(25)] +
        [pd.Timestamp('2026-08-26 09:15', tz='Asia/Kolkata') + pd.Timedelta(minutes=15*i) for i in range(25)] +
        [pd.Timestamp('2026-08-27 09:15', tz='Asia/Kolkata') + pd.Timedelta(minutes=15*i) for i in range(25)]
    )
    close = [100.0] * len(idx)
    high = [100.0] * len(idx)
    low = [100.0] * len(idx)
    open_ = [100.0] * len(idx)
    # Signal pos 0 -> entry pos 1 at 100.  Within 1D window create +2 ATR move.
    high[30] = 104.0
    low[30] = 99.0
    df = pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close, 'volume': 1}, index=idx)
    out = v93_component_lab.movement_outcomes(df, signal_pos=0, atr=2.0)
    assert out['1D']['max_abs_move_atr'] == pytest.approx(2.0)
    assert out['2D']['max_abs_move_atr'] == pytest.approx(2.0)

def _session_index(start='2026-08-10', sessions=10, bars=25):
    days = pd.bdate_range(start, periods=sessions)
    out = []
    for d in days:
        base = pd.Timestamp(d.date()).tz_localize('Asia/Kolkata') + pd.Timedelta(hours=9, minutes=15)
        out.extend([base + pd.Timedelta(minutes=15*i) for i in range(bars)])
    return pd.DatetimeIndex(out)


def test_v930_feature_frame_exposes_price_flat_atr_and_point_in_time_daily_oi():
    from app import early_research
    import numpy as np
    idx = _session_index(sessions=12)
    base = np.linspace(100.0, 103.0, len(idx))
    df = pd.DataFrame({
        'open': base, 'high': base + 0.4, 'low': base - 0.4,
        'close': base + np.sin(np.arange(len(idx))/10)*0.1,
        'volume': np.full(len(idx), 100000.0),
    }, index=idx)
    oi = pd.Series(np.linspace(1_000_000, 1_050_000, len(idx)), index=idx)
    daily_idx = pd.bdate_range('2026-07-15', periods=35, tz='Asia/Kolkata')
    daily_oi = pd.Series(np.linspace(900_000, 1_200_000, len(daily_idx)), index=daily_idx)
    feat = early_research.build_feature_frame(df, '15minute', oi_series=oi, daily_oi_series=daily_oi)
    assert 'price_move_60m_atr' in feat.columns
    assert 'daily_oi_z_pti' in feat.columns
    assert 'daily_oi_chg_pct_pti' in feat.columns
    assert feat['price_move_60m_atr'].notna().sum() > 0
    # Daily point-in-time value should be populated for intraday bars once history exists.
    assert feat['daily_oi_level_pti'].notna().sum() > 0

def test_v930_replay_links_silent_oi_to_first_regime_aligned_ignition():
    from app import early_research
    import numpy as np
    idx = _session_index(start='2026-08-24', sessions=4)
    n = len(idx)
    price = np.full(n, 100.0)
    df = pd.DataFrame({
        'open': price, 'high': price + 0.5, 'low': price - 0.5,
        'close': price, 'volume': np.full(n, 100000.0),
    }, index=idx)
    feat = pd.DataFrame(index=idx)
    feat['atr'] = 1.0
    feat['energy_building'] = False
    feat['compression_score'] = 50.0
    feat['price_chg_60m_pct'] = 0.0
    feat['price_move_60m_atr'] = 0.8
    feat['oi_z'] = 0.0
    feat['oi_chg_30m_pct'] = 0.0
    feat['oi_chg_60m_pct'] = 0.0
    feat['oi_acceleration'] = 0.0
    feat['daily_oi_z_pti'] = 0.0
    feat['daily_oi_chg_pct_pti'] = 0.0
    feat['tod_rvol'] = 1.0
    feat['opening_rvol'] = 1.0
    feat['bar_range_atr'] = 1.0
    feat['gap_atr'] = 0.0
    feat['rs_pct'] = 0.0
    feat['rs_acceleration'] = 0.0
    feat['index_ret_8_pct'] = 0.0
    feat['index_vol_20bar_pct'] = 0.1
    feat['sector_rank_percentile'] = 50.0
    feat['stock_sector_lead_pct'] = 0.0
    feat['basis_pct'] = 0.0
    feat['basis_acceleration'] = 0.0
    feat['bull_vwap_available'] = True
    feat['bull_above_vwap'] = True
    feat['vwap_side_agrees'] = True
    feat['vwap_distance_atr'] = 0.1
    feat['vwap_proximity_quality'] = True
    feat['entry_is_extended'] = False
    feat['oi_recent_agrees'] = True
    feat['sector_agrees'] = True
    feat['htf_agrees'] = True
    feat['fresh_breakout'] = False
    feat['breakout_direction'] = None
    feat['breakout_source'] = None
    feat['breakout_level'] = np.nan
    feat['breakout_extension_atr'] = np.nan
    feat['retained_breakout_direction'] = None
    feat['retained_breakout_source'] = None
    feat['retained_breakout_level'] = np.nan
    feat['retained_breakout_extension_atr'] = np.nan
    feat['breakout_retained'] = False
    feat['breakout_retest_confirmed'] = False
    feat['failed_breakout_direction'] = None
    feat['failed_breakout_source'] = None
    feat['failed_breakout_level'] = np.nan
    feat['failed_breakout_extension_atr'] = np.nan
    feat['failed_breakout_vwap_reject'] = False
    # Silent positioning setup on bar 8.
    feat.iloc[8, feat.columns.get_loc('oi_z')] = 2.0
    feat.iloc[8, feat.columns.get_loc('price_move_60m_atr')] = 0.25
    feat.iloc[8, feat.columns.get_loc('oi_chg_60m_pct')] = 0.8
    # First ignition two bars later, bullish and regime-aligned.
    feat.iloc[10, feat.columns.get_loc('fresh_breakout')] = True
    feat.iloc[10, feat.columns.get_loc('breakout_direction')] = 'Bullish'
    feat.iloc[10, feat.columns.get_loc('breakout_source')] = 'Recent Range'
    feat.iloc[10, feat.columns.get_loc('breakout_level')] = 100.0
    feat.iloc[10, feat.columns.get_loc('breakout_extension_atr')] = 0.4
    feat.iloc[10, feat.columns.get_loc('index_ret_8_pct')] = 0.2
    feat.iloc[10, feat.columns.get_loc('tod_rvol')] = 1.5
    feat.iloc[10, feat.columns.get_loc('oi_chg_60m_pct')] = 0.9

    replay = early_research.replay_feature_frame(
        df, feat, 'TEST', cost_pct=0.08, slippage_pct=0.05,
        setup_timeframe='15minute', fast_v8=True,
    )
    rows = replay['v9_playbook_events']
    silent = [e for e in rows if e.get('v93_event_type') == 'silent_oi']
    trial = [e for e in rows if e.get('v93_trial13_candidate') is True]
    assert len(silent) == 1
    assert len(trial) == 1
    assert trial[0]['direction'] == 'Bullish'
    assert trial[0]['v93_silent_oi_lead_bars'] == 2
    assert trial[0]['v93_absolute_regime_aligned'] is True

def test_v930_streaming_compactor_preserves_component_and_trial13_payloads():
    from app import backtest
    raw = {
        'symbol': 'AAA', 'signal_time': '2026-08-31T10:00:00+05:30',
        'entry_time': '2026-08-31T10:15:00+05:30',
        'v93_event_type': 'silent_oi',
        'movement_outcomes': {'1D': {'max_abs_move_atr': 1.5}},
        'oi_z': 2.0, 'price_move_60m_atr': 0.2,
    }
    trial = {
        'symbol': 'AAA', 'signal_time': '2026-08-31T11:00:00+05:30',
        'entry_time': '2026-08-31T11:15:00+05:30', 'direction': 'Bullish',
        'v93_event_type': 'fresh_breakout', 'v93_trial13_candidate': True,
        'v93_silent_oi_lead': True, 'v93_silent_oi_lead_bars': 2,
        'v93_absolute_regime_aligned': True, 'entry_is_extended': False,
        'intraday_returns': {'2h': 0.2}, 'swing_returns': {'1D': 0.4, '2D': 0.6},
    }
    rows = backtest._compact_v91_events({'v9_playbook_events': [raw, trial]})
    assert len(rows) == 2
    assert rows[0]['movement_outcomes']['1D']['max_abs_move_atr'] == pytest.approx(1.5)
    assert rows[1]['v93_trial13_candidate'] is True
    assert rows[1]['v93_silent_oi_lead_bars'] == 2

def test_v930_streaming_aggregate_emits_component_lab_only_in_v93_mode():
    from app import early_research
    rows = [_event(i, event_type='fresh_breakout') for i in range(10)]
    out = early_research.aggregate_v91_compact_events(
        rows, {}, run_context={'research_mode': 'v93_lab', 'history_coverage': {'oi_bar_coverage_pct': 35.2}}
    )
    assert 'v93_component_lab' in out
    assert out['v93_component_lab']['research_only'] is True
    assert out['v93_component_lab']['trial13']['final_test']['locked'] is True


def test_v930_web_endpoint_accepts_v93_lab_mode():
    text = Path('app/web.py').read_text(encoding='utf-8')
    assert '"v93_lab"' in text
    assert 'mode in ("v91_fast", "v91_bear_final", "v93_lab")' in text


def test_v930_directional_stats_include_day_cluster_confidence_interval():
    rows = []
    # Multiple correlated events per day: CI should be based on daily means, not 20 pseudo-independent trades.
    for day, ret in enumerate([0.40, 0.20, -0.10, 0.30], start=1):
        for j in range(5):
            row = _event(day * 10 + j, ret1d=ret, ret2d=ret)
            row['signal_time'] = f'2026-08-{day:02d}T10:{j:02d}:00+05:30'
            rows.append(row)
    stats = v93_component_lab.directional_stats(rows, 'swing_returns', '1D')
    assert stats['distinct_days'] == 4
    assert stats['day_cluster_avg_ci95_low_pct'] is not None
    assert stats['day_cluster_avg_ci95_high_pct'] is not None
    assert stats['day_cluster_avg_ci95_low_pct'] < stats['avg_return_pct'] < stats['day_cluster_avg_ci95_high_pct']


def test_v930_stage2_checkpoint_preserves_history_coverage_for_restart(tmp_path):
    from app import backtest
    idx = pd.date_range('2026-08-28 09:15', periods=4, freq='15min', tz='Asia/Kolkata')
    frame = pd.DataFrame({
        'tod_rvol':[1.0]*4, 'opening_rvol':[1.0]*4, 'bar_range_atr':[0.5]*4,
        'gap_atr':[0.0]*4, 'turnover_notional':[1000.0]*4, 'oi_chg_60m_pct':[1.0]*4,
        'rs_pct':[0.1]*4, 'stock_sector_lead_pct':[0.1]*4,
    }, index=idx).astype('float32')
    cov = {'symbol':'AAA','price_bars':100,'oi_bars':40,'oi_bar_coverage_pct':40.0,
           'price_first_timestamp':'2026-03-01T09:15:00+05:30','price_last_timestamp':'2026-08-31T15:30:00+05:30',
           'oi_first_timestamp':'2026-07-01T09:15:00+05:30','oi_last_timestamp':'2026-08-31T15:30:00+05:30'}
    shard = backtest._write_research_symbol_shard(
        tmp_path, 0, 'AAA', compact_frame=frame, replay=None, note=None,
        v91_events=[], v91_confirmation={}, history_coverage=cov,
    )
    path = backtest._build_v91_ranked_events_checkpoint(tmp_path, {'AAA': shard})
    payload = backtest._load_v91_ranked_events_checkpoint(path)
    assert payload['history_coverage']['oi_bars'] == 40
    assert payload['history_coverage']['price_bars'] == 100
    assert payload['history_coverage']['oi_bar_coverage_pct'] == pytest.approx(40.0)


def test_v930_trial13_split_keeps_each_trading_day_in_only_one_partition():
    rows = []
    for day in range(1, 11):
        count = 8 if day == 1 else 3
        for j in range(count):
            row = _event(day * 10 + j)
            row['signal_time'] = f'2026-08-{day:02d}T10:{j:02d}:00+05:30'
            row['entry_time'] = f'2026-08-{day:02d}T10:{j+1:02d}:00+05:30'
            rows.append(row)
    dev, val, final = v93_component_lab._split_60_20_20(rows)
    dayset = lambda xs: {pd.Timestamp(x['signal_time']).date().isoformat() for x in xs}
    assert dayset(dev).isdisjoint(dayset(val))
    assert dayset(dev).isdisjoint(dayset(final))
    assert dayset(val).isdisjoint(dayset(final))
    assert len(dayset(dev)) == 6
    assert len(dayset(val)) == 2
    assert len(dayset(final)) == 2


def test_v930_component_lab_measures_declared_independent_streams_on_1d_2d():
    rows = []
    for i in range(40):
        direction = 'Bullish' if i % 2 == 0 else 'Bearish'
        row = _event(i, ret1d=0.2, ret2d=0.3, direction=direction, trial=False, event_type='fresh_breakout')
        row.update({
            'fresh_breakout': True,
            'oi_acceleration': 0.8,
            'tod_rvol': 1.5,
            'compression_score': 70.0,
            'rs_pct': 0.2 if direction == 'Bullish' else -0.2,
            'vwap_side_agrees': True,
            'atr_pct': 0.30,
            'entry_is_extended': False,
            'v93_absolute_regime_aligned': True,
        })
        rows.append(row)
    report = v93_component_lab.build_report(rows, run_context={'effective_atr_floor_pct': 0.24})
    comps = report['directional_components']
    for key in (
        'oi_acceleration_moderate_plus', 'tod_rvol_1_3_plus', 'compression_60_plus',
        'relative_direction_aligned', 'vwap_aligned', 'atr_floor_scaled', 'not_extended',
    ):
        assert key in comps
        assert comps[key]['event_count'] == 40
        assert comps[key]['1D']['trade_count'] == 40
        assert comps[key]['2D']['trade_count'] == 40
    assert report['component_reference']['effective_atr_floor_pct'] == pytest.approx(0.24)
    assert report['component_reference']['oi_acceleration_moderate_pp'] == pytest.approx(0.5)
    assert report['component_reference']['tod_rvol_min'] == pytest.approx(1.3)
    assert report['component_reference']['compression_min'] == pytest.approx(60.0)


def test_v930_fresh_breakout_event_carries_atr_pct_for_component_lab():
    from app import early_research
    import inspect
    source = inspect.getsource(early_research._replay_breakout_feature_frame)
    assert '"atr_pct": row.get("atr_pct")' in source


def test_v930_streaming_compactor_preserves_atr_pct_for_component_lab():
    from app import backtest
    row = {
        'symbol': 'AAA', 'signal_time': '2026-08-31T10:00:00+05:30',
        'entry_time': '2026-08-31T10:15:00+05:30', 'direction': 'Bullish',
        'v93_event_type': 'fresh_breakout', 'fresh_breakout': True,
        'atr_pct': 0.31, 'intraday_returns': {'2h': 0.1}, 'swing_returns': {'1D': 0.2, '2D': 0.3},
    }
    compact = backtest._compact_v91_events({'v9_playbook_events': [row]})
    assert compact[0]['atr_pct'] == pytest.approx(0.31)
