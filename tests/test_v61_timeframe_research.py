import pandas as pd
import numpy as np

from app import backtest, early_research, stock_in_play


def make_4h_setup(days=8):
    idx = []
    rows = []
    price = 100.0
    for day in pd.bdate_range('2026-07-01', periods=days):
        for hour, minute in ((9, 15), (13, 15)):
            ts = pd.Timestamp(day.date()) + pd.Timedelta(hours=hour, minutes=minute)
            rows.append((price - 0.2, price + 0.4, price - 0.5, price, 1000.0))
            idx.append(ts)
            price += 0.1
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close', 'volume'], index=pd.DatetimeIndex(idx))


def make_15m_execution(days=8):
    idx = []
    rows = []
    price = 100.0
    for day in pd.bdate_range('2026-07-01', periods=days):
        ts = pd.Timestamp(day.date()) + pd.Timedelta(hours=9, minutes=15)
        for _ in range(25):
            rows.append((price, price + 0.2, price - 0.2, price + 0.05, 500.0))
            idx.append(ts)
            price += 0.02
            ts += pd.Timedelta(minutes=15)
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close', 'volume'], index=pd.DatetimeIndex(idx))


def test_4hour_recent_range_uses_prior_4hour_bars_across_sessions():
    df = make_4h_setup(days=8)
    # Force the final completed 4H candle to escape the prior six 4H highs.
    prior_high = float(df['high'].iloc[-7:-1].max())
    df.iloc[-1, df.columns.get_loc('close')] = prior_high + 1.0
    df.iloc[-1, df.columns.get_loc('high')] = prior_high + 1.2
    atr = pd.Series(1.0, index=df.index)

    feat = stock_in_play.build_price_features(
        df, atr, timeframe='4hour', tod_rvol=pd.Series(1.5, index=df.index)
    )

    assert feat['recent_range_high'].iloc[-1] == prior_high
    assert bool(feat['fresh_breakout'].iloc[-1]) is True
    assert feat['breakout_direction'].iloc[-1] == 'Bullish'
    assert feat['breakout_source'].iloc[-1] == 'Recent Range'


def test_4hour_signal_executes_on_first_15minute_bar_after_setup_candle_closes():
    setup = make_4h_setup(days=5)
    execution = make_15m_execution(days=5)
    signal_pos = 4  # 09:15 4H candle on the third business day; known at 13:15.

    features = pd.DataFrame(index=setup.index)
    features['fresh_breakout'] = False
    features['breakout_direction'] = None
    features['breakout_source'] = None
    features['atr'] = 2.0
    features['energy_building'] = False
    features['compression_score'] = 20.0
    features['tod_rvol'] = 1.5
    features['oi_chg_30m_pct'] = 1.0
    features['oi_chg_60m_pct'] = 1.0
    features['oi_acceleration'] = 0.1
    features['oi_recent_agrees'] = True
    features['vwap_side_agrees'] = True
    features['entry_is_extended'] = False
    features['htf_agrees'] = True
    features['sector_agrees'] = True
    features['breakout_retained'] = False
    features['breakout_retest_confirmed'] = False
    features['breakout_extension_atr'] = 0.2
    features['turnover_notional'] = 1_000_000.0
    features['turnover_percentile'] = 90.0
    features['gap_atr'] = 0.1
    features['opening_rvol'] = 1.4
    features['bar_range_atr'] = 0.8
    features['catalyst_score'] = 70.0
    features['market_regime'] = 'Trend Up'
    features['sector_rank_percentile'] = 80.0
    features['stock_sector_lead_pct'] = 0.3
    features['price_location_score'] = 80.0
    features['basis_pct'] = 0.1
    features['basis_acceleration'] = 0.1
    features.loc[setup.index[signal_pos], 'fresh_breakout'] = True
    features.loc[setup.index[signal_pos], 'breakout_direction'] = 'Bullish'
    features.loc[setup.index[signal_pos], 'breakout_source'] = 'Recent Range'

    out = early_research.replay_feature_frame(
        setup, features, 'ABC', execution_df=execution, setup_timeframe='4hour'
    )

    assert len(out['ignition_events']) == 1
    event = out['ignition_events'][0]
    expected_entry = pd.Timestamp(setup.index[signal_pos].date()) + pd.Timedelta(hours=13, minutes=15)
    assert pd.Timestamp(event['entry_time']) == expected_entry
    assert event['entry_price'] == float(execution.loc[expected_entry, 'open'])
    assert event['setup_timeframe'] == '4hour'
    assert event['execution_timeframe'] == '15minute'


def test_primary_research_respects_4hour_setup_and_fetches_15minute_execution(monkeypatch):
    setup = make_4h_setup(days=30)
    execution = make_15m_execution(days=30)
    calls = []
    replay_seen = {}
    build_seen = {}

    monkeypatch.setattr(backtest, '_load_instrument_map', lambda kite: {'ABC': 1})
    monkeypatch.setattr(backtest, '_load_index_token', lambda kite, symbol: None)
    monkeypatch.setattr(backtest.scanner_mod, 'SYMBOL_SECTOR_MAP', {})
    monkeypatch.setattr(backtest.time, 'sleep', lambda *_args, **_kwargs: None)

    def fake_fetch(token, timeframe, days, kite):
        calls.append((token, timeframe, days))
        return setup.copy() if timeframe == '4hour' else execution.copy()

    monkeypatch.setattr(backtest, '_fetch_history', fake_fetch)
    monkeypatch.setattr(backtest, '_fetch_oi_history_for_backtest', lambda *a, **k: None)
    monkeypatch.setattr(backtest, '_fetch_near_futures_history_for_research', lambda *a, **k: None)

    def fake_build(df, timeframe, **kwargs):
        build_seen['timeframe'] = timeframe
        build_seen['rows'] = len(df)
        return pd.DataFrame({'turnover_notional': np.arange(len(df), dtype=float)}, index=df.index)

    def fake_replay(df, feat, symbol, **kwargs):
        replay_seen.update({
            'setup_rows': len(df),
            'execution_rows': len(kwargs['execution_df']),
            'setup_timeframe': kwargs['setup_timeframe'],
        })
        return {
            'energy_events': [], 'baseline_energy_events': [], 'ignition_events': [],
            'best_entry_events': [], 'swing_events': [], 'recent_range_confirmation_events': [],
        }

    monkeypatch.setattr(backtest.early_research, 'build_feature_frame', fake_build)
    monkeypatch.setattr(backtest.early_research, 'replay_feature_frame', fake_replay)
    monkeypatch.setattr(backtest.early_research, 'aggregate_research', lambda *a, **k: {})

    result = backtest.run_early_movement_research(
        object(), symbols=['ABC'], timeframe='4hour', days=180
    )

    assert result['timeframe'] == '4hour'
    assert result['setup_timeframe'] == '4hour'
    assert result['execution_timeframe'] == '15minute'
    assert any(tf == '4hour' for _token, tf, _days in calls)
    assert any(tf == '15minute' for _token, tf, _days in calls)
    assert build_seen['timeframe'] == '4hour'
    assert replay_seen['setup_timeframe'] == '4hour'
    assert replay_seen['execution_rows'] == len(execution)


def test_diagnostic_research_uses_selected_scope_while_v81_primary_is_fixed_15m():
    text = open('app/templates/backtest.html', encoding='utf-8').read().replace(' ', '')
    assert "timeframe:scope.timeframe" in text
    assert "timeframe:'15minute'" in text
    assert 'er-v8-run-btn' in text
    assert 'er-v7-run-btn' not in text
