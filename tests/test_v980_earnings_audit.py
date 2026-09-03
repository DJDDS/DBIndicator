import pandas as pd
import pytest

from app import nse_earnings_history as neh
from app import v98_incremental_oi as v98


class _Client:
    def fetch_symbol(self, symbol, start, end):
        if symbol == 'AAA':
            return pd.DatetimeIndex(['2020-01-08', '2020-04-08'])
        return pd.DatetimeIndex([])


def test_v980_earnings_map_reports_symbols_with_actual_dates_not_just_fetch_success():
    out = neh.build_earnings_map(['AAA', 'BBB'], '2020-01-01', '2020-12-31', _Client())
    meta = out['_meta']
    assert meta['symbols_loaded'] == 2
    assert meta['symbols_with_dates'] == 1
    assert meta['result_dates_loaded'] == 2
    assert meta['symbol_date_coverage'] == pytest.approx(0.5)


def _frame():
    dates = pd.bdate_range('2020-01-01', periods=25)
    rows=[]
    for d in dates:
        for s in ('AAA','BBB'):
            event = (s == 'AAA' and d in (pd.Timestamp('2020-01-07'), pd.Timestamp('2020-01-09'))) or (s == 'BBB' and d == pd.Timestamp('2020-01-09'))
            rows.append({
                'date':d,'symbol':s,'dte_bucket':'11-20','trial19_eligible':True,'extreme_oi_event':event,
                'next_yz_var':0.0004 if event else 0.0002,'next_gk_var':0.00035 if event else 0.00018,
                'har_daily_var':0.0001,'har_weekly_var':0.0001,'har_monthly_var':0.0001,'futures_volume_z':0.0,
            })
    return pd.DataFrame(rows)


def _frozen_pass():
    return {'gates':{'sample_ok':True,'matched_lift_ok':True,'binary_event_t_ok':True,'tail_ok':True,'stability_ok':True}}


def test_v980_earnings_split_marks_actual_event_overlap_and_examples(monkeypatch):
    df=_frame(); monkeypatch.setattr(v98, '_stack', lambda frames: df.copy())
    emap={
        'AAA':pd.DatetimeIndex(['2020-01-08']),
        'BBB':pd.DatetimeIndex(['2020-01-30']),
        '_meta':{'symbols_requested':2,'symbols_loaded':2,'symbols_with_dates':2,'result_dates_loaded':2,'symbol_date_coverage':1.0},
    }
    out=v98.evaluate_earnings_split({'x':pd.DataFrame()}, frozen_result=_frozen_pass(), earnings_map=emap, bootstrap_reps=20)
    assert out['audit']['audit_valid'] is True
    assert out['audit']['event_overlap_count'] >= 2
    assert out['audit']['matched_symbol_count'] == 2
    assert out['audit']['examples']
    assert out['inside_earnings']['event_count'] >= 2
    assert out['outside_earnings']['event_count'] >= 1


def test_v980_earnings_100pct_fetch_with_zero_dates_is_invalid(monkeypatch):
    df=_frame(); monkeypatch.setattr(v98, '_stack', lambda frames: df.copy())
    emap={'AAA':pd.DatetimeIndex([]),'BBB':pd.DatetimeIndex([]),'_meta':{'symbols_requested':2,'symbols_loaded':2,'symbol_coverage':1.0,'symbols_with_dates':0,'result_dates_loaded':0,'symbol_date_coverage':0.0}}
    out=v98.evaluate_earnings_split({'x':pd.DataFrame()}, frozen_result=_frozen_pass(), earnings_map=emap, bootstrap_reps=10)
    assert out['audit']['audit_valid'] is False
    assert out['status']=='INCONCLUSIVE_EARNINGS_JOIN'
    assert out['event_overlap_count']==0
