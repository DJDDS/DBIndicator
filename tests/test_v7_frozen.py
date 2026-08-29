import pytest

from app import v7_frozen


def _event(i, ret=0.25, catalyst=70.0, direction='Bullish', source='Recent Range'):
    return {
        'entry_time': f'2026-01-{(i % 28) + 1:02d}T{9 + (i // 28):02d}:30:00',
        'direction': direction,
        'breakout_source': source,
        'catalyst_score': catalyst,
        'swing_returns': {'1D': ret},
    }


def test_frozen_rule_selects_only_bullish_recent_range_with_catalyst_60_or_more():
    events = [
        _event(0, catalyst=60),
        _event(1, catalyst=59.9),
        _event(2, catalyst=90, direction='Bearish'),
        _event(3, catalyst=90, source='Compression'),
    ]
    selected = v7_frozen.select_frozen_candidates(events)
    assert len(selected) == 1
    assert selected[0]['catalyst_score'] == 60


def test_frozen_rule_fingerprint_and_thresholds_are_predeclared():
    spec = v7_frozen.frozen_rule_spec()
    assert spec['rule_id'] == 'RR_LONG_CATALYST60_15M_NEXTBAR_1D'
    assert spec['setup_timeframe'] == '15minute'
    assert spec['direction'] == 'Bullish'
    assert spec['breakout_source'] == 'Recent Range'
    assert spec['catalyst_score_min'] == 60.0
    assert spec['evaluation_horizon'] == '1D'
    assert spec['acceptance']['min_final_trades'] == 80
    assert spec['acceptance']['min_avg_return_pct'] == 0.15
    assert spec['acceptance']['min_profit_factor'] == 1.20
    assert spec['acceptance']['required_positive_blocks'] == 3
    assert len(spec['fingerprint']) == 12


def test_final_verdict_passes_only_when_all_frozen_acceptance_checks_pass():
    stats = {'trade_count': 120, 'avg_return_pct': 0.21, 'profit_factor': 1.32}
    blocks = [
        {'avg_return_pct': 0.11},
        {'avg_return_pct': 0.19},
        {'avg_return_pct': -0.03},
        {'avg_return_pct': 0.25},
    ]
    verdict = v7_frozen.final_verdict(stats, blocks)
    assert verdict['verdict'] == 'PASS'
    assert all(verdict['checks'].values())

    bad_pf = v7_frozen.final_verdict({**stats, 'profit_factor': 1.19}, blocks)
    assert bad_pf['verdict'] == 'REJECT'
    assert bad_pf['checks']['profit_factor'] is False


def test_final_verdict_requires_three_of_four_positive_chronological_blocks():
    stats = {'trade_count': 120, 'avg_return_pct': 0.21, 'profit_factor': 1.32}
    blocks = [
        {'avg_return_pct': 0.11},
        {'avg_return_pct': -0.01},
        {'avg_return_pct': -0.02},
        {'avg_return_pct': 0.25},
    ]
    verdict = v7_frozen.final_verdict(stats, blocks)
    assert verdict['verdict'] == 'REJECT'
    assert verdict['positive_blocks'] == 2
    assert verdict['checks']['chronological_stability'] is False


def test_protocol_reveals_final_only_for_exact_frozen_run_context():
    valid = v7_frozen.validate_protocol({
        'setup_timeframe': '15minute',
        'execution_timeframe': '15minute',
        'days': 180,
        'cost_pct': 0.08,
        'slippage_pct': 0.05,
        'universe_is_full_fno': True,
    })
    assert valid['valid'] is True
    assert valid['mismatches'] == []

    invalid = v7_frozen.validate_protocol({
        'setup_timeframe': '4hour',
        'execution_timeframe': '15minute',
        'days': 180,
        'cost_pct': 0.08,
        'slippage_pct': 0.05,
        'universe_is_full_fno': True,
    })
    assert invalid['valid'] is False
    assert any('15minute' in x for x in invalid['mismatches'])


