import datetime as dt

import numpy as np
import pandas as pd
import pytest
import sys
import types

# The runtime dependency is unavailable in this offline verification container.
# Stub only the constructor symbol used at import time; tests exercise pure screener logic.
if 'kiteconnect' not in sys.modules:
    mod = types.ModuleType('kiteconnect')
    class KiteConnect:  # pragma: no cover - import shim only
        pass
    mod.KiteConnect = KiteConnect
    sys.modules['kiteconnect'] = mod

from app import alerts, backtest, background, early_signal, scanner
from app.config import settings


def _idx(n=5):
    return pd.date_range('2026-08-01', periods=n, freq='D')


def test_required_oi_gate_rejects_missing_oi_reading():
    idx = _idx()
    series = {
        'rsi_line': pd.Series([60]*5, index=idx),
        'rsi_smooth': pd.Series([50]*5, index=idx),
        'macd_line': pd.Series([2]*5, index=idx),
        'signal_line': pd.Series([1]*5, index=idx),
        'cmf': pd.Series([0.2]*5, index=idx),
        'df': pd.DataFrame({'close': [100, 101, 102, 103, 104]}, index=idx),
    }
    has_signal, _direction = backtest._signal_series(
        series,
        params=('cmf_flow',),
        required=1,
        timeframe='day',
        require_oi_agreement=True,
        oi_history=None,
    )
    assert not has_signal.any(), 'missing OI must not silently pass an OI-required backtest'


def test_live_required_oi_gate_rejects_unknown_oi(monkeypatch):
    monkeypatch.setattr(settings, 'REQUIRE_OI_AGREEMENT', True)
    rows = [{'signal_confirmed': True, 'oi_agrees': None}]
    background._apply_oi_gate(rows)
    assert rows[0]['signal_confirmed'] is False


def test_daily_backtest_oi_fetch_requests_full_research_window(monkeypatch):
    calls = []

    def fake_fetch(_kite, symbols, timeframe='day', throttle=None, days_override=None):
        calls.append((tuple(symbols), timeframe, days_override))
        return {symbols[0]: pd.Series([100, 110], index=_idx(2))}

    monkeypatch.setattr(scanner, 'fetch_oi_history', fake_fetch)
    out = backtest._fetch_oi_history_for_backtest(object(), 'RELIANCE', 'day', days=365)
    assert out is not None
    assert calls == [(('RELIANCE',), 'day', 365 + backtest.WARMUP_DAYS)]


def test_default_horizons_focus_on_1_to_3_day_window():
    assert backtest.DEFAULT_HORIZONS == (1, 2, 3, 5, 10)


def test_structure_score_is_direction_aware():
    bullish_good = early_signal.early_signal_score(
        'Bullish', close_pos=90, big_candle_agrees=None, coiling=False, nr7=False
    )
    bullish_bad = early_signal.early_signal_score(
        'Bullish', close_pos=10, big_candle_agrees=None, coiling=False, nr7=False
    )
    good_pts = next(p['points'] for p in bullish_good['parts'] if p['id'] == 'structure')
    bad_pts = next(p['points'] for p in bullish_bad['parts'] if p['id'] == 'structure')
    assert good_pts > bad_pts


def test_shortlist_requires_fresh_entry_trigger(monkeypatch):
    monkeypatch.setattr(settings, 'MIN_EARLY_SCORE', 75)
    monkeypatch.setattr(settings, 'MIN_SHORTLIST_COVERAGE', 0.8)
    monkeypatch.setattr(settings, 'SHORTLIST_MAX', 8)
    base = {
        'error': None,
        'signal_confirmed': True,
        'early_eligible': True,
        'early_score': 90,
        'early_coverage': 1.0,
        'oi_z': 2.5,
        'oi_agrees': True,
        'direction': 'Bullish',
        'entry_trigger': None,
        'entry_trigger_bars_ago': None,
        'entry_is_extended': False,
        'oi_chg_60m_pct': 0.5,
        'oi_acceleration': 0.2,
    }
    rows = [dict(base)]
    background._apply_shortlist(rows)
    assert rows[0]['shortlist_rank'] is None

    rows = [dict(base, entry_trigger='Bullish', entry_trigger_bars_ago=1)]
    background._apply_shortlist(rows)
    assert rows[0]['shortlist_rank'] == 1


