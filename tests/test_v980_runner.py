import pandas as pd

from app import backtest


def _fake_research():
    return {
        'status':'INCONCLUSIVE_INTEGRITY','event_symbols':['AAA'],
        'gates':{'sample_ok':True,'matched_lift_ok':True,'binary_event_t_ok':True,'tail_ok':True,'stability_ok':True,'integrity_ok':False},
        'controls':{'historical_membership':'APPLIED','historical_cash_price':'APPLIED','lot_size_normalization':'APPLIED','mwpl_control':'UNAVAILABLE'},
    }


def test_v980_runner_invokes_incremental_core_and_keeps_trial18_locked(monkeypatch, tmp_path):
    from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches
    from tests.test_v952_nse_runner import _history_for
    from tests.test_v970_runner import _cash_for
    _clear_scanner_caches()
    histories=_history_for(['AAA'], start='2018-03-01', periods=950)
    # V9.8 requires point-in-time futures volume; inject it into the same history object.
    for sym,h in histories.items():
        if sym == '_meta': continue
        h['total_volume']=pd.Series(100000.0, index=h['total_oi'].index)

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
            return {'available':False,'reason':'NO_OLD_MWPL','date_coverage':0.0,'month_coverage':0.0,'observation_coverage':0.0,'mwpl_by_symbol':{},'ban_by_symbol':{},'source':'TEST','errors':{}}
    class StubEarnings:
        class NSEEarningsHistoryClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_earnings_map(*args, **kwargs):
            return {'AAA':pd.DatetimeIndex(['2020-01-20']), '_meta':{'symbols_requested':1,'symbols_loaded':1,'symbols_with_dates':1,'result_dates_loaded':1,'symbol_date_coverage':1.0}}

    monkeypatch.setattr(backtest,'nse_futures_history',StubHistory,raising=False)
    monkeypatch.setattr(backtest,'nse_cash_history',StubCash,raising=False)
    monkeypatch.setattr(backtest,'nse_mwpl',StubMWPL,raising=False)
    monkeypatch.setattr(backtest,'nse_earnings_history',StubEarnings,raising=False)
    monkeypatch.setattr(backtest.v97_trial19,'evaluate_trial19',lambda *a,**k:_fake_research(),raising=False)
    monkeypatch.setattr(backtest.v97_trial19,'evaluate_volatility_confound',lambda *a,**k:{'status':'PASS_VOLATILITY_CONFOUND','pass':True},raising=False)
    monkeypatch.setattr(backtest.v97_trial19,'evaluate_earnings_promotion',lambda *a,**k:{'status':'FAIL_EARNINGS_PROMOTION','confound_pass':False,'trial18_eligible':False},raising=False)
    monkeypatch.setattr(backtest,'_build_v97_recent_mwpl_bound',lambda **k:{'non_load_bearing':False,'status':'INCONCLUSIVE'},raising=False)
    monkeypatch.setattr(backtest.v97_trial19,'evaluate_trial18_eligibility',lambda **k:{'trial18_eligible':True,'status':'ELIGIBLE_FOR_PREREGISTRATION','reasons':[]},raising=False)

    calls={'core':0,'earn':0,'final':0}
    monkeypatch.setattr(backtest.v98_incremental_oi,'evaluate_incremental_core',lambda *a,**k:calls.__setitem__('core',calls['core']+1) or {'pass':True,'status':'PASS_INCREMENTAL_CORE'},raising=False)
    monkeypatch.setattr(backtest.v98_incremental_oi,'evaluate_earnings_split',lambda *a,**k:calls.__setitem__('earn',calls['earn']+1) or {'audit':{'audit_valid':True},'outside_earnings_pass':True,'status':'PASS_EARNINGS_SPLIT'},raising=False)
    monkeypatch.setattr(backtest.v98_incremental_oi,'finalize_v98',lambda core,earnings_control:calls.__setitem__('final',calls['final']+1) or {'status':'PASS_INCREMENTAL_OI','pass':True,'trial18_state':'LOCKED','eligible_for_direction_preregistration':False},raising=False)

    out=backtest.run_v97_trial19(FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path, integrity_data={})
    assert calls == {'core':1,'earn':1,'final':1}
    assert out['v98_validation']['status']=='PASS_INCREMENTAL_OI'
    assert out['trial18_eligible'] is False
    assert out['promotion_controls']['trial18_state']=='LOCKED'