def test_frozen_report_keeps_final_hidden_for_non_protocol_runs():
    events = [_event(i, ret=0.25, catalyst=70) for i in range(100)]
    report = v7_frozen.frozen_candidate_report(events, run_context={
        'setup_timeframe': '4hour',
        'execution_timeframe': '15minute',
        'days': 180,
        'cost_pct': 0.08,
        'slippage_pct': 0.05,
        'universe_is_full_fno': True,
    })
    assert report['final_test']['locked'] is True
    assert report['verdict']['verdict'] == 'NOT_RUN'
    assert report['validation']['trade_count'] > 0


def test_frozen_report_reveals_only_frozen_final_and_returns_one_verdict():
    events = []
    # 100 qualifying signals -> 60 dev, 20 validation, 20 final.
    # Final four chronological blocks are all positive.
    for i in range(100):
        events.append(_event(i, ret=0.25, catalyst=75))
    report = v7_frozen.frozen_candidate_report(events, run_context={
        'setup_timeframe': '15minute',
        'execution_timeframe': '15minute',
        'days': 180,
        'cost_pct': 0.08,
        'slippage_pct': 0.05,
        'universe_is_full_fno': True,
    })
    assert report['final_test']['locked'] is False
    assert report['final_test']['trade_count'] == 20
    assert len(report['chronological_blocks']) == 4
    # N is deliberately below the frozen min sample, so this must reject
    # despite attractive returns. This proves the verdict is not cosmetic.
    assert report['verdict']['verdict'] == 'REJECT'
    assert report['verdict']['checks']['sample'] is False


def test_aggregate_research_exposes_frozen_report_with_run_context():
    from app import early_research
    events = []
    for i in range(100):
        e = _event(i, ret=0.25, catalyst=75)
        e['intraday_returns'] = {'30m': 0.1, '1h': 0.1, '2h': 0.1, '4h': 0.1, 'eod': 0.1}
        e['oi_status'] = 'Unavailable'
        events.append(e)
    out = early_research.aggregate_research(
        [{'ignition_events': events}],
        run_context={
            'setup_timeframe': '15minute',
            'execution_timeframe': '15minute',
            'days': 180,
            'cost_pct': 0.08,
            'slippage_pct': 0.05,
            'universe_is_full_fno': True,
        },
    )
    assert out['research_build_id'] == '2026-08-29-INSTITUTIONAL-V8-DUAL-ALPHA'
    assert out['v7_frozen']['rule']['rule_id'] == 'RR_LONG_CATALYST60_15M_NEXTBAR_1D'
    assert out['v7_frozen']['final_test']['locked'] is False


def test_v7_keeps_legacy_v6_final_locked_even_if_old_unlock_env_is_set(monkeypatch):
    from app import v6_edge
    monkeypatch.setenv('V6_UNLOCK_FINAL_TEST', 'true')
    payload = v6_edge.final_test_payload({'trade_count': 50, 'avg_return_pct': 1.0, 'profit_factor': 9.0})
    assert payload['locked'] is True
    assert 'profit_factor' not in payload


def test_live_fno_research_can_mark_dynamic_kite_universe_as_full_protocol_universe():
    import inspect
    from app import backtest
    sig = inspect.signature(backtest.run_early_movement_research)
    assert 'universe_is_full_fno' in sig.parameters
    web_source = open('app/web.py', encoding='utf-8').read()
    assert 'universe_is_full_fno=True' in web_source


def test_infinite_profit_factor_counts_as_passing_not_invalid():
    stats = {'trade_count': 100, 'avg_return_pct': 0.25, 'profit_factor': float('inf')}
    blocks = [{'avg_return_pct': 0.1}] * 4
    verdict = v7_frozen.final_verdict(stats, blocks)
    assert verdict['checks']['profit_factor'] is True
    assert verdict['verdict'] == 'PASS'