def test_alerts_only_fire_for_ranked_best_entries(monkeypatch):
    monkeypatch.setattr(alerts, 'telegram_enabled', lambda: False)
    with alerts._lock:
        alerts._seen.clear()
        alerts._recent.clear()
    row = {
        'symbol': 'TEST', 'fresh_signal': 'Bullish', 'direction': 'Bullish',
        'vol_confirmed': True, 'htf_agrees': True, 'timestamp': '2026-08-28T15:00:00',
        'close': 100, 'rsi': 60, 'rsi_state': 'Bullish', 'macd_params': '8,17,9',
        'macd_state': 'Bullish', 'vol_flow_direction': 'Bullish', 'aligned': 3,
        'vol_multiple': 2.0, 'shortlist_rank': None,
    }
    alerts.process_scan_results([row], 'day')
    assert alerts.get_recent() == []

    row['shortlist_rank'] = 1
    alerts.process_scan_results([row], 'day')
    assert len(alerts.get_recent()) == 1


def test_overnight_research_compares_continuation_and_reversal():
    idx = _idx(4)
    df = pd.DataFrame({
        'open': [100, 99, 101, 100],
        'close': [102, 100, 103, 99],
    }, index=idx)
    direction = pd.Series(['Bullish']*4, index=idx)
    mask = pd.Series([True, False, False, False], index=idx)
    res = backtest.compare_overnight_outcomes(df, direction, mask, cost_pct=0, slippage_pct=0)
    assert set(res) == {'continuation', 'reversal'}
    assert res['continuation']['next_open']['avg_return_pct'] < 0
    assert res['reversal']['next_open']['avg_return_pct'] > 0


def test_summary_reports_profit_factor_and_payoff():
    trades = [
        {'returns_pct': {3: 2.0}, 'mae_pct': -0.4, 'exit_reason': 'horizon', 'direction': 'Bullish'},
        {'returns_pct': {3: -1.0}, 'mae_pct': -1.2, 'exit_reason': 'horizon', 'direction': 'Bullish'},
        {'returns_pct': {3: 1.0}, 'mae_pct': -0.2, 'exit_reason': 'horizon', 'direction': 'Bullish'},
    ]
    s = backtest._summarize_group(trades, (3,))['3']
    assert s['profit_factor'] == 3.0
    assert s['avg_winner_pct'] == 1.5
    assert s['avg_loser_pct'] == -1.0
    assert s['payoff_ratio'] == 1.5

def test_shortlist_rejects_fading_recent_oi_when_available(monkeypatch):
    monkeypatch.setattr(settings, 'MIN_EARLY_SCORE', 75)
    monkeypatch.setattr(settings, 'MIN_SHORTLIST_COVERAGE', 0.8)
    monkeypatch.setattr(settings, 'SHORTLIST_MAX', 8)
    row = {
        'error': None, 'signal_confirmed': True, 'early_eligible': True,
        'early_score': 90, 'early_coverage': 1.0, 'oi_z': 2.5, 'oi_agrees': True,
        'direction': 'Bullish', 'entry_trigger': 'Bullish', 'entry_trigger_bars_ago': 0,
        'entry_is_extended': False, 'oi_chg_60m_pct': -0.4, 'oi_acceleration': -0.6,
    }
    background._apply_shortlist([row])
    assert row['shortlist_rank'] is None


def test_shortlist_tie_breaks_toward_fresher_trigger(monkeypatch):
    monkeypatch.setattr(settings, 'MIN_EARLY_SCORE', 75)
    monkeypatch.setattr(settings, 'MIN_SHORTLIST_COVERAGE', 0.8)
    monkeypatch.setattr(settings, 'SHORTLIST_MAX', 8)
    common = {
        'error': None, 'signal_confirmed': True, 'early_eligible': True,
        'early_score': 90, 'early_coverage': 1.0, 'oi_z': 2.5, 'oi_agrees': True,
        'direction': 'Bullish', 'entry_trigger': 'Bullish', 'entry_is_extended': False,
        'oi_chg_60m_pct': 0.5, 'oi_acceleration': 0.2,
    }
    older = dict(common, symbol='OLD', entry_trigger_bars_ago=2)
    fresh = dict(common, symbol='FRESH', entry_trigger_bars_ago=0)
    rows = [older, fresh]
    background._apply_shortlist(rows)
    assert fresh['shortlist_rank'] == 1
    assert older['shortlist_rank'] == 2


