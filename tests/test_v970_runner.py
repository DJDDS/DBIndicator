import pandas as pd
from app import backtest
from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches
from tests.test_v952_nse_runner import _history_for


def _cash_for(histories):
    out={}
    for symbol,hist in histories.items():
        if symbol=='_meta': continue
        idx=hist['membership'].index
        out[symbol]=pd.DataFrame(index=idx,data={'open':100.0,'high':102.0,'low':99.0,'close':101.0})
    any_idx=next((v.index for k,v in out.items()),pd.DatetimeIndex([]))
    out['_meta']={'date_coverage':1.0,'dates_loaded':len(any_idx),'dates_requested':len(any_idx),'source':'TEST'}
    return out


class NoMWPL:
    class NSEHistoricalReportClient:
        def __init__(self, **kwargs): pass
    @staticmethod
    def build_validation_mwpl_controls(**kwargs):
        return {'available':False,'reason':'TEST','date_coverage':0.0,'mwpl_by_symbol':{},'ban_by_symbol':{},'source':'TEST','errors':{}}


def test_v970_runner_uses_third_older_window_and_discovers_membership(monkeypatch,tmp_path):
    _clear_scanner_caches()
    histories=_history_for(['AAA'],start='2018-03-01',periods=950)
    captured={}
    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(days,symbols,client,progress_cb=None,discover_historical=False):
            idx=pd.DatetimeIndex(days); captured['min']=idx.min(); captured['max']=idx.max(); captured['discover']=discover_historical; return histories
    class StubCash:
        class NSECashArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_price_histories(*args,**kwargs): return _cash_for(histories)
    monkeypatch.setattr(backtest,'nse_futures_history',StubHistory,raising=False)
    monkeypatch.setattr(backtest,'nse_cash_history',StubCash,raising=False)
    monkeypatch.setattr(backtest,'nse_mwpl',NoMWPL,raising=False)
    out=backtest.run_v97_trial19(FakeKite(),symbols=['AAA'],resume_run_dir=tmp_path,integrity_data={'earnings_map':{'_meta':{'symbol_coverage':0.0}}})
    assert captured['min'] < pd.Timestamp('2018-09-01')
    assert captured['max'] <= pd.Timestamp('2021-08-31')
    assert captured['discover'] is True
    assert out['research']['trial19']['total_oi_z_min']==1.5
    assert out['research']['evidence_window']['start']=='2018-09-01'
    assert out['research']['evidence_window']['end']=='2021-08-31'
    assert out['research']['trial18']['locked'] is True
    assert out['research_only'] is True


def test_v970_runner_never_calls_trial17_or_trial15_evaluators(monkeypatch,tmp_path):
    _clear_scanner_caches()
    histories=_history_for(['AAA'],start='2018-03-01',periods=950)
    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(*args,**kwargs): return histories
    class StubCash:
        class NSECashArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_price_histories(*args,**kwargs): return _cash_for(histories)
    monkeypatch.setattr(backtest,'nse_futures_history',StubHistory,raising=False)
    monkeypatch.setattr(backtest,'nse_cash_history',StubCash,raising=False)
    monkeypatch.setattr(backtest,'nse_mwpl',NoMWPL,raising=False)
    monkeypatch.setattr(backtest.v96_trial17,'evaluate_trial17',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Trial17 is closed')))
    monkeypatch.setattr(backtest.v95_daily_evidence,'evaluate_trial15',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Trial15 is closed')))
    out=backtest.run_v97_trial19(FakeKite(),symbols=['AAA'],resume_run_dir=tmp_path,integrity_data={'earnings_map':{'_meta':{'symbol_coverage':0.0}}})
    assert out['research']['trial18']['locked'] is True


def test_v970_state_accessor_exists_and_is_fail_safe():
    state=backtest.get_v97_trial19_state()
    assert state['mode']=='v97_trial19'
    assert state['research_only'] is True
