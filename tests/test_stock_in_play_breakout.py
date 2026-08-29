import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

import sys
import types

if 'kiteconnect' not in sys.modules:
    mod = types.ModuleType('kiteconnect')
    class _KC:
        def __init__(self, *a, **k): pass
        def set_access_token(self, *a, **k): pass
    mod.KiteConnect = _KC
    sys.modules['kiteconnect'] = mod

from app import early_research


def _bars(n=20, start='2026-08-28 09:15', tz='Asia/Kolkata'):
    idx = pd.date_range(start, periods=n, freq='15min', tz=tz)
    base = np.linspace(100.0, 100.5, n)
    return pd.DataFrame({
        'open': base,
        'high': base + 0.20,
        'low': base - 0.20,
        'close': base + 0.05,
        'volume': np.full(n, 1000.0),
    }, index=idx)


def test_session_pct_change_preserves_timezone_and_resets_between_sessions():
    idx = pd.DatetimeIndex([
        '2026-08-27 15:00+05:30', '2026-08-27 15:15+05:30',
        '2026-08-28 09:15+05:30', '2026-08-28 09:30+05:30',
        '2026-08-28 09:45+05:30', '2026-08-28 10:00+05:30',
        '2026-08-28 10:15+05:30',
    ])
    oi = pd.Series([100, 102, 110, 111, 112, 114, 116], index=idx, dtype=float)
    out = early_research._session_pct_change(oi, 2)
    assert math.isnan(out.iloc[2])  # crosses overnight: deliberately unavailable
    assert out.iloc[4] == pytest.approx((112 / 110 - 1) * 100)
    assert out.iloc[6] == pytest.approx((116 / 112 - 1) * 100)


def test_price_breakout_assigns_direction_from_actual_range_escape_not_indicators():
    from app.stock_in_play import build_price_features
    df = _bars(12)
    # Keep first 11 bars tightly capped around 100.6, then close beyond the prior 6-bar high.
    df.iloc[:11, df.columns.get_loc('high')] = 100.60
    df.iloc[:11, df.columns.get_loc('low')] = 99.80
    df.iloc[:11, df.columns.get_loc('close')] = 100.20
    df.iloc[11, df.columns.get_loc('open')] = 100.25
    df.iloc[11, df.columns.get_loc('high')] = 101.10
    df.iloc[11, df.columns.get_loc('low')] = 100.15
    df.iloc[11, df.columns.get_loc('close')] = 100.95
    atr = pd.Series(0.8, index=df.index)
    comp = pd.DataFrame({'compression_score': [70.0] * 11 + [50.0]}, index=df.index)
    tod = pd.Series([1.0] * 11 + [1.4], index=df.index)
    feat = build_price_features(df, atr, comp, tod)
    last = feat.iloc[-1]
    assert last['breakout_direction'] == 'Bullish'
    assert last['fresh_breakout']
    assert last['breakout_source'] in ('Compression', 'Recent Range', 'Opening Range')
    assert last['breakout_level'] == pytest.approx(100.60)


def test_opening_range_breakout_is_not_available_until_first_two_bars_complete():
    from app.stock_in_play import build_price_features
    df = _bars(6)
    df.iloc[0, df.columns.get_loc('high')] = 101
    df.iloc[1, df.columns.get_loc('high')] = 101.2
    df.iloc[0, df.columns.get_loc('low')] = 99
    df.iloc[1, df.columns.get_loc('low')] = 98.8
    df.iloc[2:, df.columns.get_loc('close')] = [100.5, 101.4, 101.5, 101.6]
    df.iloc[2:, df.columns.get_loc('high')] = [100.8, 101.6, 101.7, 101.8]
    atr = pd.Series(1.0, index=df.index)
    comp = pd.DataFrame({'compression_score': [20.0] * len(df)}, index=df.index)
    tod = pd.Series([1.0] * len(df), index=df.index)
    feat = build_price_features(df, atr, comp, tod)
    assert pd.isna(feat.iloc[0]['opening_range_high'])
    assert pd.isna(feat.iloc[1]['opening_range_high'])
    assert feat.iloc[3]['opening_range_high'] == pytest.approx(101.2)
    assert feat.iloc[3]['breakout_direction'] == 'Bullish'
    assert feat.iloc[3]['breakout_source'] == 'Opening Range'


def test_stock_in_play_can_trigger_without_compression_from_abnormal_participation_or_gap():
    from app.stock_in_play import build_price_features
    df = _bars(8)
    atr = pd.Series(1.0, index=df.index)
    comp = pd.DataFrame({'compression_score': [20.0] * len(df)}, index=df.index)
    tod = pd.Series([1.0] * 7 + [1.7], index=df.index)
    feat = build_price_features(df, atr, comp, tod)
    assert bool(feat.iloc[-1]['stock_in_play']) is True
    assert bool(feat.iloc[-1]['energy_building']) is False


def _live_row(**overrides):
    row = {
        'symbol': 'AAA', 'breakout_direction': 'Bullish', 'fresh_breakout': True,
        'breakout_source': 'Recent Range', 'breakout_level': 100.0,
        'breakout_extension_atr': 0.4, 'entry_is_extended': False,
        'vwap_side_agrees': True, 'tod_rvol': 1.5, 'stock_in_play': True,
        'compression_score': 70.0, 'energy_building': True,
        'oi_chg_30m_pct': 0.6, 'oi_chg_60m_pct': 1.2, 'oi_acceleration': 0.2,
        'oi_recent_agrees': True, 'sector_agrees': True, 'htf_agrees': True,
        'rs_pct': 0.6, 'timestamp': '2026-08-28T13:15:00+05:30',
    }
    row.update(overrides)
    return row


def test_intraday_best_entry_uses_breakout_participation_location_and_sponsorship():
    from app.stock_in_play import classify_live_candidate
    out = classify_live_candidate(_live_row())
    assert out['stage'] == 'Intraday Best Entry'
    assert out['intraday_eligible'] is True
    assert out['direction'] == 'Bullish'
    assert out['oi_status'] == 'Confirmed'
    assert 'RSI' not in ' '.join(out['blockers'])
    assert 'MACD' not in ' '.join(out['blockers'])


