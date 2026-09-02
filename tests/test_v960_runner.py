import pandas as pd

from app import backtest
from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches
from tests.test_v952_nse_runner import _history_for




def _cash_for(histories):
    out = {}
    for symbol, hist in histories.items():
        if symbol == '_meta':
            continue
        idx = hist['membership'].index
        out[symbol] = pd.DataFrame(index=idx, data={'open':100.0,'high':102.0,'low':99.0,'close':101.0})
    any_idx = next((v.index for k,v in out.items()), pd.DatetimeIndex([]))
    out['_meta'] = {'date_coverage':1.0,'dates_loaded':len(any_idx),'dates_requested':len(any_idx),'source':'TEST'}
    return out


class StubCashHistory:
    class NSECashArchiveClient:
        def __init__(self, **kwargs): pass
    histories = None
    @classmethod
    def build_symbol_price_histories(cls, *args, **kwargs):
        return _cash_for(cls.histories)

class NoMWPL:
    class NSEHistoricalReportClient:
        def __init__(self, **kwargs): pass
    @staticmethod
    def build_validation_mwpl_controls(**kwargs):
        return {'available': False, 'reason':'TEST', 'date_coverage':0.0, 'mwpl_by_symbol':{}, 'ban_by_symbol':{}, 'source':'TEST', 'errors':{}}


def test_v960_runner_uses_fixed_older_window_and_official_total_oi(monkeypatch, tmp_path):
    _clear_scanner_caches()
    histories = _history_for(['AAA'], start='2021-04-01', periods=650)
    captured = {}
    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, discover_historical=False):
            idx = pd.DatetimeIndex(days)
            captured['min_day'] = idx.min()
            captured['max_day'] = idx.max()
            captured['discover_historical'] = discover_historical
            return histories
    monkeypatch.setattr(backtest, 'nse_futures_history', StubHistory, raising=False)
    StubCashHistory.histories = histories
    monkeypatch.setattr(backtest, 'nse_cash_history', StubCashHistory, raising=False)
    monkeypatch.setattr(backtest, 'nse_mwpl', NoMWPL, raising=False)
    kite = FakeKite()
    out = backtest.run_v96_trial17(kite, symbols=['AAA'], resume_run_dir=tmp_path)
    assert captured['min_day'] < pd.Timestamp('2021-09-01')
    assert captured['max_day'] <= pd.Timestamp('2023-09-05')
    assert captured['discover_historical'] is True
    assert out['research']['trial17']['total_oi_z_min'] == 1.5
    assert out['research']['evidence_window']['end'] == '2023-09-01'
    assert out['integrity']['historical_oi_primary'] == 'NSE_TOTAL_FUTSTK_OI_SHARE_EQUIVALENT'
    assert not [c for c in kite.calls if c[3] is True]


def test_v960_runner_never_calls_trial15_evaluator(monkeypatch, tmp_path):
    _clear_scanner_caches()
    histories = _history_for(['AAA'], start='2021-04-01', periods=650)
    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, discover_historical=False): return histories
    monkeypatch.setattr(backtest, 'nse_futures_history', StubHistory, raising=False)
    StubCashHistory.histories = histories
    monkeypatch.setattr(backtest, 'nse_cash_history', StubCashHistory, raising=False)
    monkeypatch.setattr(backtest, 'nse_mwpl', NoMWPL, raising=False)
    monkeypatch.setattr(backtest.v95_daily_evidence, 'evaluate_trial15', lambda *a, **k: (_ for _ in ()).throw(AssertionError('Trial 15 must stay closed')))
    out = backtest.run_v96_trial17(FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path)
    assert out['research']['trial18']['locked'] is True
