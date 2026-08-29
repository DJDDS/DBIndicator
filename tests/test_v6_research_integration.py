from app import early_research


def _event(i, *, direction='Bullish', turnover=90, catalyst=80, lead=0.5, loc=90,
           sponsored=True, retained=True, path=0.2):
    return {
        'symbol': f'S{i}',
        'entry_time': f'2026-06-{(i % 28)+1:02d}T10:00:00',
        'direction': direction,
        'breakout_source': 'Recent Range',
        'turnover_percentile': turnover,
        'catalyst_score': catalyst,
        'stock_sector_lead_pct': lead,
        'price_location_score': loc,
        'v6_sponsorship': {'sponsored': sponsored},
        'breakout_retained': retained,
        'retest_confirmed': retained,
        'swing_returns': {'1D': 0.4 if i % 3 else -0.2, '2D': 0.5 if i % 3 else -0.3},
        'intraday_returns': {'2h': 0.25 if i % 3 else -0.15, 'eod': 0.3 if i % 3 else -0.2},
        'path_exits': {
            'T1.00_S0.50': {'outcome': 'target' if path > 0 else 'stop', 'net_return_pct': path, 'bars': 3},
            'breakeven_0.50': {'outcome': 'target' if path > 0 else 'stop', 'net_return_pct': path, 'bars': 4},
        },
    }


def test_v6_edge_report_includes_path_exit_lab_and_locked_final_test():
    events = [_event(i) for i in range(60)]
    report = early_research.v6_edge_report(events)
    assert 'path_exit_lab' in report
    assert 'T1.00_S0.50' in report['path_exit_lab']
    assert report['recent_range_long']['final_test']['locked'] is True
    assert report['path_exit_lab']['T1.00_S0.50']['validation']['trade_count'] > 0


def test_aggregate_research_exposes_v6_edge_lab():
    events = [_event(i) for i in range(60)]
    replay = {
        'energy_events': [], 'baseline_energy_events': [],
        'ignition_events': events, 'best_entry_events': [],
        'swing_events': [], 'recent_range_confirmation_events': [],
    }
    out = early_research.aggregate_research([replay], holdout_pct=30.0)
    assert 'v6_edge_lab' in out
    assert out['v6_edge_lab']['recent_range_long']['validation']['trade_count'] > 0