def test_alert_can_use_best_entry_even_when_old_fresh_signal_field_is_empty(monkeypatch):
    monkeypatch.setattr(alerts, 'telegram_enabled', lambda: False)
    with alerts._lock:
        alerts._seen.clear(); alerts._recent.clear()
    row = {
        'symbol': 'BEST', 'fresh_signal': None, 'direction': 'Bullish', 'entry_trigger': 'Bullish',
        'entry_trigger_bars_ago': 1, 'shortlist_rank': 1,
        'vol_confirmed': True, 'htf_agrees': True, 'timestamp': '2026-08-28T15:00:00',
        'close': 100, 'rsi': 60, 'rsi_state': 'Bullish', 'macd_params': '8,17,9',
        'macd_state': 'Bullish', 'vol_flow_direction': 'Bullish', 'aligned': 3,
        'vol_multiple': 2.0,
    }
    alerts.process_scan_results([row], 'day')
    assert len(alerts.get_recent()) == 1


def test_ablation_includes_targeted_pair_interactions():
    pairs = getattr(backtest, 'ABLATION_PAIRS', [])
    ids = {tuple(x[0]) for x in pairs}
    assert ('require_oi_agreement', 'require_htf') in ids
    assert ('require_oi_agreement', 'require_entry_location') in ids


def test_oi_diagnostics_count_pass_fail_and_missing(monkeypatch):
    idx = _idx(3)
    series = {
        'rsi_line': pd.Series([60]*3, index=idx),
        'rsi_smooth': pd.Series([50]*3, index=idx),
        'macd_line': pd.Series([2]*3, index=idx),
        'signal_line': pd.Series([1]*3, index=idx),
        'cmf': pd.Series([0.2]*3, index=idx),
        'df': pd.DataFrame({'close': [100, 101, 102]}, index=idx),
    }
    z = pd.Series([2.0, 2.0, np.nan], index=idx)
    verdict = pd.Series([1.0, 0.0, np.nan], index=idx)
    monkeypatch.setattr(backtest, '_oi_zscore_series', lambda *a, **k: z)
    monkeypatch.setattr(backtest, '_oi_agrees_series', lambda *a, **k: verdict)
    diag = {}
    backtest._signal_series(series, ('cmf_flow',), 1, timeframe='day',
                            require_oi_agreement=True, oi_history=pd.Series([1,2]), diag=diag)
    d = diag['require_oi_agreement']
    assert d['passed'] == 1 and d['failed'] == 1 and d['missing'] == 1

def test_run_backtest_can_reuse_price_history_cache(monkeypatch):
    idx = pd.date_range('2026-01-01', periods=60, freq='D')
    df = pd.DataFrame({
        'open': np.linspace(100, 110, 60), 'high': np.linspace(101, 111, 60),
        'low': np.linspace(99, 109, 60), 'close': np.linspace(100, 110, 60),
        'volume': np.repeat(1000, 60),
    }, index=idx)
    calls = {'n': 0}
    monkeypatch.setattr(backtest, '_load_instrument_map', lambda _k: {'TEST': 1})
    monkeypatch.setattr(backtest, 'now_ist', lambda: dt.datetime(2026, 3, 1, 15, 30))
    monkeypatch.setattr(backtest.time, 'sleep', lambda _x: None)
    def fake_fetch(*_a, **_k):
        calls['n'] += 1
        return df
    monkeypatch.setattr(backtest, '_fetch_history', fake_fetch)
    monkeypatch.setattr(backtest, '_replay_symbol', lambda *a, **k: [])
    cache = {}
    backtest.run_backtest(object(), ['TEST'], timeframe='day', days=120, history_cache=cache)
    backtest.run_backtest(object(), ['TEST'], timeframe='day', days=120, history_cache=cache)
    assert calls['n'] == 1


def test_gate_ablation_reference_horizon_defaults_to_three():
    import inspect
    assert inspect.signature(backtest.run_gate_ablation).parameters['ref_horizon'].default == 3


def test_gate_ablation_executes_pair_interactions(monkeypatch):
    calls = []
    def fake_run(_kite, _symbols, **kwargs):
        calls.append(kwargs)
        h = str(kwargs['horizons'][0])
        # run_gate_ablation asks for a set containing the ref; use 3 explicitly
        summary = {'all': {'3': {'trade_count': 100, 'win_rate_pct': 50.0, 'avg_return_pct': 0.1}}}
        return {'summary': summary, 'gate_diagnostics': {}, 'train_holdout': None}
    monkeypatch.setattr(backtest, 'run_backtest', fake_run)
    monkeypatch.setattr(backtest, '_gate_applicability', lambda *a, **k: None)
    out = backtest.run_gate_ablation(object(), symbols=['TEST'], timeframe='day', days=120, ref_horizon=3)
    pair_labels = {r['label'] for r in out['rows'] if r.get('kind') == 'pair'}
    assert 'OI + higher-timeframe trend' in pair_labels
    assert any(c.get('require_oi_agreement') and c.get('require_htf') for c in calls)


