import pandas as pd

from app import scanner


def _oi_rows():
    idx = pd.date_range('2026-08-01', periods=4, freq='D', tz='Asia/Kolkata')
    return [
        {'date': ts.to_pydatetime(), 'oi': 1000 + i * 10}
        for i, ts in enumerate(idx)
    ]


def test_fetch_oi_history_reports_symbol_progress(monkeypatch):
    scanner._oi_history_cache.clear()
    monkeypatch.setattr(scanner, '_fut_token_map', lambda kite: {'AAA': 1, 'BBB': 2, 'CCC': 3})
    monkeypatch.setattr(scanner, '_fetch_historical_chunked', lambda *a, **k: _oi_rows())

    seen = []
    out = scanner.fetch_oi_history(
        object(), ['AAA', 'BBB', 'CCC'], timeframe='day', days_override=30,
        throttle=0, progress_cb=lambda done, total, symbol: seen.append((done, total, symbol)),
    )

    assert set(out) == {'AAA', 'BBB', 'CCC'}
    assert seen[0] == (0, 3, None)
    assert (0, 3, 'AAA') in seen
    assert (1, 3, 'AAA') in seen
    assert (2, 3, 'BBB') in seen
    assert seen[-1] == (3, 3, 'CCC')


def test_fetch_oi_history_cached_path_reports_complete(monkeypatch):
    scanner._oi_history_cache.clear()
    monkeypatch.setattr(scanner, '_fut_token_map', lambda kite: {'AAA': 1})
    monkeypatch.setattr(scanner, '_fetch_historical_chunked', lambda *a, **k: _oi_rows())
    scanner.fetch_oi_history(object(), ['AAA'], timeframe='day', days_override=30, throttle=0)

    seen = []
    scanner.fetch_oi_history(
        object(), ['AAA'], timeframe='day', days_override=30, throttle=0,
        progress_cb=lambda done, total, symbol: seen.append((done, total, symbol)),
    )
    assert seen == [(1, 1, None)]
import time
from app import backtest


def test_v93_start_wires_daily_input_progress_into_job_state(monkeypatch):
    monkeypatch.setattr(backtest, '_persist_early_research_state', lambda: None)
    monkeypatch.setattr(backtest, '_early_research_run_dir', lambda **kwargs: None)
    monkeypatch.setattr(backtest, '_research_resume_summary', lambda *args, **kwargs: None)
    monkeypatch.setattr(backtest, '_completed_research_symbol_shards', lambda *args, **kwargs: {})

    snapshots = []

    def fake_run(*args, input_progress_cb=None, **kwargs):
        assert input_progress_cb is not None
        input_progress_cb(0, 3, 'AAA')
        input_progress_cb(1, 3, 'AAA')
        snapshots.append(backtest.get_early_research_state())
        return {'research': {}, 'setup_timeframe': '15minute', 'execution_timeframe': '15minute'}

    monkeypatch.setattr(backtest, 'run_early_movement_research', fake_run)
    with backtest._early_research_lock:
        backtest._early_research_state.update({
            'status': 'idle', 'progress': {}, 'result': None, 'error': None,
            'started_at': None, 'finished_at': None, 'params': {},
        })

    started = backtest.start_early_movement_research(
        object(), symbols=['AAA', 'BBB', 'CCC'], timeframe='15minute', days=180,
        fast_v8=True, research_mode='v93_lab',
    )
    assert started['started'] is True
    for _ in range(100):
        if backtest.get_early_research_state()['status'] != 'running':
            break
        time.sleep(0.01)

    assert snapshots
    p = snapshots[-1]['progress']
    assert p['done'] == 1
    assert p['total'] == 3
    assert p['symbol'] == 'AAA'
    assert p['stage'] == 'Loading point-in-time daily continuous OI baseline'
    assert p['overall_pct'] >= 1