def test_missing_oi_is_explicit_and_cannot_be_promoted_to_best_entry():
    from app.stock_in_play import classify_live_candidate
    weak = classify_live_candidate(_live_row(
        oi_chg_30m_pct=None, oi_chg_60m_pct=None, oi_acceleration=None,
        oi_recent_agrees=None, tod_rvol=1.4, rs_pct=0.2, sector_agrees=None,
    ))
    assert weak['oi_status'] == 'Unavailable'
    assert weak['intraday_eligible'] is False

    strong = classify_live_candidate(_live_row(
        oi_chg_30m_pct=None, oi_chg_60m_pct=None, oi_acceleration=None,
        oi_recent_agrees=None, tod_rvol=1.8, rs_pct=0.8, sector_agrees=True,
    ))
    assert strong['oi_status'] == 'Unavailable'
    assert strong['intraday_eligible'] is False
    assert 'OI unavailable' in strong['blockers']


def test_swing_candidate_is_separate_and_requires_late_session_persistence_and_htf():
    from app.stock_in_play import classify_live_candidate
    early = classify_live_candidate(_live_row(timestamp='2026-08-28T12:00:00+05:30'))
    assert early['intraday_eligible'] is True
    assert early['swing_eligible'] is False

    late = classify_live_candidate(_live_row(timestamp='2026-08-28T14:45:00+05:30', breakout_retained=True))
    assert late['swing_eligible'] is True
    assert late['stage'] == 'High-Quality Swing 1-2D'

    opposed = classify_live_candidate(_live_row(timestamp='2026-08-28T14:45:00+05:30', breakout_retained=True, htf_agrees=False))
    assert opposed['swing_eligible'] is False


def test_trade_outcomes_report_intraday_eod_and_one_two_day_exits_with_excursions():
    from app.stock_in_play import compute_trade_outcomes
    # Three NSE sessions, 4 bars each, easy to reason about.
    idx = pd.DatetimeIndex([
        '2026-08-26 14:30+05:30', '2026-08-26 14:45+05:30', '2026-08-26 15:00+05:30', '2026-08-26 15:15+05:30',
        '2026-08-27 09:15+05:30', '2026-08-27 12:00+05:30', '2026-08-27 14:00+05:30', '2026-08-27 15:15+05:30',
        '2026-08-28 09:15+05:30', '2026-08-28 12:00+05:30', '2026-08-28 14:00+05:30', '2026-08-28 15:15+05:30',
    ])
    close = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111]
    df = pd.DataFrame({
        'open': close, 'close': close,
        'high': [x + 0.5 for x in close], 'low': [x - 0.5 for x in close],
        'volume': [1000] * len(idx),
    }, index=idx)
    out = compute_trade_outcomes(df, signal_pos=0, direction='Bullish', atr=2.0, cost_pct=0, slippage_pct=0)
    assert out['entry_pos'] == 1
    assert out['intraday']['eod'] == pytest.approx(2.0 / 101 * 100, rel=1e-3)
    assert out['swing']['1D'] == pytest.approx(6.0 / 101 * 100, rel=1e-3)
    assert out['swing']['2D'] == pytest.approx(10.0 / 101 * 100, rel=1e-3)
    assert out['mfe_atr']['2D'] > 0
    assert out['mae_atr']['2D'] >= 0
    assert out['time_to_0_5atr_bars'] is not None


def test_compression_lift_compares_events_with_unconditional_baseline():
    from app.stock_in_play import expansion_lift_table
    event_vals = [
        {'future_abs_move_atr': {'4': 1.2, '8': 1.5}},
        {'future_abs_move_atr': {'4': 0.8, '8': 1.1}},
        {'future_abs_move_atr': {'4': 1.4, '8': 1.7}},
    ]
    baseline_vals = [
        {'future_abs_move_atr': {'4': 0.4, '8': 0.6}},
        {'future_abs_move_atr': {'4': 0.6, '8': 0.9}},
        {'future_abs_move_atr': {'4': 1.2, '8': 1.3}},
        {'future_abs_move_atr': {'4': 0.3, '8': 0.5}},
    ]
    rows = expansion_lift_table(event_vals, baseline_vals, horizons=('4', '8'), thresholds=(1.0,))
    row4 = next(r for r in rows if r['horizon'] == '4')
    assert row4['event_hit_rate_pct'] > row4['baseline_hit_rate_pct']
    assert row4['lift'] > 1


def test_research_interactions_are_motivated_not_cartesian_grid():
    from app.stock_in_play import interaction_variants
    events = [
        {'tod_rvol': 1.4, 'oi_status': 'Confirmed', 'htf_agrees': True, 'vwap_side_agrees': True, 'entry_is_extended': False},
    ]
    variants = interaction_variants(events)
    assert set(variants) == {
        'breakout_only', 'breakout_plus_volume', 'breakout_plus_oi',
        'breakout_plus_volume_oi', 'breakout_plus_4h', 'live_quality_stack',
    }


def test_dashboard_and_backtest_copy_use_new_stages_and_real_horizons():
    index = open('app/templates/index.html', encoding='utf-8').read()
    backtest = open('app/templates/backtest.html', encoding='utf-8').read()
    assert 'Live Decision Console' in index
    assert '1–2D Swing' in index
    assert 'Bullish Leaders' in index and 'Bearish Leaders' in index
    assert '3-bar primary' not in backtest
    for label in ('30m', '1h', '2h', '4h', '1D', '2D'):
        assert label in backtest

def test_compute_signal_exposes_stock_in_play_breakout_fields():
    from app import indicators
    # Use several synthetic sessions so TOD RVOL and core indicators can warm.
    frames = []
    for d in pd.bdate_range('2026-08-03', periods=12):
        idx = pd.date_range(d.replace(hour=9, minute=15), periods=25, freq='15min')
        base = 100 + np.linspace(0, 0.3, 25)
        f = pd.DataFrame({
            'open': base, 'high': base + 0.2, 'low': base - 0.2,
            'close': base + 0.05, 'volume': np.full(25, 1000.0),
        }, index=idx)
        frames.append(f)
    df = pd.concat(frames)
    # Force a clear final-bar escape.
    prior_high = float(df['high'].iloc[-7:-1].max())
    df.iloc[-1, df.columns.get_loc('close')] = prior_high + 0.5
    df.iloc[-1, df.columns.get_loc('high')] = prior_high + 0.7
    df.iloc[-1, df.columns.get_loc('volume')] = 2500
    out = indicators.compute_signal(df, '15minute')
    for key in ('breakout_direction', 'fresh_breakout', 'breakout_source', 'breakout_level',
                'breakout_extension_atr', 'stock_in_play', 'breakout_vwap_agrees'):
        assert key in out
    assert out['breakout_direction'] == 'Bullish'