def _summary_stub(wr=50.0, avg=0.1, pf=1.2, n=100):
    return {'all': {'3': {'trade_count': n, 'win_rate_pct': wr, 'avg_return_pct': avg, 'profit_factor': pf}}}


def _split_stub(wr=51.0, avg=0.12, pf=1.3, n=30):
    return {'holdout': _summary_stub(wr, avg, pf, n), 'train': _summary_stub(49.0, 0.08, 1.1, 70)}


def test_ablation_row_reports_profit_factor_holdout_and_oi_diagnostics():
    diag = {
        'require_oi_agreement': {'total': 10, 'readable': 7, 'passed': 3, 'failed': 4, 'missing': 3},
        'require_oi_agreement__data': {'total': 10, 'readable': 7},
    }
    row = backtest._ablation_row(
        'OI positioning agreement', 'require_oi_agreement',
        _summary_stub(52.0, 0.2, 1.4), _summary_stub(50.0, 0.1, 1.1), 3,
        diagnostics=diag,
        train_holdout=_split_stub(53.0, 0.25, 1.5),
        baseline_train_holdout=_split_stub(50.0, 0.05, 1.0),
    )
    assert row['profit_factor'] == 1.4
    assert row['holdout_avg_return_pct'] == 0.25
    assert row['holdout_profit_factor'] == 1.5
    assert row['holdout_avg_return_delta'] == 0.2
    assert row['oi_passed'] == 3
    assert row['oi_failed'] == 4
    assert row['oi_missing'] == 3


def test_gate_ablation_uses_chronological_holdout_and_ranks_by_holdout_expectancy(monkeypatch):
    calls = []
    counter = {'n': 0}
    def fake_run(_kite, _symbols, **kwargs):
        calls.append(kwargs)
        counter['n'] += 1
        # baseline first. Make first gate lower win-rate but much better untouched expectancy.
        if counter['n'] == 1:
            full, split = _summary_stub(50, 0.0, 1.0), _split_stub(50, 0.0, 1.0)
        elif counter['n'] == 2:
            full, split = _summary_stub(49, 0.2, 1.4), _split_stub(48, 0.3, 1.6)
        else:
            full, split = _summary_stub(55, 0.1, 1.2), _split_stub(55, 0.05, 1.1)
        return {'summary': full, 'gate_diagnostics': {}, 'train_holdout': split}
    monkeypatch.setattr(backtest, 'run_backtest', fake_run)
    monkeypatch.setattr(backtest, '_gate_applicability', lambda *a, **k: None)
    out = backtest.run_gate_ablation(object(), symbols=['TEST'], timeframe='day', days=120, ref_horizon=3)
    assert calls and all(c.get('holdout_pct') == 30.0 for c in calls)
    assert out['rows'][0]['holdout_avg_return_pct'] >= out['rows'][-1]['holdout_avg_return_pct']


def test_auto_weights_default_to_three_bar_research_window(monkeypatch):
    import inspect
    assert inspect.signature(backtest.compute_param_weights).parameters['ref_horizon'].default == 3
    assert inspect.signature(backtest.start_weight_computation).parameters['ref_horizon'].default == 3


def test_shortlist_requires_recent_oi_measurement_not_unknown(monkeypatch):
    monkeypatch.setattr(settings, 'MIN_EARLY_SCORE', 75)
    monkeypatch.setattr(settings, 'MIN_SHORTLIST_COVERAGE', 0.8)
    monkeypatch.setattr(settings, 'SHORTLIST_MAX', 8)
    row = {
        'error': None, 'signal_confirmed': True, 'early_eligible': True,
        'early_score': 90, 'early_coverage': 1.0, 'oi_z': 2.5, 'oi_agrees': True,
        'direction': 'Bullish', 'entry_trigger': 'Bullish', 'entry_trigger_bars_ago': 0,
        'entry_is_extended': False, 'oi_chg_60m_pct': None, 'oi_acceleration': None,
    }
    background._apply_shortlist([row])
    assert row['shortlist_rank'] is None
