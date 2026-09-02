import pandas as pd


def test_promotion_verdict_requires_all_declared_controls():
    from app import v96_trial17 as t17
    base = dict(
        frozen_status='PASS_INDEPENDENT_VALIDATION',
        integrity_ok=True,
        earnings_coverage=0.95,
        earnings_report={'lift':1.08,'ci95_low':1.02},
        same_day_report={'lift':1.06,'ci95_low':1.01},
        two_way_reg={'coef':{'total_z':0.12},'t':{'total_z':3.4}},
        market_regime_coverage=0.95,
        dte_report={'lift':1.05},
    )
    assert t17.promotion_verdict(**base) == 'PASS_PROMOTION_CONTROLS'
    bad=dict(base); bad['earnings_report']={'lift':1.01,'ci95_low':0.99}
    assert t17.promotion_verdict(**bad) == 'FAIL_EARNINGS_CONFOUND'
    bad=dict(base); bad['same_day_report']={'lift':1.0,'ci95_low':0.98}
    assert t17.promotion_verdict(**bad) == 'FAIL_SAME_DAY_MATCH'
    bad=dict(base); bad['two_way_reg']={'coef':{'total_z':0.12},'t':{'total_z':2.9}}
    assert t17.promotion_verdict(**bad) == 'FAIL_TWO_WAY_INFERENCE'
    bad=dict(base); bad['market_regime_coverage']=0.80
    assert t17.promotion_verdict(**bad) == 'INCONCLUSIVE_MARKET_REGIME_COVERAGE'


def test_evaluate_promotion_controls_reports_earnings_same_day_market_and_dte():
    from app import v96_trial17 as t17
    from tests.test_v960_trial17 import _frame
    frames={f'S{i}': _frame(boost=1.45, event_every=4+i%2) for i in range(6)}
    # Determine dates from the frozen preparation and provide fully covered empty earnings calendars.
    earnings={s: pd.DatetimeIndex([]) for s in frames}
    earnings['_meta']={'loaded_symbols':sorted(frames),'symbol_coverage':1.0}
    dates=pd.bdate_range('2021-09-01','2023-09-01')
    regime=pd.DataFrame(index=dates, data={'india_vix':18.0,'nifty_close':17000.0,'nifty_rv20_prev':0.15})
    frozen=t17.evaluate_trial17(frames, controls={
        'historical_membership_available':True,'historical_cash_price_available':True,
        'lot_size_normalization_available':True,'mwpl_available':True,
    }, bootstrap_reps=20)
    assert 'event_symbols' in frozen
    # Promotion computation is useful even if synthetic frozen sample misses one efficacy hurdle.
    out=t17.evaluate_promotion_controls(frames, frozen_result=frozen, controls={
        'historical_membership_available':True,'historical_cash_price_available':True,
        'lot_size_normalization_available':True,'mwpl_available':True,
    }, earnings_map=earnings, market_regime=regime, bootstrap_reps=20)
    assert 'earnings_excluded_1D' in out
    assert 'same_day_matched_1D' in out
    assert 'two_way_regression_1D' in out
    assert 'dte_matched_1D' in out
    assert out['earnings_symbol_coverage'] == 1.0
    assert out['market_regime_event_day_coverage'] > 0.9
    assert out['trial18_eligible'] is (out['status']=='PASS_PROMOTION_CONTROLS')


def test_v962_runner_attaches_promotion_controls_with_injected_calendars(monkeypatch, tmp_path):
    from app import backtest
    from tests.test_v952_nse_runner import _history_for
    from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches

    _clear_scanner_caches()
    histories=_history_for(['AAA','BBB'], start='2021-04-01', periods=650)
    histories['_meta']['date_coverage']=1.0
    histories['_meta']['historical_symbols_discovered']=2
    idx=histories['AAA']['membership'].index
    cash={
        'AAA':pd.DataFrame(index=idx,data={'open':100.0,'high':102.0,'low':99.0,'close':101.0}),
        'BBB':pd.DataFrame(index=idx,data={'open':200.0,'high':203.0,'low':198.0,'close':201.0}),
        '_meta':{'date_coverage':1.0,'dates_loaded':len(idx),'dates_requested':len(idx),'source':'TEST'},
    }
    trial_dates=pd.bdate_range('2021-09-01','2023-09-01')
    earnings={'AAA':pd.DatetimeIndex([]),'BBB':pd.DatetimeIndex([]),'_meta':{'loaded_symbols':['AAA','BBB'],'symbol_coverage':1.0}}
    regime=pd.DataFrame(index=trial_dates,data={'india_vix':18.0,'nifty_close':17000.0,'nifty_rv20_prev':0.15})

    class StubMWPL:
        class NSEHistoricalReportClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_validation_mwpl_controls(**kwargs):
            dates=pd.DatetimeIndex(kwargs['validation_dates'])
            return {'available':True,'reason':'APPLIED','date_coverage':1.0,
                    'mwpl_by_symbol':{s:pd.Series(50.0,index=dates) for s in ['AAA','BBB']},
                    'ban_by_symbol':{s:pd.Series(False,index=dates) for s in ['AAA','BBB']},
                    'source':'TEST','errors':{}}
    monkeypatch.setattr(backtest,'nse_mwpl',StubMWPL,raising=False)
    out=backtest.run_v96_trial17(FakeKite(), symbols=['AAA','BBB'], integrity_data={
        'nse_history_by_symbol':histories,
        'nse_cash_by_symbol':cash,
        'earnings_map':earnings,
        'market_regime':regime,
    }, resume_run_dir=tmp_path)
    assert 'promotion_controls' in out
    assert out['promotion_controls']['controls']['earnings_calendar']=='APPLIED'
    assert out['promotion_controls']['controls']['market_regime']=='APPLIED'
    assert out['trial18_eligible'] == out['promotion_controls']['trial18_eligible']