def test_live_shortlist_ranks_intraday_and_swing_from_breakout_classifier(monkeypatch):
    from app import background
    rows = [
        {'symbol': 'A', 'breakout_direction': 'Bullish', 'fresh_breakout': True, 'close': 101, 'prev_close': 100},
        {'symbol': 'B', 'breakout_direction': 'Bearish', 'fresh_breakout': True, 'close': 99, 'prev_close': 100},
    ]
    def fake_classify(row):
        if row['symbol'] == 'A':
            return {'direction': 'Bullish', 'stage': 'Intraday Best Entry', 'intraday_eligible': True,
                    'swing_eligible': False, 'oi_status': 'Confirmed', 'score': 82, 'blockers': []}
        return {'direction': 'Bearish', 'stage': 'Swing 1-2D Candidate', 'intraday_eligible': True,
                'swing_eligible': True, 'oi_status': 'Confirmed', 'score': 90, 'blockers': []}
    monkeypatch.setattr(background.stock_in_play, 'classify_live_candidate', fake_classify)
    intraday, swing = background._apply_stock_in_play_shortlists(rows)
    assert [r['symbol'] for r in intraday] == ['B', 'A']
    assert [r['symbol'] for r in swing] == ['B']
    assert rows[1]['swing_rank'] == 1

def test_feature_frame_produces_measurable_tzaware_oi_velocity_after_breakout():
    from app.early_research import build_feature_frame
    frames = []
    oivals = []
    for d in pd.bdate_range('2026-08-17', periods=8, tz='Asia/Kolkata'):
        idx = pd.date_range(d.replace(hour=9, minute=15), periods=25, freq='15min')
        base = np.full(25, 100.0)
        f = pd.DataFrame({'open': base, 'high': base + .2, 'low': base - .2,
                          'close': base + .05, 'volume': np.full(25, 1000.)}, index=idx)
        frames.append(f)
        oivals.extend(np.linspace(1000, 1050, 25))
    df = pd.concat(frames)
    # Final breakout and steadily rising OI.
    df.iloc[-1, df.columns.get_loc('close')] = 101.0
    df.iloc[-1, df.columns.get_loc('high')] = 101.2
    oi = pd.Series(oivals, index=df.index)
    feat = build_feature_frame(df, '15minute', oi_series=oi, index_df=df)
    assert feat['oi_chg_30m_pct'].notna().sum() > 0
    assert feat['oi_chg_60m_pct'].notna().sum() > 0
    assert feat['oi_chg_60m_pct'].iloc[-1] > 0


def test_new_replay_uses_breakout_direction_and_records_intraday_swing_outcomes():
    from app.early_research import replay_feature_frame
    idx = pd.DatetimeIndex([
        '2026-08-26 14:00+05:30','2026-08-26 14:15+05:30','2026-08-26 14:30+05:30','2026-08-26 14:45+05:30','2026-08-26 15:00+05:30','2026-08-26 15:15+05:30',
        '2026-08-27 09:15+05:30','2026-08-27 12:00+05:30','2026-08-27 15:15+05:30',
        '2026-08-28 09:15+05:30','2026-08-28 12:00+05:30','2026-08-28 15:15+05:30',
    ])
    px = np.arange(100., 112.)
    df = pd.DataFrame({'open': px, 'high': px+.4, 'low': px-.4, 'close': px+.1, 'volume': 1000}, index=idx)
    feat = pd.DataFrame(index=idx)
    feat['breakout_direction'] = None
    feat.loc[idx[1], 'breakout_direction'] = 'Bullish'
    feat['direction'] = feat['breakout_direction']
    feat['fresh_breakout'] = False
    feat.loc[idx[1], 'fresh_breakout'] = True
    feat['breakout_source'] = 'Recent Range'
    feat['breakout_level'] = 100.5
    feat['breakout_extension_atr'] = 0.2
    feat['breakout_retained'] = False
    feat.loc[idx[2], 'breakout_retained'] = True
    feat['energy_building'] = False
    feat['stock_in_play'] = True
    feat['compression_score'] = 30.0
    feat['vwap_side_agrees'] = True
    feat['entry_is_extended'] = False
    feat['oi_chg_30m_pct'] = .5
    feat['oi_chg_60m_pct'] = 1.0
    feat['oi_acceleration'] = .2
    feat['oi_recent_agrees'] = True
    feat['tod_rvol'] = 1.5
    feat['sector_agrees'] = True
    feat['htf_agrees'] = True
    feat['rs_pct'] = .5
    feat['atr'] = 1.0
    out = replay_feature_frame(df, feat, 'AAA', cost_pct=0, slippage_pct=0)
    assert len(out['ignition_events']) == 1
    ev = out['ignition_events'][0]
    assert ev['direction'] == 'Bullish'
    assert ev['breakout_source'] == 'Recent Range'
    assert 'eod' in ev['intraday_returns']
    assert '1D' in ev['swing_returns'] and '2D' in ev['swing_returns']
    assert ev['oi_status'] == 'Confirmed'


def test_aggregate_report_exposes_real_horizons_lift_interactions_and_oi_coverage():
    from app.early_research import aggregate_research
    event = {
        'entry_time': '2026-08-28T10:00:00+05:30', 'direction': 'Bullish',
        'intraday_returns': {'30m': .2, '1h': .3, '2h': .5, '4h': .6, 'eod': .4},
        'swing_returns': {'1D': .8, '2D': 1.2}, 'breakout_source': 'Compression',
        'tod_rvol': 1.5, 'oi_status': 'Confirmed', 'htf_agrees': True,
        'vwap_side_agrees': True, 'entry_is_extended': False,
        'mfe_atr': {'2D': 1.5}, 'mae_atr': {'2D': .4},
    }
    replay = {
        'energy_events': [{'entry_time': event['entry_time'], 'future_abs_move_atr': {'4':1.2,'8':1.4,'16':1.6,'25':1.8}}],
        'baseline_energy_events': [{'entry_time': event['entry_time'], 'future_abs_move_atr': {'4':.4,'8':.6,'16':.8,'25':1.0}}],
        'ignition_events': [event], 'best_entry_events': [event], 'swing_events': [event],
    }
    out = aggregate_research([replay], holdout_pct=0)
    assert 'intraday' in out and 'swing' in out
    assert set(out['intraday']['all']) >= {'30m','1h','2h','4h','eod'}
    assert set(out['swing']['all']) >= {'1D','2D'}
    assert out['compression_lift']
    assert 'breakout_plus_volume_oi' in out['interactions']
    assert out['oi_coverage']['available'] == 1

