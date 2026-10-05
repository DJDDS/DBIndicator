import datetime as dt
import importlib
import pandas as pd
import pytest


def module():
    assert importlib.util.find_spec('app.sector_analytics'), 'Sector analytics missing'
    return importlib.import_module('app.sector_analytics')


def candles(start='2024-01-01', end='2026-10-02'):
    dates=pd.bdate_range(start,end)
    return [{'date':d.isoformat(),'open':100+i,'high':102+i,'low':99+i,'close':101+i,'volume':1000} for i,d in enumerate(dates)]


def test_returns_use_boundary_close_and_completed_quarters_not_live_quote():
    a=module(); bars=candles(); now=dt.datetime(2026,10,5,11)
    result=a.summarize(bars,now,{'last_price':999,'ohlc':{'close':818}})
    df=pd.DataFrame(bars); df['date']=pd.to_datetime(df.date)
    end=float(df[df.date<=pd.Timestamp('2026-09-30')].iloc[-1].close)
    start=float(df[df.date<=pd.Timestamp('2026-06-30')].iloc[-1].close)
    assert result['returns']['Q1']==pytest.approx((end/start-1)*100)
    assert result['quarter_labels']['Q1']=='2026 Q3'
    assert result['price']==999
    assert result['returns']['today']==pytest.approx((999/818-1)*100)
    assert result['sma']['200'] is not None


def test_short_history_and_missing_quote_are_not_zero_or_today():
    r=module().summarize(candles('2026-09-28','2026-10-02'),dt.datetime(2026,10,5,11))
    assert r['returns']['52W'] is None
    assert r['returns']['today'] is None
    assert r['sma']['200'] is None
    assert r['ema']['200'] is None
    assert r['returns']['Q1'] is None


def test_vwap_resets_at_ist_session_and_zero_volume_is_missing():
    bars=[{'date':'2026-10-02T09:15:00+05:30','high':10,'low':10,'close':10,'volume':10},
          {'date':'2026-10-02T09:20:00+05:30','high':20,'low':20,'close':20,'volume':30},
          {'date':'2026-10-05T09:15:00+05:30','high':50,'low':50,'close':50,'volume':0},
          {'date':'2026-10-05T09:20:00+05:30','high':30,'low':30,'close':30,'volume':10}]
    r=module().vwap_series(bars)
    assert [x['value'] for x in r]==[10,17.5,None,30]


def test_rankings_exclude_missing_and_rank_relative_separately():
    a=module(); rows=[{'symbol':s,'returns':{'1M':v},'vs_sector':{'1M':rel}} for s,v,rel in [('A',3,-2),('B',2,4),('C',-1,1),('D',-3,-5),('E',None,None)]]
    assert [x['symbol'] for x in a.rank_members(rows,'1M')['top']]==['A','B']
    assert [x['symbol'] for x in a.rank_members(rows,'1M')['bottom']]==['D','C']
    assert [x['symbol'] for x in a.rank_members(rows,'1M',True)['top']]==['B','C']


def test_discontinuity_is_flagged_and_json_has_no_nonfinite_numbers():
    import json
    b=candles(); b[-1]['close']=b[-2]['close']/2
    r=module().summarize(b,dt.datetime(2026,10,5,11))
    assert r['quality']['discontinuity']
    json.dumps(r,allow_nan=False)


def test_relative_returns_require_both_values():
    a=module()
    assert a.relative_returns({'returns':{'1M':3,'1W':None}}, {'returns':{'1M':1,'1W':2}})['1M']==2
    assert a.relative_returns({'returns':{'1M':3}}, {'returns':{}})['1M'] is None


def test_relative_returns_reject_mismatched_asof_dates():
    a=module()
    row={'returns':{'1M':3},'returns_asof':'2026-10-02'}
    benchmark={'returns':{'1M':1},'returns_asof':'2026-10-05'}
    assert a.relative_returns(row,benchmark)['1M'] is None


def test_quarter_without_end_coverage_is_missing():
    r=module().summarize(candles('2024-01-01','2026-07-10'),dt.datetime(2026,10,5,11))
    assert r['returns']['Q1'] is None


def test_advance_decline_excludes_missing_and_handles_zero_declines():
    a=module()
    rows=[{'returns':{'today':v}} for v in [2,-1,0,None,float('nan')]]
    assert a.advance_decline(rows)=={'advances':1,'declines':1,'unchanged':1,'unavailable':2,'covered':3,'total':5,'ratio':1.0,'net':0}
    assert a.advance_decline([{'returns':{'today':2}}])['ratio'] is None
    assert a.advance_decline([])['net'] is None
