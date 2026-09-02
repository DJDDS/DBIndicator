from app import backtest


def test_v960_state_accessor_exposes_worker_snapshot_without_crashing():
    state = backtest.get_v96_trial17_state()
    assert isinstance(state, dict)
    assert isinstance(state.get('worker'), dict)
    assert 'pid' in state['worker']
    assert 'rss_mb' in state['worker']