def test_alert_uses_breakout_trade_direction_not_legacy_indicator_vote():
    from app.alerts import _format_message
    row = {
        'symbol': 'XYZ', 'trade_direction': 'Bearish', 'direction': 'Bullish',
        'entry_trigger': None, 'fresh_signal': None, 'close': 250.0,
        'movement_score': 84.0, 'oi_chg_60m_pct': 1.1, 'oi_acceleration': 0.4,
        'tod_rvol': 1.6, 'rs_pct': -0.8, 'htf_direction': 'Bearish',
        'breakout_source': 'Opening Range', 'breakout_level': 252.0,
        'oi_status': 'Confirmed', 'breakout_extension_atr': 0.45,
    }
    msg = _format_message(row, '15minute')
    assert 'Bearish' in msg
    assert 'Opening Range' in msg
    assert 'breakout' in msg.lower()

def test_primary_dashboard_hides_legacy_indicator_tuning_controls():
    text = open('app/templates/index.html', encoding='utf-8').read()
    primary_toolbar = text[text.index('<div class="toolbar">'):text.index('<div class="card" style="border-color:#3a2f0f;">')]
    for legacy in ('MACD preset', 'RSI len', 'RSI smooth', 'MACD fast', 'MACD slow', 'MACD signal', 'BB len'):
        assert legacy not in primary_toolbar
    assert 'Stock-in-Play thresholds are validated on the Backtest page' in primary_toolbar

def test_aggregate_report_includes_holdout_excursion_and_speed_diagnostics():
    from app.early_research import aggregate_research
    events = []
    for i in range(4):
        events.append({
            'entry_time': f'2026-08-2{5+i}T10:00:00+05:30', 'direction': 'Bullish',
            'intraday_returns': {'30m': .1, '1h': .2, '2h': .3, '4h': .4, 'eod': .5},
            'swing_returns': {'1D': .6, '2D': .8}, 'breakout_source': 'Recent Range',
            'tod_rvol': 1.5, 'oi_status': 'Confirmed', 'htf_agrees': True,
            'vwap_side_agrees': True, 'entry_is_extended': False,
            'mfe_atr': {'1D': 1.1 + i*.1, '2D': 1.4 + i*.1},
            'mae_atr': {'1D': .3, '2D': .4},
            'time_to_0_5atr_bars': 3 + i, 'time_to_1atr_bars': 8 + i,
        })
    replay = {'energy_events': [], 'baseline_energy_events': [], 'ignition_events': events,
              'best_entry_events': events, 'swing_events': events}
    out = aggregate_research([replay], holdout_pct=25)
    ex = out['excursions']['holdout']
    assert ex['events'] == 1
    assert ex['avg_mfe_1D_atr'] > ex['avg_mae_1D_atr']
    assert ex['hit_0_5atr_pct'] == 100.0
    assert ex['median_bars_to_1atr'] is not None

def test_fresh_breakout_fires_once_for_consecutive_trend_escape_bars():
    from app.stock_in_play import build_price_features
    df = _bars(14)
    # Tight range first, then three consecutive closes making new highs.
    df.iloc[:10, df.columns.get_loc('high')] = 100.6
    df.iloc[:10, df.columns.get_loc('low')] = 99.8
    df.iloc[:10, df.columns.get_loc('close')] = 100.2
    for pos, close in zip((10, 11, 12, 13), (100.9, 101.2, 101.5, 101.8)):
        df.iloc[pos, df.columns.get_loc('close')] = close
        df.iloc[pos, df.columns.get_loc('high')] = close + .1
        df.iloc[pos, df.columns.get_loc('low')] = close - .3
    atr = pd.Series(.8, index=df.index)
    comp = pd.DataFrame({'compression_score': [70.] * len(df)}, index=df.index)
    tod = pd.Series(1.5, index=df.index)
    feat = build_price_features(df, atr, comp, tod)
    tail = feat.iloc[10:14]
    assert tail['breakout_direction'].eq('Bullish').sum() >= 2
    assert tail['fresh_breakout'].sum() == 1

def test_swing_can_qualify_on_retention_bar_without_a_second_fresh_breakout():
    from app.stock_in_play import classify_live_candidate
    row = _live_row(
        breakout_direction=None, retained_breakout_direction='Bullish', fresh_breakout=False,
        breakout_retained=True, timestamp='2026-08-28T14:45:00+05:30',
    )
    out = classify_live_candidate(row)
    assert out['direction'] == 'Bullish'
    assert out['intraday_eligible'] is False
    assert out['swing_eligible'] is True

def test_background_uses_retained_breakout_direction_for_swing_confirmation(monkeypatch):
    from app import background
    seen = {}
    def fake_classify(row):
        seen.update(row)
        return {'direction': row.get('trade_direction'), 'stage': 'Swing 1-2D Candidate',
                'intraday_eligible': False, 'swing_eligible': True, 'oi_status': 'Confirmed',
                'score': 88, 'blockers': []}
    monkeypatch.setattr(background.stock_in_play, 'classify_live_candidate', fake_classify)
    row = {'symbol':'A', 'breakout_direction':None, 'retained_breakout_direction':'Bearish',
           'breakout_retained':True, 'close':99, 'prev_close':100, 'sector_direction':'Bearish',
           'htf_direction':'Bearish', 'oi_chg_60m_pct':1.0, 'breakout_vwap_agrees':True,
           'breakout_entry_extended':False}
    _, swing = background._apply_stock_in_play_shortlists([row])
    assert seen['trade_direction'] == 'Bearish'
    assert swing and swing[0]['symbol'] == 'A'

def test_compute_signal_exposes_retained_breakout_fields_for_swing_layer():
    from app import indicators
    frames = []
    for d in pd.bdate_range('2026-08-03', periods=12):
        idx = pd.date_range(d.replace(hour=9, minute=15), periods=25, freq='15min')
        base = np.full(25, 100.0)
        f = pd.DataFrame({'open':base,'high':base+.2,'low':base-.2,'close':base+.05,'volume':1000.0}, index=idx)
        frames.append(f)
    df = pd.concat(frames)
    out = indicators.compute_signal(df, '15minute')
    for key in ('retained_breakout_direction','retained_breakout_source','retained_breakout_level'):
        assert key in out

