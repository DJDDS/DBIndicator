import pandas as pd

from app import backtest


def _fake_research(status='INCONCLUSIVE_INTEGRITY'):
    return {
        'status': status,
        'event_symbols': ['AAA'],
        'gates': {
            'sample_ok': True, 'matched_lift_ok': True, 'binary_event_t_ok': True,
            'tail_ok': True, 'stability_ok': True, 'integrity_ok': False,
        },
        'controls': {
            'historical_membership': 'APPLIED', 'historical_cash_price': 'APPLIED',
            'lot_size_normalization': 'APPLIED', 'mwpl_control': 'UNAVAILABLE',
        },
    }


def test_v974_runner_loads_earnings_when_efficacy_passes_before_mwpl(monkeypatch, tmp_path):
    from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches
    from tests.test_v952_nse_runner import _history_for
    from tests.test_v970_runner import _cash_for
    _clear_scanner_caches()
    histories = _history_for(['AAA'], start='2018-03-01', periods=950)

    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(*args, **kwargs): return histories
    class StubCash:
        class NSECashArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_price_histories(*args, **kwargs): return _cash_for(histories)
    class StubMWPL:
        class NSEHistoricalReportClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_monthly_mwpl_controls(**kwargs):
            return {'available': False, 'reason': 'NO_OLD_MWPL', 'date_coverage': 0.0, 'month_coverage': 0.0,
                    'observation_coverage': 0.0, 'mwpl_by_symbol': {}, 'ban_by_symbol': {}, 'source': 'TEST', 'errors': {}}

    called = {'earnings': 0, 'bound': 0}
    class StubEarnings:
        class NSEEarningsHistoryClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_earnings_map(*args, **kwargs):
            called['earnings'] += 1
            return {'AAA': pd.DatetimeIndex([]), '_meta': {'symbol_coverage': 1.0, 'loaded_symbols': ['AAA']}}

    monkeypatch.setattr(backtest, 'nse_futures_history', StubHistory, raising=False)
    monkeypatch.setattr(backtest, 'nse_cash_history', StubCash, raising=False)
    monkeypatch.setattr(backtest, 'nse_mwpl', StubMWPL, raising=False)
    monkeypatch.setattr(backtest, 'nse_earnings_history', StubEarnings, raising=False)
    monkeypatch.setattr(backtest.v97_trial19, 'evaluate_trial19', lambda *a, **k: _fake_research(), raising=False)
    monkeypatch.setattr(backtest.v97_trial19, 'evaluate_volatility_confound', lambda *a, **k: {'status':'PASS_VOLATILITY_CONFOUND','pass':True}, raising=False)
    monkeypatch.setattr(backtest.v97_trial19, 'evaluate_earnings_promotion', lambda *a, **k: {'status':'PASS_EARNINGS_PROMOTION','confound_pass':True,'trial18_eligible':False}, raising=False)
    monkeypatch.setattr(backtest, '_build_v97_recent_mwpl_bound', lambda **k: called.__setitem__('bound', called['bound'] + 1) or {'non_load_bearing':True}, raising=False)
    monkeypatch.setattr(backtest.v97_trial19, 'evaluate_trial18_eligibility', lambda **k: {'trial18_eligible':False,'status':'LOCKED'}, raising=False)

    out = backtest.run_v97_trial19(FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path, integrity_data={})
    assert called['earnings'] == 1
    assert called['bound'] == 1
    assert out['confound_controls']['volatility']['pass'] is True
    assert out['confound_controls']['earnings']['confound_pass'] is True
    assert out['confound_controls']['recent_mwpl_bound']['non_load_bearing'] is True