def test_background_promotes_retained_breakout_metadata_for_swing_display(monkeypatch):
    from app import background
    monkeypatch.setattr(background.stock_in_play, 'classify_live_candidate', lambda row: {
        'direction': row.get('trade_direction'), 'stage':'Swing 1-2D Candidate',
        'intraday_eligible':False, 'swing_eligible':True, 'oi_status':'Confirmed', 'score':86, 'blockers':[]})
    row = {'symbol':'A','breakout_direction':None,'retained_breakout_direction':'Bullish',
           'retained_breakout_source':'Opening Range','retained_breakout_level':101.5,
           'retained_breakout_extension_atr':0.6,'breakout_retained':True,'close':102,'prev_close':101,
           'sector_direction':'Bullish','htf_direction':'Bullish','oi_chg_60m_pct':1.0,
           'breakout_vwap_agrees':True,'breakout_entry_extended':False}
    background._apply_stock_in_play_shortlists([row])
    assert row['breakout_source'] == 'Opening Range'
    assert row['breakout_level'] == 101.5
    assert row['breakout_extension_atr'] == 0.6

def test_replay_creates_swing_event_on_late_retention_bar_not_initial_breakout():
    from app.early_research import replay_feature_frame
    idx = pd.DatetimeIndex([
        '2026-08-26 14:00+05:30','2026-08-26 14:15+05:30','2026-08-26 14:30+05:30','2026-08-26 14:45+05:30','2026-08-26 15:00+05:30','2026-08-26 15:15+05:30',
        '2026-08-27 09:15+05:30','2026-08-27 12:00+05:30','2026-08-27 15:15+05:30',
        '2026-08-28 09:15+05:30','2026-08-28 12:00+05:30','2026-08-28 15:15+05:30'])
    px = np.arange(100., 112.)
    df = pd.DataFrame({'open':px,'high':px+.4,'low':px-.4,'close':px+.1,'volume':1000}, index=idx)
    feat = pd.DataFrame(index=idx)
    feat['breakout_direction'] = None; feat.loc[idx[1], 'breakout_direction'] = 'Bullish'
    feat['retained_breakout_direction'] = None; feat.loc[idx[2], 'retained_breakout_direction'] = 'Bullish'
    feat['fresh_breakout'] = False; feat.loc[idx[1], 'fresh_breakout'] = True
    feat['breakout_retained'] = False; feat.loc[idx[2], 'breakout_retained'] = True
    feat['breakout_source'] = 'Recent Range'; feat['retained_breakout_source'] = 'Recent Range'
    feat['breakout_level'] = 100.5; feat['retained_breakout_level'] = 100.5
    feat['breakout_extension_atr'] = .2; feat['retained_breakout_extension_atr'] = .3
    feat['energy_building'] = False; feat['stock_in_play'] = True; feat['compression_score'] = 30.
    feat['vwap_side_agrees'] = True; feat['entry_is_extended'] = False
    feat['oi_chg_30m_pct'] = .5; feat['oi_chg_60m_pct'] = 1.; feat['oi_acceleration'] = .2; feat['oi_recent_agrees'] = True
    feat['tod_rvol'] = 1.5; feat['sector_agrees'] = True; feat['htf_agrees'] = True; feat['rs_pct'] = .5; feat['atr'] = 1.
    out = replay_feature_frame(df, feat, 'AAA', cost_pct=0, slippage_pct=0)
    assert len(out['ignition_events']) == 1
    assert len(out['swing_events']) == 1
    assert out['swing_events'][0]['signal_time'] == idx[2].isoformat()

def test_historical_retention_bar_keeps_vwap_oi_and_context_direction_for_swing():
    from app.early_research import build_feature_frame
    frames=[]; oi_vals=[]
    for d in pd.bdate_range('2026-08-03', periods=12, tz='Asia/Kolkata'):
        idx=pd.date_range(d.replace(hour=9, minute=15), periods=25, freq='15min')
        base=np.full(25,100.0)
        f=pd.DataFrame({'open':base,'high':base+.2,'low':base-.2,'close':base+.05,'volume':np.full(25,1000.)}, index=idx)
        frames.append(f); oi_vals.extend(np.linspace(1000,1050,25))
    df=pd.concat(frames)
    # Breakout then a retained bar that does not make another recent-range high.
    df.iloc[-2, df.columns.get_loc('close')] = 101.0
    df.iloc[-2, df.columns.get_loc('high')] = 101.1
    df.iloc[-2, df.columns.get_loc('volume')] = 2000
    df.iloc[-1, df.columns.get_loc('open')] = 100.95
    df.iloc[-1, df.columns.get_loc('close')] = 100.9
    df.iloc[-1, df.columns.get_loc('high')] = 101.0
    df.iloc[-1, df.columns.get_loc('low')] = 100.7
    df.iloc[-1, df.columns.get_loc('volume')] = 1800
    oi=pd.Series(oi_vals,index=df.index)
    feat=build_feature_frame(df,'15minute',oi_series=oi,index_df=df)
    last=feat.iloc[-1]
    assert last['retained_breakout_direction'] == 'Bullish'
    assert pd.notna(last['vwap_side_agrees'])
    assert pd.notna(last['oi_recent_agrees'])

def test_settings_exposes_only_live_stock_in_play_thresholds_not_rsi_macd_tuning():
    tpl = open('app/templates/settings.html', encoding='utf-8').read()
    web = open('app/web.py', encoding='utf-8').read()
    live = tpl[tpl.index('<h2>Live Early-Movement Engine</h2>'):tpl.index('<details class="card">', tpl.index('<h2>Live Early-Movement Engine</h2>'))]
    assert 'RSI length' not in live and 'MACD' not in live
    for name in ('compression_radar_score','tod_rvol_min','tod_rvol_strong_no_oi','max_entry_extension_atr','shortlist_max','scan_interval_seconds'):
        assert f'name="{name}"' in live
    assert '"COMPRESSION_RADAR_SCORE": form.get("compression_radar_score"' in web
    assert '"TOD_RVOL_MIN": form.get("tod_rvol_min"' in web


def test_opening_relative_volume_compares_first_30m_with_prior_same_opening_window():
    from app.indicators import opening_relative_volume
    frames = []
    for d, scale in [('2026-08-24', 1.0), ('2026-08-25', 1.0), ('2026-08-26', 1.0), ('2026-08-27', 1.0), ('2026-08-28', 2.0)]:
        idx = pd.date_range(f'{d} 09:15', periods=4, freq='15min', tz='Asia/Kolkata')
        frames.append(pd.DataFrame({
            'open': 100.0, 'high': 100.5, 'low': 99.5, 'close': 100.1,
            'volume': [1000*scale, 1000*scale, 800, 700],
        }, index=idx))
    df = pd.concat(frames)
    r = opening_relative_volume(df, opening_bars=2, lookback_sessions=4)
    last_session = r[r.index.date == dt.date(2026, 8, 28)]
    assert last_session.iloc[0] == pytest.approx(2.0)
    assert last_session.iloc[1] == pytest.approx(2.0)
    # After the opening window, preserve the completed 30-minute reading as
    # stock-in-play context rather than reverting to ordinary TOD RVOL.
    assert last_session.iloc[-1] == pytest.approx(2.0)


def test_depth_shadow_metrics_extract_futures_order_book_imbalance_without_promoting_it():
    from app.stock_in_play import depth_shadow_metrics
    quote = {
        'last_price': 100.0,
        'depth': {
            'buy': [
                {'price': 99.95, 'quantity': 500},
                {'price': 99.90, 'quantity': 400},
            ],
            'sell': [
                {'price': 100.05, 'quantity': 200},
                {'price': 100.10, 'quantity': 100},
            ],
        },
    }
    m = depth_shadow_metrics(quote)
    assert m['depth_imbalance'] > 0.40
    assert m['spread_bps'] == pytest.approx(10.0, rel=1e-3)
    assert m['microprice_bias_bps'] > 0
    assert m['shadow_only'] is True


def test_institutional_benchmark_requires_sample_expectancy_pf_and_walkforward_stability():
    from app.early_research import institutional_benchmark
    good = {
        'trade_count': 160, 'avg_return_pct': 0.18, 'profit_factor': 1.35,
        'walkforward': [
            {'trade_count': 40, 'avg_return_pct': 0.14, 'profit_factor': 1.25},
            {'trade_count': 40, 'avg_return_pct': 0.16, 'profit_factor': 1.30},
            {'trade_count': 40, 'avg_return_pct': 0.12, 'profit_factor': 1.20},
            {'trade_count': 40, 'avg_return_pct': 0.20, 'profit_factor': 1.40},
        ],
        'avg_mfe_atr': 1.10, 'avg_mae_atr': 0.60,
    }
    out = institutional_benchmark(good, mode='intraday')
    assert out['status'] == 'Benchmark'
    assert out['passed'] is True

    unstable = dict(good)
    unstable['walkforward'] = list(good['walkforward'])
    unstable['walkforward'][2] = {'trade_count': 40, 'avg_return_pct': -0.08, 'profit_factor': 0.75}
    bad = institutional_benchmark(unstable, mode='intraday')
    assert bad['passed'] is False
    assert bad['status'] != 'Benchmark'


def test_opening_rvol_is_live_and_research_stock_in_play_context():
    from app.stock_in_play import build_price_features
    df = _bars(8)
    atr = pd.Series(1.0, index=df.index)
    comp = pd.DataFrame({'compression_score': [20.0] * len(df)}, index=df.index)
    tod = pd.Series([0.8] * len(df), index=df.index)
    opening = pd.Series([1.8] * len(df), index=df.index)
    feat = build_price_features(df, atr, comp, tod, opening_rvol=opening)
    assert bool(feat.iloc[-1]['stock_in_play']) is True
    assert feat.iloc[-1]['opening_rvol'] == pytest.approx(1.8)


def test_feature_frame_exposes_opening_rvol_for_stocks_in_play_research():
    from app.early_research import build_feature_frame
    frames = []
    for day in pd.date_range('2026-07-01', periods=18, freq='B'):
        idx = pd.date_range(day.strftime('%Y-%m-%d') + ' 09:15', periods=25, freq='15min', tz='Asia/Kolkata')
        scale = 2.0 if day == pd.date_range('2026-07-01', periods=18, freq='B')[-1] else 1.0
        base = np.linspace(100, 101, 25)
        frames.append(pd.DataFrame({
            'open': base, 'high': base + .3, 'low': base - .3, 'close': base + .05,
            'volume': [1200*scale, 1200*scale] + [900]*23,
        }, index=idx))
    df = pd.concat(frames)
    feat = build_feature_frame(df, '15minute', index_df=df)
    assert 'opening_rvol' in feat.columns
    assert feat['opening_rvol'].dropna().iloc[-1] > 1.5


def test_fetch_oi_map_carries_near_futures_depth_as_shadow_research(monkeypatch):
    from app import scanner
    monkeypatch.setattr(scanner, '_load_fut_contracts_map', lambda kite: {
        'AAA': [
            {'tradingsymbol': 'AAA26SEPFUT', 'expiry': dt.date(2026, 9, 29)},
            {'tradingsymbol': 'AAA26OCTFUT', 'expiry': dt.date(2026, 10, 27)},
        ]
    })
    class K:
        def quote(self, keys):
            out = {}
            for key in keys:
                oi = 1000 if 'SEP' in key else 500
                out[key] = {
                    'oi': oi, 'oi_day_high': oi + 50, 'oi_day_low': oi - 50,
                    'last_price': 100.0,
                    'depth': {
                        'buy': [{'price':99.95,'quantity':500}],
                        'sell':[{'price':100.05,'quantity':200}],
                    },
                }
            return out
    out = scanner.fetch_oi_map(K(), ['AAA'])['AAA']
    assert out['oi_total'] == 1500
    assert out['fut_depth_imbalance'] > 0
    assert out['fut_spread_bps'] > 0
    assert out['microstructure_shadow_only'] is True


def test_aggregate_research_exposes_strict_intraday_and_swing_promotion_benchmarks():
    from app.early_research import aggregate_research
    import pandas as pd

    def event(i, intraday=0.30, swing=0.45):
        ts = (pd.Timestamp('2025-01-02 10:00', tz='Asia/Kolkata') + pd.Timedelta(days=i)).isoformat()
        return {
            'entry_time': ts,
            'direction': 'Bullish' if i % 2 == 0 else 'Bearish',
            'intraday_returns': {'2h': intraday, '30m': intraday/3, '1h': intraday/2, '4h': intraday*1.1, 'eod': intraday*1.1},
            'swing_returns': {'1D': swing, '2D': swing*1.2},
            'mfe_atr': {'1D': 1.2, '2D': 1.5},
            'mae_atr': {'1D': 0.5, '2D': 0.6},
            'time_to_0_5atr_bars': 4,
            'time_to_1atr_bars': 8,
            'oi_status': 'Confirmed',
            'breakout_source': 'Opening Range',
            'tod_rvol': 1.6,
            'oi_chg_60m_pct': 1.0,
            'oi_acceleration': 0.2,
            'htf_agrees': True,
            'sector_agrees': True,
            'vwap_side_agrees': True,
            'entry_is_extended': False,
        }

    intraday_best = [event(i) for i in range(600)]
    swing_best = [event(i, swing=0.55) for i in range(300)]
    replay = {
        'energy_events': [], 'baseline_energy_events': [],
        'ignition_events': list(intraday_best),
        'best_entry_events': list(intraday_best),
        'swing_events': list(swing_best),
    }
    out = aggregate_research([replay], holdout_pct=30.0)
    assert out['promotion_benchmark']['intraday']['status'] == 'Benchmark'
    assert out['promotion_benchmark']['swing']['status'] == 'Benchmark'
    assert out['promotion_benchmark']['intraday']['checks']['walkforward'] is True
    assert out['promotion_benchmark']['swing']['requirements']['min_profit_factor'] == 1.25


def test_backtest_dashboard_surfaces_research_promotion_benchmark():
    html = open('app/templates/backtest.html', encoding='utf-8').read()
    assert 'Promotion Benchmark' in html
    assert 'promotion_benchmark' in html
    assert 'Research' in html and 'Promising' in html and 'Benchmark' in html


def test_early_research_window_trim_applies_to_energy_baseline_breakout_and_swing_events():
    from app.backtest import _trim_replay_to_window
    replay = {
        'energy_events': [{'signal_time':'2026-01-01'}, {'signal_time':'2026-02-01'}],
        'baseline_energy_events': [{'entry_time':'2026-01-01'}, {'entry_time':'2026-02-01'}],
        'ignition_events': [{'signal_time':'2026-01-01'}, {'signal_time':'2026-02-01'}],
        'best_entry_events': [{'entry_time':'2026-01-01'}, {'entry_time':'2026-02-01'}],
        'swing_events': [{'signal_time':'2026-01-01'}, {'signal_time':'2026-02-01'}],
    }
    out = _trim_replay_to_window(replay, '2026-01-15')
    for key in replay:
        assert len(out[key]) == 1
        assert (out[key][0].get('signal_time') or out[key][0].get('entry_time')) == '2026-02-01'


def test_numpy_boolean_flags_count_as_confirmed_in_live_classifier():
    import numpy as np
    from app.stock_in_play import classify_live_candidate
    row = _live_row(
        oi_recent_agrees=np.bool_(True),
        vwap_side_agrees=np.bool_(True),
        sector_agrees=np.bool_(True),
        htf_agrees=np.bool_(True),
        entry_is_extended=np.bool_(False),
        breakout_retained=np.bool_(True),
        timestamp='2026-08-28T14:45:00+05:30',
    )
    from app.stock_in_play import classify_live_candidate
    out = classify_live_candidate(row)
    assert out['oi_status'] == 'Confirmed'
    assert out['intraday_eligible'] is True
    assert out['swing_eligible'] is True


def test_interaction_variants_accept_numpy_boolean_research_flags():
    import numpy as np
    from app.stock_in_play import interaction_variants
    event = {
        'tod_rvol': 1.4,
        'oi_status': 'Confirmed',
        'htf_agrees': np.bool_(True),
        'vwap_side_agrees': np.bool_(True),
        'entry_is_extended': np.bool_(False),
    }
    variants = interaction_variants([event])
    assert len(variants['breakout_plus_4h']) == 1
    assert len(variants['live_quality_stack']) == 1


def test_flag_normalizes_numeric_boolean_values_from_numpy_where():
    """Research np.where(bool, np.nan) stores flags as 1.0/0.0 floats."""
    from app.stock_in_play import _flag

    assert _flag(1.0) is True
    assert _flag(0.0) is False
    assert _flag(float("nan")) is None


def test_numeric_zero_context_blocks_historical_swing_candidate():
    from app.stock_in_play import classify_live_candidate
    row = _live_row(
        timestamp='2026-08-28T14:45:00+05:30',
        breakout_retained=True,
        htf_agrees=0.0,
        sector_agrees=1.0,
        oi_recent_agrees=1.0,
        vwap_side_agrees=1.0,
        entry_is_extended=0.0,
    )
    out = classify_live_candidate(row)
    assert out['oi_status'] == 'Confirmed'
    assert out['swing_eligible'] is False


def test_nan_oi_values_are_unavailable_not_false_coverage():
    from app.stock_in_play import classify_live_candidate
    row = _live_row(
        oi_chg_30m_pct=np.nan,
        oi_chg_60m_pct=np.nan,
        oi_acceleration=np.nan,
        oi_recent_agrees=np.nan,
    )
    out = classify_live_candidate(row)
    assert out['oi_status'] == 'Unavailable'


def test_confirmation_diagnostics_distinguish_missing_false_and_true_flags():
    from app.early_research import confirmation_diagnostics
    events = [
        {'oi_chg_60m_pct': 1.2, 'oi_acceleration': 0.1, 'oi_status': 'Confirmed', 'htf_agrees': 1.0,
         'vwap_side_agrees': 1.0, 'entry_is_extended': 0.0},
        {'oi_chg_60m_pct': -0.4, 'oi_acceleration': -0.3, 'oi_status': 'Not Confirming', 'htf_agrees': 0.0,
         'vwap_side_agrees': 0.0, 'entry_is_extended': 1.0},
        {'oi_chg_60m_pct': np.nan, 'oi_acceleration': np.nan, 'oi_status': 'Unavailable', 'htf_agrees': np.nan,
         'vwap_side_agrees': np.nan, 'entry_is_extended': np.nan},
    ]
    d = confirmation_diagnostics(events)
    assert d['oi_60m_finite'] == 2
    assert d['oi_60m_positive'] == 1
    assert d['oi_confirmed'] == 1
    assert d['htf_available'] == 2
    assert d['htf_true'] == 1
    assert d['vwap_available'] == 2
    assert d['vwap_true'] == 1
    assert d['entry_extended_available'] == 2
    assert d['entry_extended_true'] == 1


def test_backtest_template_surfaces_raw_confirmation_diagnostics():
    html = open('app/templates/backtest.html', encoding='utf-8').read()
    assert 'confirmation_diagnostics' in html
    assert 'OI 60m finite' in html
    assert '4H available' in html


def test_retest_confirmation_requires_probe_of_breakout_level_and_close_beyond_it():
    from app.stock_in_play import build_price_features
    # Build a clean recent-range breakout at bar 8, then a one-bar retest that
    # probes back toward the old range high and closes above it.
    df = _bars(10)
    df.iloc[:8, df.columns.get_loc('high')] = 100.60
    df.iloc[:2, df.columns.get_loc('high')] = 102.00  # keep ORB above the later recent-range break
    df.iloc[:8, df.columns.get_loc('low')] = 99.80
    df.iloc[:8, df.columns.get_loc('close')] = 100.20
    df.iloc[8] = [100.25, 101.10, 100.15, 100.95, 1800]
    df.iloc[9] = [100.90, 101.00, 100.68, 100.82, 1600]
    atr = pd.Series(0.8, index=df.index)
    comp = pd.DataFrame({'compression_score': [20.0] * len(df)}, index=df.index)
    tod = pd.Series([1.0] * 8 + [1.5, 1.4], index=df.index)
    feat = build_price_features(df, atr, comp, tod)
    assert feat.iloc[8]['breakout_source'] == 'Recent Range'
    assert bool(feat.iloc[9]['breakout_retained']) is True
    assert bool(feat.iloc[9]['breakout_retest_confirmed']) is True


def test_recent_range_edge_variants_focus_on_bullish_sponsorship_and_confirmation():
    from app.early_research import recent_range_edge_variants

    base = {
        'breakout_source': 'Recent Range', 'direction': 'Bullish',
        'tod_rvol': 1.5, 'oi_status': 'Confirmed', 'htf_agrees': True,
        'entry_is_extended': False, 'vwap_distance_atr': 0.35,
        'intraday_returns': {'2h': 0.25, 'eod': 0.30},
        'swing_returns': {'1D': 0.45, '2D': 0.55},
        'entry_time': '2026-01-01T10:00:00+05:30',
    }
    bearish = dict(base, direction='Bearish', entry_time='2026-01-02T10:00:00+05:30')
    opening = dict(base, breakout_source='Opening Range', entry_time='2026-01-03T10:00:00+05:30')
    variants = recent_range_edge_variants([base, bearish, opening], confirmation_events=[])
    assert len(variants['recent_range_all']) == 2
    assert len(variants['recent_range_bullish']) == 1
    assert len(variants['bullish_plus_volume_oi']) == 1
    assert len(variants['bullish_plus_volume_oi_4h_no_chase']) == 1
    assert len(variants['bullish_plus_volume_oi_vwap_proximity']) == 1

    retained = dict(base, breakout_retained=True, retest_confirmed=False)
    retest = dict(base, breakout_retained=True, retest_confirmed=True,
                  entry_time='2026-01-04T10:15:00+05:30')
    variants = recent_range_edge_variants([base], confirmation_events=[retained, retest])
    assert len(variants['bullish_retained']) == 2
    assert len(variants['bullish_retest']) == 1
    assert len(variants['bullish_retest_volume_oi_4h']) == 1


def test_live_best_entry_is_restricted_to_sponsored_recent_range_breakout():
    from app.stock_in_play import classify_live_candidate
    recent = classify_live_candidate(_live_row(breakout_source='Recent Range'))
    assert recent['intraday_eligible'] is True
    assert recent['stage'] == 'Intraday Best Entry'

    opening = classify_live_candidate(_live_row(breakout_source='Opening Range'))
    assert opening['intraday_eligible'] is False
    assert opening['stage'] == 'Breakout Research'

    no_oi = classify_live_candidate(_live_row(
        breakout_source='Recent Range', oi_recent_agrees=False,
        oi_chg_60m_pct=1.2, oi_chg_30m_pct=0.6, oi_acceleration=0.2,
    ))
    assert no_oi['intraday_eligible'] is False
    assert 'OI not confirming' in no_oi['blockers']


def test_swing_best_entry_requires_bullish_recent_range_retention_and_true_4h_agreement():
    from app.stock_in_play import classify_live_candidate
    good = classify_live_candidate(_live_row(
        breakout_source='Recent Range', breakout_retained=True,
        timestamp='2026-08-28T14:45:00+05:30',
    ))
    assert good['swing_eligible'] is True
    assert good['stage'] == 'High-Quality Swing 1-2D'

    bearish = classify_live_candidate(_live_row(
        direction='Bearish', breakout_direction='Bearish', breakout_source='Recent Range',
        breakout_retained=True, timestamp='2026-08-28T14:45:00+05:30',
    ))
    assert bearish['swing_eligible'] is False

    neutral_4h = classify_live_candidate(_live_row(
        breakout_source='Recent Range', breakout_retained=True, htf_agrees=None,
        timestamp='2026-08-28T14:45:00+05:30',
    ))
    assert neutral_4h['swing_eligible'] is False


def test_aggregate_research_exposes_recent_range_edge_lab():
    from app.early_research import aggregate_research
    base_time = pd.Timestamp('2026-01-01 10:00', tz='Asia/Kolkata')
    events = []
    confirms = []
    for i in range(80):
        e = {
            'entry_time': (base_time + pd.Timedelta(days=i)).isoformat(),
            'direction': 'Bullish', 'breakout_source': 'Recent Range',
            'tod_rvol': 1.5, 'oi_status': 'Confirmed', 'htf_agrees': True,
            'entry_is_extended': False, 'vwap_distance_atr': 0.4,
            'intraday_returns': {'2h': 0.20, 'eod': 0.25},
            'swing_returns': {'1D': 0.35, '2D': 0.45},
            'mfe_atr': {'1D': 1.2, '2D': 1.5}, 'mae_atr': {'1D': 0.5, '2D': 0.6},
            'time_to_0_5atr_bars': 3, 'time_to_1atr_bars': 6,
            'vwap_side_agrees': True,
        }
        events.append(e)
        confirms.append(dict(e, breakout_retained=True, retest_confirmed=(i % 2 == 0)))
    replay = {
        'energy_events': [], 'baseline_energy_events': [],
        'ignition_events': events, 'best_entry_events': events,
        'swing_events': confirms, 'recent_range_confirmation_events': confirms,
    }
    out = aggregate_research([replay], holdout_pct=30.0)
    lab = out['recent_range_edge_lab']
    assert 'rows' in lab and lab['rows']
    names = {r['variant'] for r in lab['rows']}
    assert 'bullish_plus_volume_oi_4h_no_chase' in names
    assert 'bullish_retest_volume_oi_4h' in names


def test_backtest_page_surfaces_recent_range_edge_lab_and_live_dashboard_copy_is_focused():
    backtest = open('app/templates/backtest.html', encoding='utf-8').read()
    index = open('app/templates/index.html', encoding='utf-8').read()
    assert 'Recent-Range Edge Lab' in backtest
    assert 'er-recent-range-lab' in backtest
    assert 'V8.1 Evidence-Locked' in index
    assert 'Production model status' in index
